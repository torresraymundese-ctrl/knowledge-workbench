import json
import tempfile
import unittest
from pathlib import Path

from knowledge_workbench.config import WorkspacePaths
from knowledge_workbench.conflict_batch_workpacks import (
    apply_conflict_batch_annotation_pack,
    apply_conflict_batch_review_pack,
    export_conflict_batch_annotation_pack,
    export_conflict_batch_review_pack,
    inspect_conflict_batch_work_pack,
)
from knowledge_workbench.conflict_candidates import (
    create_cross_document_candidate_pack,
    submit_cross_document_candidate_annotations_by_id,
)
from knowledge_workbench.conflict_labeling_plan import (
    create_conflict_labeling_plan,
    inspect_conflict_labeling_plan,
)
from knowledge_workbench.database import Database
from knowledge_workbench.errors import (
    InvalidTransitionError,
    KnowledgeWorkbenchError,
)
from knowledge_workbench.ingest import ingest_file
from knowledge_workbench.models import Classification
from knowledge_workbench.quality_closure import (
    build_quality_closure_status,
)
from knowledge_workbench.review_assurance import (
    SOLO_ATTESTATION_PHRASE,
)
from knowledge_workbench.utils import sha256_file, sha256_text


class ConflictBatchWorkPackTests(unittest.TestCase):
    def test_solo_review_is_attested_but_never_counted_as_independent(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = WorkspacePaths(root / "workspace")
            self._ingest_sources(root, paths)
            database = Database(paths.database)
            candidate_path = paths.evaluations / "solo-candidates.json"
            candidate_pack = create_cross_document_candidate_pack(
                database,
                paths,
                candidate_path,
                actor="pack-builder",
                limit=100,
                minimum_similarity=0.5,
            )
            plan_path = paths.evaluations / "solo-plan.json"
            plan = create_conflict_labeling_plan(
                database,
                paths,
                candidate_path,
                plan_path,
                actor="solo-owner",
                batch_size=100,
            )
            batch = plan["batches"][0]
            annotation_path = (
                paths.evaluations / "solo-annotation.md"
            )
            export_conflict_batch_annotation_pack(
                database,
                paths,
                plan["plan_id"],
                batch["batch_id"],
                annotation_path,
                actor="solo-owner",
            )
            annotation_path.write_text(
                self._fill_annotation_pack(
                    annotation_path.read_text(encoding="utf-8"),
                    candidate_pack,
                    batch["candidate_ids"],
                ),
                encoding="utf-8",
            )
            apply_conflict_batch_annotation_pack(
                database,
                paths,
                annotation_path,
                actor="solo-owner",
            )
            submit_cross_document_candidate_annotations_by_id(
                database,
                paths,
                candidate_pack["pack_id"],
                expected_content_sha256=sha256_file(candidate_path),
                actor="solo-owner",
            )

            with self.assertRaisesRegex(
                KnowledgeWorkbenchError, "必须不同"
            ):
                export_conflict_batch_review_pack(
                    database,
                    paths,
                    plan["plan_id"],
                    batch["batch_id"],
                    paths.evaluations / "false-independent.md",
                    actor="solo-owner",
                )
            review_path = paths.evaluations / "solo-review.md"
            export_conflict_batch_review_pack(
                database,
                paths,
                plan["plan_id"],
                batch["batch_id"],
                review_path,
                actor="solo-owner",
                review_mode="solo_attested",
            )
            content = review_path.read_text(encoding="utf-8")
            self.assertIn("非独立复核", content)
            self.assertIn(SOLO_ATTESTATION_PHRASE, content)
            review_path.write_text(
                self._approve_review_pack(
                    content, batch["candidate_ids"]
                ),
                encoding="utf-8",
            )
            status = inspect_conflict_batch_work_pack(
                database, paths, review_path
            )
            self.assertEqual(status["review_mode"], "solo_attested")
            self.assertFalse(status["independent_review"])
            self.assertFalse(status["actor_separation_valid"])
            self.assertTrue(status["review_actor_policy_valid"])
            self.assertTrue(status["solo_attestation_required"])
            self.assertTrue(status["apply_ready"])

            with self.assertRaisesRegex(
                KnowledgeWorkbenchError, "确认声明"
            ):
                apply_conflict_batch_review_pack(
                    database,
                    paths,
                    review_path,
                    actor="solo-owner",
                )
            with self.assertRaisesRegex(
                KnowledgeWorkbenchError, "确认短语"
            ):
                apply_conflict_batch_review_pack(
                    database,
                    paths,
                    review_path,
                    actor="solo-owner",
                    solo_attestation="我确认",
                )
            apply_conflict_batch_review_pack(
                database,
                paths,
                review_path,
                actor="solo-owner",
                solo_attestation=SOLO_ATTESTATION_PHRASE,
            )
            plan_status = inspect_conflict_labeling_plan(
                database, paths, plan_path
            )
            summary = plan_status["summary"]
            self.assertEqual(
                summary["solo_attested_approved_count"],
                len(batch["candidate_ids"]),
            )
            self.assertEqual(
                summary["independent_approved_count"], 0
            )
            self.assertTrue(summary["human_attested_review_complete"])
            self.assertFalse(summary["independent_review_complete"])
            with database.connect() as connection:
                row = connection.execute(
                    """
                    SELECT details_json FROM audit_log
                    WHERE event_type =
                      'conflict_candidate_review_batch_applied'
                    ORDER BY id DESC LIMIT 1
                    """
                ).fetchone()
            details = json.loads(row["details_json"])
            self.assertEqual(details["review_mode"], "solo_attested")
            self.assertFalse(details["independent_review"])
            self.assertEqual(
                details["solo_attestation_sha256"],
                sha256_text(SOLO_ATTESTATION_PHRASE),
            )
            self.assertNotIn(
                SOLO_ATTESTATION_PHRASE, row["details_json"]
            )
            tampered = json.loads(
                candidate_path.read_text(encoding="utf-8")
            )
            tampered["candidates"][0]["review"]["note"] = (
                "绕过受控入口修改"
            )
            candidate_path.write_text(
                json.dumps(
                    tampered,
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            tampered_status = inspect_conflict_labeling_plan(
                database, paths, plan_path
            )["summary"]
            self.assertFalse(tampered_status["review_audit_current"])
            self.assertEqual(
                tampered_status["human_attested_approved_count"], 0
            )
            self.assertEqual(
                tampered_status["unattributed_approved_count"],
                len(batch["candidate_ids"]),
            )
            self.assertFalse(
                tampered_status["human_attested_review_complete"]
            )

    def test_annotation_and_review_work_packs_round_trip_atomically(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = WorkspacePaths(root / "workspace")
            self._ingest_sources(root, paths)
            database = Database(paths.database)
            candidate_path = paths.evaluations / "candidates.json"
            candidate_pack = create_cross_document_candidate_pack(
                database,
                paths,
                candidate_path,
                actor="pack-builder",
                limit=100,
                minimum_similarity=0.5,
            )
            plan_path = paths.evaluations / "plan.json"
            plan = create_conflict_labeling_plan(
                database,
                paths,
                candidate_path,
                plan_path,
                actor="coordinator-01",
                batch_size=10,
                seed="work-pack-v1",
            )
            first_batch, second_batch = plan["batches"]

            first_path = paths.evaluations / "batch-001-annotation.md"
            stale_second_path = (
                paths.evaluations / "batch-002-annotation-stale.md"
            )
            export_conflict_batch_annotation_pack(
                database,
                paths,
                plan["plan_id"],
                first_batch["batch_id"],
                first_path,
                actor="annotator-01",
            )
            export_conflict_batch_annotation_pack(
                database,
                paths,
                plan["plan_id"],
                second_batch["batch_id"],
                stale_second_path,
                actor="annotator-01",
            )
            original = candidate_path.read_bytes()
            content = first_path.read_text(encoding="utf-8")
            self.assertIn(
                "type: conflict-batch-annotation-pack", content
            )
            self.assertIn("规则预测只用于召回与排序", content)
            self.assertIn("甲项目年度预算", content)
            with database.connect() as connection:
                audit_count_before_status = connection.execute(
                    "SELECT COUNT(*) FROM audit_log"
                ).fetchone()[0]
            blank_status = inspect_conflict_batch_work_pack(
                database, paths, first_path
            )
            self.assertEqual(
                blank_status["work_pack_type"], "annotation"
            )
            self.assertEqual(
                blank_status["candidate_count"],
                len(first_batch["candidate_ids"]),
            )
            self.assertEqual(
                blank_status["decision_counts"]["undecided"],
                len(first_batch["candidate_ids"]),
            )
            self.assertTrue(blank_status["integrity_valid"])
            self.assertEqual(
                blank_status["source_phase"], "labeling"
            )
            self.assertFalse(blank_status["apply_ready"])
            blank_quality = build_quality_closure_status(
                database, paths, target_gold_documents=10
            )
            self.assertEqual(
                blank_quality["metrics"]["work_packs"][
                    "conflict_annotation_incomplete_count"
                ],
                2,
            )
            with database.connect() as connection:
                audit_count_after_status = connection.execute(
                    "SELECT COUNT(*) FROM audit_log"
                ).fetchone()[0]
            self.assertEqual(
                audit_count_after_status, audit_count_before_status
            )

            incomplete = self._fill_annotation_pack(
                content,
                candidate_pack,
                first_batch["candidate_ids"][:-1],
            )
            first_path.write_text(incomplete, encoding="utf-8")
            incomplete_status = inspect_conflict_batch_work_pack(
                database, paths, first_path
            )
            self.assertEqual(
                incomplete_status["remaining_decision_count"], 1
            )
            self.assertFalse(incomplete_status["apply_ready"])
            with self.assertRaisesRegex(
                KnowledgeWorkbenchError, "必须且只能选择"
            ):
                apply_conflict_batch_annotation_pack(
                    database,
                    paths,
                    first_path,
                    actor="annotator-01",
                )
            self.assertEqual(candidate_path.read_bytes(), original)

            complete = self._fill_annotation_pack(
                content,
                candidate_pack,
                first_batch["candidate_ids"],
            )
            first_path.write_text(complete, encoding="utf-8")
            ready_status = inspect_conflict_batch_work_pack(
                database, paths, first_path
            )
            self.assertEqual(
                ready_status["complete_decision_count"],
                len(first_batch["candidate_ids"]),
            )
            self.assertEqual(ready_status["issue_codes"], [])
            self.assertTrue(ready_status["apply_ready"])
            copied = paths.evaluations / "copied-annotation.md"
            copied.write_text(complete, encoding="utf-8")
            with self.assertRaisesRegex(
                KnowledgeWorkbenchError, "导出审计记录"
            ):
                apply_conflict_batch_annotation_pack(
                    database, paths, copied, actor="annotator-01"
                )
            with self.assertRaisesRegex(
                KnowledgeWorkbenchError, "标注人.*actor"
            ):
                apply_conflict_batch_annotation_pack(
                    database, paths, first_path, actor="other-annotator"
                )

            applied_first = apply_conflict_batch_annotation_pack(
                database,
                paths,
                first_path,
                actor="annotator-01",
            )
            self.assertEqual(
                len(applied_first["candidate_ids"]),
                len(first_batch["candidate_ids"]),
            )
            applied_first_work_pack = (
                inspect_conflict_batch_work_pack(
                    database, paths, first_path
                )
            )
            self.assertTrue(
                applied_first_work_pack["already_applied"]
            )
            self.assertFalse(
                applied_first_work_pack["apply_ready"]
            )
            self.assertIn(
                "work_pack_already_applied",
                applied_first_work_pack["issue_codes"],
            )
            applied_quality = build_quality_closure_status(
                database, paths, target_gold_documents=10
            )
            applied_work_packs = applied_quality["metrics"][
                "work_packs"
            ]
            self.assertEqual(
                applied_work_packs[
                    "conflict_annotation_applied_count"
                ],
                1,
            )
            self.assertEqual(
                applied_work_packs[
                    "conflict_annotation_invalid_count"
                ],
                2,
            )
            first_status = inspect_conflict_labeling_plan(
                database, paths, plan_path
            )
            self.assertEqual(
                first_status["summary"]["labeled_count"],
                len(first_batch["candidate_ids"]),
            )

            stale_content = self._fill_annotation_pack(
                stale_second_path.read_text(encoding="utf-8"),
                candidate_pack,
                second_batch["candidate_ids"],
            )
            stale_second_path.write_text(
                stale_content, encoding="utf-8"
            )
            stale_status = inspect_conflict_batch_work_pack(
                database, paths, stale_second_path
            )
            self.assertFalse(stale_status["integrity_valid"])
            self.assertIn(
                "source_content_drift", stale_status["issue_codes"]
            )
            self.assertFalse(stale_status["apply_ready"])
            with self.assertRaisesRegex(
                KnowledgeWorkbenchError, "重新导出"
            ):
                apply_conflict_batch_annotation_pack(
                    database,
                    paths,
                    stale_second_path,
                    actor="annotator-01",
                )

            second_path = paths.evaluations / "batch-002-annotation.md"
            export_conflict_batch_annotation_pack(
                database,
                paths,
                plan["plan_id"],
                second_batch["batch_id"],
                second_path,
                actor="annotator-01",
            )
            second_content = self._fill_annotation_pack(
                second_path.read_text(encoding="utf-8"),
                candidate_pack,
                second_batch["candidate_ids"],
            )
            second_path.write_text(second_content, encoding="utf-8")
            ready_second = inspect_conflict_batch_work_pack(
                database, paths, second_path
            )
            self.assertTrue(ready_second["apply_ready"])
            ready_second_quality = build_quality_closure_status(
                database, paths, target_gold_documents=10
            )
            self.assertEqual(
                ready_second_quality["metrics"]["work_packs"][
                    "conflict_annotation_ready_count"
                ],
                1,
            )
            apply_conflict_batch_annotation_pack(
                database,
                paths,
                second_path,
                actor="annotator-01",
            )
            complete_status = inspect_conflict_labeling_plan(
                database, paths, plan_path
            )
            self.assertTrue(
                complete_status["summary"]["annotation_complete"]
            )

            submit_cross_document_candidate_annotations_by_id(
                database,
                paths,
                candidate_pack["pack_id"],
                expected_content_sha256=sha256_file(candidate_path),
                actor="annotator-01",
            )
            with self.assertRaisesRegex(
                InvalidTransitionError, "必须不同"
            ):
                export_conflict_batch_review_pack(
                    database,
                    paths,
                    plan["plan_id"],
                    first_batch["batch_id"],
                    paths.evaluations / "same-actor-review.md",
                    actor="annotator-01",
                )

            review_path = paths.evaluations / "batch-001-review.md"
            export_conflict_batch_review_pack(
                database,
                paths,
                plan["plan_id"],
                first_batch["batch_id"],
                review_path,
                actor="reviewer-01",
            )
            review_content = self._approve_review_pack(
                review_path.read_text(encoding="utf-8"),
                first_batch["candidate_ids"],
            )
            review_path.write_text(review_content, encoding="utf-8")
            review_status = inspect_conflict_batch_work_pack(
                database, paths, review_path
            )
            self.assertEqual(review_status["work_pack_type"], "review")
            self.assertTrue(review_status["actor_separation_valid"])
            self.assertEqual(
                review_status["complete_decision_count"],
                len(first_batch["candidate_ids"]),
            )
            self.assertTrue(review_status["apply_ready"])
            with self.assertRaisesRegex(
                KnowledgeWorkbenchError, "复核人.*actor"
            ):
                apply_conflict_batch_review_pack(
                    database,
                    paths,
                    review_path,
                    actor="reviewer-02",
                )
            reviewed = apply_conflict_batch_review_pack(
                database,
                paths,
                review_path,
                actor="reviewer-01",
            )
            self.assertEqual(
                len(reviewed["candidate_ids"]),
                len(first_batch["candidate_ids"]),
            )
            applied_review_work_pack = (
                inspect_conflict_batch_work_pack(
                    database, paths, review_path
                )
            )
            self.assertTrue(
                applied_review_work_pack["already_applied"]
            )
            self.assertFalse(
                applied_review_work_pack["apply_ready"]
            )
            reviewed_status = inspect_conflict_labeling_plan(
                database, paths, plan_path
            )
            self.assertEqual(
                reviewed_status["summary"]["approved_count"],
                len(first_batch["candidate_ids"]),
            )
            with database.connect() as connection:
                events = connection.execute(
                    """
                    SELECT event_type, details_json FROM audit_log
                    WHERE entity_id = ?
                      AND event_type IN (
                        'conflict_candidate_label_batch_applied',
                        'conflict_candidate_review_batch_applied'
                      )
                    ORDER BY id
                    """,
                    (candidate_pack["pack_id"],),
                ).fetchall()
            self.assertEqual(len(events), 3)
            audit_text = json.dumps(
                [json.loads(row["details_json"]) for row in events],
                ensure_ascii=False,
            )
            self.assertNotIn("甲项目年度预算", audit_text)
            self.assertNotIn("人工核对通过", audit_text)
            self.assertIn("work_pack_sha256", audit_text)

    def test_work_pack_paths_and_decisions_are_strict(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = WorkspacePaths(root / "workspace")
            self._ingest_sources(root, paths)
            database = Database(paths.database)
            candidate_path = paths.evaluations / "candidates.json"
            candidate_pack = create_cross_document_candidate_pack(
                database,
                paths,
                candidate_path,
                actor="pack-builder",
                limit=100,
                minimum_similarity=0.5,
            )
            plan = create_conflict_labeling_plan(
                database,
                paths,
                candidate_path,
                paths.evaluations / "plan.json",
                actor="coordinator-01",
                batch_size=10,
            )
            outside = root / "outside.md"
            with self.assertRaisesRegex(
                KnowledgeWorkbenchError, "workspace/evaluations"
            ):
                export_conflict_batch_annotation_pack(
                    database,
                    paths,
                    plan["plan_id"],
                    plan["batches"][0]["batch_id"],
                    outside,
                    actor="annotator-01",
                )
            self.assertFalse(outside.exists())

            output = paths.evaluations / "annotation.md"
            export_conflict_batch_annotation_pack(
                database,
                paths,
                plan["plan_id"],
                plan["batches"][0]["batch_id"],
                output,
                actor="annotator-01",
            )
            original = output.read_bytes()
            with self.assertRaisesRegex(
                KnowledgeWorkbenchError, "不允许静默覆盖"
            ):
                export_conflict_batch_annotation_pack(
                    database,
                    paths,
                    plan["plan_id"],
                    plan["batches"][0]["batch_id"],
                    output,
                    actor="annotator-01",
                )
            self.assertEqual(output.read_bytes(), original)

            completed = self._fill_annotation_pack(
                output.read_text(encoding="utf-8"),
                candidate_pack,
                plan["batches"][0]["candidate_ids"],
            )
            protected_tamper = completed.replace(
                "甲项目年度预算", "伪造项目年度预算", 1
            )
            output.write_text(protected_tamper, encoding="utf-8")
            tamper_status = inspect_conflict_batch_work_pack(
                database, paths, output
            )
            self.assertFalse(tamper_status["integrity_valid"])
            self.assertIn(
                "export_audit_or_template_invalid",
                tamper_status["issue_codes"],
            )
            with self.assertRaisesRegex(
                KnowledgeWorkbenchError, "受保护内容"
            ):
                apply_conflict_batch_annotation_pack(
                    database,
                    paths,
                    output,
                    actor="annotator-01",
                )

            tampered = completed.replace(
                plan["batches"][0]["candidate_ids"][0],
                "xdoc_00000000000000000000",
                1,
            )
            output.write_text(tampered, encoding="utf-8")
            with self.assertRaisesRegex(
                KnowledgeWorkbenchError, "顺序或范围"
            ):
                apply_conflict_batch_annotation_pack(
                    database,
                    paths,
                    output,
                    actor="annotator-01",
                )

            duplicated = completed.replace(
                "- [x] 冲突",
                "- [x] 冲突\n- [x] 冲突",
                1,
            )
            output.write_text(duplicated, encoding="utf-8")
            with self.assertRaisesRegex(
                KnowledgeWorkbenchError, "包含重复"
            ):
                apply_conflict_batch_annotation_pack(
                    database,
                    paths,
                    output,
                    actor="annotator-01",
                )

            quoted_actor_output = (
                paths.evaluations / "quoted-actor-annotation.md"
            )
            export_conflict_batch_annotation_pack(
                database,
                paths,
                plan["plan_id"],
                plan["batches"][0]["batch_id"],
                quoted_actor_output,
                actor="annotator'one",
            )
            self.assertIn(
                "--actor 'annotator''one'",
                quoted_actor_output.read_text(encoding="utf-8"),
            )

    @staticmethod
    def _fill_annotation_pack(
        content: str,
        candidate_pack: dict,
        candidate_ids: list[str],
    ) -> str:
        by_id = {
            candidate["candidate_id"]: candidate
            for candidate in candidate_pack["candidates"]
        }
        for candidate_id in candidate_ids:
            start = content.index(f"## `{candidate_id}`")
            next_start = content.find("\n## `", start + 1)
            if next_start < 0:
                next_start = len(content)
            block = content[start:next_start]
            candidate = by_id[candidate_id]
            if candidate["predicted_conflict"]:
                block = block.replace(
                    "- [ ] 冲突", "- [x] 冲突", 1
                ).replace(
                    "- 冲突类型：`null`",
                    f"- 冲突类型：`{candidate['predicted_type']}`",
                    1,
                )
            else:
                block = block.replace(
                    "- [ ] 非冲突", "- [x] 非冲突", 1
                )
            block = block.replace(
                '- 标注依据 JSON：""',
                "- 标注依据 JSON："
                + json.dumps("人工核对通过", ensure_ascii=False),
                1,
            )
            content = content[:start] + block + content[next_start:]
        return content

    @staticmethod
    def _approve_review_pack(
        content: str, candidate_ids: list[str]
    ) -> str:
        for candidate_id in candidate_ids:
            start = content.index(f"## `{candidate_id}`")
            next_start = content.find("\n## `", start + 1)
            if next_start < 0:
                next_start = len(content)
            block = content[start:next_start].replace(
                "- [ ] 批准人工标签",
                "- [x] 批准人工标签",
                1,
            )
            content = content[:start] + block + content[next_start:]
        return content

    @staticmethod
    def _ingest_sources(root: Path, paths: WorkspacePaths) -> None:
        for index, amount in enumerate(
            (100, 110, 120, 130, 140, 150), start=1
        ):
            source = root / f"预算-{index}.md"
            source.write_text(
                f"甲项目年度预算为{amount}万元，适用于研发部门。",
                encoding="utf-8",
            )
            ingest_file(source, paths, Classification.INTERNAL)


if __name__ == "__main__":
    unittest.main()
