import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from knowledge_workbench.config import WorkspacePaths
from knowledge_workbench.database import Database
from knowledge_workbench.errors import KnowledgeWorkbenchError
from knowledge_workbench.ingest import ingest_file
from knowledge_workbench.models import Classification
from knowledge_workbench.models import EvidenceStatus
from knowledge_workbench.review import transition_evidence
from knowledge_workbench.web_service import WorkbenchReadService
from knowledge_workbench.webapp import (
    WorkbenchWebApplication,
    build_web_server,
)


class WorkbenchWebTests(unittest.TestCase):
    def test_read_service_uses_current_runs_and_redacts_restricted_names(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = WorkspacePaths(root / "workspace")
            internal = root / "内部说明.md"
            restricted = root / "绝密项目.md"
            internal.write_text("内部证据一。\n\n内部证据二。", encoding="utf-8")
            restricted.write_text("受限证据。", encoding="utf-8")
            ingest_file(internal, paths, Classification.INTERNAL)
            ingest_file(restricted, paths, Classification.RESTRICTED)
            database = Database(paths.database)
            with database.connect() as connection:
                restricted_evidence_id = connection.execute(
                    """
                    SELECT e.id FROM evidence e
                    JOIN document_versions dv ON dv.id = e.document_version_id
                    JOIN documents d ON d.id = dv.document_id
                    WHERE d.classification = 'restricted'
                    """
                ).fetchone()[0]
            transition_evidence(
                database,
                restricted_evidence_id,
                EvidenceStatus.REVIEWING,
                actor="reviewer-01",
            )

            service = WorkbenchReadService(database, paths)
            summary = service.summary()
            documents = service.documents(limit=20)
            review_queue = service.review_queue(limit=20)

            self.assertEqual(summary["document_count"], 2)
            self.assertEqual(summary["current_evidence_count"], 3)
            self.assertEqual(documents["total"], 2)
            restricted_item = next(
                item
                for item in documents["items"]
                if item["classification"] == "restricted"
            )
            self.assertEqual(restricted_item["display_name"], "[受限资料]")
            serialized = json.dumps(documents, ensure_ascii=False)
            self.assertNotIn("绝密项目", serialized)
            self.assertNotIn("source_path", serialized)
            self.assertNotIn("stored_path", serialized)
            restricted_review = next(
                item
                for item in review_queue["evidence"]
                if item["classification"] == "restricted"
            )
            self.assertEqual(restricted_review["document_name"], "[受限资料]")
            self.assertEqual(restricted_review["locator"], {})

    def test_evaluation_projection_exposes_metrics_without_local_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            paths = WorkspacePaths(Path(temporary) / "workspace")
            paths.create()
            (paths.evaluations / "pilot.evaluation.json").write_text(
                json.dumps(
                    {
                        "schema_version": "1.1",
                        "dataset_name": "试点评测",
                        "dataset_path": "D:/private/source.json",
                        "evaluated_at": "2026-07-22T00:00:00+00:00",
                        "aggregate": {
                            "case_count": 10,
                            "passed_cases": 10,
                            "pass_rate": 1.0,
                            "evidence_count": 1320,
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            service = WorkbenchReadService(Database(paths.database), paths)

            reports = service.evaluations()

            self.assertEqual(reports[0]["metrics"]["pass_rate"], 1.0)
            self.assertNotIn("dataset_path", reports[0])
            self.assertNotIn("D:/private", json.dumps(reports))

    def test_application_is_read_only_and_validates_pagination(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = WorkspacePaths(root / "workspace")
            source = root / "source.md"
            source.write_text("证据。", encoding="utf-8")
            ingest_file(source, paths, Classification.INTERNAL)
            application = WorkbenchWebApplication(
                WorkbenchReadService(Database(paths.database), paths)
            )

            response = application.handle("GET", "/api/v1/bootstrap")
            payload = json.loads(response.body.decode("utf-8"))
            self.assertEqual(response.status, 200)
            self.assertEqual(payload["summary"]["access_mode"], "local-read-only")

            rejected = application.handle("POST", "/api/v1/summary")
            self.assertEqual(rejected.status, 405)
            invalid = application.handle("GET", "/api/v1/documents?limit=201")
            self.assertEqual(invalid.status, 400)
            missing = application.handle("GET", "/api/v1/missing")
            self.assertEqual(missing.status, 404)

    def test_http_server_sets_security_headers_and_binds_loopback(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = WorkspacePaths(root / "workspace")
            source = root / "source.md"
            source.write_text("证据。", encoding="utf-8")
            ingest_file(source, paths, Classification.INTERNAL)
            database = Database(paths.database)
            with self.assertRaisesRegex(KnowledgeWorkbenchError, "只能绑定"):
                build_web_server(database, paths, host="0.0.0.0", port=0)

            server = build_web_server(database, paths, port=0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                port = server.server_address[1]
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/api/v1/health", timeout=5
                ) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                    self.assertEqual(payload["status"], "ok")
                    self.assertEqual(response.headers["X-Frame-Options"], "DENY")
                    self.assertIn("default-src 'self'", response.headers["Content-Security-Policy"])

                request = urllib.request.Request(
                    f"http://127.0.0.1:{port}/api/v1/summary",
                    method="POST",
                )
                with self.assertRaises(urllib.error.HTTPError) as captured:
                    urllib.request.urlopen(request, timeout=5)
                self.assertEqual(captured.exception.code, 405)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
