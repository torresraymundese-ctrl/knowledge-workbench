import json
import tempfile
import unittest
from pathlib import Path

from knowledge_workbench.config import WorkspacePaths
from knowledge_workbench.database import Database
from knowledge_workbench.errors import InvalidTransitionError, KnowledgeWorkbenchError
from knowledge_workbench.evaluation import evaluate_dataset
from knowledge_workbench.ingest import ingest_file
from knowledge_workbench.labeling import (
    approve_labeling_session,
    create_labeling_session,
    export_labeling_dataset,
    labeling_session_summary,
    labeling_session_readiness,
    list_labeling_candidates,
    reject_labeling_session,
    select_expected_evidence,
    select_expected_evidence_batch,
    submit_labeling_session,
)
from knowledge_workbench.linting import lint_workspace
from knowledge_workbench.models import Classification


class LabelingWorkflowTests(unittest.TestCase):
    def test_readiness_reports_all_missing_cases_and_stale_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.md"
            source.write_text("可验证证据。", encoding="utf-8")
            paths = WorkspacePaths(root / "workspace")
            ingest_file(source, paths, Classification.INTERNAL)
            database = Database(paths.database)
            session_id = create_labeling_session(
                database,
                _write_template(root / "template.json", source),
                actor="alice",
                minimum_required_per_case=1,
            )

            initial = labeling_session_readiness(database, session_id)
            self.assertFalse(initial["ready"])
            self.assertEqual(initial["ready_case_count"], 0)
            self.assertEqual(
                {issue["code"] for issue in initial["cases"][0]["issues"]},
                {"insufficient_evidence"},
            )
            evidence_id = list_labeling_candidates(
                database, session_id, "case-1"
            )["candidates"][0]["id"]
            select_expected_evidence(
                database, session_id, "case-1", evidence_id, actor="alice"
            )
            ready = labeling_session_readiness(database, session_id)
            self.assertTrue(ready["ready"])
            self.assertTrue(ready["can_submit"])

            source.write_text("来源发生变化。", encoding="utf-8")
            stale = labeling_session_readiness(database, session_id)
            self.assertFalse(stale["ready"])
            self.assertIn(
                "source_or_processing_stale",
                {issue["code"] for issue in stale["cases"][0]["issues"]},
            )

    def test_batch_selection_is_atomic_deduplicated_and_idempotent(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.md"
            source.write_text("证据一。\n\n证据二。\n\n证据三。", encoding="utf-8")
            paths = WorkspacePaths(root / "workspace")
            ingest_file(source, paths, Classification.INTERNAL)
            database = Database(paths.database)
            session_id = create_labeling_session(
                database,
                _write_template(root / "template.json", source),
                actor="alice",
                minimum_required_per_case=1,
            )
            candidates = list_labeling_candidates(
                database, session_id, "case-1", limit=20
            )["candidates"]
            first, second = candidates[0]["id"], candidates[1]["id"]

            result = select_expected_evidence_batch(
                database,
                session_id,
                "case-1",
                [first, second, first],
                actor="alice",
            )
            repeated = select_expected_evidence_batch(
                database,
                session_id,
                "case-1",
                [first, second],
                actor="alice",
            )

            self.assertEqual(result["requested_count"], 3)
            self.assertEqual(result["unique_count"], 2)
            self.assertEqual(result["added_count"], 2)
            self.assertEqual(repeated["added_count"], 0)
            self.assertEqual(
                labeling_session_summary(database, session_id)["cases"][0][
                    "selected_evidence_count"
                ],
                2,
            )

            third = candidates[2]["id"]
            with self.assertRaisesRegex(KnowledgeWorkbenchError, "不属于该用例"):
                select_expected_evidence_batch(
                    database,
                    session_id,
                    "case-1",
                    [third, "ev_missing"],
                    actor="alice",
                )
            selected_ids = set(
                labeling_session_summary(database, session_id)["cases"][0][
                    "selected_evidence_ids"
                ].split(",")
            )
            self.assertNotIn(third, selected_ids)

    def test_candidates_are_paginated_from_current_run_and_show_selection(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.md"
            source.write_text("证据一。\n\n证据二。\n\n证据三。", encoding="utf-8")
            paths = WorkspacePaths(root / "workspace")
            ingest_file(source, paths, Classification.INTERNAL)
            database = Database(paths.database)
            session_id = create_labeling_session(
                database,
                _write_template(root / "template.json", source),
                actor="alice",
                minimum_required_per_case=1,
            )

            page = list_labeling_candidates(
                database, session_id, "case-1", limit=1, offset=1
            )

            self.assertEqual(page["total"], 3)
            self.assertEqual(len(page["candidates"]), 1)
            self.assertEqual(page["candidates"][0]["run_ordinal"], 2)
            self.assertEqual(page["case"]["classification"], "internal")
            self.assertEqual(page["candidates"][0]["locator"]["line_start"], 3)
            selected_id = page["candidates"][0]["id"]
            select_expected_evidence(
                database, session_id, "case-1", selected_id, actor="alice"
            )
            remaining = list_labeling_candidates(
                database,
                session_id,
                "case-1",
                limit=20,
                only_unselected=True,
            )
            self.assertEqual(remaining["total"], 2)
            self.assertNotIn(
                selected_id, {item["id"] for item in remaining["candidates"]}
            )

    def test_candidates_refuse_stale_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.md"
            source.write_text("原始证据。", encoding="utf-8")
            paths = WorkspacePaths(root / "workspace")
            ingest_file(source, paths, Classification.INTERNAL)
            database = Database(paths.database)
            session_id = create_labeling_session(
                database,
                _write_template(root / "template.json", source),
                actor="alice",
                minimum_required_per_case=1,
            )
            source.write_text("来源已被修改。", encoding="utf-8")

            with self.assertRaisesRegex(KnowledgeWorkbenchError, "内容已经变化"):
                list_labeling_candidates(database, session_id, "case-1")

    def test_two_person_workflow_exports_ready_evaluation_dataset(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.md"
            source.write_text("必须保留原始证据。", encoding="utf-8")
            paths = WorkspacePaths(root / "workspace")
            result = ingest_file(source, paths, Classification.INTERNAL)
            database = Database(paths.database)
            template = _write_template(root / "template.json", source)
            session_id = create_labeling_session(
                database,
                template,
                actor="alice",
                minimum_required_per_case=1,
            )
            with database.connect() as connection:
                evidence_id = connection.execute(
                    "SELECT id FROM evidence WHERE document_version_id = ?",
                    (result.version_id,),
                ).fetchone()[0]

            select_expected_evidence(
                database,
                session_id,
                "case-1",
                evidence_id,
                actor="alice",
            )
            submit_labeling_session(database, session_id, actor="alice")
            with self.assertRaisesRegex(InvalidTransitionError, "必须与提交人不同"):
                approve_labeling_session(database, session_id, actor="alice")
            approve_labeling_session(database, session_id, actor="bob")
            output = paths.evaluations / "approved-dataset.json"
            export_labeling_dataset(
                database, paths, session_id, output, actor="bob"
            )

            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(
                payload["cases"][0]["expected_evidence"],
                [{"required": True, "text": "必须保留原始证据。"}],
            )
            self.assertEqual(evaluate_dataset(output)["aggregate"]["pass_rate"], 1.0)
            summary = labeling_session_summary(database, session_id)
            self.assertEqual(summary["session"]["status"], "approved")
            self.assertEqual(summary["session"]["approved_by"], "bob")
            with database.connect() as connection:
                events = {
                    row[0]
                    for row in connection.execute(
                        "SELECT event_type FROM audit_log WHERE entity_id = ?",
                        (session_id,),
                    ).fetchall()
                }
            self.assertIn("labeling_session_submitted", events)
            self.assertIn("labeling_session_approved", events)
            self.assertIn("labeling_dataset_exported", events)

            source.write_text("批准后来源被修改。", encoding="utf-8")
            lint_report = lint_workspace(database, paths)
            self.assertFalse(lint_report["passed"])
            self.assertIn(
                "labeling_session_invalid",
                {issue["code"] for issue in lint_report["issues"]},
            )

    def test_source_update_blocks_submission_of_stale_selected_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.md"
            source.write_text("第一版事实。", encoding="utf-8")
            paths = WorkspacePaths(root / "workspace")
            first = ingest_file(source, paths, Classification.INTERNAL)
            database = Database(paths.database)
            session_id = create_labeling_session(
                database,
                _write_template(root / "template.json", source),
                actor="alice",
                minimum_required_per_case=1,
            )
            with database.connect() as connection:
                evidence_id = connection.execute(
                    "SELECT id FROM evidence WHERE document_version_id = ?",
                    (first.version_id,),
                ).fetchone()[0]
            select_expected_evidence(
                database, session_id, "case-1", evidence_id, actor="alice"
            )

            source.write_text("第二版事实。", encoding="utf-8")
            ingest_file(source, paths, Classification.INTERNAL)

            with self.assertRaisesRegex(KnowledgeWorkbenchError, "来源文件内容已经变化"):
                submit_labeling_session(database, session_id, actor="alice")

    def test_reviewer_can_reject_back_to_draft_with_reason(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.md"
            source.write_text("需要复核的事实。", encoding="utf-8")
            paths = WorkspacePaths(root / "workspace")
            result = ingest_file(source, paths, Classification.INTERNAL)
            database = Database(paths.database)
            session_id = create_labeling_session(
                database,
                _write_template(root / "template.json", source),
                actor="alice",
                minimum_required_per_case=1,
            )
            with database.connect() as connection:
                evidence_id = connection.execute(
                    "SELECT id FROM evidence WHERE document_version_id = ?",
                    (result.version_id,),
                ).fetchone()[0]
            select_expected_evidence(
                database, session_id, "case-1", evidence_id, actor="alice"
            )
            submit_labeling_session(database, session_id, actor="alice")
            reject_labeling_session(
                database,
                session_id,
                actor="bob",
                note="需要补充定位核对",
            )

            self.assertEqual(
                labeling_session_summary(database, session_id)["session"]["status"],
                "draft",
            )


def _write_template(path: Path, source: Path) -> Path:
    path.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "name": "人工黄金标注",
                "cases": [
                    {
                        "case_id": "case-1",
                        "source_path": source.name,
                        "classification": "internal",
                        "expected_evidence": [
                            {"text": "【待人工填写】", "required": True}
                        ],
                        "forbidden_substrings": [],
                        "max_duplicate_rate": 0,
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path


if __name__ == "__main__":
    unittest.main()
