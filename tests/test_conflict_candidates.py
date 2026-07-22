import json
import tempfile
import unittest
from pathlib import Path

from knowledge_workbench.config import WorkspacePaths
from knowledge_workbench.conflict_candidates import (
    create_cross_document_candidate_pack,
    cross_document_candidate_page,
    finalize_cross_document_candidate_pack,
    list_cross_document_candidate_packs,
    submit_cross_document_candidate_annotations,
    submit_cross_document_candidate_annotations_by_id,
    update_cross_document_candidate_label,
    update_cross_document_candidate_review,
)
from knowledge_workbench.conflict_evaluation import evaluate_conflict_dataset
from knowledge_workbench.database import Database
from knowledge_workbench.errors import KnowledgeWorkbenchError
from knowledge_workbench.ingest import ingest_file
from knowledge_workbench.models import Classification


class CrossDocumentConflictCandidateTests(unittest.TestCase):
    def test_candidate_pack_excludes_restricted_and_keeps_cross_document_pairs(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = WorkspacePaths(root / "workspace")
            self._ingest_sources(root, paths)
            database = Database(paths.database)
            output = paths.evaluations / "candidates.json"

            pack = create_cross_document_candidate_pack(
                database,
                paths,
                output,
                actor="annotator-01",
                limit=20,
                minimum_similarity=0.5,
            )

            self.assertTrue(output.is_file())
            self.assertEqual(pack["statistics"]["eligible_evidence_count"], 3)
            self.assertEqual(pack["statistics"]["restricted_evidence_excluded"], 1)
            self.assertEqual(pack["statistics"]["total_candidate_count"], 3)
            self.assertFalse(pack["statistics"]["truncated"])
            self.assertEqual(len(pack["candidates"]), 3)
            serialized = json.dumps(pack, ensure_ascii=False)
            self.assertNotIn("受限资料禁止发送到云端模型", serialized)
            for candidate in pack["candidates"]:
                self.assertNotEqual(
                    candidate["left"]["document_id"],
                    candidate["right"]["document_id"],
                )
                self.assertTrue(candidate["left"]["locators"])
                self.assertTrue(candidate["right"]["locators"])
                self.assertIsNone(candidate["label"]["expected_conflict"])
                self.assertIsNone(candidate["review"]["decision"])
            with database.connect() as connection:
                audit = connection.execute(
                    """
                    SELECT actor, details_json FROM audit_log
                    WHERE event_type = 'conflict_candidate_pack_created'
                    """
                ).fetchone()
            self.assertEqual(audit["actor"], "annotator-01")
            self.assertEqual(
                json.loads(audit["details_json"])["restricted_evidence_excluded"],
                1,
            )

    def test_candidate_pack_output_is_workspace_scoped_and_never_overwritten(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = WorkspacePaths(root / "workspace")
            self._ingest_sources(root, paths)
            database = Database(paths.database)
            outside = root / "outside.json"
            with self.assertRaisesRegex(KnowledgeWorkbenchError, "只能保存到"):
                create_cross_document_candidate_pack(
                    database, paths, outside, actor="annotator-01"
                )
            self.assertFalse(outside.exists())

            output = paths.evaluations / "candidates.json"
            create_cross_document_candidate_pack(
                database, paths, output, actor="annotator-01"
            )
            original = output.read_bytes()
            with self.assertRaisesRegex(KnowledgeWorkbenchError, "不会覆盖"):
                create_cross_document_candidate_pack(
                    database, paths, output, actor="annotator-01"
                )
            self.assertEqual(output.read_bytes(), original)

    def test_finalize_requires_complete_two_person_review_and_builds_dataset(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = WorkspacePaths(root / "workspace")
            self._ingest_sources(root, paths)
            database = Database(paths.database)
            pack_path = paths.evaluations / "candidates.json"
            create_cross_document_candidate_pack(
                database,
                paths,
                pack_path,
                actor="pack-builder",
                limit=20,
                minimum_similarity=0.5,
            )

            with self.assertRaisesRegex(KnowledgeWorkbenchError, "尚未填写"):
                finalize_cross_document_candidate_pack(
                    database,
                    paths,
                    pack_path,
                    paths.evaluations / "pending.json",
                    name="真实跨文档冲突基线",
                    reviewer="reviewer-01",
                )
            pack = json.loads(pack_path.read_text(encoding="utf-8"))
            for candidate in pack["candidates"]:
                candidate["label"]["expected_conflict"] = candidate[
                    "predicted_conflict"
                ]
                candidate["label"]["expected_type"] = candidate["predicted_type"]
                candidate["label"]["note"] = "已回到两份来源核对。"
            pack_path.write_text(
                json.dumps(pack, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            submitted = submit_cross_document_candidate_annotations(
                database, paths, pack_path, actor="annotator-01"
            )
            self.assertEqual(submitted["candidate_count"], 3)

            pack = json.loads(pack_path.read_text(encoding="utf-8"))
            for candidate in pack["candidates"]:
                candidate["review"]["decision"] = "approved"
                candidate["review"]["note"] = "复核通过。"
            tampered = json.loads(json.dumps(pack, ensure_ascii=False))
            tampered["candidates"][0]["label"]["note"] = "提交后改动标签。"
            pack_path.write_text(
                json.dumps(tampered, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            with self.assertRaisesRegex(KnowledgeWorkbenchError, "尚未通过 submit-pack"):
                finalize_cross_document_candidate_pack(
                    database,
                    paths,
                    pack_path,
                    paths.evaluations / "tampered.json",
                    name="真实跨文档冲突基线",
                    reviewer="reviewer-01",
                )
            pack_path.write_text(
                json.dumps(pack, ensure_ascii=False, indent=2), encoding="utf-8"
            )

            with self.assertRaisesRegex(KnowledgeWorkbenchError, "必须不同"):
                finalize_cross_document_candidate_pack(
                    database,
                    paths,
                    pack_path,
                    paths.evaluations / "same-actor.json",
                    name="真实跨文档冲突基线",
                    reviewer="annotator-01",
                )
            output = paths.evaluations / "dataset.json"
            dataset = finalize_cross_document_candidate_pack(
                database,
                paths,
                pack_path,
                output,
                name="真实跨文档冲突基线",
                reviewer="reviewer-01",
            )

            self.assertEqual(len(dataset["cases"]), 3)
            report = evaluate_conflict_dataset(output)
            self.assertEqual(report["aggregate"]["pass_rate"], 1.0)
            with database.connect() as connection:
                audit = connection.execute(
                    """
                    SELECT actor, details_json FROM audit_log
                    WHERE event_type = 'conflict_dataset_finalized'
                    """
                ).fetchone()
            details = json.loads(audit["details_json"])
            self.assertEqual(audit["actor"], "reviewer-01")
            self.assertEqual(details["annotator"], "annotator-01")
            self.assertEqual(details["case_count"], 3)

    def test_truncated_pack_cannot_be_finalized_as_complete_baseline(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = WorkspacePaths(root / "workspace")
            self._ingest_sources(root, paths)
            database = Database(paths.database)
            pack_path = paths.evaluations / "truncated.json"
            pack = create_cross_document_candidate_pack(
                database,
                paths,
                pack_path,
                actor="pack-builder",
                limit=1,
                minimum_similarity=0.5,
            )
            self.assertTrue(pack["statistics"]["truncated"])
            with self.assertRaisesRegex(KnowledgeWorkbenchError, "已截断"):
                finalize_cross_document_candidate_pack(
                    database,
                    paths,
                    pack_path,
                    paths.evaluations / "dataset.json",
                    name="不完整基线",
                    reviewer="reviewer-01",
                )

    def test_annotation_submission_rejects_stale_or_restricted_sources(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = WorkspacePaths(root / "workspace")
            self._ingest_sources(root, paths)
            database = Database(paths.database)
            pack_path = paths.evaluations / "stale.json"
            pack = create_cross_document_candidate_pack(
                database,
                paths,
                pack_path,
                actor="pack-builder",
                limit=20,
                minimum_similarity=0.5,
            )
            for candidate in pack["candidates"]:
                candidate["label"]["expected_conflict"] = candidate[
                    "predicted_conflict"
                ]
                candidate["label"]["expected_type"] = candidate["predicted_type"]
            pack_path.write_text(
                json.dumps(pack, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            stale_evidence_id = pack["candidates"][0]["left"]["evidence_id"]
            with database.transaction() as connection:
                connection.execute(
                    "UPDATE evidence SET status = 'deprecated' WHERE id = ?",
                    (stale_evidence_id,),
                )

            with self.assertRaisesRegex(KnowledgeWorkbenchError, "已过期、受限"):
                submit_cross_document_candidate_annotations(
                    database, paths, pack_path, actor="annotator-01"
                )

    def test_web_annotation_projection_enforces_hash_lock_and_two_person_review(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = WorkspacePaths(root / "workspace")
            self._ingest_sources(root, paths)
            database = Database(paths.database)
            pack_path = paths.evaluations / "web-candidates.json"
            pack = create_cross_document_candidate_pack(
                database,
                paths,
                pack_path,
                actor="pack-builder",
                limit=20,
                minimum_similarity=0.5,
            )

            listing = list_cross_document_candidate_packs(database, paths)
            self.assertEqual(listing["total"], 1)
            self.assertEqual(listing["items"][0]["phase"], "labeling")
            self.assertNotIn("web-candidates.json", json.dumps(listing))
            page = cross_document_candidate_page(
                database,
                paths,
                pack["pack_id"],
                limit=1,
                state="unlabeled",
            )
            self.assertEqual(page["total"], 3)
            stale_sha256 = page["content_sha256"]
            first = page["items"][0]
            updated = update_cross_document_candidate_label(
                database,
                paths,
                pack["pack_id"],
                first["candidate_id"],
                expected_content_sha256=stale_sha256,
                expected_conflict=first["predicted_conflict"],
                expected_type=first["predicted_type"],
                note="已回源核对。",
                actor="annotator-01",
            )
            with self.assertRaisesRegex(KnowledgeWorkbenchError, "刷新后重试"):
                update_cross_document_candidate_label(
                    database,
                    paths,
                    pack["pack_id"],
                    pack["candidates"][1]["candidate_id"],
                    expected_content_sha256=stale_sha256,
                    expected_conflict=False,
                    expected_type=None,
                    note=None,
                    actor="annotator-01",
                )

            content_sha256 = updated["content_sha256"]
            for candidate in pack["candidates"][1:]:
                updated = update_cross_document_candidate_label(
                    database,
                    paths,
                    pack["pack_id"],
                    candidate["candidate_id"],
                    expected_content_sha256=content_sha256,
                    expected_conflict=candidate["predicted_conflict"],
                    expected_type=candidate["predicted_type"],
                    note=None,
                    actor="annotator-01",
                )
                content_sha256 = updated["content_sha256"]
            submitted = submit_cross_document_candidate_annotations_by_id(
                database,
                paths,
                pack["pack_id"],
                expected_content_sha256=content_sha256,
                actor="annotator-01",
            )
            self.assertEqual(submitted["phase"], "reviewing")
            with self.assertRaisesRegex(KnowledgeWorkbenchError, "不能继续修改"):
                update_cross_document_candidate_label(
                    database,
                    paths,
                    pack["pack_id"],
                    first["candidate_id"],
                    expected_content_sha256=content_sha256,
                    expected_conflict=False,
                    expected_type=None,
                    note=None,
                    actor="annotator-01",
                )
            with self.assertRaisesRegex(KnowledgeWorkbenchError, "必须不同"):
                update_cross_document_candidate_review(
                    database,
                    paths,
                    pack["pack_id"],
                    first["candidate_id"],
                    expected_content_sha256=content_sha256,
                    decision="approved",
                    note=None,
                    actor="annotator-01",
                )
            reviewed = update_cross_document_candidate_review(
                database,
                paths,
                pack["pack_id"],
                first["candidate_id"],
                expected_content_sha256=content_sha256,
                decision="approved",
                note="复核通过。",
                actor="reviewer-01",
            )
            approved = cross_document_candidate_page(
                database,
                paths,
                pack["pack_id"],
                state="approved",
            )
            self.assertEqual(reviewed["phase"], "reviewing")
            self.assertEqual(approved["total"], 1)
            with database.connect() as connection:
                events = {
                    row[0]
                    for row in connection.execute(
                        """
                        SELECT event_type FROM audit_log
                        WHERE entity_id = ?
                        """,
                        (pack["pack_id"],),
                    ).fetchall()
                }
            self.assertIn("conflict_candidate_label_updated", events)
            self.assertIn("conflict_candidate_review_updated", events)

    @staticmethod
    def _ingest_sources(root: Path, paths: WorkspacePaths) -> None:
        sources = (
            ("允许.md", "内部资料允许发送到云端模型。", Classification.INTERNAL),
            ("禁止.md", "内部资料禁止发送到云端模型。", Classification.INTERNAL),
            (
                "补充.md",
                "内部资料允许发送到云端模型，适用于所有部门。",
                Classification.INTERNAL,
            ),
            (
                "受限.md",
                "受限资料禁止发送到云端模型。",
                Classification.RESTRICTED,
            ),
        )
        for name, content, classification in sources:
            source = root / name
            source.write_text(content, encoding="utf-8")
            ingest_file(source, paths, classification)


if __name__ == "__main__":
    unittest.main()
