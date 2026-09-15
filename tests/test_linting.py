import json
import tempfile
import unittest
from pathlib import Path

from knowledge_workbench.config import WorkspacePaths
from knowledge_workbench.database import Database
from knowledge_workbench.ingest import ingest_file
from knowledge_workbench.linting import lint_workspace
from knowledge_workbench.models import Classification
from knowledge_workbench.review import publish_revision, request_revision_review
from knowledge_workbench.topic_wiki import build_topic_wiki_baseline
from knowledge_workbench.utils import sha256_text, utc_now


class WorkspaceLintTests(unittest.TestCase):
    def test_legacy_run_missing_new_schema_artifacts_is_a_visible_warning(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.md"
            source.write_text("历史证据。", encoding="utf-8")
            paths = WorkspacePaths(root / "workspace")
            result = ingest_file(source, paths, Classification.INTERNAL)
            database = Database(paths.database)
            legacy_run_id = f"run_legacy_{result.version_id}"
            with database.connect() as connection:
                connection.execute("PRAGMA foreign_keys = OFF")
                connection.execute(
                    "UPDATE processing_runs SET id = ? WHERE id = ?",
                    (legacy_run_id, result.processing_run_id),
                )
                connection.execute(
                    "UPDATE evidence SET processing_run_id = ? WHERE processing_run_id = ?",
                    (legacy_run_id, result.processing_run_id),
                )
                connection.execute(
                    "UPDATE wiki_revisions SET processing_run_id = ? WHERE processing_run_id = ?",
                    (legacy_run_id, result.processing_run_id),
                )
            (paths.analysis / f"{result.processing_run_id}.analysis.json").unlink()
            (paths.analysis / f"{result.revision_id}.wiki-generation.json").unlink()
            (paths.evidence / f"{result.processing_run_id}.jsonl").replace(
                paths.evidence / f"{result.version_id}.jsonl"
            )

            report = lint_workspace(database, paths)

            self.assertTrue(report["passed"])
            self.assertEqual(report["summary"]["error_count"], 0)
            self.assertEqual(report["summary"]["warning_count"], 2)

    def test_ingested_workspace_passes_full_artifact_lint(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.md"
            source.write_text(
                "# 规则\n\n正式知识必须引用原始证据。\n\n机密资料不得发送到云端。",
                encoding="utf-8",
            )
            paths = WorkspacePaths(root / "workspace")
            ingest_file(source, paths, Classification.INTERNAL)

            report = lint_workspace(Database(paths.database), paths)

            self.assertTrue(report["passed"])
            self.assertEqual(report["summary"]["document_count"], 1)
            self.assertEqual(report["summary"]["current_processing_run_count"], 1)
            self.assertEqual(report["summary"]["current_evidence_count"], 2)

    def test_jsonl_lint_preserves_unicode_line_separator_inside_excerpt(self):
        from docx import Document

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.docx"
            document = Document()
            document.add_paragraph("第一部分\u2028第二部分")
            document.save(source)
            paths = WorkspacePaths(root / "workspace")
            ingest_file(source, paths, Classification.INTERNAL)

            report = lint_workspace(Database(paths.database), paths)

            self.assertTrue(report["passed"])
            self.assertNotIn(
                "evidence_mirror_invalid",
                {issue["code"] for issue in report["issues"]},
            )

    def test_lint_reports_analysis_tampering_without_modifying_database(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.md"
            source.write_text("必须保留原始证据。", encoding="utf-8")
            paths = WorkspacePaths(root / "workspace")
            result = ingest_file(source, paths, Classification.INTERNAL)
            analysis_path = paths.analysis / f"{result.processing_run_id}.analysis.json"
            analysis = json.loads(analysis_path.read_text(encoding="utf-8"))
            analysis["evidence"][0]["excerpt"] = "被篡改的证据。"
            analysis_path.write_text(
                json.dumps(analysis, ensure_ascii=False), encoding="utf-8"
            )

            report = lint_workspace(Database(paths.database), paths)

            self.assertFalse(report["passed"])
            self.assertIn(
                "analysis_evidence_mismatch",
                {issue["code"] for issue in report["issues"]},
            )
            with Database(paths.database).connect() as connection:
                excerpt = connection.execute("SELECT excerpt FROM evidence").fetchone()[0]
            self.assertEqual(excerpt, "必须保留原始证据。")

    def test_lint_rejects_missing_evidence_locations(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.md"
            source.write_text("必须保留全部定位。", encoding="utf-8")
            paths = WorkspacePaths(root / "workspace")
            ingest_file(source, paths, Classification.INTERNAL)
            database = Database(paths.database)
            with database.transaction() as connection:
                connection.execute("DELETE FROM evidence_locations")

            report = lint_workspace(database, paths)

            self.assertFalse(report["passed"])
            codes = {issue["code"] for issue in report["issues"]}
            self.assertIn("evidence_location_missing", codes)

    def test_lint_validates_topic_wiki_sources_and_section_markers(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = root / "权限矩阵.md"
            second = root / "角色资料提交.md"
            first.write_text("学校管理员可以维护本校角色和权限。", encoding="utf-8")
            second.write_text("机构提交资料后进入平台准入审核。", encoding="utf-8")
            paths = WorkspacePaths(root / "workspace")
            ingest_file(first, paths, Classification.INTERNAL)
            ingest_file(second, paths, Classification.INTERNAL)
            database = Database(paths.database)
            with database.transaction() as connection:
                connection.execute(
                    """
                    UPDATE document_governance
                    SET purpose = 'production', scope_status = 'in_scope',
                        authority_status = 'reference',
                        knowledge_domain = 'business',
                        reviewed_by = 'owner-01', reviewed_at = ?,
                        decision_reason = 'test setup', updated_at = ?
                    """,
                    (utc_now(), utc_now()),
                )
                connection.execute("UPDATE evidence SET status = 'verified'")
                connection.execute(
                    """
                    UPDATE evidence_technical_validation
                    SET status = 'passed', validated_at = ?
                    """,
                    (utc_now(),),
                )
            built = build_topic_wiki_baseline(
                database,
                paths,
                actor="organizer-01",
            )
            topic = next(
                item
                for item in built["topics"]
                if item["topic_key"] == "organization-access"
            )

            report = lint_workspace(database, paths)

            self.assertTrue(report["passed"])
            self.assertEqual(report["summary"]["topic_wiki_revision_count"], 1)

            with database.transaction() as connection:
                evidence_id = connection.execute(
                    """
                    SELECT evidence_id
                    FROM revision_evidence
                    WHERE revision_id = ?
                    ORDER BY evidence_id
                    LIMIT 1
                    """,
                    (topic["revision_id"],),
                ).fetchone()[0]
                connection.execute(
                    """
                    DELETE FROM evidence_technical_validation
                    WHERE evidence_id = ?
                    """,
                    (evidence_id,),
                )
            missing_validation = lint_workspace(database, paths)
            self.assertFalse(missing_validation["passed"])
            self.assertIn(
                "topic_wiki_source_invalid",
                {issue["code"] for issue in missing_validation["issues"]},
            )
            now = utc_now()
            with database.transaction() as connection:
                connection.execute(
                    """
                    INSERT INTO evidence_technical_validation(
                        evidence_id, status, validator, checks_json,
                        validated_at, created_at, updated_at
                    ) VALUES (?, 'passed', 'test-repair', '{}', ?, ?, ?)
                    """,
                    (evidence_id, now, now, now),
                )

            markdown_path = paths.root / topic["markdown_path"]
            content = markdown_path.read_text(encoding="utf-8")
            content = content.replace(
                "<!-- topic-section-evidence:",
                "<!-- removed-topic-section-evidence:",
            )
            markdown_path.write_text(content, encoding="utf-8")
            with database.transaction() as connection:
                connection.execute(
                    """
                    UPDATE wiki_revisions
                    SET content_sha256 = ?
                    WHERE id = ?
                    """,
                    (sha256_text(content), topic["revision_id"]),
                )

            tampered = lint_workspace(database, paths)

            self.assertFalse(tampered["passed"])
            self.assertIn(
                "topic_wiki_evidence_marker_mismatch",
                {issue["code"] for issue in tampered["issues"]},
            )

    def test_lint_checks_current_verified_topic_when_newer_draft_exists(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = root / "权限矩阵.md"
            second = root / "角色资料提交.md"
            first.write_text("学校管理员可以维护本校角色和权限。", encoding="utf-8")
            second.write_text("机构提交资料后进入平台准入审核。", encoding="utf-8")
            paths = WorkspacePaths(root / "workspace")
            ingest_file(first, paths, Classification.INTERNAL)
            ingest_file(second, paths, Classification.INTERNAL)
            database = Database(paths.database)
            with database.transaction() as connection:
                connection.execute(
                    """
                    UPDATE document_governance
                    SET purpose = 'production', scope_status = 'in_scope',
                        authority_status = 'reference',
                        knowledge_domain = 'business',
                        reviewed_by = 'owner-01', reviewed_at = ?,
                        decision_reason = 'test setup', updated_at = ?
                    """,
                    (utc_now(), utc_now()),
                )
                connection.execute("UPDATE evidence SET status = 'verified'")
                connection.execute(
                    """
                    UPDATE evidence_technical_validation
                    SET status = 'passed', validated_at = ?
                    """,
                    (utc_now(),),
                )
            first_build = build_topic_wiki_baseline(
                database,
                paths,
                actor="organizer-01",
            )
            topic = next(
                item
                for item in first_build["topics"]
                if item["topic_key"] == "organization-access"
            )
            request_revision_review(
                database,
                topic["revision_id"],
                actor="author-01",
                paths=paths,
            )
            published_path = publish_revision(
                database,
                paths,
                topic["revision_id"],
                actor="reviewer-01",
            )
            with database.transaction() as connection:
                connection.execute(
                    """
                    UPDATE wiki_pages
                    SET needs_revalidation = 1
                    WHERE id = ?
                    """,
                    (topic["page_id"],),
                )
            refreshed = build_topic_wiki_baseline(
                database,
                paths,
                actor="organizer-02",
                refresh=True,
            )
            refreshed_topic = next(
                item
                for item in refreshed["topics"]
                if item["topic_key"] == "organization-access"
            )
            self.assertNotEqual(
                refreshed_topic["revision_id"],
                topic["revision_id"],
            )

            published_path.write_text(
                published_path.read_text(encoding="utf-8") + "\n外部篡改。\n",
                encoding="utf-8",
            )
            report = lint_workspace(database, paths)

            self.assertFalse(report["passed"])
            self.assertEqual(report["summary"]["topic_wiki_revision_count"], 2)
            self.assertTrue(
                any(
                    issue["code"] == "topic_wiki_markdown_sha256_mismatch"
                    and issue["entity_id"] == topic["revision_id"]
                    for issue in report["issues"]
                )
            )

    def test_lint_ignores_retired_topic_when_current_sources_are_insufficient(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = root / "权限矩阵.md"
            second = root / "开发数据" / "角色资料提交.md"
            second.parent.mkdir()
            first.write_text("学校管理员可以维护本校角色和权限。", encoding="utf-8")
            second.write_text("机构提交资料后进入平台准入审核。", encoding="utf-8")
            paths = WorkspacePaths(root / "workspace")
            ingest_file(first, paths, Classification.INTERNAL)
            ingest_file(second, paths, Classification.INTERNAL)
            database = Database(paths.database)
            with database.transaction() as connection:
                connection.execute(
                    """
                    UPDATE document_governance
                    SET purpose = 'production', scope_status = 'in_scope',
                        authority_status = 'reference',
                        knowledge_domain = 'business',
                        reviewed_by = 'owner-01', reviewed_at = ?,
                        decision_reason = 'test setup', updated_at = ?
                    """,
                    (utc_now(), utc_now()),
                )
                connection.execute("UPDATE evidence SET status = 'verified'")
                connection.execute(
                    """
                    UPDATE evidence_technical_validation
                    SET status = 'passed', validated_at = ?
                    """,
                    (utc_now(),),
                )
            built = build_topic_wiki_baseline(
                database,
                paths,
                actor="organizer-01",
            )
            topic = next(
                item
                for item in built["topics"]
                if item["topic_key"] == "organization-access"
            )
            with database.transaction() as connection:
                connection.execute(
                    """
                    UPDATE document_governance
                    SET purpose = 'development_fixture',
                        scope_status = 'out_of_scope'
                    WHERE document_id = (
                        SELECT id FROM documents
                        WHERE original_name = '角色资料提交.md'
                    )
                    """
                )
            build_topic_wiki_baseline(
                database,
                paths,
                actor="organizer-02",
                refresh=True,
            )

            report = lint_workspace(database, paths)

            self.assertTrue(report["passed"])
            self.assertEqual(report["summary"]["topic_wiki_revision_count"], 0)
            with database.connect() as connection:
                status = connection.execute(
                    "SELECT status FROM wiki_revisions WHERE id = ?",
                    (topic["revision_id"],),
                ).fetchone()[0]
            self.assertEqual(status, "superseded")


if __name__ == "__main__":
    unittest.main()
