import json
import tempfile
import unittest
from pathlib import Path

from knowledge_workbench.config import WorkspacePaths
from knowledge_workbench.conflict_candidates import (
    create_cross_document_candidate_pack,
    update_cross_document_candidate_label,
)
from knowledge_workbench.conflict_labeling_plan import (
    create_conflict_labeling_plan,
    inspect_conflict_labeling_plan,
)
from knowledge_workbench.database import Database
from knowledge_workbench.errors import KnowledgeWorkbenchError
from knowledge_workbench.ingest import ingest_file
from knowledge_workbench.models import Classification
from knowledge_workbench.utils import sha256_file


class ConflictLabelingPlanTests(unittest.TestCase):
    def test_partitions_complete_pack_once_and_tracks_live_progress(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = WorkspacePaths(root / "workspace")
            self._ingest_sources(root, paths)
            database = Database(paths.database)
            pack_path = paths.evaluations / "full-candidates.json"
            pack = create_cross_document_candidate_pack(
                database,
                paths,
                pack_path,
                actor="pack-builder",
                limit=100,
                minimum_similarity=0.5,
            )
            self.assertFalse(pack["statistics"]["truncated"])
            self.assertGreater(len(pack["candidates"]), 10)

            first_path = paths.evaluations / "labeling-plan-a.json"
            first = create_conflict_labeling_plan(
                database,
                paths,
                pack_path,
                first_path,
                actor="coordinator-01",
                batch_size=10,
                seed="quality-v1",
            )
            second = create_conflict_labeling_plan(
                database,
                paths,
                pack_path,
                paths.evaluations / "labeling-plan-b.json",
                actor="coordinator-02",
                batch_size=10,
                seed="quality-v1",
            )
            self.assertEqual(first["batches"], second["batches"])
            assigned = [
                candidate_id
                for batch in first["batches"]
                for candidate_id in batch["candidate_ids"]
            ]
            expected = [
                candidate["candidate_id"]
                for candidate in pack["candidates"]
            ]
            self.assertCountEqual(assigned, expected)
            self.assertEqual(len(assigned), len(set(assigned)))
            self.assertTrue(
                all(
                    len(batch["candidate_ids"]) <= 10
                    for batch in first["batches"]
                )
            )
            serialized = json.dumps(first, ensure_ascii=False)
            self.assertNotIn("项目预算", serialized)
            self.assertNotIn("document_name", serialized)

            status = inspect_conflict_labeling_plan(
                database, paths, first_path
            )
            self.assertEqual(
                status["summary"]["candidate_count"], len(expected)
            )
            self.assertEqual(status["summary"]["labeled_count"], 0)
            self.assertFalse(status["summary"]["annotation_complete"])

            first_candidate = pack["candidates"][0]
            update_cross_document_candidate_label(
                database,
                paths,
                pack["pack_id"],
                first_candidate["candidate_id"],
                expected_content_sha256=sha256_file(pack_path),
                expected_conflict=first_candidate["predicted_conflict"],
                expected_type=first_candidate["predicted_type"],
                note="人工回源确认。",
                actor="annotator-01",
            )
            updated = inspect_conflict_labeling_plan(
                database, paths, first_path
            )
            self.assertEqual(updated["summary"]["labeled_count"], 1)
            self.assertEqual(updated["summary"]["unlabeled_count"], len(expected) - 1)
            with database.connect() as connection:
                audit = connection.execute(
                    """
                    SELECT details_json FROM audit_log
                    WHERE event_type = 'conflict_labeling_plan_created'
                      AND entity_id = ?
                    """,
                    (first["plan_id"],),
                ).fetchone()
            details = json.loads(audit["details_json"])
            self.assertNotIn("quality-v1", json.dumps(details))
            self.assertEqual(details["candidate_count"], len(expected))

    def test_rejects_truncated_tampered_copied_and_stale_inputs(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = WorkspacePaths(root / "workspace")
            self._ingest_sources(root, paths)
            database = Database(paths.database)
            truncated_path = paths.evaluations / "truncated.json"
            create_cross_document_candidate_pack(
                database,
                paths,
                truncated_path,
                actor="pack-builder",
                limit=1,
                minimum_similarity=0.5,
            )
            with self.assertRaisesRegex(KnowledgeWorkbenchError, "未截断"):
                create_conflict_labeling_plan(
                    database,
                    paths,
                    truncated_path,
                    paths.evaluations / "invalid-plan.json",
                    actor="coordinator-01",
                )

            pack_path = paths.evaluations / "full.json"
            pack = create_cross_document_candidate_pack(
                database,
                paths,
                pack_path,
                actor="pack-builder",
                limit=100,
                minimum_similarity=0.5,
            )
            plan_path = paths.evaluations / "plan.json"
            create_conflict_labeling_plan(
                database,
                paths,
                pack_path,
                plan_path,
                actor="coordinator-01",
                batch_size=10,
            )
            copied_path = paths.evaluations / "copied-plan.json"
            copied_path.write_bytes(plan_path.read_bytes())
            with self.assertRaisesRegex(KnowledgeWorkbenchError, "审计记录"):
                inspect_conflict_labeling_plan(
                    database, paths, copied_path
                )

            tampered = json.loads(plan_path.read_text(encoding="utf-8"))
            tampered["batches"][0]["candidate_ids"].reverse()
            plan_path.write_text(
                json.dumps(tampered, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(KnowledgeWorkbenchError, "身份校验"):
                inspect_conflict_labeling_plan(
                    database, paths, plan_path
                )

            with database.transaction() as connection:
                connection.execute(
                    "UPDATE evidence SET status = 'deprecated' WHERE id = ?",
                    (pack["candidates"][0]["left"]["evidence_id"],),
                )
            with self.assertRaisesRegex(KnowledgeWorkbenchError, "已过期、受限"):
                create_conflict_labeling_plan(
                    database,
                    paths,
                    pack_path,
                    paths.evaluations / "stale-plan.json",
                    actor="coordinator-01",
                )

    @staticmethod
    def _ingest_sources(root: Path, paths: WorkspacePaths) -> None:
        for index, amount in enumerate((100, 110, 120, 130, 140, 150), start=1):
            source = root / f"预算-{index}.md"
            source.write_text(
                f"甲项目年度预算为{amount}万元，适用于研发部门。",
                encoding="utf-8",
            )
            ingest_file(source, paths, Classification.INTERNAL)


if __name__ == "__main__":
    unittest.main()
