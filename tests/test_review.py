import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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
    @staticmethod
    def _admit_document(database: Database, document_id: str) -> None:
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
                (document_id,),
            )

    @staticmethod
    def _verify_run_evidence(
        database: Database,
        processing_run_id: str,
    ) -> None:
        with database.connect() as connection:
            evidence_ids = [
                row[0]
                for row in connection.execute(
                    "SELECT id FROM evidence WHERE processing_run_id = ?",
                    (processing_run_id,),
                ).fetchall()
            ]
        for evidence_id in evidence_ids:
            transition_evidence(
                database,
                evidence_id,
                EvidenceStatus.REVIEWING,
                actor="technical-validator",
            )
            transition_evidence(
                database,
                evidence_id,
                EvidenceStatus.VERIFIED,
                actor="technical-validator",
            )

    def test_rejection_requires_note_and_preserves_previous_verified_revision(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = WorkspacePaths(root / "workspace")
            source = root / "policy.md"
            source.write_text("系统允许提交申请。", encoding="utf-8")
            first = ingest_file(source, paths, Classification.INTERNAL)
            database = Database(paths.database)
            self._admit_document(database, first.document_id)
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
            self._verify_run_evidence(database, second.processing_run_id)
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

    def test_publish_rechecks_content_hash_inside_core_state_machine(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = WorkspacePaths(root / "workspace")
            source = root / "policy.md"
            source.write_text("发布必须保护内容完整性。", encoding="utf-8")
            result = ingest_file(source, paths, Classification.INTERNAL)
            database = Database(paths.database)
            self._admit_document(database, result.document_id)
            with database.connect() as connection:
                evidence_id = connection.execute(
                    "SELECT id FROM evidence WHERE processing_run_id = ?",
                    (result.processing_run_id,),
                ).fetchone()[0]
                markdown_path = connection.execute(
                    "SELECT markdown_path FROM wiki_revisions WHERE id = ?",
                    (result.revision_id,),
                ).fetchone()[0]
            transition_evidence(
                database, evidence_id, EvidenceStatus.REVIEWING, actor="reviewer-01"
            )
            transition_evidence(
                database, evidence_id, EvidenceStatus.VERIFIED, actor="reviewer-01"
            )
            request_revision_review(database, result.revision_id, actor="author-01")
            revision_path = paths.root / markdown_path
            revision_path.write_text(
                revision_path.read_text(encoding="utf-8") + "\n外部编辑。\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(KnowledgeWorkbenchError, "外部修改"):
                publish_revision(
                    database, paths, result.revision_id, actor="publisher-01"
                )
            with database.connect() as connection:
                status = connection.execute(
                    "SELECT status FROM wiki_revisions WHERE id = ?",
                    (result.revision_id,),
                ).fetchone()[0]
            self.assertEqual(status, "reviewing")

    def test_publish_requires_at_least_one_cited_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = WorkspacePaths(root / "workspace")
            source = root / "policy.md"
            source.write_text("正式知识必须存在证据引用。", encoding="utf-8")
            result = ingest_file(source, paths, Classification.INTERNAL)
            database = Database(paths.database)
            self._admit_document(database, result.document_id)
            self._verify_run_evidence(database, result.processing_run_id)
            request_revision_review(database, result.revision_id, actor="author-01")
            with database.transaction() as connection:
                connection.execute(
                    "DELETE FROM revision_evidence WHERE revision_id = ?",
                    (result.revision_id,),
                )

            with self.assertRaisesRegex(InvalidTransitionError, "至少引用一条"):
                publish_revision(
                    database, paths, result.revision_id, actor="publisher-01"
                )

    def test_publish_rolls_back_verified_file_when_audit_write_fails(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = WorkspacePaths(root / "workspace")
            source = root / "policy.md"
            source.write_text("发布事务失败时不能留下正式文件。", encoding="utf-8")
            result = ingest_file(source, paths, Classification.INTERNAL)
            database = Database(paths.database)
            self._admit_document(database, result.document_id)
            self._verify_run_evidence(database, result.processing_run_id)
            request_revision_review(
                database,
                result.revision_id,
                actor="author-01",
                paths=paths,
            )
            with database.connect() as connection:
                relative_draft = connection.execute(
                    "SELECT markdown_path FROM wiki_revisions WHERE id = ?",
                    (result.revision_id,),
                ).fetchone()[0]
            draft_path = paths.root / relative_draft
            verified_path = paths.wiki_verified / draft_path.name

            with patch(
                "knowledge_workbench.review.record_event",
                side_effect=RuntimeError("audit unavailable"),
            ):
                with self.assertRaisesRegex(RuntimeError, "audit unavailable"):
                    publish_revision(
                        database,
                        paths,
                        result.revision_id,
                        actor="publisher-01",
                    )

            self.assertTrue(draft_path.is_file())
            self.assertFalse(verified_path.exists())
            with database.connect() as connection:
                status = connection.execute(
                    "SELECT status FROM wiki_revisions WHERE id = ?",
                    (result.revision_id,),
                ).fetchone()[0]
            self.assertEqual(status, "reviewing")

    def test_review_actions_reject_empty_actor(self):
        with tempfile.TemporaryDirectory() as temporary:
            paths = WorkspacePaths(Path(temporary) / "workspace")
            source = Path(temporary) / "policy.md"
            source.write_text("审核动作必须记录真实操作人。", encoding="utf-8")
            result = ingest_file(source, paths, Classification.INTERNAL)
            database = Database(paths.database)

            with self.assertRaisesRegex(KnowledgeWorkbenchError, "actor 不能为空"):
                request_revision_review(
                    database,
                    result.revision_id,
                    actor="  ",
                    paths=paths,
                )
            with self.assertRaisesRegex(KnowledgeWorkbenchError, "actor 不能为空"):
                reject_revision(
                    database,
                    result.revision_id,
                    actor="",
                    note="测试",
                )
            with self.assertRaisesRegex(KnowledgeWorkbenchError, "actor 不能为空"):
                publish_revision(
                    database,
                    paths,
                    result.revision_id,
                    actor="\n",
                )


if __name__ == "__main__":
    unittest.main()
