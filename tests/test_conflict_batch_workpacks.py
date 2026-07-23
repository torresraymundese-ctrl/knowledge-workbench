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
from knowledge_workbench.utils import sha256_file


class ConflictBatchWorkPackTests(unittest.TestCase):
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

            incomplete = self._fill_annotation_pack(
                content,
                candidate_pack,
                first_batch["candidate_ids"][:-1],
            )
            first_path.write_text(incomplete, encoding="utf-8")
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
