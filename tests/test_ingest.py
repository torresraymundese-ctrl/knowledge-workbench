import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from knowledge_workbench.config import WorkspacePaths
from knowledge_workbench.ingest import _cleanup_generated_files, ingest_file
from knowledge_workbench.models import (
    Classification,
    EvidenceStatus,
    ParsedUnit,
    ParseResult,
)
from knowledge_workbench.review import transition_evidence
from knowledge_workbench.search import search_evidence


class IngestTests(unittest.TestCase):
    @staticmethod
    def _admit_document(database, document_id: str) -> None:
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

    def test_duplicate_excerpt_uses_one_evidence_id_with_multiple_locations(self):
        from knowledge_workbench.database import Database
        from knowledge_workbench.web_service import WorkbenchReadService

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "duplicate.md"
            source.write_text("重复规则。\n\n重复规则。", encoding="utf-8")
            paths = WorkspacePaths(root / "workspace")

            result = ingest_file(source, paths, Classification.INTERNAL)
            database = Database(paths.database)
            with database.connect() as connection:
                evidence = connection.execute(
                    "SELECT id, locator_json FROM evidence WHERE processing_run_id = ?",
                    (result.processing_run_id,),
                ).fetchall()
                locations = connection.execute(
                    """
                    SELECT location_ordinal, locator_json
                    FROM evidence_locations WHERE evidence_id = ?
                    ORDER BY location_ordinal
                    """,
                    (evidence[0]["id"],),
                ).fetchall()
                citation_count = connection.execute(
                    "SELECT COUNT(*) FROM revision_evidence WHERE revision_id = ?",
                    (result.revision_id,),
                ).fetchone()[0]

            self.assertEqual(result.evidence_count, 1)
            self.assertEqual(len(evidence), 1)
            self.assertEqual(len(locations), 2)
            parsed_locations = [json.loads(row["locator_json"]) for row in locations]
            self.assertEqual([item["line_start"] for item in parsed_locations], [1, 3])
            self.assertEqual(json.loads(evidence[0]["locator_json"]), parsed_locations[0])
            self.assertEqual(citation_count, 1)

            analysis = json.loads(
                (paths.analysis / f"{result.processing_run_id}.analysis.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(analysis["evidence"][0]["locators"], parsed_locations)
            mirror = json.loads(
                (paths.evidence / f"{result.processing_run_id}.jsonl").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(mirror["locators"], parsed_locations)
            draft = next(paths.wiki_drafts.glob("*.md")).read_text(encoding="utf-8")
            self.assertIn("定位（2 处）", draft)

            detail = WorkbenchReadService(database, paths).evidence_detail(
                evidence[0]["id"]
            )
            self.assertEqual(detail["location_count"], 2)
            self.assertEqual(
                [item["line_start"] for item in detail["locators"]], [1, 3]
            )
            self.assertTrue(all("heading_path" not in item for item in detail["locators"]))

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
            self._admit_document(database, first.document_id)
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

    def test_new_version_marks_published_aggregate_page_that_uses_old_evidence(self):
        from knowledge_workbench.database import Database

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "policy.md"
            paths = WorkspacePaths(root / "workspace")
            source.write_text("当前课程每班不超过30人。", encoding="utf-8")
            first = ingest_file(source, paths, Classification.INTERNAL)
            database = Database(paths.database)
            with database.transaction() as connection:
                evidence_id = connection.execute(
                    "SELECT id FROM evidence WHERE document_version_id = ?",
                    (first.version_id,),
                ).fetchone()[0]
                connection.execute(
                    """
                    INSERT INTO wiki_pages(
                        id, source_document_id, slug, title, status,
                        current_verified_revision_id, needs_revalidation,
                        created_at, updated_at
                    ) VALUES (
                        'page_topic_capacity', NULL, 'topic-capacity', '班级容量',
                        'verified', NULL, 0,
                        '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z'
                    )
                    """
                )
                connection.execute(
                    """
                    INSERT INTO wiki_revisions(
                        id, page_id, revision_number, status, markdown_path,
                        content_sha256, generator, created_at, updated_at,
                        processing_run_id
                    ) VALUES (
                        'rev_topic_capacity', 'page_topic_capacity', 1, 'verified',
                        'wiki/published/topic-capacity.md',
                        'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
                        'topic-extractive-v1',
                        '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z', NULL
                    )
                    """
                )
                connection.execute(
                    """
                    INSERT INTO revision_evidence(revision_id, evidence_id)
                    VALUES ('rev_topic_capacity', ?)
                    """,
                    (evidence_id,),
                )
                connection.execute(
                    """
                    UPDATE wiki_pages
                    SET current_verified_revision_id = 'rev_topic_capacity'
                    WHERE id = 'page_topic_capacity'
                    """
                )

            source.write_text("更新后的课程每班不超过25人。", encoding="utf-8")
            ingest_file(source, paths, Classification.INTERNAL)

            with database.connect() as connection:
                source_page = connection.execute(
                    "SELECT needs_revalidation FROM wiki_pages WHERE id = ?",
                    (first.page_id,),
                ).fetchone()[0]
                aggregate_page = connection.execute(
                    """
                    SELECT needs_revalidation
                    FROM wiki_pages WHERE id = 'page_topic_capacity'
                    """
                ).fetchone()[0]
                aggregate_audit = connection.execute(
                    """
                    SELECT COUNT(*) FROM audit_log
                    WHERE event_type = 'wiki_revalidation_required'
                      AND entity_id = 'page_topic_capacity'
                    """
                ).fetchone()[0]
            self.assertEqual(source_page, 0)
            self.assertEqual(aggregate_page, 1)
            self.assertEqual(aggregate_audit, 1)

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

    def test_cleanup_continues_after_one_generated_file_cannot_be_removed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            blocked = root / "blocked.txt"
            removable = root / "removable.txt"
            blocked.write_text("blocked", encoding="utf-8")
            removable.write_text("removable", encoding="utf-8")
            original_unlink = Path.unlink

            def selective_unlink(path: Path, *, missing_ok: bool = False):
                if path == blocked:
                    raise PermissionError("simulated lock")
                return original_unlink(path, missing_ok=missing_ok)

            with patch.object(Path, "unlink", selective_unlink):
                failures = _cleanup_generated_files((blocked, removable, None))

            self.assertTrue(blocked.exists())
            self.assertFalse(removable.exists())
            self.assertEqual(len(failures), 1)
            self.assertEqual(failures[0][0], blocked)
            self.assertIsInstance(failures[0][1], PermissionError)

    def test_ingest_failure_rolls_back_database_and_generated_files(self):
        from knowledge_workbench.database import Database

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.md"
            source.write_text("必须保留可追溯的原始证据。", encoding="utf-8")
            paths = WorkspacePaths(root / "workspace")

            with patch(
                "knowledge_workbench.ingest.record_event",
                side_effect=RuntimeError("simulated audit failure"),
            ):
                with self.assertRaisesRegex(RuntimeError, "simulated audit failure"):
                    ingest_file(source, paths, Classification.INTERNAL)

            database = Database(paths.database)
            with database.connect() as connection:
                for table in (
                    "documents",
                    "document_versions",
                    "processing_runs",
                    "evidence",
                    "wiki_revisions",
                ):
                    count = connection.execute(
                        f"SELECT COUNT(*) FROM {table}"
                    ).fetchone()[0]
                    self.assertEqual(count, 0, table)

            for directory in (
                paths.raw,
                paths.wiki_drafts,
                paths.evidence,
                paths.analysis,
            ):
                self.assertFalse(any(path.is_file() for path in directory.rglob("*")))

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
            self._admit_document(database, result.document_id)
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
