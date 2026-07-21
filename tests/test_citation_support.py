import unittest

from knowledge_workbench.citation_support import assess_generation_citations
from knowledge_workbench.errors import KnowledgeWorkbenchError
from knowledge_workbench.schema_validation import validate_wiki_generation


class CitationSupportTests(unittest.TestCase):
    def test_supported_paraphrase_has_measurable_textual_support(self):
        analysis = _analysis()
        generation = _generation("正式知识需要绑定原始证据。")

        validate_wiki_generation(generation, analysis)
        assessment = assess_generation_citations(analysis, generation)

        self.assertEqual(assessment["conclusion_count"], 1)
        self.assertGreater(assessment["minimum_support_score"], 0.2)
        self.assertEqual(assessment["low_support_conclusion_count"], 0)

    def test_existing_evidence_id_cannot_launder_unrelated_conclusion(self):
        analysis = _analysis()
        generation = _generation("系统每天凌晨自动备份到异地机房。")

        with self.assertRaisesRegex(KnowledgeWorkbenchError, "缺乏可验证文本关联"):
            validate_wiki_generation(generation, analysis)

    def test_short_common_excerpt_cannot_gain_full_support_by_containment(self):
        analysis = _analysis()
        analysis["evidence"][0]["excerpt"] = "系统"
        generation = _generation("系统每天凌晨自动备份到异地机房。")

        with self.assertRaisesRegex(KnowledgeWorkbenchError, "缺乏可验证文本关联"):
            validate_wiki_generation(generation, analysis)

    def test_opposite_polarity_cannot_be_hidden_by_high_text_similarity(self):
        analysis = _analysis()
        analysis["evidence"][0]["excerpt"] = "内部资料禁止发送到云端模型。"
        generation = _generation("内部资料允许发送到云端模型。")

        with self.assertRaisesRegex(KnowledgeWorkbenchError, "方向或关键数值不一致"):
            validate_wiki_generation(generation, analysis)

    def test_changed_number_cannot_be_hidden_by_high_text_similarity(self):
        analysis = _analysis()
        analysis["evidence"][0]["excerpt"] = "项目应在45个工作日内完成。"
        generation = _generation("项目应在30个工作日内完成。")

        with self.assertRaisesRegex(KnowledgeWorkbenchError, "方向或关键数值不一致"):
            validate_wiki_generation(generation, analysis)

    def test_one_contradictory_citation_blocks_mixed_citation_set(self):
        analysis = _analysis()
        analysis["evidence"].append(
            {
                **analysis["evidence"][0],
                "candidate_id": "E0002",
                "excerpt": "正式知识不需要绑定原始证据。",
            }
        )
        generation = _generation("正式知识需要绑定原始证据。")
        generation["pages"][0]["conclusions"][0]["evidence_ids"].append("E0002")

        assessment = assess_generation_citations(analysis, generation)
        self.assertEqual(assessment["minimum_support_score"], 0.0)
        self.assertEqual(assessment["signal_conflict_conclusion_count"], 1)

        with self.assertRaisesRegex(KnowledgeWorkbenchError, "方向或关键数值不一致"):
            validate_wiki_generation(generation, analysis)

    def test_exact_supported_clause_is_not_poisoned_by_other_clause_polarity(self):
        analysis = _analysis()
        analysis["evidence"][0]["excerpt"] = (
            "内部资料禁止上传云端，但必须保留本地审计记录。"
        )
        generation = _generation("必须保留本地审计记录。")

        validate_wiki_generation(generation, analysis)

    def test_faithful_generation_without_conclusions_is_explicitly_not_applicable(self):
        analysis = _analysis()
        generation = _generation("正式知识需要绑定原始证据。")
        generation["pages"][0]["conclusions"] = []

        assessment = assess_generation_citations(analysis, generation)

        self.assertEqual(assessment["conclusion_count"], 0)
        self.assertIsNone(assessment["minimum_support_score"])
        self.assertIsNone(assessment["average_support_score"])


def _analysis() -> dict:
    return {
        "schema_version": "1.0",
        "source": {
            "document_version_id": "ver_abcdef",
            "sha256": "a" * 64,
            "classification": "internal",
        },
        "provenance": {
            "mode": "faithful",
            "provider": "deterministic",
            "model": None,
            "prompt_version": "test",
        },
        "evidence": [
            {
                "candidate_id": "E0001",
                "excerpt": "所有正式知识必须绑定可以回到原始文件的证据。",
                "locator": {"line": 1},
                "evidence_type": "requirement",
                "entities": [],
                "concepts": [],
                "projects": [],
                "applicability": {
                    "scope": None,
                    "valid_from": None,
                    "valid_to": None,
                },
                "potential_conflicts": [],
            }
        ],
    }


def _generation(conclusion: str) -> dict:
    return {
        "schema_version": "1.0",
        "analysis_schema_version": "1.0",
        "pages": [
            {
                "title": "知识规则",
                "slug_suggestion": "知识规则",
                "summary": None,
                "conclusions": [
                    {
                        "text": conclusion,
                        "evidence_ids": ["E0001"],
                        "confidence": "high",
                        "applicability": None,
                    }
                ],
                "evidence_sections": [
                    {"heading": "依据", "evidence_ids": ["E0001"]}
                ],
                "links": [],
            }
        ],
        "human_tasks": [],
    }


if __name__ == "__main__":
    unittest.main()
