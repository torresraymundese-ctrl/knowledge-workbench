import json
import http.client
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
from knowledge_workbench.web_service import (
    WorkbenchActionService,
    WorkbenchReadService,
)
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
            self.assertEqual(review_queue["totals"]["evidence"], 3)

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
                WorkbenchReadService(Database(paths.database), paths),
                WorkbenchActionService(Database(paths.database)),
                csrf_token="test-csrf-token",
            )

            response = application.handle("GET", "/api/v1/bootstrap")
            payload = json.loads(response.body.decode("utf-8"))
            self.assertEqual(response.status, 200)
            self.assertEqual(
                payload["summary"]["access_mode"], "local-controlled-write"
            )
            self.assertEqual(payload["web"]["csrf_token"], "test-csrf-token")

            rejected = application.handle("POST", "/api/v1/summary")
            self.assertEqual(rejected.status, 405)
            invalid = application.handle("GET", "/api/v1/documents?limit=201")
            self.assertEqual(invalid.status, 400)
            missing = application.handle("GET", "/api/v1/missing")
            self.assertEqual(missing.status, 404)

    def test_evidence_detail_and_transition_enforce_csrf_audit_and_restricted_policy(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = WorkspacePaths(root / "workspace")
            internal = root / "internal.md"
            restricted = root / "restricted.md"
            internal.write_text("可审核的内部证据。", encoding="utf-8")
            restricted.write_text("不可在Web展示的受限证据。", encoding="utf-8")
            ingest_file(internal, paths, Classification.INTERNAL)
            ingest_file(restricted, paths, Classification.RESTRICTED)
            database = Database(paths.database)
            with database.connect() as connection:
                rows = connection.execute(
                    """
                    SELECT e.id, d.classification
                    FROM evidence e
                    JOIN processing_runs pr
                      ON pr.id = e.processing_run_id AND pr.is_current = 1
                    JOIN documents d ON d.current_version_id = pr.document_version_id
                    ORDER BY d.classification
                    """
                ).fetchall()
            evidence_ids = {row["classification"]: row["id"] for row in rows}
            application = WorkbenchWebApplication(
                WorkbenchReadService(database, paths),
                WorkbenchActionService(database),
                csrf_token="csrf",
            )

            detail = application.handle(
                "GET", f"/api/v1/evidence/{evidence_ids['internal']}"
            )
            self.assertEqual(detail.status, 200)
            detail_payload = json.loads(detail.body.decode("utf-8"))
            self.assertEqual(detail_payload["excerpt"], "可审核的内部证据。")
            self.assertNotIn("source_path", detail_payload)
            restricted_detail = application.handle(
                "GET", f"/api/v1/evidence/{evidence_ids['restricted']}"
            )
            self.assertEqual(restricted_detail.status, 403)

            route = f"/api/v1/evidence/{evidence_ids['internal']}/transition"
            request_body = json.dumps(
                {"target": "reviewing", "actor": "reviewer-01"}
            ).encode("utf-8")
            missing_csrf = application.handle(
                "POST",
                route,
                body=request_body,
                headers={"Content-Type": "application/json"},
            )
            self.assertEqual(missing_csrf.status, 403)
            wrong_content_type = application.handle(
                "POST",
                route,
                body=request_body,
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "X-Workbench-CSRF": "csrf",
                },
            )
            self.assertEqual(wrong_content_type.status, 415)
            missing_actor = application.handle(
                "POST",
                route,
                body=json.dumps({"target": "reviewing", "actor": ""}).encode(
                    "utf-8"
                ),
                headers={
                    "Content-Type": "application/json",
                    "X-Workbench-CSRF": "csrf",
                },
            )
            self.assertEqual(missing_actor.status, 400)
            wrong_origin = application.handle(
                "POST",
                route,
                body=request_body,
                headers={
                    "Content-Type": "application/json",
                    "X-Workbench-CSRF": "csrf",
                    "Origin": "http://attacker.invalid",
                    "Host": "127.0.0.1:8765",
                },
            )
            self.assertEqual(wrong_origin.status, 403)
            transitioned = application.handle(
                "POST",
                route,
                body=request_body,
                headers={
                    "Content-Type": "application/json",
                    "X-Workbench-CSRF": "csrf",
                },
            )
            self.assertEqual(transitioned.status, 200)
            with database.connect() as connection:
                status = connection.execute(
                    "SELECT status FROM evidence WHERE id = ?",
                    (evidence_ids["internal"],),
                ).fetchone()[0]
                audit = connection.execute(
                    """
                    SELECT actor FROM audit_log
                    WHERE event_type = 'evidence_status_changed'
                      AND entity_id = ?
                    ORDER BY id DESC LIMIT 1
                    """,
                    (evidence_ids["internal"],),
                ).fetchone()
            self.assertEqual(status, "reviewing")
            self.assertEqual(audit["actor"], "reviewer-01")

            restricted_route = (
                f"/api/v1/evidence/{evidence_ids['restricted']}/transition"
            )
            restricted_write = application.handle(
                "POST",
                restricted_route,
                body=request_body,
                headers={
                    "Content-Type": "application/json",
                    "X-Workbench-CSRF": "csrf",
                },
            )
            self.assertEqual(restricted_write.status, 403)

    def test_conflict_transition_delegates_to_existing_state_machine(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = WorkspacePaths(root / "workspace")
            source = root / "policy.md"
            source.write_text("系统允许用户提交申请。", encoding="utf-8")
            ingest_file(source, paths, Classification.INTERNAL)
            source.write_text("系统禁止用户提交申请。", encoding="utf-8")
            ingest_file(source, paths, Classification.INTERNAL)
            database = Database(paths.database)
            with database.connect() as connection:
                conflict_id = connection.execute(
                    "SELECT id FROM conflicts ORDER BY created_at DESC LIMIT 1"
                ).fetchone()[0]
            application = WorkbenchWebApplication(
                WorkbenchReadService(database, paths),
                WorkbenchActionService(database),
                csrf_token="csrf",
            )
            headers = {
                "Content-Type": "application/json",
                "X-Workbench-CSRF": "csrf",
            }
            route = f"/api/v1/conflicts/{conflict_id}/transition"

            reviewing = application.handle(
                "POST",
                route,
                body=json.dumps(
                    {"target": "reviewing", "actor": "reviewer-01"}
                ).encode("utf-8"),
                headers=headers,
            )
            self.assertEqual(reviewing.status, 200)
            missing_note = application.handle(
                "POST",
                route,
                body=json.dumps(
                    {"target": "resolved", "actor": "reviewer-01"}
                ).encode("utf-8"),
                headers=headers,
            )
            self.assertEqual(missing_note.status, 409)
            resolved = application.handle(
                "POST",
                route,
                body=json.dumps(
                    {
                        "target": "resolved",
                        "actor": "reviewer-01",
                        "note": "已核对新版本适用范围。",
                    },
                    ensure_ascii=False,
                ).encode("utf-8"),
                headers=headers,
            )
            self.assertEqual(resolved.status, 200)
            with database.connect() as connection:
                row = connection.execute(
                    "SELECT status, resolution_note FROM conflicts WHERE id = ?",
                    (conflict_id,),
                ).fetchone()
            self.assertEqual(row["status"], "resolved")
            self.assertEqual(row["resolution_note"], "已核对新版本适用范围。")

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

                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/api/v1/bootstrap", timeout=5
                ) as response:
                    bootstrap = json.loads(response.read().decode("utf-8"))
                with database.connect() as connection:
                    evidence_id = connection.execute(
                        "SELECT id FROM evidence ORDER BY created_at LIMIT 1"
                    ).fetchone()[0]
                payload = json.dumps(
                    {"target": "reviewing", "actor": "reviewer-01"}
                ).encode("utf-8")
                request = urllib.request.Request(
                    f"http://127.0.0.1:{port}/api/v1/evidence/{evidence_id}/transition",
                    data=payload,
                    method="POST",
                    headers={
                        "Content-Type": "application/json",
                        "X-Workbench-CSRF": bootstrap["web"]["csrf_token"],
                    },
                )
                with urllib.request.urlopen(request, timeout=5) as response:
                    self.assertEqual(response.status, 200)
                with database.connect() as connection:
                    status = connection.execute(
                        "SELECT status FROM evidence WHERE id = ?", (evidence_id,)
                    ).fetchone()[0]
                self.assertEqual(status, "reviewing")

                connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                try:
                    connection.request(
                        "GET", "/api/v1/health", headers={"Host": "attacker.invalid"}
                    )
                    response = connection.getresponse()
                    response.read()
                    self.assertEqual(response.status, 403)
                finally:
                    connection.close()

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
