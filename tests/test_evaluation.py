import json
import tempfile
import unittest
from pathlib import Path

from knowledge_workbench.errors import KnowledgeWorkbenchError, UnsupportedFormatError
from knowledge_workbench.config import WorkspacePaths
from knowledge_workbench.database import Database
from knowledge_workbench.evaluation import (
    _assess_duplicate_evidence,
    build_labeling_candidate_pack,
    evaluate_dataset,
)
from knowledge_workbench.ingest import ingest_file
from knowledge_workbench.models import Classification
from knowledge_workbench.parsers.legacy_word import ConversionMetadata


class EvaluationTests(unittest.TestCase):
    def test_formal_evaluation_rejects_unfinished_labeling_template(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "source.md").write_text("已有证据。", encoding="utf-8")
            dataset = root / "dataset.json"
            dataset.write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "name": "unfinished",
                        "cases": [
                            {
                                "case_id": "case-1",
                                "source_path": "source.md",
                                "classification": "internal",
                                "expected_evidence": [
                                    {
                                        "text": "【待人工填写：必要证据】",
                                        "required": True,
                                    }
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

            with self.assertRaisesRegex(
                KnowledgeWorkbenchError, "仍包含待人工填写"
            ):
                evaluate_dataset(dataset)

    def test_labeling_pack_samples_only_current_version_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.md"
            paths = WorkspacePaths(root / "workspace")
            source.write_text("旧事实一。\n\n旧事实二。", encoding="utf-8")
            ingest_file(source, paths, Classification.INTERNAL)
            source.write_text(
                "新事实一。\n\n新事实二。\n\n新事实三。\n\n新事实四。\n\n新事实五。",
                encoding="utf-8",
            )
            current = ingest_file(source, paths, Classification.INTERNAL)
            dataset = root / "dataset.json"
            dataset.write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "name": "labeling",
                        "cases": [
                            {
                                "case_id": "case-1",
                                "source_path": "source.md",
                                "classification": "internal",
                                "expected_evidence": [
                                    {"text": "待人工填写", "required": True}
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

            pack = build_labeling_candidate_pack(
                Database(paths.database), dataset, candidates_per_case=3
            )

            case = pack["cases"][0]
            self.assertEqual(case["document_version_id"], current.version_id)
            self.assertEqual(case["total_current_evidence"], 5)
            self.assertEqual(
                [item["run_ordinal"] for item in case["candidate_evidence"]],
                [1, 3, 5],
            )
            self.assertTrue(
                all("旧事实" not in item["excerpt"] for item in case["candidate_evidence"])
            )

    def test_legacy_doc_evaluation_requires_explicit_permission(self):
        from docx import Document

        class FakeWordConverter:
            def convert(self, source: Path, output: Path) -> ConversionMetadata:
                document = Document()
                document.add_paragraph("必须保留的旧版证据。")
                document.save(output)
                return ConversionMetadata("fake-word", "1.0")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "source.doc").write_bytes(b"legacy word placeholder")
            dataset = root / "dataset.json"
            dataset.write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "name": "legacy",
                        "cases": [
                            {
                                "case_id": "legacy-1",
                                "source_path": "source.doc",
                                "classification": "internal",
                                "expected_evidence": [
                                    {"text": "必须保留的旧版证据", "required": True}
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

            with self.assertRaises(UnsupportedFormatError):
                evaluate_dataset(dataset)

            report = evaluate_dataset(
                dataset,
                allow_legacy_word_conversion=True,
                legacy_doc_converter=FakeWordConverter(),
            )
            self.assertEqual(report["aggregate"]["pass_rate"], 1.0)
            self.assertEqual(
                report["aggregate"]["signal_conflict_conclusion_count"], 0
            )

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
            self.assertEqual(report["aggregate"]["duplicate_excess_count"], 0)
            self.assertEqual(
                report["aggregate"]["exact_location_duplicate_count"], 0
            )
            self.assertEqual(
                report["aggregate"]["repeated_across_locations_count"], 0
            )
            self.assertEqual(
                report["aggregate"]["cases_exceeding_duplicate_rate"], 0
            )
            self.assertEqual(report["cases"][0]["duplicate_rate"], 0.0)
            self.assertEqual(report["cases"][0]["failure_reasons"], ())

    def test_duplicate_diagnostics_separate_locator_collisions_from_source_repetition(self):
        diagnostics = _assess_duplicate_evidence(
            [
                {"excerpt": "同一事实", "locator": {"paragraph": 1}},
                {"excerpt": " 同一事实 ", "locator": {"paragraph": 1}},
                {"excerpt": "同一事实", "locator": {"paragraph": 2}},
                {"excerpt": "另一事实", "locator": {"paragraph": 3}},
            ]
        )

        self.assertEqual(diagnostics["duplicate_rate"], 0.5)
        self.assertEqual(diagnostics["duplicate_excess_count"], 2)
        self.assertEqual(diagnostics["duplicate_group_count"], 1)
        self.assertEqual(diagnostics["largest_duplicate_group"], 3)
        self.assertEqual(diagnostics["exact_location_duplicate_count"], 1)
        self.assertEqual(diagnostics["repeated_across_locations_count"], 1)
        group = diagnostics["duplicate_groups"][0]
        self.assertNotIn("同一事实", json.dumps(group, ensure_ascii=False))
        self.assertEqual(group["unique_locator_count"], 2)
        self.assertEqual(
            group["position_samples"],
            ({"paragraph": 1}, {"paragraph": 1}, {"paragraph": 2}),
        )

    def test_duplicate_diagnostics_report_merged_source_locations(self):
        diagnostics = _assess_duplicate_evidence(
            [
                {
                    "excerpt": "同一事实",
                    "locator": {"paragraph": 1},
                    "locators": [
                        {"paragraph": 1},
                        {"paragraph": 2},
                        {"paragraph": 3},
                    ],
                }
            ]
        )

        self.assertEqual(diagnostics["duplicate_rate"], 0.0)
        self.assertEqual(diagnostics["multi_location_evidence_count"], 1)
        self.assertEqual(diagnostics["additional_location_count"], 2)

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
                report["cases"][0]["failure_reasons"],
                ("required_evidence_missing",),
            )
            self.assertEqual(
                report["cases"][0]["missing_required_evidence"],
                ("不存在的必要事实",),
            )


if __name__ == "__main__":
    unittest.main()
