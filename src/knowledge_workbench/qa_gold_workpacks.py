from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .audit import record_event
from .config import WorkspacePaths
from .database import Database
from .errors import InvalidTransitionError, KnowledgeWorkbenchError
from .question_answering import (
    MAX_CITATIONS,
    MAX_QUESTION_CHARACTERS,
    answer_question,
)
from .review_assurance import (
    required_review_mode,
    review_audit_context,
    validate_review_actor_policy,
    validate_review_attestation,
)
from .utils import new_id, sha256_text, utc_now
from .wiki import write_text_atomic


PACK_TYPE = "qa-gold-annotation-work-pack"
DATASET_TYPE = "knowledge-qa-gold"
EXPORT_EVENT = "qa_gold_work_pack_exported"
FINALIZED_EVENT = "qa_gold_dataset_finalized"
ANSWER_TYPES = ("evidence", "ambiguous", "conflict", "insufficient")
MIN_CASES = 3
MAX_CASES = 50

_FRONTMATTER_PATTERN = re.compile(r"\A---\n(?P<body>.*?)\n---\n", re.DOTALL)
_PROTECTED_PATTERN = re.compile(
    r"^<!-- QA_GOLD_PROTECTED_JSON:(?P<json>\{.*\}) -->$",
    re.MULTILINE,
)
_CASE_PATTERN = re.compile(
    r"^## `(?P<case_id>[^`]+)`\n(?P<body>.*?)(?=^## `|\Z)",
    re.MULTILINE | re.DOTALL,
)
_TYPE_PATTERN = re.compile(
    r"^\s*- (?P<box>\[[^\]]*\]) `(?P<value>"
    + "|".join(ANSWER_TYPES)
    + r")`\s*$",
    re.MULTILINE,
)
_CITATION_PATTERN = re.compile(
    r"^\s*- (?P<box>\[[^\]]*\]) `(?P<evidence_id>ev_[a-f0-9]+)`\s+\|",
    re.MULTILINE,
)
_APPROVAL_PATTERN = re.compile(
    r"^- (?P<box>\[[^\]]*\]) 我已核对问题、答案类型和全部必要引用\s*$",
    re.MULTILINE,
)
_NOTE_PATTERN = re.compile(r"^- 说明（可选）：(?P<note>.*)$", re.MULTILINE)
_CASE_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,80}$")


def export_qa_gold_work_pack(
    database: Database,
    paths: WorkspacePaths,
    question_set_path: Path,
    output: Path,
    *,
    actor: str,
    limit: int = MAX_CITATIONS,
) -> dict[str, Any]:
    actor = _required_actor(actor)
    if limit < 1 or limit > MAX_CITATIONS:
        raise KnowledgeWorkbenchError(
            f"候选引用数量必须在 1 到 {MAX_CITATIONS} 之间"
        )
    question_path, question_content, question_set = _load_question_set(
        paths, question_set_path
    )
    output = _new_evaluation_path(paths, output, "问答黄金工作包")
    pack_id = new_id("qagoldpack")
    generated_at = utc_now()
    cases: list[dict[str, Any]] = []
    for source_case in question_set["cases"]:
        result = answer_question(
            database,
            source_case["question"],
            actor=actor,
            history=source_case.get("history", []),
            limit=limit,
            paths=paths,
        )
        citations = [
            {
                "evidence_id": item["evidence_id"],
                "document_name": item["document_name"],
                "classification": item["classification"],
                "ordinal": item["ordinal"],
                "locator": item["locator"],
                "excerpt": item["excerpt"],
                "excerpt_sha256": sha256_text(item["excerpt"]),
            }
            for item in result["citations"]
        ]
        cases.append(
            {
                "case_id": source_case["case_id"],
                "category": source_case.get("category", ""),
                "question": source_case["question"],
                "history": source_case.get("history", []),
                "prediction": {
                    "answer_type": result["answer_type"],
                    "answer": result["answer"],
                    "conflict_ids": [
                        item["conflict_id"] for item in result["conflicts"]
                    ],
                    "ambiguity_count": len(result["ambiguities"]),
                },
                "citations": citations,
            }
        )
    protected = {
        "schema_version": "1.0",
        "pack_type": PACK_TYPE,
        "pack_id": pack_id,
        "question_set": {
            "name": question_set["name"],
            "path": _relative(paths, question_path),
            "content_sha256": sha256_text(question_content),
        },
        "annotator": actor,
        "generated_at": generated_at,
        "cases": cases,
    }
    protected_sha256 = _canonical_sha256(protected)
    content = _render_work_pack(protected, protected_sha256)
    write_text_atomic(output, content)
    try:
        with database.transaction() as connection:
            record_event(
                connection,
                EXPORT_EVENT,
                "qa_gold_work_pack",
                pack_id,
                actor=actor,
                details={
                    "output": _relative(paths, output),
                    "question_set_path": protected["question_set"]["path"],
                    "question_set_sha256": protected["question_set"][
                        "content_sha256"
                    ],
                    "protected_sha256": protected_sha256,
                    "case_count": len(cases),
                    "candidate_evidence_ids": sorted(
                        {
                            citation["evidence_id"]
                            for case in cases
                            for citation in case["citations"]
                        }
                    ),
                },
            )
    except Exception:
        output.unlink(missing_ok=True)
        raise
    return {
        "pack_id": pack_id,
        "output": output,
        "case_count": len(cases),
        "protected_sha256": protected_sha256,
    }


def inspect_qa_gold_work_pack(
    database: Database,
    paths: WorkspacePaths,
    work_pack_path: Path,
) -> dict[str, Any]:
    path, content = _read_evaluation_file(
        paths, work_pack_path, "问答黄金工作包"
    )
    issue_codes: list[str] = []
    try:
        metadata = _parse_frontmatter(content)
        protected = _parse_protected(content)
        decisions = _parse_decisions(content, protected)
        structure_valid = True
    except (KnowledgeWorkbenchError, KeyError, TypeError, ValueError):
        metadata = {}
        protected = {}
        decisions = []
        structure_valid = False
        issue_codes.append("work_pack_structure_invalid")

    protected_valid = False
    protected_display_valid = False
    source_valid = False
    evidence_snapshot_valid = False
    export_audit_valid = False
    already_applied = False
    if structure_valid:
        protected_sha256 = _canonical_sha256(protected)
        protected_valid = (
            metadata.get("type") == PACK_TYPE
            and metadata.get("pack_id") == protected.get("pack_id")
            and metadata.get("annotator") == protected.get("annotator")
            and metadata.get("protected_sha256") == protected_sha256
        )
        if not protected_valid:
            issue_codes.append("protected_snapshot_invalid")
        try:
            expected_content = _render_work_pack(
                protected, protected_sha256
            )
            protected_display_valid = (
                sha256_text(_normalize_editable_content(content))
                == sha256_text(
                    _normalize_editable_content(expected_content)
                )
            )
        except (KeyError, TypeError, ValueError):
            protected_display_valid = False
        if not protected_display_valid:
            issue_codes.append("protected_display_invalid")
        try:
            _validate_question_source(paths, protected)
            source_valid = True
        except (KnowledgeWorkbenchError, KeyError, TypeError):
            issue_codes.append("question_source_invalid")
        try:
            _validate_evidence_snapshot(database, protected)
            evidence_snapshot_valid = True
        except (KnowledgeWorkbenchError, KeyError, TypeError):
            issue_codes.append("evidence_snapshot_invalid")
        try:
            _validate_export_audit(
                database,
                paths,
                path,
                protected,
                protected_sha256,
            )
            export_audit_valid = True
        except (KnowledgeWorkbenchError, KeyError, TypeError):
            issue_codes.append("export_audit_invalid")
        already_applied = _dataset_already_finalized(
            database,
            _relative(paths, path),
            protected_sha256,
        )
        if already_applied:
            issue_codes.append("work_pack_already_applied")

    decision_counts = {
        "complete": 0,
        "incomplete": 0,
        "conflicting": 0,
    }
    for decision in decisions:
        if decision["conflicting"]:
            decision_counts["conflicting"] += 1
        elif decision["complete"]:
            decision_counts["complete"] += 1
        else:
            decision_counts["incomplete"] += 1
    if decision_counts["incomplete"]:
        issue_codes.append("decision_missing")
    if decision_counts["conflicting"]:
        issue_codes.append("decision_conflicting")

    integrity_valid = (
        structure_valid
        and protected_valid
        and protected_display_valid
        and source_valid
        and evidence_snapshot_valid
        and export_audit_valid
    )
    decisions_complete = (
        bool(decisions)
        and decision_counts["complete"] == len(decisions)
        and decision_counts["conflicting"] == 0
    )
    return {
        "schema_version": "1.0",
        "kind": "qa-gold-work-pack-status",
        "work_pack_path": _relative(paths, path),
        "content_sha256": sha256_text(content),
        "pack_id": protected.get("pack_id"),
        "annotator": protected.get("annotator"),
        "case_count": len(decisions),
        "structure_valid": structure_valid,
        "protected_snapshot_valid": protected_valid,
        "protected_display_valid": protected_display_valid,
        "question_source_valid": source_valid,
        "evidence_snapshot_valid": evidence_snapshot_valid,
        "export_audit_valid": export_audit_valid,
        "integrity_valid": integrity_valid,
        "decision_counts": decision_counts,
        "decisions_complete": decisions_complete,
        "already_applied": already_applied,
        "issue_codes": list(dict.fromkeys(issue_codes)),
        "apply_ready": (
            integrity_valid and decisions_complete and not already_applied
        ),
        "_protected": protected,
        "_decisions": decisions,
    }


def finalize_qa_gold_dataset(
    database: Database,
    paths: WorkspacePaths,
    work_pack_path: Path,
    output: Path,
    *,
    actor: str,
    review_mode: str,
    solo_attestation: str | None = None,
) -> dict[str, Any]:
    actor = _required_actor(actor)
    mode = required_review_mode(review_mode)
    status = inspect_qa_gold_work_pack(database, paths, work_pack_path)
    if not status["apply_ready"]:
        raise InvalidTransitionError(
            "问答黄金工作包尚未完成或来源已变化，请先运行 qa gold-status"
        )
    protected = status["_protected"]
    decisions = status["_decisions"]
    validate_review_actor_policy(
        submitter=protected["annotator"],
        reviewer=actor,
        review_mode=mode,
    )
    attestation_sha256 = validate_review_attestation(
        mode, solo_attestation
    )
    output = _new_evaluation_path(paths, output, "问答黄金数据集")
    dataset_id = new_id("qagold")
    review_context = review_audit_context(mode, attestation_sha256)
    dataset = {
        "schema_version": "1.0",
        "dataset_type": DATASET_TYPE,
        "dataset_id": dataset_id,
        "name": protected["question_set"]["name"],
        "source": {
            "work_pack_id": protected["pack_id"],
            "work_pack_path": status["work_pack_path"],
            "work_pack_content_sha256": status["content_sha256"],
            "protected_sha256": _canonical_sha256(protected),
            "question_set_path": protected["question_set"]["path"],
            "question_set_sha256": protected["question_set"][
                "content_sha256"
            ],
        },
        "assurance": {
            "annotator": protected["annotator"],
            "reviewer": actor,
            **review_context,
        },
        "finalized_at": utc_now(),
        "cases": [
            {
                "case_id": decision["case_id"],
                "category": decision["category"],
                "question": decision["question"],
                "history": decision["history"],
                "expected_answer_type": decision["expected_answer_type"],
                "expected_evidence_ids": decision[
                    "expected_evidence_ids"
                ],
                "note_sha256": (
                    sha256_text(decision["note"])
                    if decision["note"]
                    else None
                ),
            }
            for decision in decisions
        ],
    }
    content = (
        json.dumps(dataset, ensure_ascii=False, indent=2, sort_keys=True)
        + "\n"
    )
    write_text_atomic(output, content)
    try:
        report = evaluate_qa_gold_dataset(database, paths, output)
        with database.transaction() as connection:
            record_event(
                connection,
                FINALIZED_EVENT,
                "qa_gold_dataset",
                dataset_id,
                actor=actor,
                details={
                    "output": _relative(paths, output),
                    "content_sha256": sha256_text(content),
                    "source_work_pack_path": status["work_pack_path"],
                    "source_protected_sha256": _canonical_sha256(protected),
                    "case_count": len(dataset["cases"]),
                    "expected_answer_type_counts": _count_answer_types(
                        dataset["cases"]
                    ),
                    "expected_evidence_ids": sorted(
                        {
                            evidence_id
                            for case in dataset["cases"]
                            for evidence_id in case["expected_evidence_ids"]
                        }
                    ),
                    "evaluation": report["aggregate"],
                    **review_context,
                },
            )
    except Exception:
        output.unlink(missing_ok=True)
        raise
    return {
        "dataset_id": dataset_id,
        "output": output,
        "dataset": dataset,
        "evaluation": report,
    }


def evaluate_qa_gold_dataset(
    database: Database,
    paths: WorkspacePaths,
    dataset_path: Path,
) -> dict[str, Any]:
    path, _, dataset = _load_qa_dataset(paths, dataset_path)
    _validate_dataset_references(database, dataset)
    case_results: list[dict[str, Any]] = []
    true_positive = false_positive = false_negative = 0
    for case in dataset["cases"]:
        response = answer_question(
            database,
            case["question"],
            actor="qa-evaluator",
            history=case["history"],
            limit=MAX_CITATIONS,
            paths=paths,
            audit=False,
        )
        expected_ids = set(case["expected_evidence_ids"])
        predicted_ids = {
            citation["evidence_id"] for citation in response["citations"]
        }
        intersection = expected_ids & predicted_ids
        true_positive += len(intersection)
        false_positive += len(predicted_ids - expected_ids)
        false_negative += len(expected_ids - predicted_ids)
        citation_recall = (
            1.0
            if not expected_ids
            else len(intersection) / len(expected_ids)
        )
        citation_precision = (
            1.0
            if not predicted_ids
            else len(intersection) / len(predicted_ids)
        )
        answer_type_correct = (
            response["answer_type"] == case["expected_answer_type"]
        )
        restricted_leak = any(
            citation["classification"] == "restricted"
            for citation in response["citations"]
        )
        passed = (
            answer_type_correct
            and citation_recall == 1.0
            and not restricted_leak
        )
        case_results.append(
            {
                "case_id": case["case_id"],
                "passed": passed,
                "expected_answer_type": case["expected_answer_type"],
                "predicted_answer_type": response["answer_type"],
                "answer_type_correct": answer_type_correct,
                "expected_evidence_ids": sorted(expected_ids),
                "predicted_evidence_ids": sorted(predicted_ids),
                "citation_precision": round(citation_precision, 6),
                "citation_recall": round(citation_recall, 6),
                "restricted_leak": restricted_leak,
            }
        )
    case_count = len(case_results)
    passed_cases = sum(item["passed"] for item in case_results)
    answer_type_correct_cases = sum(
        item["answer_type_correct"] for item in case_results
    )
    predicted_positive = true_positive + false_positive
    actual_positive = true_positive + false_negative
    return {
        "schema_version": "1.0",
        "dataset_type": DATASET_TYPE,
        "dataset_id": dataset["dataset_id"],
        "dataset_name": dataset["name"],
        "dataset_path": _relative(paths, path),
        "evaluated_at": utc_now(),
        "algorithm": {
            "name": "verified-local-lexical-v1",
            "max_citations": MAX_CITATIONS,
        },
        "aggregate": {
            "case_count": case_count,
            "passed_cases": passed_cases,
            "pass_rate": round(passed_cases / case_count, 6),
            "answer_type_accuracy": round(
                answer_type_correct_cases / case_count, 6
            ),
            "citation_precision": round(
                1.0
                if predicted_positive == 0
                else true_positive / predicted_positive,
                6,
            ),
            "citation_recall": round(
                1.0
                if actual_positive == 0
                else true_positive / actual_positive,
                6,
            ),
            "restricted_leak_count": sum(
                item["restricted_leak"] for item in case_results
            ),
        },
        "cases": case_results,
    }


def _render_work_pack(
    protected: dict[str, Any], protected_sha256: str
) -> str:
    lines = [
        "---",
        f"type: {PACK_TYPE}",
        f"pack_id: {protected['pack_id']}",
        f"annotator: {protected['annotator']}",
        f"generated_at: {protected['generated_at']}",
        f"protected_sha256: {protected_sha256}",
        "---",
        "",
        f"# 问答黄金集人工核对：{protected['question_set']['name']}",
        "",
        "- 只编辑每个用例“人工决定”中的复选框和说明。",
        "- `[x]`、`[ x]`、`[x ]` 都表示已勾选。",
        "- 期望答案类型四选一；引用只勾选回答该问题必需的证据。",
        "- 如果系统没有列出必要证据，请不要勾选“已核对”，并在说明中记录。",
        "- `insufficient` 不应勾选引用；`ambiguous` 和 `conflict` 至少勾选两条。",
        "",
        "<!-- QA_GOLD_PROTECTED_JSON:"
        + json.dumps(
            protected,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + " -->",
        "",
    ]
    for case in protected["cases"]:
        lines.extend(
            [
                f"## `{case['case_id']}`",
                "",
                f"- 分类：`{case['category'] or '未分类'}`",
                f"- 问题：{case['question']}",
                (
                    "- 系统答案类型："
                    f"`{case['prediction']['answer_type']}`"
                ),
                f"- 系统回答：{case['prediction']['answer']}",
                "",
                "### 系统候选引用",
                "",
            ]
        )
        if not case["citations"]:
            lines.append("- 当前没有候选引用。")
        for index, citation in enumerate(case["citations"], start=1):
            lines.extend(
                [
                    (
                        f"#### [{index}] `{citation['evidence_id']}` · "
                        f"{citation['document_name']} · "
                        f"#{citation['ordinal']} · "
                        f"{citation['classification']}"
                    ),
                    "",
                    (
                        "- 定位 JSON："
                        + json.dumps(
                            citation["locator"],
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        )
                    ),
                    "",
                    *[
                        f"> {line}" if line else ">"
                        for line in citation["excerpt"].splitlines()
                    ],
                    "",
                ]
            )
        lines.extend(
            [
                "### 人工决定",
                "",
                "- 期望答案类型（四选一）：",
                *[f"  - [ ] `{answer_type}`" for answer_type in ANSWER_TYPES],
                "- 期望引用证据（勾选全部必需证据）：",
            ]
        )
        if case["citations"]:
            lines.extend(
                (
                    f"  - [ ] `{citation['evidence_id']}` | "
                    f"{citation['document_name']} #{citation['ordinal']}"
                )
                for citation in case["citations"]
            )
        else:
            lines.append("  - （无候选证据）")
        lines.extend(
            [
                "- [ ] 我已核对问题、答案类型和全部必要引用",
                "- 说明（可选）：",
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def _parse_frontmatter(content: str) -> dict[str, str]:
    match = _FRONTMATTER_PATTERN.search(content)
    if not match:
        raise KnowledgeWorkbenchError("问答黄金工作包缺少 frontmatter")
    values: dict[str, str] = {}
    for line in match.group("body").splitlines():
        if ":" not in line:
            raise KnowledgeWorkbenchError("问答黄金工作包 frontmatter 无效")
        key, value = line.split(":", 1)
        values[key.strip()] = value.strip()
    return values


def _parse_protected(content: str) -> dict[str, Any]:
    match = _PROTECTED_PATTERN.search(content)
    if not match:
        raise KnowledgeWorkbenchError("问答黄金工作包缺少受保护快照")
    try:
        value = json.loads(match.group("json"))
    except json.JSONDecodeError as exc:
        raise KnowledgeWorkbenchError("受保护快照不是有效 JSON") from exc
    if (
        not isinstance(value, dict)
        or value.get("pack_type") != PACK_TYPE
        or not isinstance(value.get("cases"), list)
    ):
        raise KnowledgeWorkbenchError("受保护快照结构无效")
    return value


def _parse_decisions(
    content: str, protected: dict[str, Any]
) -> list[dict[str, Any]]:
    sections = {
        match.group("case_id"): match.group("body")
        for match in _CASE_PATTERN.finditer(content)
    }
    expected_cases = protected["cases"]
    if list(sections) != [case["case_id"] for case in expected_cases]:
        raise KnowledgeWorkbenchError("问答用例范围或顺序已变化")
    decisions: list[dict[str, Any]] = []
    for case in expected_cases:
        body = sections[case["case_id"]]
        type_matches = list(_TYPE_PATTERN.finditer(body))
        if [match.group("value") for match in type_matches] != list(
            ANSWER_TYPES
        ):
            raise KnowledgeWorkbenchError("答案类型选项已变化")
        selected_types = [
            match.group("value")
            for match in type_matches
            if _checked(match.group("box"))
        ]
        citation_matches = list(_CITATION_PATTERN.finditer(body))
        expected_candidate_ids = [
            item["evidence_id"] for item in case["citations"]
        ]
        if [
            match.group("evidence_id") for match in citation_matches
        ] != expected_candidate_ids:
            raise KnowledgeWorkbenchError("候选引用范围或顺序已变化")
        selected_evidence_ids = [
            match.group("evidence_id")
            for match in citation_matches
            if _checked(match.group("box"))
        ]
        approval_matches = list(_APPROVAL_PATTERN.finditer(body))
        if len(approval_matches) != 1:
            raise KnowledgeWorkbenchError("用例确认项缺失或重复")
        approved = _checked(approval_matches[0].group("box"))
        note_match = _NOTE_PATTERN.search(body)
        if not note_match:
            raise KnowledgeWorkbenchError("用例说明字段缺失")
        note = note_match.group("note").strip()
        conflicting = len(selected_types) > 1
        expected_type = selected_types[0] if len(selected_types) == 1 else None
        citation_valid = _citation_decision_valid(
            expected_type, selected_evidence_ids
        )
        decisions.append(
            {
                "case_id": case["case_id"],
                "category": case["category"],
                "question": case["question"],
                "history": case["history"],
                "expected_answer_type": expected_type,
                "expected_evidence_ids": selected_evidence_ids,
                "approved": approved,
                "note": note,
                "conflicting": conflicting,
                "complete": (
                    not conflicting
                    and expected_type is not None
                    and citation_valid
                    and approved
                ),
            }
        )
    return decisions


def _citation_decision_valid(
    answer_type: str | None, evidence_ids: list[str]
) -> bool:
    if answer_type == "insufficient":
        return not evidence_ids
    if answer_type in {"ambiguous", "conflict"}:
        return len(evidence_ids) >= 2
    if answer_type == "evidence":
        return len(evidence_ids) >= 1
    return False


def _checked(value: str) -> bool:
    content = value[1:-1].replace(" ", "").lower()
    return content == "x"


def _normalize_editable_content(content: str) -> str:
    normalized = content
    for pattern in (
        _TYPE_PATTERN,
        _CITATION_PATTERN,
        _APPROVAL_PATTERN,
    ):
        normalized = pattern.sub(
            lambda match: match.group(0).replace(
                match.group("box"), "[ ]", 1
            ),
            normalized,
        )
    normalized = _NOTE_PATTERN.sub("- 说明（可选）：", normalized)
    return normalized


def _load_question_set(
    paths: WorkspacePaths, path: Path
) -> tuple[Path, str, dict[str, Any]]:
    resolved, content = _read_evaluation_file(paths, path, "问答问题集")
    try:
        value = json.loads(content)
    except json.JSONDecodeError as exc:
        raise KnowledgeWorkbenchError("问答问题集不是有效 JSON") from exc
    if not isinstance(value, dict):
        raise KnowledgeWorkbenchError("问答问题集顶层必须是对象")
    name = value.get("name")
    cases = value.get("cases")
    if not isinstance(name, str) or not name.strip():
        raise KnowledgeWorkbenchError("问答问题集必须包含名称")
    if (
        not isinstance(cases, list)
        or len(cases) < MIN_CASES
        or len(cases) > MAX_CASES
    ):
        raise KnowledgeWorkbenchError(
            f"问答问题集必须包含 {MIN_CASES} 到 {MAX_CASES} 个用例"
        )
    normalized_cases: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for item in cases:
        if not isinstance(item, dict):
            raise KnowledgeWorkbenchError("问答用例必须是对象")
        case_id = item.get("case_id")
        question = item.get("question")
        category = item.get("category", "")
        history = item.get("history", [])
        if (
            not isinstance(case_id, str)
            or not _CASE_ID_PATTERN.fullmatch(case_id)
            or case_id in seen_ids
        ):
            raise KnowledgeWorkbenchError("问答 case_id 无效或重复")
        if (
            not isinstance(question, str)
            or len(question.strip()) < 2
            or len(question.strip()) > MAX_QUESTION_CHARACTERS
        ):
            raise KnowledgeWorkbenchError("问答问题长度无效")
        if not isinstance(category, str) or len(category) > 80:
            raise KnowledgeWorkbenchError("问答分类无效")
        if not isinstance(history, list):
            raise KnowledgeWorkbenchError("问答历史必须是数组")
        seen_ids.add(case_id)
        normalized_cases.append(
            {
                "case_id": case_id,
                "question": question.strip(),
                "category": category.strip(),
                "history": history,
            }
        )
    return resolved, content, {"name": name.strip(), "cases": normalized_cases}


def _validate_question_source(
    paths: WorkspacePaths, protected: dict[str, Any]
) -> None:
    source = protected.get("question_set")
    if not isinstance(source, dict):
        raise KnowledgeWorkbenchError("问题集来源缺失")
    path, content = _read_evaluation_file(
        paths, Path(str(source.get("path", ""))), "问答问题集"
    )
    if (
        _relative(paths, path) != source.get("path")
        or sha256_text(content) != source.get("content_sha256")
    ):
        raise KnowledgeWorkbenchError("问题集来源已变化")


def _validate_evidence_snapshot(
    database: Database, protected: dict[str, Any]
) -> None:
    citations = [
        citation
        for case in protected.get("cases", [])
        for citation in case.get("citations", [])
    ]
    if not citations:
        return
    with database.connect() as connection:
        for citation in citations:
            row = connection.execute(
                """
                SELECT e.excerpt, e.run_ordinal, e.locator_json,
                       d.original_name, d.classification
                FROM evidence e
                JOIN processing_runs pr
                  ON pr.id = e.processing_run_id AND pr.is_current = 1
                JOIN document_versions dv ON dv.id = e.document_version_id
                JOIN documents d
                  ON d.id = dv.document_id AND d.current_version_id = dv.id
                WHERE e.id = ? AND e.status = 'verified'
                  AND d.classification != 'restricted'
                """,
                (citation["evidence_id"],),
            ).fetchone()
            if row is None:
                raise KnowledgeWorkbenchError("候选证据已不再有效")
            try:
                locator = json.loads(row["locator_json"])
            except json.JSONDecodeError:
                locator = {}
            if (
                sha256_text(row["excerpt"])
                != citation["excerpt_sha256"]
                or row["run_ordinal"] != citation["ordinal"]
                or row["original_name"] != citation["document_name"]
                or row["classification"] != citation["classification"]
                or locator != citation["locator"]
            ):
                raise KnowledgeWorkbenchError("候选证据快照已变化")


def _validate_export_audit(
    database: Database,
    paths: WorkspacePaths,
    path: Path,
    protected: dict[str, Any],
    protected_sha256: str,
) -> None:
    with database.connect() as connection:
        rows = connection.execute(
            """
            SELECT actor, details_json
            FROM audit_log
            WHERE event_type = ? AND entity_type = 'qa_gold_work_pack'
              AND entity_id = ?
            ORDER BY id DESC
            """,
            (EXPORT_EVENT, protected["pack_id"]),
        ).fetchall()
    for row in rows:
        try:
            details = json.loads(row["details_json"])
        except json.JSONDecodeError:
            continue
        if (
            row["actor"] == protected["annotator"]
            and details.get("output") == _relative(paths, path)
            and details.get("protected_sha256") == protected_sha256
            and details.get("question_set_path")
            == protected["question_set"]["path"]
            and details.get("question_set_sha256")
            == protected["question_set"]["content_sha256"]
        ):
            return
    raise KnowledgeWorkbenchError("问答黄金工作包缺少匹配的导出审计")


def _dataset_already_finalized(
    database: Database, source_path: str, protected_sha256: str
) -> bool:
    with database.connect() as connection:
        rows = connection.execute(
            """
            SELECT details_json FROM audit_log
            WHERE event_type = ? AND entity_type = 'qa_gold_dataset'
            ORDER BY id DESC
            """,
            (FINALIZED_EVENT,),
        ).fetchall()
    for row in rows:
        try:
            details = json.loads(row["details_json"])
        except json.JSONDecodeError:
            continue
        if (
            details.get("source_work_pack_path") == source_path
            and details.get("source_protected_sha256") == protected_sha256
        ):
            return True
    return False


def _load_qa_dataset(
    paths: WorkspacePaths, path: Path
) -> tuple[Path, str, dict[str, Any]]:
    resolved, content = _read_evaluation_file(paths, path, "问答黄金数据集")
    try:
        value = json.loads(content)
    except json.JSONDecodeError as exc:
        raise KnowledgeWorkbenchError("问答黄金数据集不是有效 JSON") from exc
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != "1.0"
        or value.get("dataset_type") != DATASET_TYPE
        or not isinstance(value.get("dataset_id"), str)
        or not isinstance(value.get("name"), str)
        or not isinstance(value.get("cases"), list)
        or not value["cases"]
    ):
        raise KnowledgeWorkbenchError("问答黄金数据集结构无效")
    case_ids: set[str] = set()
    for case in value["cases"]:
        if not isinstance(case, dict):
            raise KnowledgeWorkbenchError("问答黄金用例结构无效")
        case_id = case.get("case_id")
        answer_type = case.get("expected_answer_type")
        evidence_ids = case.get("expected_evidence_ids")
        if (
            not isinstance(case_id, str)
            or case_id in case_ids
            or answer_type not in ANSWER_TYPES
            or not isinstance(evidence_ids, list)
            or not all(isinstance(item, str) for item in evidence_ids)
            or not _citation_decision_valid(answer_type, evidence_ids)
            or not isinstance(case.get("question"), str)
            or not isinstance(case.get("history"), list)
        ):
            raise KnowledgeWorkbenchError("问答黄金用例字段无效")
        case_ids.add(case_id)
    return resolved, content, value


def _validate_dataset_references(
    database: Database, dataset: dict[str, Any]
) -> None:
    evidence_ids = sorted(
        {
            evidence_id
            for case in dataset["cases"]
            for evidence_id in case["expected_evidence_ids"]
        }
    )
    if not evidence_ids:
        return
    placeholders = ",".join("?" for _ in evidence_ids)
    with database.connect() as connection:
        rows = connection.execute(
            f"""
            SELECT e.id
            FROM evidence e
            JOIN processing_runs pr
              ON pr.id = e.processing_run_id AND pr.is_current = 1
            JOIN document_versions dv ON dv.id = e.document_version_id
            JOIN documents d
              ON d.id = dv.document_id AND d.current_version_id = dv.id
            WHERE e.id IN ({placeholders})
              AND e.status = 'verified'
              AND d.classification != 'restricted'
            """,
            tuple(evidence_ids),
        ).fetchall()
    if {row["id"] for row in rows} != set(evidence_ids):
        raise KnowledgeWorkbenchError(
            "问答黄金数据集引用了过期、未验证或 restricted 证据"
        )


def _read_evaluation_file(
    paths: WorkspacePaths, path: Path, label: str
) -> tuple[Path, str]:
    resolved = _resolve_evaluation_path(paths, path)
    if not resolved.is_file() or resolved.is_symlink():
        raise KnowledgeWorkbenchError(f"{label}不存在或不是普通文件")
    try:
        return resolved, resolved.read_text(encoding="utf-8")
    except OSError as exc:
        raise KnowledgeWorkbenchError(f"无法读取{label}：{exc}") from exc


def _new_evaluation_path(
    paths: WorkspacePaths, path: Path, label: str
) -> Path:
    resolved = _resolve_evaluation_path(paths, path)
    if resolved.exists():
        raise KnowledgeWorkbenchError(f"{label}已存在，禁止覆盖")
    resolved.parent.mkdir(parents=True, exist_ok=True)
    return resolved


def _resolve_evaluation_path(paths: WorkspacePaths, path: Path) -> Path:
    root = paths.evaluations.resolve()
    candidate = path.expanduser()
    candidates = (
        [candidate.resolve()]
        if candidate.is_absolute()
        else [
            (paths.root / candidate).resolve(),
            (Path.cwd() / candidate).resolve(),
        ]
    )
    for resolved in candidates:
        try:
            resolved.relative_to(root)
        except ValueError:
            continue
        return resolved
    raise KnowledgeWorkbenchError(
        "问答黄金文件必须位于当前 workspace/evaluations/"
    )


def _relative(paths: WorkspacePaths, path: Path) -> str:
    return path.resolve().relative_to(paths.root.resolve()).as_posix()


def _canonical_sha256(value: object) -> str:
    return sha256_text(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )


def _required_actor(actor: str) -> str:
    if not isinstance(actor, str) or not actor.strip():
        raise KnowledgeWorkbenchError("操作者不能为空")
    if len(actor.strip()) > 80:
        raise KnowledgeWorkbenchError("操作者不能超过 80 个字符")
    return actor.strip()


def _count_answer_types(cases: list[dict[str, Any]]) -> dict[str, int]:
    counts = {answer_type: 0 for answer_type in ANSWER_TYPES}
    for case in cases:
        counts[case["expected_answer_type"]] += 1
    return counts
