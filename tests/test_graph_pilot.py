import json
import tempfile
import unittest
from pathlib import Path

from knowledge_workbench.config import WorkspacePaths
from knowledge_workbench.database import Database
from knowledge_workbench.errors import KnowledgeWorkbenchError
from knowledge_workbench.graph_pilot import (
    build_graph_pilot_pack,
    graph_pilot_pack_page,
    inspect_graph_pilot_pack,
    list_graph_pilot_packs,
)
from knowledge_workbench.ingest import ingest_file
from knowledge_workbench.labeling import (
    approve_labeling_session,
    create_labeling_session,
    review_labeling_case,
    select_expected_evidence,
    submit_labeling_session,
)
from knowledge_workbench.models import Classification
from knowledge_workbench.web_service import (
    WorkbenchActionService,
    WorkbenchReadService,
)
from knowledge_workbench.webapp import WorkbenchWebApplication


class GraphPilotPackTests(unittest.TestCase):
    def test_builds_audited_pack_from_approved_gold_without_state_changes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            internal_source = root / "internal.md"
            restricted_source = root / "restricted.md"
            internal_source.write_text(
                "甲公司负责平台建设。", encoding="utf-8"
            )
            restricted_source.write_text(
                "受限项目由秘密团队负责。", encoding="utf-8"
            )
            paths = WorkspacePaths(root / "workspace")
            internal_ingestion = ingest_file(
                internal_source, paths, Classification.INTERNAL
            )
            restricted_ingestion = ingest_file(
                restricted_source, paths, Classification.RESTRICTED
            )
            database = Database(paths.database)
            template = root / "template.json"
            template.write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "name": "图谱试点来源",
                        "cases": [
                            _template_case(
                                "internal-case",
                                internal_source.name,
                                "internal",
                            ),
                            _template_case(
                                "restricted-case",
                                restricted_source.name,
                                "restricted",
                            ),
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            session_id = create_labeling_session(
                database,
                template,
                actor="annotator-01",
                minimum_required_per_case=1,
            )
            with database.connect() as connection:
                internal_evidence = connection.execute(
                    "SELECT id FROM evidence WHERE processing_run_id = ?",
                    (internal_ingestion.processing_run_id,),
                ).fetchone()[0]
                restricted_evidence = connection.execute(
                    "SELECT id FROM evidence WHERE processing_run_id = ?",
                    (restricted_ingestion.processing_run_id,),
                ).fetchone()[0]
            select_expected_evidence(
                database,
                session_id,
                "internal-case",
                internal_evidence,
                actor="annotator-01",
            )
            select_expected_evidence(
                database,
                session_id,
                "restricted-case",
                restricted_evidence,
                actor="annotator-01",
            )
            submit_labeling_session(
                database, session_id, actor="annotator-01"
            )
            for case_id in ("internal-case", "restricted-case"):
                review_labeling_case(
                    database,
                    session_id,
                    case_id,
                    "approved",
                    actor="reviewer-02",
                )
            approve_labeling_session(
                database, session_id, actor="reviewer-02"
            )

            output = paths.evaluations / "graph-pilot.json"
            pack = build_graph_pilot_pack(
                database,
                paths,
                session_id,
                output,
                actor="pilot-builder",
            )

            self.assertEqual(
                pack["kind"], "graph-pilot-evidence-pack"
            )
            self.assertEqual(
                pack["statistics"]["selected_evidence_count"], 2
            )
            self.assertEqual(
                pack["statistics"]["exported_evidence_count"], 1
            )
            self.assertEqual(
                pack["statistics"]["restricted_evidence_excluded"], 1
            )
            self.assertEqual(
                pack["statistics"]["status_counts"], {"draft": 1}
            )
            self.assertEqual(
                pack["candidates"][0]["evidence_id"], internal_evidence
            )
            self.assertTrue(pack["candidates"][0]["locators"])
            self.assertFalse(
                pack["workflow"]["automatic_status_changes"]
            )
            content = output.read_text(encoding="utf-8")
            self.assertIn("甲公司负责平台建设", content)
            self.assertNotIn("受限项目由秘密团队负责", content)
            with database.connect() as connection:
                statuses = {
                    row["id"]: row["status"]
                    for row in connection.execute(
                        "SELECT id, status FROM evidence"
                    ).fetchall()
                }
                audit = connection.execute(
                    """
                    SELECT details_json FROM audit_log
                    WHERE event_type = 'graph_pilot_pack_created'
                    """
                ).fetchone()
            self.assertEqual(statuses[internal_evidence], "draft")
            self.assertEqual(statuses[restricted_evidence], "draft")
            audit_text = audit["details_json"]
            self.assertNotIn("甲公司负责平台建设", audit_text)
            self.assertNotIn("受限项目由秘密团队负责", audit_text)
            self.assertIn("content_sha256", audit_text)
            status = inspect_graph_pilot_pack(database, paths, output)
            self.assertEqual(
                status["summary"]["snapshot_valid_count"], 1
            )
            self.assertEqual(
                status["summary"]["status_counts"], {"draft": 1}
            )
            self.assertEqual(
                status["summary"]["verified_evidence_coverage"], 0.0
            )
            self.assertFalse(
                status["summary"]["graph_gold_prerequisites_met"]
            )
            self.assertNotIn(
                "甲公司负责平台建设",
                json.dumps(status, ensure_ascii=False),
            )
            listing = list_graph_pilot_packs(database, paths)
            self.assertEqual(listing["total"], 1)
            self.assertEqual(listing["invalid_pack_count"], 0)
            page = graph_pilot_pack_page(
                database, paths, pack["pack_id"], limit=1
            )
            self.assertEqual(page["total"], 1)
            self.assertEqual(page["items"][0]["status"], "draft")
            self.assertNotIn(
                "excerpt", json.dumps(page, ensure_ascii=False)
            )

            application = WorkbenchWebApplication(
                WorkbenchReadService(database, paths),
                WorkbenchActionService(database, paths),
                csrf_token="csrf",
            )
            packs_response = application.handle(
                "GET", "/api/v1/graph-pilot-packs"
            )
            self.assertEqual(packs_response.status, 200)
            self.assertNotIn(
                "甲公司负责平台建设",
                packs_response.body.decode("utf-8"),
            )
            page_response = application.handle(
                "GET",
                f"/api/v1/graph-pilot-packs/{pack['pack_id']}?status=draft",
            )
            self.assertEqual(page_response.status, 200)
            self.assertNotIn(
                "甲公司负责平台建设",
                page_response.body.decode("utf-8"),
            )
            invalid_filter = application.handle(
                "GET",
                f"/api/v1/graph-pilot-packs/{pack['pack_id']}?status=pending",
            )
            self.assertEqual(invalid_filter.status, 400)
            invalid_limit = application.handle(
                "GET",
                f"/api/v1/graph-pilot-packs/{pack['pack_id']}?limit=51",
            )
            self.assertEqual(invalid_limit.status, 400)
            transition_route = (
                f"/api/v1/evidence/{internal_evidence}/transition"
            )
            transition_body = json.dumps(
                {"target": "reviewing", "actor": "reviewer-03"}
            ).encode("utf-8")
            missing_csrf = application.handle(
                "POST",
                transition_route,
                body=transition_body,
                headers={"Content-Type": "application/json"},
            )
            self.assertEqual(missing_csrf.status, 403)
            headers = {
                "Content-Type": "application/json",
                "X-Workbench-CSRF": "csrf",
            }
            transitioned = application.handle(
                "POST",
                transition_route,
                body=transition_body,
                headers=headers,
            )
            self.assertEqual(transitioned.status, 200)
            verified = application.handle(
                "POST",
                transition_route,
                body=json.dumps(
                    {"target": "verified", "actor": "reviewer-04"}
                ).encode("utf-8"),
                headers=headers,
            )
            self.assertEqual(verified.status, 200)
            verified_page = graph_pilot_pack_page(
                database,
                paths,
                pack["pack_id"],
                status="verified",
            )
            self.assertEqual(verified_page["total"], 1)
            self.assertEqual(
                verified_page["summary"]["verified_evidence_coverage"],
                1.0,
            )
            with self.assertRaisesRegex(
                KnowledgeWorkbenchError, "不允许静默覆盖"
            ):
                build_graph_pilot_pack(
                    database,
                    paths,
                    session_id,
                    output,
                    actor="pilot-builder",
                )
            copied = paths.evaluations / "copied.json"
            copied.write_text(content, encoding="utf-8")
            with self.assertRaisesRegex(
                KnowledgeWorkbenchError, "审计记录"
            ):
                inspect_graph_pilot_pack(database, paths, copied)
            listing_with_copy = list_graph_pilot_packs(database, paths)
            self.assertEqual(listing_with_copy["total"], 1)
            self.assertEqual(
                listing_with_copy["invalid_pack_count"], 1
            )

    def test_requires_approved_session_and_workspace_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.md"
            source.write_text("候选证据。", encoding="utf-8")
            paths = WorkspacePaths(root / "workspace")
            ingest_file(source, paths, Classification.INTERNAL)
            database = Database(paths.database)
            template = root / "template.json"
            template.write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "name": "未批准",
                        "cases": [
                            _template_case(
                                "case-1", source.name, "internal"
                            )
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            session_id = create_labeling_session(
                database,
                template,
                actor="annotator-01",
                minimum_required_per_case=1,
            )
            with self.assertRaisesRegex(
                KnowledgeWorkbenchError, "workspace/evaluations"
            ):
                build_graph_pilot_pack(
                    database,
                    paths,
                    session_id,
                    root / "outside.json",
                    actor="pilot-builder",
                )
            with self.assertRaisesRegex(
                KnowledgeWorkbenchError, "只有 approved"
            ):
                build_graph_pilot_pack(
                    database,
                    paths,
                    session_id,
                    paths.evaluations / "not-approved.json",
                    actor="pilot-builder",
                )


def _template_case(
    case_id: str, source_name: str, classification: str
) -> dict:
    return {
        "case_id": case_id,
        "source_path": source_name,
        "classification": classification,
        "expected_evidence": [
            {"text": "【待人工填写】", "required": True}
        ],
        "forbidden_substrings": [],
        "max_duplicate_rate": 0,
    }


if __name__ == "__main__":
    unittest.main()
