import json
import tempfile
import unittest
from pathlib import Path

from knowledge_workbench.config import WorkspacePaths
from knowledge_workbench.database import Database
from knowledge_workbench.ingest import ingest_file
from knowledge_workbench.linting import lint_workspace
from knowledge_workbench.models import Classification


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


if __name__ == "__main__":
    unittest.main()
