import tempfile
import unittest
from pathlib import Path

from knowledge_workbench.config import WorkspacePaths
from knowledge_workbench.ingest import ingest_file
from knowledge_workbench.models import Classification, EvidenceStatus
from knowledge_workbench.review import transition_evidence


class IngestTests(unittest.TestCase):
    def test_ingest_is_traceable_and_idempotent(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.md"
            source.write_text(
                "# 项目规则\n\n所有正式知识必须绑定原始证据。\n\n机密资料禁止发送到云端。\n",
                encoding="utf-8",
            )
            paths = WorkspacePaths(root / "workspace")

            first = ingest_file(source, paths, Classification.CONFIDENTIAL)
            second = ingest_file(source, paths, Classification.CONFIDENTIAL)

            self.assertFalse(first.duplicate)
            self.assertTrue(second.duplicate)
            self.assertEqual(first.version_id, second.version_id)
            self.assertEqual(first.evidence_count, 2)
            self.assertTrue(next(paths.raw.rglob("*.md")).is_file())
            self.assertTrue(next(paths.wiki_drafts.glob("*.md")).is_file())

            from knowledge_workbench.database import Database

            with Database(paths.database).connect() as connection:
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM document_versions").fetchone()[0],
                    1,
                )
                evidence = connection.execute(
                    "SELECT id, excerpt FROM evidence ORDER BY ordinal"
                ).fetchall()
                self.assertEqual(evidence[0][1], "所有正式知识必须绑定原始证据。")

    def test_evidence_cannot_skip_reviewing(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.txt"
            source.write_text("一条可审核的证据。", encoding="utf-8")
            paths = WorkspacePaths(root / "workspace")
            result = ingest_file(source, paths, Classification.INTERNAL)

            from knowledge_workbench.database import Database
            from knowledge_workbench.errors import InvalidTransitionError

            database = Database(paths.database)
            with database.connect() as connection:
                evidence_id = connection.execute("SELECT id FROM evidence").fetchone()[0]
            with self.assertRaises(InvalidTransitionError):
                transition_evidence(
                    database,
                    evidence_id,
                    EvidenceStatus.VERIFIED,
                    actor="tester",
                )

    def test_review_and_publish_full_revision(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.txt"
            source.write_text("一条可发布的原始证据。", encoding="utf-8")
            paths = WorkspacePaths(root / "workspace")
            result = ingest_file(source, paths, Classification.INTERNAL)

            from knowledge_workbench.database import Database
            from knowledge_workbench.review import publish_revision, request_revision_review

            database = Database(paths.database)
            with database.connect() as connection:
                evidence_id = connection.execute("SELECT id FROM evidence").fetchone()[0]
            transition_evidence(
                database, evidence_id, EvidenceStatus.REVIEWING, actor="reviewer"
            )
            transition_evidence(
                database, evidence_id, EvidenceStatus.VERIFIED, actor="reviewer"
            )
            request_revision_review(database, result.revision_id, actor="reviewer")
            published = publish_revision(
                database, paths, result.revision_id, actor="reviewer"
            )

            self.assertTrue(published.is_file())
            self.assertIn("status: verified", published.read_text(encoding="utf-8"))
            with database.connect() as connection:
                page_status = connection.execute(
                    "SELECT status FROM wiki_pages WHERE id = ?", (result.page_id,)
                ).fetchone()[0]
            self.assertEqual(page_status, "verified")


if __name__ == "__main__":
    unittest.main()
