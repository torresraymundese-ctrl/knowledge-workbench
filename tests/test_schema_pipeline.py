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

