import json
import tempfile
import unittest
from pathlib import Path

from knowledge_workbench.evaluation import evaluate_dataset


class EvaluationTests(unittest.TestCase):
    def test_quality_report_measures_coverage_traceability_and_duplicates(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.md"
            source.write_text(
                "# 规则\n\n所有知识必须引用证据。\n\n机密资料禁止上传。\n",
                encoding="utf-8",
            )
            dataset = root / "dataset.json"
            dataset.write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "name": "test",
                        "cases": [
                            {
                                "case_id": "case-1",
                                "source_path": "source.md",
                                "classification": "confidential",
                                "expected_evidence": [
                                    {"text": "必须引用证据", "required": True},
                                    {"text": "禁止上传", "required": True},
                                ],
                                "forbidden_substrings": ["凭空结论"],
                                "max_duplicate_rate": 0,
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            report = evaluate_dataset(dataset)
            self.assertEqual(report["aggregate"]["pass_rate"], 1.0)
            self.assertTrue(report["aggregate"]["all_evidence_traceable"])
            self.assertEqual(report["cases"][0]["duplicate_rate"], 0.0)

    def test_missing_required_evidence_fails_case_without_hiding_details(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "source.txt").write_text("已有事实。", encoding="utf-8")
            dataset = root / "dataset.json"
            dataset.write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "name": "missing",
                        "cases": [
                            {
                                "case_id": "case-missing",
                                "source_path": "source.txt",
                                "classification": "internal",
                                "expected_evidence": [
                                    {"text": "不存在的必要事实", "required": True}
                                ],
                                "forbidden_substrings": [],
                                "max_duplicate_rate": 0,
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            report = evaluate_dataset(dataset)
            self.assertEqual(report["aggregate"]["pass_rate"], 0.0)
            self.assertEqual(
                report["cases"][0]["missing_required_evidence"],
                ("不存在的必要事实",),
            )


if __name__ == "__main__":
    unittest.main()

