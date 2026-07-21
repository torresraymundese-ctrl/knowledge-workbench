import json
import tempfile
import unittest
from dataclasses import dataclass, field
from pathlib import Path

from knowledge_workbench.config import WorkspacePaths
from knowledge_workbench.errors import KnowledgeWorkbenchError
from knowledge_workbench.ingest import initialize_workspace
from knowledge_workbench.model_pipeline import analyze_with_model, generate_wiki_with_model
from knowledge_workbench.models import Classification, ParsedUnit, ParseResult
from knowledge_workbench.policy import ProviderLocation
from knowledge_workbench.providers import AuditedModelGateway


@dataclass
class ResponseQueueModel:
    responses: list[str]
    name: str = "deepseek-chat"
    location: ProviderLocation = ProviderLocation.CLOUD
    calls: list[str] = field(default_factory=list)

    def generate(self, prompt: str) -> str:
        self.calls.append(prompt)
        return self.responses.pop(0)


class ModelPipelineTests(unittest.TestCase):
    def test_two_model_stages_validate_against_shared_evidence_ids(self):
        evidence = {
            "candidate_id": "E0001",
            "excerpt": "所有正式知识必须绑定原始证据。",
            "locator": {"line_start": 1},
            "evidence_type": "requirement",
            "entities": [],
            "concepts": ["证据"],
            "projects": [],
            "applicability": {"scope": None, "valid_from": None, "valid_to": None},
            "potential_conflicts": [],
        }
        generation = {
            "schema_version": "1.0",
            "analysis_schema_version": "1.0",
            "pages": [{
                "title": "知识规则",
                "slug_suggestion": "知识规则",
                "summary": "证据要求",
                "conclusions": [{
                    "text": "正式知识需要原始证据。",
                    "evidence_ids": ["E0001"],
                    "confidence": "high",
                    "applicability": None,
                }],
                "evidence_sections": [{"heading": "依据", "evidence_ids": ["E0001"]}],
                "links": [],
            }],
            "human_tasks": [],
        }
        model = ResponseQueueModel(
            [json.dumps({"evidence": [evidence]}, ensure_ascii=False), json.dumps(generation, ensure_ascii=False)]
        )
        parsed = ParseResult(
            "test", "1", (ParsedUnit(evidence["excerpt"], evidence["locator"]),)
        )
        with tempfile.TemporaryDirectory() as temporary:
            database = initialize_workspace(
                WorkspacePaths(Path(temporary) / "workspace")
            )
            gateway = AuditedModelGateway(database, model)
            analysis = analyze_with_model(
                gateway,
                parsed,
                document_version_id="ver_abcdef",
                source_sha256="b" * 64,
                classification=Classification.PUBLIC,
                actor="tester",
            )
            output = generate_wiki_with_model(
                gateway,
                analysis,
                title="知识规则",
                classification=Classification.PUBLIC,
                actor="tester",
            )
        self.assertEqual(output["pages"][0]["conclusions"][0]["evidence_ids"], ["E0001"])
        self.assertEqual(len(model.calls), 2)

    def test_model_cannot_invent_excerpt(self):
        evidence = {
            "candidate_id": "E0001",
            "excerpt": "模型凭空生成的句子。",
            "locator": {},
            "evidence_type": "fact",
            "entities": [],
            "concepts": [],
            "projects": [],
            "applicability": {"scope": None, "valid_from": None, "valid_to": None},
            "potential_conflicts": [],
        }
        model = ResponseQueueModel([json.dumps({"evidence": [evidence]}, ensure_ascii=False)])
        parsed = ParseResult("test", "1", (ParsedUnit("真实原文。", {}),))
        with tempfile.TemporaryDirectory() as temporary:
            database = initialize_workspace(
                WorkspacePaths(Path(temporary) / "workspace")
            )
            gateway = AuditedModelGateway(database, model)
            with self.assertRaisesRegex(KnowledgeWorkbenchError, "无法回到解析结果"):
                analyze_with_model(
                    gateway,
                    parsed,
                    document_version_id="ver_abcdef",
                    source_sha256="c" * 64,
                    classification=Classification.PUBLIC,
                    actor="tester",
                )


if __name__ == "__main__":
    unittest.main()

