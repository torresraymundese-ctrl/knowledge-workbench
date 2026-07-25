from __future__ import annotations

import json
from importlib.resources import files
from typing import Iterable

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import ValidationError

from .errors import KnowledgeWorkbenchError
from .citation_support import MINIMUM_CITATION_SUPPORT, assess_generation_citations
from .models import ParsedUnit
from .review_assurance import (
    INDEPENDENT_REVIEW_MODE,
    validate_review_actor_policy,
)


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
    for item in payload["evidence"]:
        locators = item.get("locators")
        if locators is None:
            continue
        if locators[0] != item["locator"]:
            raise KnowledgeWorkbenchError(
                f"证据 {item['candidate_id']} 的 locator 必须等于 locators 首项"
            )
        canonical = [
            json.dumps(locator, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            for locator in locators
        ]
        if len(canonical) != len(set(canonical)):
            raise KnowledgeWorkbenchError(
                f"证据 {item['candidate_id']} 包含重复定位"
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


def validate_conflict_candidate_pack(payload: dict) -> None:
    _validate(payload, "conflict-candidate-pack-v1.json")
    candidate_ids = [item["candidate_id"] for item in payload["candidates"]]
    if len(candidate_ids) != len(set(candidate_ids)):
        raise KnowledgeWorkbenchError("跨文档冲突候选包包含重复 candidate_id")
    for candidate in payload["candidates"]:
        if candidate["left"]["document_id"] == candidate["right"]["document_id"]:
            raise KnowledgeWorkbenchError(
                f"候选 {candidate['candidate_id']} 不是跨文档证据对"
            )
        predicted_conflict = candidate["predicted_conflict"]
        predicted_type = candidate["predicted_type"]
        if predicted_conflict != (predicted_type is not None):
            raise KnowledgeWorkbenchError(
                f"候选 {candidate['candidate_id']} 的预测结果与类型不一致"
            )
        review = candidate["review"]
        if review["decision"] == "rejected" and not (review["note"] or "").strip():
            raise KnowledgeWorkbenchError(
                f"候选 {candidate['candidate_id']} 复核驳回时必须填写原因"
            )


def validate_conflict_labeling_plan(payload: dict) -> None:
    _validate(payload, "conflict-labeling-plan-v1.json")
    batch_ids = [item["batch_id"] for item in payload["batches"]]
    if len(batch_ids) != len(set(batch_ids)):
        raise KnowledgeWorkbenchError("跨文档冲突标注计划包含重复 batch_id")
    candidate_ids = [
        candidate_id
        for batch in payload["batches"]
        for candidate_id in batch["candidate_ids"]
    ]
    if len(candidate_ids) != len(set(candidate_ids)):
        raise KnowledgeWorkbenchError("跨文档冲突标注计划重复分配了候选")
    if len(candidate_ids) != payload["source_pack"]["candidate_count"]:
        raise KnowledgeWorkbenchError("跨文档冲突标注计划未完整覆盖来源候选")
    for expected_ordinal, batch in enumerate(payload["batches"], start=1):
        if batch["ordinal"] != expected_ordinal:
            raise KnowledgeWorkbenchError("跨文档冲突标注批次序号必须连续")
        if len(batch["candidate_ids"]) > payload["batch_size"]:
            raise KnowledgeWorkbenchError(
                f"标注批次 {batch['batch_id']} 超过 batch_size"
            )
        if sum(batch["stratum_counts"].values()) != len(
            batch["candidate_ids"]
        ):
            raise KnowledgeWorkbenchError(
                f"标注批次 {batch['batch_id']} 的分层计数不一致"
            )


def validate_citation_evaluation_dataset(payload: dict) -> None:
    _validate(payload, "citation-evaluation-v1.json")
    case_ids = [case["case_id"] for case in payload["cases"]]
    if len(case_ids) != len(set(case_ids)):
        raise KnowledgeWorkbenchError("引用支撑评测数据集包含重复 case_id")


def validate_graph_evaluation_dataset(payload: dict) -> None:
    _validate(payload, "graph-evaluation-v1.json")
    provenance = payload["provenance"]
    if not payload["name"].strip():
        raise KnowledgeWorkbenchError("图谱评测数据集名称不能为空")
    annotator = provenance["annotator"].strip()
    reviewer = provenance["reviewer"].strip()
    if not annotator or not reviewer:
        raise KnowledgeWorkbenchError("图谱评测标注人与复核人不能为空")
    review_mode = provenance.get(
        "review_mode", INDEPENDENT_REVIEW_MODE
    )
    validate_review_actor_policy(
        submitter=annotator,
        reviewer=reviewer,
        review_mode=review_mode,
    )
    if "review_mode" in provenance:
        if "independent_review" not in provenance:
            raise KnowledgeWorkbenchError(
                "图谱评测复核保证级别缺少 independent_review"
            )
        expected_independent = (
            review_mode == INDEPENDENT_REVIEW_MODE
        )
        if provenance["independent_review"] is not expected_independent:
            raise KnowledgeWorkbenchError(
                "图谱评测复核保证级别与 independent_review 不一致"
            )
    elif "independent_review" in provenance:
        raise KnowledgeWorkbenchError(
            "图谱评测 independent_review 缺少 review_mode"
        )
    cases = [*payload["relation_cases"], *payload["path_cases"]]
    if not cases:
        raise KnowledgeWorkbenchError("图谱评测数据集至少需要一个用例")
    case_ids = [case["case_id"] for case in cases]
    if len(case_ids) != len(set(case_ids)):
        raise KnowledgeWorkbenchError("图谱评测数据集包含重复 case_id")
    for case in payload["relation_cases"]:
        if case["source_entity_id"] == case["target_entity_id"]:
            raise KnowledgeWorkbenchError(
                f"图谱关系用例 {case['case_id']} 的两个实体不能相同"
            )
    for case in payload["path_cases"]:
        case_id = case["case_id"]
        if case["source_entity_id"] == case["target_entity_id"]:
            raise KnowledgeWorkbenchError(
                f"图谱路径用例 {case_id} 的起点和目标不能相同"
            )
        if not case["expected_reachable"]:
            continue
        entity_ids = case["expected_entity_ids"]
        relation_keys = case["expected_relation_keys"]
        directions = case["expected_traversal_directions"]
        if entity_ids[0] != case["source_entity_id"]:
            raise KnowledgeWorkbenchError(
                f"图谱路径用例 {case_id} 的期望路径起点不一致"
            )
        if entity_ids[-1] != case["target_entity_id"]:
            raise KnowledgeWorkbenchError(
                f"图谱路径用例 {case_id} 的期望路径目标不一致"
            )
        if len(relation_keys) != len(entity_ids) - 1:
            raise KnowledgeWorkbenchError(
                f"图谱路径用例 {case_id} 的关系数量必须比实体数量少 1"
            )
        if len(directions) != len(relation_keys):
            raise KnowledgeWorkbenchError(
                f"图谱路径用例 {case_id} 的方向数量与关系数量不一致"
            )
        if len(relation_keys) > case["max_depth"]:
            raise KnowledgeWorkbenchError(
                f"图谱路径用例 {case_id} 超出 max_depth"
            )
        if (
            "reverse" in directions
            and not case["include_inverse"]
        ):
            raise KnowledgeWorkbenchError(
                f"图谱路径用例 {case_id} 包含反向遍历但未开启 include_inverse"
            )


def validate_graph_gold_candidate_pack(payload: dict) -> None:
    _validate(payload, "graph-gold-candidate-v1.json")
    annotation_dataset = {
        "schema_version": "1.0",
        "name": payload["name"],
        "provenance": {
            "annotator": payload["annotator"],
            "reviewer": "__candidate_schema_reviewer__",
            "reviewed_at": payload["annotated_at"],
            "decision": "approved",
        },
        "relation_cases": payload["relation_cases"],
        "path_cases": payload["path_cases"],
    }
    if payload["annotator"] == "__candidate_schema_reviewer__":
        annotation_dataset["provenance"]["reviewer"] = (
            "__candidate_schema_reviewer_2__"
        )
    validate_graph_evaluation_dataset(annotation_dataset)


def validate_graph_pilot_pack(payload: dict) -> None:
    _validate(payload, "graph-pilot-pack-v1.json")
    evidence_ids = [
        candidate["evidence_id"] for candidate in payload["candidates"]
    ]
    if len(evidence_ids) != len(set(evidence_ids)):
        raise KnowledgeWorkbenchError("图谱试点证据包包含重复 evidence_id")
    exported_count = payload["statistics"]["exported_evidence_count"]
    if exported_count != len(evidence_ids):
        raise KnowledgeWorkbenchError("图谱试点证据包导出计数不一致")
    selected_count = payload["statistics"]["selected_evidence_count"]
    restricted_count = payload["statistics"][
        "restricted_evidence_excluded"
    ]
    if selected_count != exported_count + restricted_count:
        raise KnowledgeWorkbenchError("图谱试点证据包选择与排除计数不一致")
    status_total = sum(
        payload["statistics"]["status_counts"].values()
    )
    if status_total != exported_count:
        raise KnowledgeWorkbenchError("图谱试点证据包状态计数不一致")


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
