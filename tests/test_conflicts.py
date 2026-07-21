import tempfile
import unittest
from pathlib import Path

from knowledge_workbench.config import WorkspacePaths
from knowledge_workbench.ingest import ingest_file
from knowledge_workbench.models import Classification, EvidenceStatus
from knowledge_workbench.review import (
    publish_revision,
    request_revision_review,
    transition_evidence,
)


class ConflictQueueTests(unittest.TestCase):
    def test_opposite_new_version_is_queued_without_overwriting_verified_revision(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = WorkspacePaths(root / "workspace")
            source = root / "policy.md"
            source.write_text("内部资料允许发送到云端模型。", encoding="utf-8")
            first = ingest_file(source, paths, Classification.INTERNAL)

            from knowledge_workbench.database import Database

            database = Database(paths.database)
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

