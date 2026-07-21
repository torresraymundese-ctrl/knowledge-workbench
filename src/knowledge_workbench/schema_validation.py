from __future__ import annotations

import json
from importlib.resources import files
from typing import Iterable

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import ValidationError

from .errors import KnowledgeWorkbenchError
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


def validate_evaluation_dataset(payload: dict) -> None:
    _validate(payload, "evaluation-dataset-v1.json")
    case_ids = [case["case_id"] for case in payload["cases"]]
    if len(case_ids) != len(set(case_ids)):
        raise KnowledgeWorkbenchError("评测数据集包含重复 case_id")


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
