import tempfile
import unittest
from pathlib import Path

from knowledge_workbench.config import WorkspacePaths
from knowledge_workbench.conflicts import _candidate_pairs, _classify_conflict
from knowledge_workbench.ingest import ingest_file
from knowledge_workbench.models import Classification, EvidenceStatus
from knowledge_workbench.review import (
    publish_revision,
    request_revision_review,
    transition_evidence,
)


class ConflictQueueTests(unittest.TestCase):
    def test_candidate_blocking_stays_near_linear_and_keeps_moved_matches(self):
        older = [
            {"id": f"old-{index}", "run_ordinal": index, "excerpt": f"规则{index}金额100万元。"}
            for index in range(1, 101)
        ]
        newer = [
            {"id": f"new-{index}", "run_ordinal": index, "excerpt": f"规则{index}金额120万元。"}
            for index in range(1, 101)
        ]
        older.append(
            {"id": "old-moved", "run_ordinal": 101, "excerpt": "内部资料允许发送到云端。"}
        )
        newer.append(
            {"id": "new-moved", "run_ordinal": 1_000, "excerpt": "内部资料禁止发送到云端。"}
        )

        pairs = list(_candidate_pairs(older, newer))
        pair_ids = {(old["id"], new["id"]) for old, new in pairs}

        self.assertLess(len(pairs), 800)
        self.assertIn(("old-moved", "new-moved"), pair_ids)

    def test_business_value_change_is_classified_as_potential_conflict(self):
        result = _classify_conflict(
            "合同金额为100万元，付款条件保持不变。",
            "合同金额为120万元，付款条件保持不变。",
        )

        self.assertIsNotNone(result)
        self.assertEqual(result[0], "value_change")

    def test_version_date_and_clause_number_changes_are_not_value_conflicts(self):
        result = _classify_conflict(
            "本方案V2.0于2024年1月1日发布，详见第3条。",
            "本方案V2.1于2024年2月1日发布，详见第4条。",
        )

        self.assertIsNone(result)

    def test_word_containing_shi_character_is_not_positive_polarity(self):
        result = _classify_conflict(
            "处理方式采用本地流程。",
            "处理方式采用离线流程。",
        )

        self.assertIsNone(result)

    def test_opposite_new_version_is_queued_without_overwriting_verified_revision(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = WorkspacePaths(root / "workspace")
            source = root / "policy.md"
            source.write_text("内部资料允许发送到云端模型。", encoding="utf-8")
            first = ingest_file(source, paths, Classification.INTERNAL)

            from knowledge_workbench.database import Database

            database = Database(paths.database)
            with database.transaction() as connection:
                connection.execute(
                    """
                    UPDATE document_governance
                    SET purpose = 'production',
                        scope_status = 'in_scope',
                        authority_status = 'reference',
                        reviewed_by = 'scope-reviewer',
                        reviewed_at = updated_at,
                        decision_reason = '测试中明确准入'
                    WHERE document_id = ?
                    """,
                    (first.document_id,),
                )
            with database.connect() as connection:
                old_evidence_id = connection.execute(
                    "SELECT id FROM evidence WHERE document_version_id = ?",
                    (first.version_id,),
                ).fetchone()[0]
            transition_evidence(
                database, old_evidence_id, EvidenceStatus.REVIEWING, actor="reviewer"
            )
            transition_evidence(
                database, old_evidence_id, EvidenceStatus.VERIFIED, actor="reviewer"
            )
            request_revision_review(database, first.revision_id, actor="reviewer")
            publish_revision(database, paths, first.revision_id, actor="reviewer")

            source.write_text("内部资料禁止发送到云端模型。", encoding="utf-8")
            second = ingest_file(source, paths, Classification.INTERNAL)
            self.assertEqual(second.conflict_count, 1)

            with database.connect() as connection:
                page = connection.execute(
                    "SELECT status, current_verified_revision_id FROM wiki_pages WHERE id = ?",
                    (first.page_id,),
                ).fetchone()
                latest = connection.execute(
                    "SELECT status FROM wiki_revisions WHERE id = ?", (second.revision_id,)
                ).fetchone()[0]
                conflict = connection.execute(
                    "SELECT status, conflict_type FROM conflicts"
                ).fetchone()
            self.assertEqual(page["status"], "verified")
            self.assertEqual(page["current_verified_revision_id"], first.revision_id)
            self.assertEqual(latest, "draft")
            self.assertEqual(conflict["status"], "pending")
            self.assertEqual(conflict["conflict_type"], "polarity_change")


if __name__ == "__main__":
    unittest.main()
