import json
import tempfile
import unittest
from pathlib import Path

from knowledge_workbench.config import WorkspacePaths
from knowledge_workbench.database import Database
from knowledge_workbench.errors import (
    InvalidTransitionError,
    KnowledgeWorkbenchError,
)
from knowledge_workbench.graph_pilot import (
    build_graph_pilot_pack,
    inspect_graph_pilot_pack,
)
from knowledge_workbench.graph_pilot_review_workpacks import (
    apply_graph_pilot_triage_work_pack,
    apply_graph_pilot_verification_work_pack,
    export_graph_pilot_triage_work_pack,
    export_graph_pilot_verification_work_pack,
)
from knowledge_workbench.ingest import ingest_file
from knowledge_workbench.labeling import (
    approve_labeling_session,
    create_labeling_session,
    review_labeling_case,
    select_expected_evidence,
    submit_labeling_session,
)
from knowledge_workbench.models import (
    Classification,
    EvidenceStatus,
)
from knowledge_workbench.review import transition_evidence


class GraphPilotReviewWorkPackTests(unittest.TestCase):
    def test_triage_and_verification_round_trip_is_atomic_and_separated(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths, database, source_pack_path, source_pack = (
                self._build_pilot(root)
            )
            evidence_ids = [
                item["evidence_id"]
                for item in source_pack["candidates"]
            ]
            triage_path = paths.evaluations / "triage.md"
            export_graph_pilot_triage_work_pack(
                database,
                paths,
                source_pack_path,
                triage_path,
                actor="curator-01",
            )
            original = triage_path.read_text(encoding="utf-8")
            self.assertIn(
                "type: graph-pilot-evidence-triage-pack", original
            )
            self.assertIn("甲项目第", original)
            self.assertEqual(self._statuses(database), {"draft": 3})

            incomplete = self._fill_triage(
                original,
                submit_ids=set(evidence_ids[:-1]),
                hold_ids=set(),
            )
            triage_path.write_text(incomplete, encoding="utf-8")
            with self.assertRaisesRegex(
                KnowledgeWorkbenchError, "必须且只能选择"
            ):
                apply_graph_pilot_triage_work_pack(
                    database,
                    paths,
                    triage_path,
                    actor="curator-01",
                )
            self.assertEqual(self._statuses(database), {"draft": 3})

            complete = self._fill_triage(
                original,
                submit_ids=set(evidence_ids[:2]),
                hold_ids={evidence_ids[2]},
            )
            triage_path.write_text(complete, encoding="utf-8")
            copied = paths.evaluations / "copied-triage.md"
            copied.write_text(complete, encoding="utf-8")
            with self.assertRaisesRegex(
                KnowledgeWorkbenchError, "导出审计记录"
            ):
                apply_graph_pilot_triage_work_pack(
                    database,
                    paths,
                    copied,
                    actor="curator-01",
                )
            with self.assertRaisesRegex(
                KnowledgeWorkbenchError, "初审人.*actor"
            ):
                apply_graph_pilot_triage_work_pack(
                    database,
                    paths,
                    triage_path,
                    actor="other-curator",
                )

            triage_result = apply_graph_pilot_triage_work_pack(
                database,
                paths,
                triage_path,
                actor="curator-01",
            )
            self.assertEqual(triage_result["submitted_count"], 2)
            self.assertEqual(triage_result["held_count"], 1)
            self.assertEqual(
                self._statuses(database),
                {"draft": 1, "reviewing": 2},
            )
            with self.assertRaisesRegex(
                InvalidTransitionError, "当前 actor"
            ):
                export_graph_pilot_verification_work_pack(
                    database,
                    paths,
                    source_pack_path,
                    paths.evaluations / "self-review.md",
                    actor="curator-01",
                )

            verification_path = paths.evaluations / "verification.md"
            export_graph_pilot_verification_work_pack(
                database,
                paths,
                source_pack_path,
                verification_path,
                actor="reviewer-02",
            )
            verification = self._fill_verification(
                verification_path.read_text(encoding="utf-8"),
                approve_ids={evidence_ids[0]},
                return_ids={evidence_ids[1]},
            )
            verification_path.write_text(
                verification, encoding="utf-8"
            )
            with self.assertRaisesRegex(
                KnowledgeWorkbenchError, "复核人.*actor"
            ):
                apply_graph_pilot_verification_work_pack(
                    database,
                    paths,
                    verification_path,
                    actor="reviewer-03",
                )
            result = apply_graph_pilot_verification_work_pack(
                database,
                paths,
                verification_path,
                actor="reviewer-02",
            )
            self.assertEqual(result["approved_count"], 1)
            self.assertEqual(result["returned_count"], 1)
            self.assertEqual(
                self._statuses(database),
                {"draft": 2, "verified": 1},
            )
            status = inspect_graph_pilot_pack(
                database, paths, source_pack_path
            )
            self.assertEqual(
                status["summary"]["verified_evidence_count"], 1
            )
            self.assertEqual(
                status["summary"]["verified_evidence_coverage"],
                0.333333,
            )
            with database.connect() as connection:
                batch_events = connection.execute(
                    """
                    SELECT event_type, details_json FROM audit_log
                    WHERE entity_type = 'graph_pilot_pack'
                      AND entity_id = ?
                      AND event_type IN (
                        'graph_pilot_triage_applied',
                        'graph_pilot_verification_applied'
                      )
                    ORDER BY id
                    """,
                    (source_pack["pack_id"],),
                ).fetchall()
                status_events = connection.execute(
                    """
                    SELECT actor, details_json FROM audit_log
                    WHERE event_type = 'evidence_status_changed'
                      AND entity_id IN (?, ?)
                    ORDER BY id
                    """,
                    tuple(evidence_ids[:2]),
                ).fetchall()
            self.assertEqual(len(batch_events), 2)
            self.assertEqual(len(status_events), 4)
            audit_text = json.dumps(
                [
                    json.loads(row["details_json"])
                    for row in batch_events
                ],
                ensure_ascii=False,
            )
            self.assertNotIn("甲项目第", audit_text)
            self.assertNotIn("来源定位待补充", audit_text)
            self.assertNotIn("原子边界需调整", audit_text)
            self.assertIn("note_sha256_by_evidence", audit_text)
            self.assertTrue(
                all(
                    "graph_pilot_pack_id"
                    in json.loads(row["details_json"])
                    for row in status_events
                )
            )

    def test_paths_duplicates_and_status_drift_are_strict(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths, database, source_pack_path, source_pack = (
                self._build_pilot(root)
            )
            outside = root / "outside.md"
            with self.assertRaisesRegex(
                KnowledgeWorkbenchError, "workspace/evaluations"
            ):
                export_graph_pilot_triage_work_pack(
                    database,
                    paths,
                    source_pack_path,
                    outside,
                    actor="curator'one",
                )
            self.assertFalse(outside.exists())

            output = paths.evaluations / "triage.md"
            export_graph_pilot_triage_work_pack(
                database,
                paths,
                source_pack_path,
                output,
                actor="curator'one",
            )
            original = output.read_text(encoding="utf-8")
            self.assertIn("--actor 'curator''one'", original)
            with self.assertRaisesRegex(
                KnowledgeWorkbenchError, "不允许静默覆盖"
            ):
                export_graph_pilot_triage_work_pack(
                    database,
                    paths,
                    source_pack_path,
                    output,
                    actor="curator'one",
                )

            evidence_ids = [
                item["evidence_id"]
                for item in source_pack["candidates"]
            ]
            completed = self._fill_triage(
                original,
                submit_ids=set(evidence_ids),
                hold_ids=set(),
            )
            protected_tamper = completed.replace(
                "甲项目第", "伪造项目第", 1
            )
            output.write_text(protected_tamper, encoding="utf-8")
            with self.assertRaisesRegex(
                KnowledgeWorkbenchError, "受保护内容"
            ):
                apply_graph_pilot_triage_work_pack(
                    database,
                    paths,
                    output,
                    actor="curator'one",
                )

            duplicated = completed.replace(
                "- [x] 提交复核",
                "- [x] 提交复核\n- [x] 提交复核",
                1,
            )
            output.write_text(duplicated, encoding="utf-8")
            with self.assertRaisesRegex(
                KnowledgeWorkbenchError, "重复字段"
            ):
                apply_graph_pilot_triage_work_pack(
                    database,
                    paths,
                    output,
                    actor="curator'one",
                )

            output.write_text(completed, encoding="utf-8")
            drifted_id = evidence_ids[-1]
            transition_evidence(
                database,
                drifted_id,
                EvidenceStatus.ARCHIVED,
                actor="external-reviewer",
            )
            with self.assertRaisesRegex(
                KnowledgeWorkbenchError, "来源或状态已变化"
            ):
                apply_graph_pilot_triage_work_pack(
                    database,
                    paths,
                    output,
                    actor="curator'one",
                )
            self.assertEqual(
                self._statuses(database),
                {"archived": 1, "draft": 2},
            )

    @staticmethod
    def _fill_triage(
        content: str,
        *,
        submit_ids: set[str],
        hold_ids: set[str],
    ) -> str:
        for evidence_id in submit_ids | hold_ids:
            start = content.index(f"## `{evidence_id}`")
            next_start = content.find("\n## `", start + 1)
            if next_start < 0:
                next_start = len(content)
            block = content[start:next_start]
            if evidence_id in submit_ids:
                block = block.replace(
                    "- [ ] 提交复核", "- [x] 提交复核", 1
                )
                note = "逐字和定位已人工核对"
            else:
                block = block.replace("- [ ] 暂缓", "- [x] 暂缓", 1)
                note = "来源定位待补充"
            block = block.replace(
                '- 决策依据 JSON：""',
                "- 决策依据 JSON："
                + json.dumps(note, ensure_ascii=False),
                1,
            )
            content = content[:start] + block + content[next_start:]
        return content

    @staticmethod
    def _fill_verification(
        content: str,
        *,
        approve_ids: set[str],
        return_ids: set[str],
    ) -> str:
        for evidence_id in approve_ids | return_ids:
            start = content.index(f"## `{evidence_id}`")
            next_start = content.find("\n## `", start + 1)
            if next_start < 0:
                next_start = len(content)
            block = content[start:next_start]
            if evidence_id in approve_ids:
                block = block.replace(
                    "- [ ] 验证通过", "- [x] 验证通过", 1
                )
                note = "独立复核通过"
            else:
                block = block.replace(
                    "- [ ] 退回草稿", "- [x] 退回草稿", 1
                )
                note = "原子边界需调整"
            block = block.replace(
                '- 复核意见 JSON：""',
                "- 复核意见 JSON："
                + json.dumps(note, ensure_ascii=False),
                1,
            )
            content = content[:start] + block + content[next_start:]
        return content

    @staticmethod
    def _statuses(database: Database) -> dict[str, int]:
        with database.connect() as connection:
            rows = connection.execute(
                """
                SELECT status, COUNT(*) AS count
                FROM evidence
                GROUP BY status
                ORDER BY status
                """
            ).fetchall()
        return {row["status"]: row["count"] for row in rows}

    @staticmethod
    def _build_pilot(
        root: Path,
    ) -> tuple[WorkspacePaths, Database, Path, dict]:
        paths = WorkspacePaths(root / "workspace")
        evidence_by_case = {}
        cases = []
        for index in range(1, 4):
            source = root / f"项目-{index}.md"
            source.write_text(
                f"甲项目第{index}阶段由实施单位负责验收。",
                encoding="utf-8",
            )
            result = ingest_file(
                source, paths, Classification.INTERNAL
            )
            case_id = f"pilot-{index}"
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
            database = Database(paths.database)
            with database.connect() as connection:
                evidence_id = connection.execute(
                    """
                    SELECT id FROM evidence
                    WHERE processing_run_id = ?
                    """,
                    (result.processing_run_id,),
                ).fetchone()[0]
            evidence_by_case[case_id] = evidence_id
        template = root / "template.json"
        template.write_text(
            json.dumps(
                {
                    "schema_version": "1.0",
                    "name": "真实图谱试点测试",
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
        source_pack_path = paths.evaluations / "graph-pilot.json"
        source_pack = build_graph_pilot_pack(
            database,
            paths,
            session_id,
            source_pack_path,
            actor="pilot-builder",
        )
        return paths, database, source_pack_path, source_pack


if __name__ == "__main__":
    unittest.main()
