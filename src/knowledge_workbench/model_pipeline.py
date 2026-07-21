from __future__ import annotations

import json

from .errors import KnowledgeWorkbenchError
from .models import Classification, ParseResult
from .providers import AuditedModelGateway
from .schema_validation import load_schema, validate_analysis, validate_wiki_generation


def analyze_with_model(
    gateway: AuditedModelGateway,
    parsed: ParseResult,
    *,
    document_version_id: str,
    source_sha256: str,
    classification: Classification,
    actor: str,
    allow_internal_cloud_once: bool = False,
) -> dict:
    schema = load_schema("analysis-result-v1.json")
    units = [
        {"text": unit.text, "locator": unit.locator}
        for unit in parsed.units
    ]
    prompt = (
        "从解析单元提取原子证据。excerpt 必须逐字来自某个 text；不要生成结论。"
        "只返回一个 JSON 对象，包含 evidence 数组，数组元素必须符合所给 Schema 的 $defs.evidenceItem。\n"
        f"evidence_item_schema={json.dumps(schema['$defs']['evidenceItem'], ensure_ascii=False)}\n"
        f"parsed_units={json.dumps(units, ensure_ascii=False)}"
    )
    content = gateway.generate(
        prompt,
        classification,
        actor=actor,
        allow_internal_cloud_once=allow_internal_cloud_once,
    )
    model_output = _json_object(content)
    payload = {
        "schema_version": "1.0",
        "source": {
            "document_version_id": document_version_id,
            "sha256": source_sha256,
            "classification": classification.value,
        },
        "provenance": {
            "mode": "model_assisted",
            "provider": type(gateway.provider).__name__,
            "model": gateway.provider.name,
            "prompt_version": "analysis-v1",
        },
        "evidence": model_output.get("evidence"),
    }
    validate_analysis(payload, parsed.units)
    return payload


def generate_wiki_with_model(
    gateway: AuditedModelGateway,
    analysis: dict,
    *,
    title: str,
    classification: Classification,
    actor: str,
    allow_internal_cloud_once: bool = False,
) -> dict:
    schema = load_schema("wiki-generation-v1.json")
    prompt = (
        "根据阶段一分析生成 Wiki 草稿。每条结论和链接必须引用 analysis 中存在的 candidate_id；"
        "证据不足时放入 human_tasks，不得补写事实。只返回符合 Schema 的 JSON。\n"
        f"suggested_title={json.dumps(title, ensure_ascii=False)}\n"
        f"schema={json.dumps(schema, ensure_ascii=False)}\n"
        f"analysis={json.dumps(analysis, ensure_ascii=False)}"
    )
    content = gateway.generate(
        prompt,
        classification,
        actor=actor,
        allow_internal_cloud_once=allow_internal_cloud_once,
    )
    payload = _json_object(content)
    validate_wiki_generation(payload, analysis)
    return payload


def _json_object(content: str) -> dict:
    try:
        payload = json.loads(content)
    except json.JSONDecodeError as exc:
        raise KnowledgeWorkbenchError("模型返回的不是有效 JSON") from exc
    if not isinstance(payload, dict):
        raise KnowledgeWorkbenchError("模型返回的 JSON 顶层必须是对象")
    return payload

