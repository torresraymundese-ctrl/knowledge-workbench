import copy
import unittest

from knowledge_workbench.errors import KnowledgeWorkbenchError
from knowledge_workbench.models import Classification, ParsedUnit, ParseResult
from knowledge_workbench.pipeline import faithful_analysis, faithful_wiki_generation
from knowledge_workbench.schema_validation import validate_analysis, validate_wiki_generation


class SchemaPipelineTests(unittest.TestCase):
    def setUp(self):
        self.parsed = ParseResult(
            "test",
            "1",
            (
                ParsedUnit(
                    "机密资料禁止发送到云端模型。",
                    {"page": 3},
                ),
            ),
        )
        self.analysis = faithful_analysis(
            self.parsed,
            document_version_id="ver_abcdef1234",
            source_sha256="a" * 64,
            classification=Classification.CONFIDENTIAL,
        )

    def test_faithful_two_stage_outputs_validate(self):
        generation = faithful_wiki_generation(self.analysis, title="资料规则")
        validate_analysis(self.analysis, self.parsed.units)
        validate_wiki_generation(generation, self.analysis)

    def test_duplicate_excerpt_is_merged_with_ordered_unique_locators(self):
        parsed = ParseResult(
            "test",
            "1",
            (
                ParsedUnit("重复规则。", {"page": 1}),
                ParsedUnit("重复规则。", {"page": 2}),
                ParsedUnit("重复规则。", {"page": 2}),
            ),
        )

        analysis = faithful_analysis(
            parsed,
            document_version_id="ver_abcdef1234",
            source_sha256="b" * 64,
            classification=Classification.INTERNAL,
        )

        self.assertEqual(len(analysis["evidence"]), 1)
        item = analysis["evidence"][0]
        self.assertEqual(item["locator"], item["locators"][0])
        self.assertEqual([locator["page"] for locator in item["locators"]], [1, 2, 2])
        self.assertEqual([locator["unit"] for locator in item["locators"]], [1, 2, 3])
        validate_analysis(analysis, parsed.units)

    def test_multi_locator_contract_rejects_primary_mismatch_and_duplicates(self):
        invalid_primary = copy.deepcopy(self.analysis)
        invalid_primary["evidence"][0]["locators"] = [{"page": 9}]
        with self.assertRaisesRegex(KnowledgeWorkbenchError, "必须等于 locators 首项"):
            validate_analysis(invalid_primary, self.parsed.units)

        duplicate = copy.deepcopy(self.analysis)
        duplicate["evidence"][0]["locators"] *= 2
        with self.assertRaisesRegex(KnowledgeWorkbenchError, "包含重复定位"):
            validate_analysis(duplicate, self.parsed.units)

    def test_fabricated_excerpt_is_rejected(self):
        invalid = copy.deepcopy(self.analysis)
        invalid["evidence"][0]["excerpt"] = "原文中不存在的结论"
        with self.assertRaisesRegex(KnowledgeWorkbenchError, "无法回到解析结果"):
            validate_analysis(invalid, self.parsed.units)

    def test_unknown_stage_one_reference_is_rejected(self):
        generation = faithful_wiki_generation(self.analysis, title="资料规则")
        generation["pages"][0]["evidence_sections"][0]["evidence_ids"] = ["E9999"]
        with self.assertRaisesRegex(KnowledgeWorkbenchError, "不存在的证据"):
            validate_wiki_generation(generation, self.analysis)

    def test_schema_rejects_unexpected_fields(self):
        invalid = copy.deepcopy(self.analysis)
        invalid["evidence"][0]["unreviewed_guess"] = "不允许出现"
        with self.assertRaisesRegex(KnowledgeWorkbenchError, "Additional properties"):
            validate_analysis(invalid, self.parsed.units)


if __name__ == "__main__":
    unittest.main()
