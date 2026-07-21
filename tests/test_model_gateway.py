import tempfile
import unittest
import io
import json
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

from knowledge_workbench.config import WorkspacePaths
from knowledge_workbench.errors import PolicyDeniedError
from knowledge_workbench.ingest import initialize_workspace
from knowledge_workbench.models import Classification
from knowledge_workbench.policy import ProviderLocation
from knowledge_workbench.providers import AuditedModelGateway, DeepSeekChatModel


@dataclass
class FakeCloudModel:
    name: str = "deepseek-chat"
    location: ProviderLocation = ProviderLocation.CLOUD

    def generate(self, prompt: str) -> str:
        return '{"ok": true}'


class ModelGatewayTests(unittest.TestCase):
    def test_internal_cloud_call_is_denied_and_audited_without_authorization(self):
        with tempfile.TemporaryDirectory() as temporary:
            database = initialize_workspace(
                WorkspacePaths(Path(temporary) / "workspace")
            )
            gateway = AuditedModelGateway(database, FakeCloudModel())
            with self.assertRaises(PolicyDeniedError):
                gateway.generate("secret", Classification.INTERNAL, actor="tester")
            with database.connect() as connection:
                event = connection.execute(
                    "SELECT event_type, details_json FROM audit_log"
                ).fetchone()
            self.assertEqual(event["event_type"], "model_call_denied")
            self.assertNotIn("secret", event["details_json"])

    def test_internal_cloud_call_requires_explicit_one_time_authorization(self):
        with tempfile.TemporaryDirectory() as temporary:
            database = initialize_workspace(
                WorkspacePaths(Path(temporary) / "workspace")
            )
            gateway = AuditedModelGateway(database, FakeCloudModel())
            output = gateway.generate(
                "payload",
                Classification.INTERNAL,
                actor="tester",
                allow_internal_cloud_once=True,
            )
            self.assertEqual(output, '{"ok": true}')
            with database.connect() as connection:
                event = connection.execute(
                    "SELECT event_type FROM audit_log"
                ).fetchone()[0]
            self.assertEqual(event, "model_call_succeeded")

    def test_deepseek_connector_requests_strict_json_mode(self):
        response = io.BytesIO(
            json.dumps(
                {"choices": [{"message": {"content": '{"evidence": []}'}}]}
            ).encode("utf-8")
        )
        with patch("urllib.request.urlopen", return_value=response) as urlopen:
            model = DeepSeekChatModel(api_key="test-key")
            content = model.generate("analyze")
        self.assertEqual(content, '{"evidence": []}')
        request = urlopen.call_args.args[0]
        request_payload = json.loads(request.data)
        self.assertEqual(request_payload["model"], "deepseek-chat")
        self.assertEqual(request_payload["response_format"], {"type": "json_object"})
        self.assertEqual(request.get_header("Authorization"), "Bearer test-key")


if __name__ == "__main__":
    unittest.main()
