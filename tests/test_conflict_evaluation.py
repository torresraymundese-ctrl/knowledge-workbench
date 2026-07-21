import json
import tempfile
import unittest
from pathlib import Path

from knowledge_workbench.conflict_evaluation import evaluate_conflict_dataset
from knowledge_workbench.errors import KnowledgeWorkbenchError


class ConflictEvaluationTests(unittest.TestCase):
    def test_report_calculates_precision_recall_and_type_accuracy(self):
        with tempfile.TemporaryDirectory() as temporary:
            dataset = Path(temporary) / "conflicts.json"
            dataset.write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "name": "conflict-test",
                        "cases": [
                            {
                                "case_id": "positive",
                                "older": "资料允许上传。",
                                "newer": "资料禁止上传。",
                                "expected_conflict": True,
                                "expected_type": "polarity_change",
                            },
                            {
                                "case_id": "negative",
                                "older": "方案V1.0于2024年1月1日发布。",
                                "newer": "方案V1.1于2024年2月1日发布。",
                                "expected_conflict": False,
                                "expected_type": None,
                            },
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            report = evaluate_conflict_dataset(dataset)

            self.assertEqual(report["aggregate"]["pass_rate"], 1.0)
            self.assertEqual(report["aggregate"]["precision"], 1.0)
            self.assertEqual(report["aggregate"]["recall"], 1.0)
            self.assertEqual(report["aggregate"]["type_accuracy"], 1.0)

    def test_expected_conflict_requires_expected_type(self):
        with tempfile.TemporaryDirectory() as temporary:
            dataset = Path(temporary) / "invalid.json"
            dataset.write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "name": "invalid",
                        "cases": [
                            {
                                "case_id": "invalid",
                                "older": "允许上传。",
                                "newer": "禁止上传。",
                                "expected_conflict": True,
                                "expected_type": None,
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(KnowledgeWorkbenchError, "必须指定"):
                evaluate_conflict_dataset(dataset)


if __name__ == "__main__":
    unittest.main()
