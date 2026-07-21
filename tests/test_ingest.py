import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from knowledge_workbench.config import WorkspacePaths
from knowledge_workbench.ingest import ingest_file
from knowledge_workbench.models import (
    Classification,
    EvidenceStatus,
    ParsedUnit,
    ParseResult,
)
from knowledge_workbench.review import transition_evidence
from knowledge_workbench.search import search_evidence


class IngestTests(unittest.TestCase):
    def test_new_version_marks_verified_page_for_revalidation_without_unpublishing_it(self):
        from knowledge_workbench.database import Database
        from knowledge_workbench.review import publish_revision, request_revision_review

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "policy.md"
            paths = WorkspacePaths(root / "workspace")
            source.write_text("当前生效规则。", encoding="utf-8")
            first = ingest_file(source, paths, Classification.INTERNAL)
            database = Database(paths.database)
            with database.connect() as connection:
                evidence_id = connection.execute(
                    "SELECT id FROM evidence WHERE document_version_id = ?",
                    (first.version_id,),
                ).fetchone()[0]
            transition_evidence(
                database, evidence_id, EvidenceStatus.REVIEWING, actor="reviewer"
            )
            transition_evidence(
                database, evidence_id, EvidenceStatus.VERIFIED, actor="reviewer"
            )
            request_revision_review(database, first.revision_id, actor="reviewer")
            publish_revision(database, paths, first.revision_id, actor="reviewer")

            source.write_text("更新后的规则。", encoding="utf-8")
            second = ingest_file(source, paths, Classification.INTERNAL)

            with database.connect() as connection:
                page = connection.execute(
                    """
                    SELECT status, current_verified_revision_id, needs_revalidation
                    FROM wiki_pages WHERE id = ?
                    """,
                    (first.page_id,),
                ).fetchone()
                new_revision_status = connection.execute(
                    "SELECT status FROM wiki_revisions WHERE id = ?",
                    (second.revision_id,),
                ).fetchone()[0]
                audit_count = connection.execute(
                    """
                    SELECT COUNT(*) FROM audit_log
                    WHERE event_type = 'wiki_revalidation_required'
                      AND entity_id = ?
                    """,
                    (first.page_id,),
                ).fetchone()[0]
            self.assertEqual(page["status"], "verified")
            self.assertEqual(page["current_verified_revision_id"], first.revision_id)
            self.assertEqual(page["needs_revalidation"], 1)
            self.assertEqual(new_revision_status, "draft")
            self.assertEqual(audit_count, 1)

    def test_new_document_version_hides_historical_evidence_from_search_and_review(self):
        from knowledge_workbench.database import Database
        from knowledge_workbench.errors import InvalidTransitionError
        from knowledge_workbench.review import request_revision_review

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.md"
            paths = WorkspacePaths(root / "workspace")
            source.write_text("旧版本专属事实。", encoding="utf-8")
            first = ingest_file(source, paths, Classification.INTERNAL)
            database = Database(paths.database)
            with database.connect() as connection:
                old_evidence_id = connection.execute(
                    "SELECT id FROM evidence WHERE document_version_id = ?",
                    (first.version_id,),
                ).fetchone()[0]

            source.write_text("新版本替代事实。", encoding="utf-8")
            second = ingest_file(source, paths, Classification.INTERNAL)

            self.assertNotEqual(first.version_id, second.version_id)
            self.assertEqual(search_evidence(database, "旧版本专属事实"), [])
            self.assertEqual(len(search_evidence(database, "新版本替代事实")), 1)
            with self.assertRaisesRegex(InvalidTransitionError, "历史文件版本"):
                transition_evidence(
                    database,
                    old_evidence_id,
                    EvidenceStatus.REVIEWING,
                    actor="reviewer",
                )
            with self.assertRaisesRegex(InvalidTransitionError, "历史文件版本"):
                request_revision_review(database, first.revision_id, actor="reviewer")

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

    def test_reprocess_versions_derived_results_without_copying_source_version(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.md"
            source.write_text(
                "# 规则\n\n正式知识必须引用证据。\n\n机密资料不得发送到云端。\n",
                encoding="utf-8",
            )
            paths = WorkspacePaths(root / "workspace")
            first = ingest_file(source, paths, Classification.INTERNAL)
            upgraded_parse = ParseResult(
                "test-parser",
                "2",
                (
                    ParsedUnit("正式知识必须引用证据。", {"line": 3}),
                    ParsedUnit("机密资料不得发送到云端。", {"line": 5}),
                ),
            )

            with patch(
                "knowledge_workbench.ingest.parse_document",
                return_value=upgraded_parse,
            ):
                second = ingest_file(
                    source,
                    paths,
                    Classification.INTERNAL,
                    reprocess=True,
                    actor="tester",
                )
                repeated = ingest_file(
                    source,
                    paths,
                    Classification.INTERNAL,
                    reprocess=True,
                    actor="tester",
                )

            self.assertTrue(second.reprocessed)
            self.assertFalse(second.duplicate)
            self.assertEqual(first.version_id, second.version_id)
            self.assertNotEqual(first.processing_run_id, second.processing_run_id)
            self.assertTrue(repeated.duplicate)
            self.assertEqual(second.processing_run_id, repeated.processing_run_id)
            self.assertEqual(len(list(paths.raw.rglob("*.md"))), 1)

            from knowledge_workbench.database import Database
            from knowledge_workbench.search import search_evidence

            database = Database(paths.database)
            with database.connect() as connection:
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM document_versions").fetchone()[0],
                    1,
                )
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM processing_runs").fetchone()[0],
                    2,
                )
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM processing_runs WHERE is_current = 1"
                    ).fetchone()[0],
                    1,
                )
                old_run = connection.execute(
                    "SELECT status, is_current FROM processing_runs WHERE id = ?",
                    (first.processing_run_id,),
                ).fetchone()
                self.assertEqual((old_run["status"], old_run["is_current"]), ("superseded", 0))
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM evidence").fetchone()[0],
                    4,
                )
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM wiki_revisions").fetchone()[0],
                    2,
                )
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM audit_log WHERE event_type = 'document_reprocessed'"
                    ).fetchone()[0],
                    1,
                )

            rows = search_evidence(database, "正式知识", limit=10)
            self.assertEqual(len(rows), 1)
            with database.connect() as connection:
                active_run = connection.execute(
                    "SELECT processing_run_id FROM evidence WHERE id = ?", (rows[0]["id"],)
                ).fetchone()[0]
            self.assertEqual(active_run, second.processing_run_id)

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
