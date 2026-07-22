from __future__ import annotations

import json
from copy import deepcopy

from .errors import KnowledgeWorkbenchError
from .extraction import FaithfulEvidenceExtractor
from .models import Classification, EvidenceCandidate, ParseResult
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
        "locator 统一填写空对象 {}，不要猜测或删改来源定位；系统会在本地按 excerpt "
        "从 parsed_units 回填 locator 和全部 locators。"
        "只返回一个 JSON 对象，包含 evidence 数组，数组元素必须符合所给 Schema 的 $defs.evidenceItem。\n"
        "prompt_version=analysis-v2-local-locators\n"
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
    source_candidates = FaithfulEvidenceExtractor().extract(parsed)
    evidence = _with_source_locators(
        model_output.get("evidence"), source_candidates
    )
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
            "prompt_version": "analysis-v2-local-locators",
        },
        "evidence": evidence,
    }
    validate_analysis(payload, parsed.units)
    return payload


def _with_source_locators(
    evidence: object, source_candidates: tuple[EvidenceCandidate, ...]
) -> object:
    if not isinstance(evidence, list):
        return evidence
    hydrated = []
    for raw_item in evidence:
        if not isinstance(raw_item, dict):
            hydrated.append(raw_item)
            continue
        item = deepcopy(raw_item)
        excerpt = item.get("excerpt")
        if not isinstance(excerpt, str) or not excerpt:
            hydrated.append(item)
            continue
        locators = []
        seen = set()
        for candidate in source_candidates:
            if excerpt not in candidate.excerpt:
                continue
            for source_locator in candidate.locators:
                locator = deepcopy(source_locator)
                canonical = json.dumps(
                    locator,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                if canonical in seen:
                    continue
                seen.add(canonical)
                locators.append(locator)
        if locators:
            item["locator"] = locators[0]
            item["locators"] = locators
        hydrated.append(item)
    return hydrated


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
        "根据阶段一分析生成抽取式 Wiki 草稿，执行以下不可放宽的规则：\n"
        "1. conclusions[].text 必须与某一条所引 evidence excerpt 逐字完全相同，"
        "包括方向词、数字、限定词和适用范围；不得改写、概括、合并或补充。\n"
        "2. 每条 conclusion 的 evidence_ids 只能包含那一条逐字匹配的 candidate_id；"
        "禁止把多条证据综合成新结论。\n"
        "3. 禁止推断原文未明确陈述的因果、目的、主体、条件、时间、范围、义务、"
        "许可、禁止、比较或数值关系；不能从标题、上下文或常识补足。\n"
        "4. 无法用单条 excerpt 逐字表达的内容不得写入 conclusions；如确有整理价值，"
        "放入 human_tasks，要求人工综合或核验。\n"
        "5. applicability 默认填写 null；只有原文 excerpt 明确包含适用范围时才可填写，"
        "且不得扩大原文范围。summary、标题、链接关系也不得陈述原文之外的新事实。\n"
        "6. 每条结论和链接必须引用 analysis 中存在的 candidate_id；"
        "只返回符合 Schema 的 JSON，不使用 Markdown。\n"
        "prompt_version=wiki-generation-v2-extractive\n"
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
