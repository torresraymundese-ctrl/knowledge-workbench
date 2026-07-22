import json
import tempfile
import unittest
from pathlib import Path

from knowledge_workbench.config import WorkspacePaths
from knowledge_workbench.database import Database
from knowledge_workbench.errors import InvalidTransitionError, KnowledgeWorkbenchError
from knowledge_workbench.ingest import ingest_file
from knowledge_workbench.models import Classification, EvidenceStatus
from knowledge_workbench.review import (
    publish_revision,
    reject_revision,
    request_revision_review,
    transition_evidence,
)


class WikiRevisionReviewTests(unittest.TestCase):
    def test_rejection_requires_note_and_preserves_previous_verified_revision(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = WorkspacePaths(root / "workspace")
            source = root / "policy.md"
            source.write_text("系统允许提交申请。", encoding="utf-8")
            first = ingest_file(source, paths, Classification.INTERNAL)
            database = Database(paths.database)
            with database.connect() as connection:
                evidence_id = connection.execute(
                    "SELECT id FROM evidence WHERE processing_run_id = ?",
                    (first.processing_run_id,),
                ).fetchone()[0]
            transition_evidence(
                database, evidence_id, EvidenceStatus.REVIEWING, actor="reviewer-01"
            )
            transition_evidence(
                database, evidence_id, EvidenceStatus.VERIFIED, actor="reviewer-01"
            )
            request_revision_review(database, first.revision_id, actor="author-01")
            publish_revision(database, paths, first.revision_id, actor="reviewer-01")

            source.write_text("系统禁止提交申请。", encoding="utf-8")
            second = ingest_file(source, paths, Classification.INTERNAL)
            request_revision_review(database, second.revision_id, actor="author-01")

            with self.assertRaisesRegex(KnowledgeWorkbenchError, "必须填写复核意见"):
                reject_revision(
                    database, second.revision_id, actor="reviewer-02", note="  "
                )
            with database.connect() as connection:
                status = connection.execute(
                    "SELECT status FROM wiki_revisions WHERE id = ?",
                    (second.revision_id,),
                ).fetchone()[0]
            self.assertEqual(status, "reviewing")

            reject_revision(
                database,
                second.revision_id,
                actor="reviewer-02",
                note="适用范围尚未核对。",
            )

            with database.connect() as connection:
                revision = connection.execute(
                    "SELECT status FROM wiki_revisions WHERE id = ?",
                    (second.revision_id,),
                ).fetchone()[0]
                page = connection.execute(
                    """
                    SELECT status, current_verified_revision_id
                    FROM wiki_pages WHERE id = ?
                    """,
                    (first.page_id,),
                ).fetchone()
                audit = connection.execute(
                    """
                    SELECT actor, details_json FROM audit_log
                    WHERE event_type = 'wiki_revision_rejected'
                      AND entity_id = ?
                    ORDER BY id DESC LIMIT 1
                    """,
                    (second.revision_id,),
                ).fetchone()
            self.assertEqual(revision, "rejected")
            self.assertEqual(page["status"], "verified")
            self.assertEqual(page["current_verified_revision_id"], first.revision_id)
            self.assertEqual(audit["actor"], "reviewer-02")
            self.assertEqual(
                json.loads(audit["details_json"])["note"], "适用范围尚未核对。"
            )
            with self.assertRaises(InvalidTransitionError):
                reject_revision(
                    database,
                    second.revision_id,
                    actor="reviewer-02",
                    note="再次驳回。",
                )


if __name__ == "__main__":
    unittest.main()
