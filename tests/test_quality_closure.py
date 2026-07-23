import json
import tempfile
import unittest
from pathlib import Path

from knowledge_workbench.config import WorkspacePaths
from knowledge_workbench.conflict_candidates import (
    create_cross_document_candidate_pack,
)
from knowledge_workbench.conflict_labeling_plan import (
    create_conflict_labeling_plan,
)
from knowledge_workbench.database import Database
from knowledge_workbench.errors import KnowledgeWorkbenchError
from knowledge_workbench.graph_pilot import build_graph_pilot_pack
from knowledge_workbench.ingest import ingest_file
from knowledge_workbench.labeling import (
    approve_labeling_session,
    create_labeling_session,
    review_labeling_case,
    select_expected_evidence,
    submit_labeling_session,
)
from knowledge_workbench.models import Classification
from knowledge_workbench.quality_closure import (
    build_quality_closure_status,
    save_quality_closure_status,
)


class QualityClosureStatusTests(unittest.TestCase):
    def test_aggregates_independent_real_data_gates_without_overclaiming(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths, database = self._build_ten_document_baseline(root)
            candidate_path = paths.evaluations / "conflicts.json"
            candidate_pack = create_cross_document_candidate_pack(
                database,
                paths,
                candidate_path,
                actor="pack-builder",
                limit=100,
                minimum_similarity=0.5,
            )
            create_conflict_labeling_plan(
                database,
                paths,
                candidate_path,
                paths.evaluations / "conflict-plan.json",
                actor="coordinator-01",
                batch_size=20,
            )

            report = build_quality_closure_status(
                database,
                paths,
                target_gold_documents=10,
            )

            self.assertEqual(report["kind"], "quality-closure-status")
            self.assertFalse(report["summary"]["complete"])
            self.assertTrue(report["summary"]["human_action_required"])
            gates = {
                item["gate_id"]: item for item in report["gates"]
            }
            for gate_id in (
                "workspace_integrity",
                "gold_baseline",
                "gold_expansion",
                "conflict_labeling_plan",
                "graph_pilot_snapshot",
            ):
                self.assertEqual(gates[gate_id]["status"], "passed")
            for gate_id in (
                "conflict_annotation",
                "conflict_independent_review",
                "graph_evidence_review",
                "graph_entity_mentions",
                "graph_business_relationship",
                "graph_gold_evaluation",
            ):
                self.assertEqual(gates[gate_id]["status"], "pending")
            self.assertEqual(
                report["metrics"]["gold"][
                    "approved_current_document_count"
                ],
                10,
            )
            self.assertEqual(
                report["metrics"]["conflict"]["candidate_count"],
                len(candidate_pack["candidates"]),
            )
            self.assertGreater(
                report["metrics"]["conflict"]["candidate_count"], 0
            )
            self.assertEqual(
                report["metrics"]["graph"]["candidate_count"], 10
            )
            self.assertEqual(
                report["metrics"]["graph"]["verified_evidence_count"],
                0,
            )
            report_text = json.dumps(report, ensure_ascii=False)
            self.assertNotIn("甲项目年度预算", report_text)
            self.assertNotIn(str(root), report_text)

    def test_saved_report_is_audited_restricted_to_workspace_and_immutable(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.md"
            source.write_text(
                "甲项目年度预算为100万元。", encoding="utf-8"
            )
            paths = WorkspacePaths(root / "workspace")
            ingest_file(source, paths, Classification.INTERNAL)
            database = Database(paths.database)

            with self.assertRaisesRegex(
                KnowledgeWorkbenchError, "10 到 1000"
            ):
                build_quality_closure_status(
                    database,
                    paths,
                    target_gold_documents=9,
                )
            outside = root / "outside.json"
            with self.assertRaisesRegex(
                KnowledgeWorkbenchError, "workspace/evaluations"
            ):
                save_quality_closure_status(
                    database,
                    paths,
                    outside,
                    actor="quality-owner",
                )
            self.assertFalse(outside.exists())
            with self.assertRaisesRegex(
                KnowledgeWorkbenchError, "actor 不能为空"
            ):
                save_quality_closure_status(
                    database,
                    paths,
                    paths.evaluations / "blank-actor.json",
                    actor="",
                )

            output = paths.evaluations / "quality-status.json"
            report = save_quality_closure_status(
                database,
                paths,
                output,
                actor="quality-owner",
            )
            self.assertTrue(output.is_file())
            self.assertFalse(report["summary"]["complete"])
            with self.assertRaisesRegex(
                KnowledgeWorkbenchError, "不允许静默覆盖"
            ):
                save_quality_closure_status(
                    database,
                    paths,
                    output,
                    actor="quality-owner",
                )
            with database.connect() as connection:
                event = connection.execute(
                    """
                    SELECT details_json FROM audit_log
                    WHERE event_type = 'quality_closure_status_saved'
                    ORDER BY id DESC LIMIT 1
                    """
                ).fetchone()
            details = json.loads(event["details_json"])
            self.assertEqual(
                details["pending_gate_count"],
                report["summary"]["pending_gate_count"],
            )
            self.assertIn("content_sha256", details)
            audit_text = json.dumps(details, ensure_ascii=False)
            self.assertNotIn("甲项目年度预算", audit_text)
            self.assertNotIn(str(root), audit_text)

    @staticmethod
    def _build_ten_document_baseline(
        root: Path,
    ) -> tuple[WorkspacePaths, Database]:
        paths = WorkspacePaths(root / "workspace")
        cases = []
        evidence_by_case = {}
        for index in range(1, 11):
            source = root / f"项目-{index}.md"
            source.write_text(
                f"甲项目年度预算为{100 + index}万元，"
                f"第{index}阶段由实施单位负责。",
                encoding="utf-8",
            )
            result = ingest_file(
                source, paths, Classification.INTERNAL
            )
            database = Database(paths.database)
            with database.connect() as connection:
                evidence_id = connection.execute(
                    """
                    SELECT id FROM evidence
                    WHERE processing_run_id = ?
                    """,
                    (result.processing_run_id,),
                ).fetchone()[0]
            case_id = f"gold-{index:02d}"
            evidence_by_case[case_id] = evidence_id
            cases.append(
                {
                    "case_id": case_id,
                    "source_path": source.name,
                    "classification": "internal",
                    "expected_evidence": [
                        {"text": "【待人工填写】", "required": True}
                    ],
                    "forbidden_substrings": [],
                    "max_duplicate_rate": 0,
                }
            )
        template = root / "gold-template.json"
        template.write_text(
            json.dumps(
                {
                    "schema_version": "1.0",
                    "name": "十份黄金基线",
                    "cases": cases,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        database = Database(paths.database)
        session_id = create_labeling_session(
            database,
            template,
            actor="gold-annotator",
            minimum_required_per_case=1,
        )
        for case_id, evidence_id in evidence_by_case.items():
            select_expected_evidence(
                database,
                session_id,
                case_id,
                evidence_id,
                actor="gold-annotator",
            )
        submit_labeling_session(
            database, session_id, actor="gold-annotator"
        )
        for case_id in evidence_by_case:
            review_labeling_case(
                database,
                session_id,
                case_id,
                "approved",
                actor="gold-reviewer",
            )
        approve_labeling_session(
            database, session_id, actor="gold-reviewer"
        )
        build_graph_pilot_pack(
            database,
            paths,
            session_id,
            paths.evaluations / "graph-pilot.json",
            actor="pilot-builder",
        )
        return paths, database


if __name__ == "__main__":
    unittest.main()
