import json
import tempfile
import unittest
from pathlib import Path

from knowledge_workbench.benchmarking import benchmark_fts_search
from knowledge_workbench.config import WorkspacePaths
from knowledge_workbench.database import Database
from knowledge_workbench.errors import KnowledgeWorkbenchError
from knowledge_workbench.ingest import ingest_file
from knowledge_workbench.models import Classification


class FtsBenchmarkTests(unittest.TestCase):
    def test_report_measures_real_current_corpus_without_copying_queries(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.md"
            source.write_text(
                "contract approval required\n\n项目合同需要验收。",
                encoding="utf-8",
            )
            paths = WorkspacePaths(root / "workspace")
            ingest_file(source, paths, Classification.INTERNAL)

            report = benchmark_fts_search(
                Database(paths.database),
                ["contract", "合同"],
                iterations=2,
                limit=10,
            )

            self.assertEqual(report["corpus"]["document_count"], 1)
            self.assertEqual(report["corpus"]["current_evidence_count"], 2)
            self.assertEqual(report["configuration"]["query_count"], 2)
            self.assertEqual(report["aggregate"]["measurement_count"], 4)
            self.assertEqual(report["aggregate"]["fts5_case_count"], 1)
            self.assertEqual(
                report["aggregate"]["substring_fallback_case_count"], 1
            )
            serialized = json.dumps(report, ensure_ascii=False)
            self.assertNotIn("contract", serialized)
            self.assertNotIn("合同", serialized)
            self.assertTrue(all(case["result_count"] == 1 for case in report["cases"]))

    def test_invalid_configuration_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            database = Database(Path(temporary) / "knowledge.sqlite3")
            database.initialize("now")

            with self.assertRaisesRegex(KnowledgeWorkbenchError, "至少需要一个"):
                benchmark_fts_search(database, ["  "])
            with self.assertRaisesRegex(KnowledgeWorkbenchError, "iterations"):
                benchmark_fts_search(database, ["query"], iterations=0)


if __name__ == "__main__":
    unittest.main()
