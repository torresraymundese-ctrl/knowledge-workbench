from __future__ import annotations

import json
from importlib.resources import files
from typing import Iterable

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import ValidationError

from .errors import KnowledgeWorkbenchError
from .citation_support import MINIMUM_CITATION_SUPPORT, assess_generation_citations
from .models import ParsedUnit


def load_schema(name: str) -> dict:
    resource = files("knowledge_workbench.schemas").joinpath(name)
    return json.loads(resource.read_text(encoding="utf-8"))


def validate_analysis(payload: dict, source_units: Iterable[ParsedUnit] | None = None) -> None:
    _validate(payload, "analysis-result-v1.json")
    candidate_ids = [item["candidate_id"] for item in payload["evidence"]]
    if len(candidate_ids) != len(set(candidate_ids)):
        raise KnowledgeWorkbenchError("阶段一输出包含重复 candidate_id")
    if source_units is not None:
        source_texts = tuple(unit.text for unit in source_units)
        for item in payload["evidence"]:
            if not any(item["excerpt"] in text for text in source_texts):
                raise KnowledgeWorkbenchError(
                    f"证据 {item['candidate_id']} 的原文片段无法回到解析结果"
                )


def validate_wiki_generation(payload: dict, analysis: dict) -> None:
    _validate(payload, "wiki-generation-v1.json")
    allowed = {item["candidate_id"] for item in analysis["evidence"]}
    referenced: set[str] = set()
    for page in payload["pages"]:
        for conclusion in page["conclusions"]:
            referenced.update(conclusion["evidence_ids"])
        for section in page["evidence_sections"]:
            referenced.update(section["evidence_ids"])
        for link in page["links"]:
            referenced.update(link["evidence_ids"])
    for task in payload["human_tasks"]:
        referenced.update(task["evidence_ids"])
    unknown = referenced - allowed
    if unknown:
        raise KnowledgeWorkbenchError(
            "阶段二输出引用了阶段一不存在的证据：" + ", ".join(sorted(unknown))
        )
    citation_assessment = assess_generation_citations(analysis, payload)
    signal_conflicts = [
        conclusion
        for conclusion in citation_assessment["conclusions"]
        if conclusion["signal_conflict_count"] > 0
    ]
    if signal_conflicts:
        locations = ", ".join(
            f"page[{item['page_index']}].conclusion[{item['conclusion_index']}]"
            for item in signal_conflicts
        )
        raise KnowledgeWorkbenchError(
            f"阶段二结论与所引证据存在方向或关键数值不一致：{locations}"
        )
    unsupported = [
        conclusion
        for conclusion in citation_assessment["conclusions"]
        if conclusion["support_score"] < MINIMUM_CITATION_SUPPORT
    ]
    if unsupported:
        locations = ", ".join(
            f"page[{item['page_index']}].conclusion[{item['conclusion_index']}]"
            for item in unsupported
        )
        raise KnowledgeWorkbenchError(
            f"阶段二结论与所引证据缺乏可验证文本关联：{locations}"
        )


def validate_evaluation_dataset(payload: dict, *, require_ready: bool = False) -> None:
    _validate(payload, "evaluation-dataset-v1.json")
    case_ids = [case["case_id"] for case in payload["cases"]]
    if len(case_ids) != len(set(case_ids)):
        raise KnowledgeWorkbenchError("评测数据集包含重复 case_id")
    if not require_ready:
        return
    for case in payload["cases"]:
        required = [
            item["text"].strip()
            for item in case["expected_evidence"]
            if item["required"]
        ]
        if not required:
            raise KnowledgeWorkbenchError(
                f"评测用例 {case['case_id']} 尚未指定任何必要证据"
            )
        labeled_text = [
            item["text"].strip() for item in case["expected_evidence"]
        ] + [item.strip() for item in case["forbidden_substrings"]]
        if any(_looks_like_labeling_placeholder(item) for item in labeled_text):
            raise KnowledgeWorkbenchError(
                f"评测用例 {case['case_id']} 仍包含待人工填写的占位内容"
            )
        normalized = [" ".join(item.split()).casefold() for item in required]
        if len(normalized) != len(set(normalized)):
            raise KnowledgeWorkbenchError(
                f"评测用例 {case['case_id']} 包含重复的必要证据标注"
            )


def validate_conflict_evaluation_dataset(payload: dict) -> None:
    _validate(payload, "conflict-evaluation-v1.json")
    case_ids = [case["case_id"] for case in payload["cases"]]
    if len(case_ids) != len(set(case_ids)):
        raise KnowledgeWorkbenchError("冲突评测数据集包含重复 case_id")
    for case in payload["cases"]:
        if case["expected_conflict"] and case["expected_type"] is None:
            raise KnowledgeWorkbenchError(
                f"冲突评测用例 {case['case_id']} 预期冲突时必须指定 expected_type"
            )
        if not case["expected_conflict"] and case["expected_type"] is not None:
            raise KnowledgeWorkbenchError(
                f"冲突评测用例 {case['case_id']} 预期非冲突时 expected_type 必须为 null"
            )


def validate_citation_evaluation_dataset(payload: dict) -> None:
    _validate(payload, "citation-evaluation-v1.json")
    case_ids = [case["case_id"] for case in payload["cases"]]
    if len(case_ids) != len(set(case_ids)):
        raise KnowledgeWorkbenchError("引用支撑评测数据集包含重复 case_id")


def _looks_like_labeling_placeholder(value: str) -> bool:
    normalized = value.strip().casefold()
    return any(
        marker in normalized
        for marker in ("待人工填写", "todo", "tbd", "placeholder")
    )


def _validate(payload: dict, schema_name: str) -> None:
    validator = Draft202012Validator(
        load_schema(schema_name), format_checker=FormatChecker()
    )
    errors = sorted(validator.iter_errors(payload), key=lambda error: list(error.path))
    if not errors:
        return
    error: ValidationError = errors[0]
    location = ".".join(str(part) for part in error.absolute_path) or "$"
    raise KnowledgeWorkbenchError(f"JSON Schema 校验失败（{location}）：{error.message}")
