import json
import tempfile
import unittest
from pathlib import Path

from knowledge_workbench.citation_evaluation import evaluate_citation_dataset
from knowledge_workbench.errors import KnowledgeWorkbenchError


class CitationEvaluationTests(unittest.TestCase):
    def test_report_calculates_precision_and_recall_without_copying_text(self):
        with tempfile.TemporaryDirectory() as temporary:
            dataset = Path(temporary) / "citations.json"
            dataset.write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "name": "citation-test",
                        "cases": [
                            {
                                "case_id": "positive",
                                "conclusion": "正式知识需要绑定原始证据。",
                                "cited_excerpts": [
                                    "所有正式知识必须绑定可以回到原始文件的证据。"
                                ],
                                "expected_supported": True,
                            },
                            {
                                "case_id": "negative",
                                "conclusion": "系统每天执行异地备份。",
                                "cited_excerpts": ["正式知识必须绑定原始证据。"],
                                "expected_supported": False,
                            },
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            report = evaluate_citation_dataset(dataset)

            self.assertEqual(report["aggregate"]["pass_rate"], 1.0)
            self.assertEqual(report["aggregate"]["precision"], 1.0)
            self.assertEqual(report["aggregate"]["recall"], 1.0)
            self.assertEqual(report["aggregate"]["signal_conflict_cases"], 0)
            self.assertEqual(
                report["algorithm"]["hard_rejection_threshold"], 0.15
            )
            self.assertEqual(
                report["algorithm"]["supported_prediction_threshold"], 0.25
            )
            serialized = json.dumps(report, ensure_ascii=False)
            self.assertNotIn("所有正式知识必须绑定", serialized)
            self.assertNotIn("每天执行异地备份", serialized)

    def test_duplicate_case_id_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            dataset = Path(temporary) / "invalid.json"
            case = {
                "case_id": "duplicate",
                "conclusion": "结论文本",
                "cited_excerpts": ["证据文本"],
                "expected_supported": True,
            }
            dataset.write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "name": "invalid",
                        "cases": [case, case],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(KnowledgeWorkbenchError, "重复 case_id"):
                evaluate_citation_dataset(dataset)


if __name__ == "__main__":
    unittest.main()
