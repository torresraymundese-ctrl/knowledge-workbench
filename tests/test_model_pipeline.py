import json
import tempfile
import unittest
from dataclasses import dataclass, field
from pathlib import Path

from knowledge_workbench.config import WorkspacePaths
from knowledge_workbench.errors import KnowledgeWorkbenchError
from knowledge_workbench.ingest import initialize_workspace
from knowledge_workbench.model_pipeline import analyze_with_model, generate_wiki_with_model
from knowledge_workbench.models import Classification, ParsedUnit, ParseResult
from knowledge_workbench.policy import ProviderLocation
from knowledge_workbench.providers import AuditedModelGateway


@dataclass
class ResponseQueueModel:
    responses: list[str]
    name: str = "deepseek-chat"
    location: ProviderLocation = ProviderLocation.CLOUD
    calls: list[str] = field(default_factory=list)

    def generate(self, prompt: str) -> str:
        self.calls.append(prompt)
        return self.responses.pop(0)


class ModelPipelineTests(unittest.TestCase):
    def test_two_model_stages_validate_against_shared_evidence_ids(self):
        evidence = {
            "candidate_id": "E0001",
            "excerpt": "所有正式知识必须绑定原始证据。",
            "locator": {"line_start": 1},
            "evidence_type": "requirement",
            "entities": [],
            "concepts": ["证据"],
            "projects": [],
            "applicability": {"scope": None, "valid_from": None, "valid_to": None},
            "potential_conflicts": [],
        }
        generation = {
            "schema_version": "1.0",
            "analysis_schema_version": "1.0",
            "pages": [{
                "title": "知识规则",
                "slug_suggestion": "知识规则",
                "summary": "证据要求",
                "conclusions": [{
                    "text": evidence["excerpt"],
                    "evidence_ids": ["E0001"],
                    "confidence": "high",
                    "applicability": None,
                }],
                "evidence_sections": [{"heading": "依据", "evidence_ids": ["E0001"]}],
                "links": [],
            }],
            "human_tasks": [],
        }
        model = ResponseQueueModel(
            [json.dumps({"evidence": [evidence]}, ensure_ascii=False), json.dumps(generation, ensure_ascii=False)]
        )
        parsed = ParseResult(
            "test", "1", (ParsedUnit(evidence["excerpt"], evidence["locator"]),)
        )
        with tempfile.TemporaryDirectory() as temporary:
            database = initialize_workspace(
                WorkspacePaths(Path(temporary) / "workspace")
            )
            gateway = AuditedModelGateway(database, model)
            analysis = analyze_with_model(
                gateway,
                parsed,
                document_version_id="ver_abcdef",
                source_sha256="b" * 64,
                classification=Classification.PUBLIC,
                actor="tester",
            )
            output = generate_wiki_with_model(
                gateway,
                analysis,
                title="知识规则",
                classification=Classification.PUBLIC,
                actor="tester",
            )
        self.assertEqual(output["pages"][0]["conclusions"][0]["evidence_ids"], ["E0001"])
        expected_locator = {"line_start": 1, "unit": 1, "segment": 1}
        self.assertEqual(analysis["evidence"][0]["locator"], expected_locator)
        self.assertEqual(analysis["evidence"][0]["locators"], [expected_locator])
        self.assertEqual(
            analysis["provenance"]["prompt_version"],
            "analysis-v3-source-anchored",
        )
        self.assertEqual(len(model.calls), 2)
        self.assertIn("按 candidate_id 强制覆盖", model.calls[0])
        self.assertIn('"candidate_id": "E0001"', model.calls[0])
        generation_prompt = model.calls[1]
        self.assertIn("逐字完全相同", generation_prompt)
        self.assertIn("禁止把多条证据综合成新结论", generation_prompt)
        self.assertIn("禁止推断原文未明确陈述", generation_prompt)
        self.assertIn("prompt_version=wiki-generation-v2-extractive", generation_prompt)

    def test_model_cannot_invent_candidate_id(self):
        evidence = {
            "candidate_id": "E9999",
            "excerpt": "模型凭空生成的句子。",
            "locator": {},
            "evidence_type": "fact",
            "entities": [],
            "concepts": [],
            "projects": [],
            "applicability": {"scope": None, "valid_from": None, "valid_to": None},
            "potential_conflicts": [],
        }
        model = ResponseQueueModel([json.dumps({"evidence": [evidence]}, ensure_ascii=False)])
        parsed = ParseResult("test", "1", (ParsedUnit("真实原文。", {}),))
        with tempfile.TemporaryDirectory() as temporary:
            database = initialize_workspace(
                WorkspacePaths(Path(temporary) / "workspace")
            )
            gateway = AuditedModelGateway(database, model)
            with self.assertRaisesRegex(KnowledgeWorkbenchError, "不存在的 candidate_id"):
                analyze_with_model(
                    gateway,
                    parsed,
                    document_version_id="ver_abcdef",
                    source_sha256="c" * 64,
                    classification=Classification.PUBLIC,
                    actor="tester",
                )

    def test_model_excerpt_is_replaced_by_source_candidate(self):
        evidence = {
            "candidate_id": "E0001",
            "excerpt": "模型改写后的句子。",
            "locator": {"invented": True},
            "evidence_type": "fact",
            "entities": [],
            "concepts": [],
            "projects": [],
            "applicability": {"scope": None, "valid_from": None, "valid_to": None},
            "potential_conflicts": [],
        }
        source_excerpt = "必须逐字保留的真实原文。"
        model = ResponseQueueModel(
            [json.dumps({"evidence": [evidence]}, ensure_ascii=False)]
        )
        parsed = ParseResult(
            "test", "1", (ParsedUnit(source_excerpt, {"paragraph": 3}),)
        )
        with tempfile.TemporaryDirectory() as temporary:
            database = initialize_workspace(
                WorkspacePaths(Path(temporary) / "workspace")
            )
            analysis = analyze_with_model(
                AuditedModelGateway(database, model),
                parsed,
                document_version_id="ver_abcdef",
                source_sha256="e" * 64,
                classification=Classification.PUBLIC,
                actor="tester",
            )

        item = analysis["evidence"][0]
        self.assertEqual(item["excerpt"], source_excerpt)
        self.assertEqual(item["locator"]["paragraph"], 3)
        self.assertNotIn("invented", item["locator"])

    def test_model_must_return_every_source_candidate_in_order(self):
        evidence = {
            "candidate_id": "E0002",
            "excerpt": "第二条。",
            "locator": {},
            "evidence_type": "fact",
            "entities": [],
            "concepts": [],
            "projects": [],
            "applicability": {"scope": None, "valid_from": None, "valid_to": None},
            "potential_conflicts": [],
        }
        model = ResponseQueueModel(
            [json.dumps({"evidence": [evidence]}, ensure_ascii=False)]
        )
        parsed = ParseResult(
            "test",
            "1",
            (
                ParsedUnit("第一条。", {"paragraph": 1}),
                ParsedUnit("第二条。", {"paragraph": 2}),
            ),
        )
        with tempfile.TemporaryDirectory() as temporary:
            database = initialize_workspace(
                WorkspacePaths(Path(temporary) / "workspace")
            )
            with self.assertRaisesRegex(KnowledgeWorkbenchError, "完整同序一致"):
                analyze_with_model(
                    AuditedModelGateway(database, model),
                    parsed,
                    document_version_id="ver_abcdef",
                    source_sha256="f" * 64,
                    classification=Classification.PUBLIC,
                    actor="tester",
                )

    def test_model_locator_is_replaced_with_all_matching_source_locators(self):
        excerpt = "同一要求在表格中重复出现。"
        evidence = {
            "candidate_id": "E0001",
            "excerpt": excerpt,
            "locator": {"invented": True},
            "evidence_type": "requirement",
            "entities": [],
            "concepts": [],
            "projects": [],
            "applicability": {"scope": None, "valid_from": None, "valid_to": None},
            "potential_conflicts": [],
        }
        model = ResponseQueueModel(
            [json.dumps({"evidence": [evidence]}, ensure_ascii=False)]
        )
        source_locators = (
            {"table_index": 1, "row_start": 2, "segment": 1, "unit": 1},
            {"table_index": 1, "row_start": 8, "segment": 1, "unit": 2},
        )
        parsed = ParseResult(
            "test",
            "1",
            tuple(ParsedUnit(excerpt, locator) for locator in source_locators),
        )
        with tempfile.TemporaryDirectory() as temporary:
            database = initialize_workspace(
                WorkspacePaths(Path(temporary) / "workspace")
            )
            analysis = analyze_with_model(
                AuditedModelGateway(database, model),
                parsed,
                document_version_id="ver_abcdef",
                source_sha256="d" * 64,
                classification=Classification.PUBLIC,
                actor="tester",
            )

        item = analysis["evidence"][0]
        self.assertEqual(item["locator"], source_locators[0])
        self.assertEqual(item["locators"], list(source_locators))
        self.assertNotIn("invented", json.dumps(item["locators"]))


if __name__ == "__main__":
    unittest.main()
