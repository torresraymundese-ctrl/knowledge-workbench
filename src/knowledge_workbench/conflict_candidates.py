from __future__ import annotations

import hashlib
import json
import threading
from collections import defaultdict
from difflib import SequenceMatcher
from itertools import combinations
from pathlib import Path
from typing import Any

from .audit import record_event
from .config import WorkspacePaths
from .conflicts import _classify_conflict, _comparison_signature, _normalize
from .database import Database
from .errors import KnowledgeWorkbenchError
from .review_assurance import (
    INDEPENDENT_REVIEW_MODE,
    review_audit_context,
    validate_review_attestation,
    validate_review_actor_policy,
)
from .schema_validation import (
    validate_conflict_candidate_pack,
    validate_conflict_evaluation_dataset,
)
from .utils import sha256_file, sha256_text, utc_now
from .wiki import write_text_atomic


_CANDIDATE_PACK_WRITE_LOCK = threading.RLock()
_WEB_PAGE_LIMIT_MAXIMUM = 100
_CONFLICT_TYPES = frozenset({"polarity_change", "value_change"})


def list_cross_document_candidate_packs(
    database: Database, paths: WorkspacePaths
) -> dict[str, Any]:
    """Return safe summaries for valid candidate packs in evaluations."""

    records, invalid_count = _candidate_pack_records(paths)
    items = []
    for path, pack in records:
        submission = _latest_annotation_submission(database, pack["pack_id"])
        annotation_sha256 = _annotation_sha256(pack)
        drifted = bool(
            submission and submission["annotation_sha256"] != annotation_sha256
        )
        counts = _candidate_counts(pack)
        items.append(
            {
                "pack_id": pack["pack_id"],
                "generated_at": pack["generated_at"],
                "generated_by": pack["generated_by"],
                "minimum_similarity": pack["minimum_similarity"],
                "statistics": pack["statistics"],
                "counts": counts,
                "phase": _candidate_pack_phase(submission, drifted, counts),
                "annotator": submission["actor"] if submission else None,
                "content_sha256": sha256_file(path),
            }
        )
    items.sort(key=lambda item: (item["generated_at"], item["pack_id"]), reverse=True)
    return {"items": items, "total": len(items), "invalid_count": invalid_count}


def cross_document_candidate_page(
    database: Database,
    paths: WorkspacePaths,
    pack_id: str,
    *,
    limit: int = 10,
    offset: int = 0,
    state: str | None = None,
    query: str | None = None,
    candidate_ids: list[str] | None = None,
) -> dict[str, Any]:
    limit, offset = _candidate_pagination(limit, offset)
    state = _candidate_state_filter(state)
    query = _candidate_query(query)
    path, pack = _candidate_pack_by_id(paths, pack_id)
    _validate_pack_provenance(database, pack)
    submission, drifted = _candidate_submission_state(database, pack)
    counts = _candidate_counts(pack)
    phase = _candidate_pack_phase(submission, drifted, counts)
    if drifted:
        raise KnowledgeWorkbenchError(
            "候选包标签在提交审计后发生变化；已锁定 Web 操作，请恢复已提交版本"
        )
    scoped_candidates = pack["candidates"]
    if candidate_ids is not None:
        if len(candidate_ids) != len(set(candidate_ids)):
            raise KnowledgeWorkbenchError(
                "候选分页范围包含重复 candidate_id"
            )
        candidates_by_id = {
            item["candidate_id"]: item for item in pack["candidates"]
        }
        unknown = [
            candidate_id
            for candidate_id in candidate_ids
            if candidate_id not in candidates_by_id
        ]
        if unknown:
            raise KnowledgeWorkbenchError(
                "候选分页范围包含来源包不存在的 candidate_id"
            )
        scoped_candidates = [
            candidates_by_id[candidate_id]
            for candidate_id in candidate_ids
        ]
    candidates = [
        candidate
        for candidate in scoped_candidates
        if _candidate_matches(candidate, state=state, query=query)
    ]
    total = len(candidates)
    page = candidates[offset : offset + limit]
    return {
        "pack_id": pack["pack_id"],
        "generated_at": pack["generated_at"],
        "generated_by": pack["generated_by"],
        "minimum_similarity": pack["minimum_similarity"],
        "statistics": pack["statistics"],
        "counts": counts,
        "scope_counts": _candidate_counts(
            {"candidates": scoped_candidates}
        ),
        "phase": phase,
        "annotator": submission["actor"] if submission else None,
        "content_sha256": sha256_file(path),
        "items": page,
        "total": total,
        "limit": limit,
        "offset": offset,
        "has_previous": offset > 0,
        "has_next": offset + len(page) < total,
        "state": state,
        "query": query,
    }


def update_cross_document_candidate_label(
    database: Database,
    paths: WorkspacePaths,
    pack_id: str,
    candidate_id: str,
    *,
    expected_content_sha256: str,
    expected_conflict: bool,
    expected_type: str | None,
    note: str | None,
    actor: str,
) -> dict[str, Any]:
    actor = _required_actor(actor)
    if not isinstance(expected_conflict, bool):
        raise KnowledgeWorkbenchError("expected_conflict 必须是布尔值")
    if expected_conflict and expected_type not in _CONFLICT_TYPES:
        raise KnowledgeWorkbenchError("冲突标签必须指定 polarity_change 或 value_change")
    if not expected_conflict and expected_type is not None:
        raise KnowledgeWorkbenchError("非冲突标签的 expected_type 必须为 null")
    note = _candidate_note(note, required=False)

    def mutate(pack: dict[str, Any]) -> dict[str, Any]:
        submission, drifted = _candidate_submission_state(database, pack)
        if submission or drifted:
            raise KnowledgeWorkbenchError("候选标签已提交，不能继续修改")
        candidate = _candidate_by_id(pack, candidate_id)
        candidate["label"] = {
            "expected_conflict": expected_conflict,
            "expected_type": expected_type,
            "note": note,
        }
        return {
            "event_type": "conflict_candidate_label_updated",
            "details": {
                "candidate_id": candidate_id,
                "expected_conflict": expected_conflict,
                "expected_type": expected_type,
                "note_sha256": sha256_text(note) if note else None,
            },
        }

    return _mutate_candidate_pack(
        database,
        paths,
        pack_id,
        expected_content_sha256=expected_content_sha256,
        actor=actor,
        mutate=mutate,
    )


def apply_cross_document_candidate_label_batch(
    database: Database,
    paths: WorkspacePaths,
    pack_id: str,
    labels: list[dict[str, Any]],
    *,
    expected_content_sha256: str,
    plan_id: str,
    batch_id: str,
    work_pack_path: str,
    work_pack_sha256: str,
    actor: str,
) -> dict[str, Any]:
    actor = _required_actor(actor)
    plan_id = _required_work_scope_id(plan_id, "plan_id")
    batch_id = _required_work_scope_id(batch_id, "batch_id")
    work_pack_path = _required_audit_text(
        work_pack_path, "work_pack_path", maximum=500
    )
    work_pack_sha256 = _required_sha256(
        work_pack_sha256, "work_pack_sha256"
    )
    normalized = _normalized_candidate_labels(labels)

    def mutate(pack: dict[str, Any]) -> dict[str, Any]:
        submission, drifted = _candidate_submission_state(database, pack)
        if submission or drifted:
            raise KnowledgeWorkbenchError("候选标签已提交，不能继续修改")
        for item in normalized:
            candidate = _candidate_by_id(pack, item["candidate_id"])
            candidate["label"] = {
                "expected_conflict": item["expected_conflict"],
                "expected_type": item["expected_type"],
                "note": item["note"],
            }
        return {
            "event_type": "conflict_candidate_label_batch_applied",
            "details": {
                "candidate_ids": [
                    item["candidate_id"] for item in normalized
                ],
                "plan_id": plan_id,
                "batch_id": batch_id,
                "work_pack_path": work_pack_path,
                "work_pack_sha256": work_pack_sha256,
                "candidate_count": len(normalized),
                "conflict_count": sum(
                    item["expected_conflict"] for item in normalized
                ),
                "note_sha256_by_candidate": {
                    item["candidate_id"]: sha256_text(item["note"])
                    for item in normalized
                    if item["note"]
                },
            },
        }

    return _mutate_candidate_pack(
        database,
        paths,
        pack_id,
        expected_content_sha256=expected_content_sha256,
        actor=actor,
        mutate=mutate,
    )


def submit_cross_document_candidate_annotations_by_id(
    database: Database,
    paths: WorkspacePaths,
    pack_id: str,
    *,
    expected_content_sha256: str,
    actor: str,
) -> dict[str, Any]:
    actor = _required_actor(actor)
    with _CANDIDATE_PACK_WRITE_LOCK:
        path, _ = _candidate_pack_by_id(paths, pack_id)
        pack, _ = _read_candidate_pack_at_sha256(
            path, pack_id, expected_content_sha256
        )
        _validate_pack_provenance(database, pack)
        submission, drifted = _candidate_submission_state(database, pack)
        if drifted:
            raise KnowledgeWorkbenchError("候选包标签在提交审计后发生变化")
        if submission:
            raise KnowledgeWorkbenchError("候选标签已经提交复核")
        _require_content_sha256(path, expected_content_sha256)
        result = _record_candidate_annotation_submission(database, pack, actor=actor)
        result["content_sha256"] = sha256_file(path)
        result["phase"] = "reviewing"
        return result


def update_cross_document_candidate_review(
    database: Database,
    paths: WorkspacePaths,
    pack_id: str,
    candidate_id: str,
    *,
    expected_content_sha256: str,
    decision: str,
    note: str | None,
    actor: str,
) -> dict[str, Any]:
    actor = _required_actor(actor)
    if decision not in {"approved", "rejected"}:
        raise KnowledgeWorkbenchError("复核决定必须是 approved 或 rejected")
    note = _candidate_note(note, required=decision == "rejected")

    def mutate(pack: dict[str, Any]) -> dict[str, Any]:
        submission, drifted = _candidate_submission_state(database, pack)
        if drifted:
            raise KnowledgeWorkbenchError("候选包标签在提交审计后发生变化")
        if not submission:
            raise KnowledgeWorkbenchError("候选标签尚未提交，不能复核")
        if submission["actor"] == actor:
            raise KnowledgeWorkbenchError("标注人与复核人必须不同")
        candidate = _candidate_by_id(pack, candidate_id)
        candidate["review"] = {"decision": decision, "note": note}
        return {
            "event_type": "conflict_candidate_review_updated",
            "details": {
                "candidate_id": candidate_id,
                "decision": decision,
                "annotator": submission["actor"],
                "note_sha256": sha256_text(note) if note else None,
            },
        }

    return _mutate_candidate_pack(
        database,
        paths,
        pack_id,
        expected_content_sha256=expected_content_sha256,
        actor=actor,
        mutate=mutate,
    )


def apply_cross_document_candidate_review_batch(
    database: Database,
    paths: WorkspacePaths,
    pack_id: str,
    decisions: list[dict[str, Any]],
    *,
    expected_content_sha256: str,
    plan_id: str,
    batch_id: str,
    work_pack_path: str,
    work_pack_sha256: str,
    actor: str,
    review_mode: str,
    solo_attestation_sha256: str | None,
) -> dict[str, Any]:
    actor = _required_actor(actor)
    plan_id = _required_work_scope_id(plan_id, "plan_id")
    batch_id = _required_work_scope_id(batch_id, "batch_id")
    work_pack_path = _required_audit_text(
        work_pack_path, "work_pack_path", maximum=500
    )
    work_pack_sha256 = _required_sha256(
        work_pack_sha256, "work_pack_sha256"
    )
    normalized = _normalized_candidate_reviews(decisions)

    def mutate(pack: dict[str, Any]) -> dict[str, Any]:
        submission, drifted = _candidate_submission_state(database, pack)
        if drifted:
            raise KnowledgeWorkbenchError(
                "候选包标签在提交审计后发生变化"
            )
        if not submission:
            raise KnowledgeWorkbenchError("候选标签尚未提交，不能复核")
        validate_review_actor_policy(
            submitter=submission["actor"],
            reviewer=actor,
            review_mode=review_mode,
        )
        for item in normalized:
            candidate = _candidate_by_id(pack, item["candidate_id"])
            candidate["review"] = {
                "decision": item["decision"],
                "note": item["note"],
            }
        return {
            "event_type": "conflict_candidate_review_batch_applied",
            "details": {
                "candidate_ids": [
                    item["candidate_id"] for item in normalized
                ],
                "plan_id": plan_id,
                "batch_id": batch_id,
                "work_pack_path": work_pack_path,
                "work_pack_sha256": work_pack_sha256,
                "candidate_count": len(normalized),
                "approved_count": sum(
                    item["decision"] == "approved"
                    for item in normalized
                ),
                "rejected_count": sum(
                    item["decision"] == "rejected"
                    for item in normalized
                ),
                "decision_by_candidate": {
                    item["candidate_id"]: item["decision"]
                    for item in normalized
                },
                "annotator": submission["actor"],
                **review_audit_context(
                    review_mode, solo_attestation_sha256
                ),
                "note_sha256_by_candidate": {
                    item["candidate_id"]: sha256_text(item["note"])
                    for item in normalized
                    if item["note"]
                },
            },
        }

    return _mutate_candidate_pack(
        database,
        paths,
        pack_id,
        expected_content_sha256=expected_content_sha256,
        actor=actor,
        mutate=mutate,
    )


def create_cross_document_candidate_pack(
    database: Database,
    paths: WorkspacePaths,
    output: Path,
    *,
    actor: str,
    limit: int = 200,
    minimum_similarity: float = 0.55,
) -> dict[str, Any]:
    actor = _required_actor(actor)
    if limit < 1 or limit > 5000:
        raise KnowledgeWorkbenchError("候选数量必须在 1 到 5000 之间")
    if not 0 <= minimum_similarity <= 1:
        raise KnowledgeWorkbenchError("minimum_similarity 必须在 0 到 1 之间")
    output = _validated_output(paths, output)

    with database.connect() as connection:
        rows = connection.execute(
            """
            SELECT e.id, e.excerpt, e.run_ordinal,
                   d.id AS document_id, d.original_name, d.classification,
                   dv.id AS document_version_id, dv.sha256,
                   pr.id AS processing_run_id
            FROM evidence e
            JOIN processing_runs pr
              ON pr.id = e.processing_run_id AND pr.is_current = 1
            JOIN document_versions dv ON dv.id = e.document_version_id
            JOIN documents d
              ON d.id = dv.document_id AND d.current_version_id = dv.id
            WHERE e.status NOT IN ('deprecated', 'archived')
            ORDER BY d.id, e.run_ordinal, e.id
            """
        ).fetchall()
        restricted_count = sum(row["classification"] == "restricted" for row in rows)
        eligible = [row for row in rows if row["classification"] != "restricted"]
        locators = _location_map(connection, [row["id"] for row in eligible])

    candidates = _cross_document_candidates(
        eligible,
        locators,
        minimum_similarity=minimum_similarity,
    )
    total_candidate_count = len(candidates)
    selected = candidates[:limit]
    pack = {
        "schema_version": "1.0",
        "kind": "cross-document-conflict-candidate-pack",
        "pack_id": "pending",
        "generated_at": utc_now(),
        "generated_by": actor,
        "minimum_similarity": minimum_similarity,
        "statistics": {
            "eligible_evidence_count": len(eligible),
            "restricted_evidence_excluded": restricted_count,
            "total_candidate_count": total_candidate_count,
            "exported_candidate_count": len(selected),
            "truncated": total_candidate_count > len(selected),
        },
        "candidates": selected,
    }
    pack["pack_id"] = _pack_id(pack)
    validate_conflict_candidate_pack(pack)
    content = json.dumps(pack, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    write_text_atomic(output, content)
    try:
        with database.transaction() as connection:
            record_event(
                connection,
                "conflict_candidate_pack_created",
                "conflict_candidate_pack",
                pack["pack_id"],
                actor=actor,
                details={
                    "output": output.relative_to(paths.root).as_posix(),
                    "eligible_evidence_count": len(eligible),
                    "restricted_evidence_excluded": restricted_count,
                    "total_candidate_count": total_candidate_count,
                    "exported_candidate_count": len(selected),
                    "minimum_similarity": minimum_similarity,
                },
            )
    except Exception:
        output.unlink(missing_ok=True)
        raise
    return pack


def submit_cross_document_candidate_annotations(
    database: Database,
    paths: WorkspacePaths,
    pack_path: Path,
    *,
    actor: str,
) -> dict[str, Any]:
    actor = _required_actor(actor)
    pack = _read_candidate_pack(paths, pack_path)
    _validate_pack_identity(pack)
    _validate_pack_provenance(database, pack)
    return _record_candidate_annotation_submission(database, pack, actor=actor)


def _record_candidate_annotation_submission(
    database: Database, pack: dict[str, Any], *, actor: str
) -> dict[str, Any]:
    if pack["statistics"]["truncated"]:
        raise KnowledgeWorkbenchError("候选包已截断，不能提交为完整质量基线")
    _validated_labels(pack)
    for candidate in pack["candidates"]:
        review = candidate["review"]
        if review["decision"] is not None or (review["note"] or "").strip():
            raise KnowledgeWorkbenchError(
                f"候选 {candidate['candidate_id']} 在标注提交前不能填写复核结果"
            )
    annotation_sha256 = _annotation_sha256(pack)
    with database.transaction() as connection:
        record_event(
            connection,
            "conflict_candidate_annotations_submitted",
            "conflict_candidate_pack",
            pack["pack_id"],
            actor=actor,
            details={
                "annotation_sha256": annotation_sha256,
                "candidate_count": len(pack["candidates"]),
            },
        )
    return {
        "pack_id": pack["pack_id"],
        "annotation_sha256": annotation_sha256,
        "candidate_count": len(pack["candidates"]),
        "annotator": actor,
    }


def finalize_cross_document_candidate_pack(
    database: Database,
    paths: WorkspacePaths,
    pack_path: Path,
    output: Path,
    *,
    name: str,
    reviewer: str,
    review_mode: str = INDEPENDENT_REVIEW_MODE,
    solo_attestation: str | None = None,
) -> dict[str, Any]:
    reviewer = _required_actor(reviewer)
    attestation_sha256 = validate_review_attestation(
        review_mode, solo_attestation
    )
    dataset_name = name.strip()
    if not dataset_name:
        raise KnowledgeWorkbenchError("数据集名称不能为空")
    pack = _read_candidate_pack(paths, pack_path)
    _validate_pack_identity(pack)
    _validate_pack_provenance(database, pack)
    if pack["statistics"]["truncated"]:
        raise KnowledgeWorkbenchError("候选包已截断，不能固化为完整质量基线")

    cases = _validated_labels(pack)
    annotation_sha256 = _annotation_sha256(pack)
    annotator = _submitted_annotator(
        database,
        pack_id=pack["pack_id"],
        annotation_sha256=annotation_sha256,
    )
    if annotator is None:
        raise KnowledgeWorkbenchError("当前候选标签尚未通过 submit-pack 提交审计")
    validate_review_actor_policy(
        submitter=annotator,
        reviewer=reviewer,
        review_mode=review_mode,
    )
    for candidate in pack["candidates"]:
        review = candidate["review"]
        if review["decision"] != "approved":
            raise KnowledgeWorkbenchError(
                f"候选 {candidate['candidate_id']} 尚未复核通过"
            )
    output = _validated_output(paths, output)
    dataset = {"schema_version": "1.0", "name": dataset_name, "cases": cases}
    validate_conflict_evaluation_dataset(dataset)
    content = json.dumps(dataset, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    write_text_atomic(output, content)
    try:
        with database.transaction() as connection:
            record_event(
                connection,
                "conflict_dataset_finalized",
                "conflict_evaluation_dataset",
                sha256_text(content),
                actor=reviewer,
                details={
                    "output": output.relative_to(paths.root).as_posix(),
                    "pack_sha256": sha256_text(
                        json.dumps(pack, ensure_ascii=False, sort_keys=True)
                    ),
                    "annotation_sha256": annotation_sha256,
                    "case_count": len(cases),
                    "annotator": annotator,
                    "reviewer": reviewer,
                    **review_audit_context(
                        review_mode, attestation_sha256
                    ),
                },
            )
    except Exception:
        output.unlink(missing_ok=True)
        raise
    return dataset


def _candidate_pack_records(
    paths: WorkspacePaths,
) -> tuple[list[tuple[Path, dict[str, Any]]], int]:
    evaluations = paths.evaluations.resolve()
    if not evaluations.exists():
        return [], 0
    records: list[tuple[Path, dict[str, Any]]] = []
    invalid_count = 0
    seen: set[str] = set()
    for path in sorted(evaluations.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict) or payload.get("kind") != (
            "cross-document-conflict-candidate-pack"
        ):
            continue
        try:
            validate_conflict_candidate_pack(payload)
            _validate_pack_identity(payload)
        except KnowledgeWorkbenchError:
            invalid_count += 1
            continue
        if payload["pack_id"] in seen:
            raise KnowledgeWorkbenchError(
                f"工作区存在重复候选包身份：{payload['pack_id']}"
            )
        seen.add(payload["pack_id"])
        records.append((path.resolve(), payload))
    return records, invalid_count


def _candidate_pack_by_id(
    paths: WorkspacePaths, pack_id: str
) -> tuple[Path, dict[str, Any]]:
    if not isinstance(pack_id, str) or not pack_id.startswith("cpack_"):
        raise KnowledgeWorkbenchError("冲突候选包 ID 无效")
    records, _ = _candidate_pack_records(paths)
    matches = [record for record in records if record[1]["pack_id"] == pack_id]
    if not matches:
        raise KnowledgeWorkbenchError(f"冲突候选包不存在或校验失败：{pack_id}")
    if len(matches) != 1:
        raise KnowledgeWorkbenchError(f"工作区存在重复候选包身份：{pack_id}")
    return matches[0]


def _candidate_submission_state(
    database: Database, pack: dict[str, Any]
) -> tuple[dict[str, str] | None, bool]:
    submission = _latest_annotation_submission(database, pack["pack_id"])
    if not submission:
        return None, False
    return submission, submission["annotation_sha256"] != _annotation_sha256(pack)


def _latest_annotation_submission(
    database: Database, pack_id: str
) -> dict[str, str] | None:
    with database.connect() as connection:
        rows = connection.execute(
            """
            SELECT actor, details_json FROM audit_log
            WHERE event_type = 'conflict_candidate_annotations_submitted'
              AND entity_type = 'conflict_candidate_pack'
              AND entity_id = ?
            ORDER BY id DESC
            """,
            (pack_id,),
        ).fetchall()
    for row in rows:
        try:
            details = json.loads(row["details_json"])
        except json.JSONDecodeError:
            continue
        annotation_sha256 = details.get("annotation_sha256")
        if isinstance(annotation_sha256, str):
            return {"actor": row["actor"], "annotation_sha256": annotation_sha256}
    return None


def _candidate_counts(pack: dict[str, Any]) -> dict[str, int]:
    candidates = pack["candidates"]
    return {
        "total": len(candidates),
        "labeled": sum(
            item["label"]["expected_conflict"] is not None for item in candidates
        ),
        "approved": sum(
            item["review"]["decision"] == "approved" for item in candidates
        ),
        "rejected": sum(
            item["review"]["decision"] == "rejected" for item in candidates
        ),
    }


def _candidate_pack_phase(
    submission: dict[str, str] | None,
    drifted: bool,
    counts: dict[str, int],
) -> str:
    if drifted:
        return "invalid"
    if not submission:
        return "labeling"
    if counts["approved"] == counts["total"] and counts["total"]:
        return "reviewed"
    return "reviewing"


def _candidate_pagination(limit: int, offset: int) -> tuple[int, int]:
    if limit < 1 or limit > _WEB_PAGE_LIMIT_MAXIMUM:
        raise KnowledgeWorkbenchError(
            f"limit 必须在 1 到 {_WEB_PAGE_LIMIT_MAXIMUM} 之间"
        )
    if offset < 0:
        raise KnowledgeWorkbenchError("offset 不能小于 0")
    return limit, offset


def _candidate_state_filter(value: str | None) -> str:
    state = (value or "all").strip()
    allowed = {
        "all",
        "unlabeled",
        "labeled",
        "unreviewed",
        "approved",
        "rejected",
        "disagreement",
    }
    if state not in allowed:
        raise KnowledgeWorkbenchError(f"候选状态筛选不受支持：{state}")
    return state


def _candidate_query(value: str | None) -> str:
    query = (value or "").strip()
    if len(query) > 120:
        raise KnowledgeWorkbenchError("q 不能超过 120 个字符")
    return query


def _candidate_matches(
    candidate: dict[str, Any], *, state: str, query: str
) -> bool:
    label = candidate["label"]
    review = candidate["review"]
    if state == "unlabeled" and label["expected_conflict"] is not None:
        return False
    if state == "labeled" and label["expected_conflict"] is None:
        return False
    if state == "unreviewed" and review["decision"] is not None:
        return False
    if state in {"approved", "rejected"} and review["decision"] != state:
        return False
    if state == "disagreement":
        if label["expected_conflict"] is None:
            return False
        if (
            label["expected_conflict"] == candidate["predicted_conflict"]
            and label["expected_type"] == candidate["predicted_type"]
        ):
            return False
    if not query:
        return True
    needle = query.casefold()
    searchable = [candidate["candidate_id"], candidate.get("reason") or ""]
    for side_name in ("left", "right"):
        side = candidate[side_name]
        searchable.extend(
            [side["evidence_id"], side["document_name"], side["excerpt"]]
        )
    return any(needle in value.casefold() for value in searchable)


def _candidate_by_id(
    pack: dict[str, Any], candidate_id: str
) -> dict[str, Any]:
    for candidate in pack["candidates"]:
        if candidate["candidate_id"] == candidate_id:
            return candidate
    raise KnowledgeWorkbenchError(f"候选不存在：{candidate_id}")


def _candidate_note(value: str | None, *, required: bool) -> str | None:
    if value is not None and not isinstance(value, str):
        raise KnowledgeWorkbenchError("note 必须是字符串或 null")
    note = (value or "").strip()
    if required and not note:
        raise KnowledgeWorkbenchError("驳回复核必须填写意见")
    if len(note) > 2000:
        raise KnowledgeWorkbenchError("note 不能超过 2000 个字符")
    return note or None


def _required_work_scope_id(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise KnowledgeWorkbenchError(f"{name} 不能为空")
    normalized = value.strip()
    if len(normalized) > 100:
        raise KnowledgeWorkbenchError(f"{name} 不能超过 100 个字符")
    return normalized


def _required_audit_text(
    value: str, name: str, *, maximum: int
) -> str:
    if not isinstance(value, str) or not value.strip():
        raise KnowledgeWorkbenchError(f"{name} 不能为空")
    normalized = value.strip()
    if len(normalized) > maximum:
        raise KnowledgeWorkbenchError(
            f"{name} 不能超过 {maximum} 个字符"
        )
    return normalized


def _required_sha256(value: str, name: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise KnowledgeWorkbenchError(f"{name} 必须是小写 SHA-256")
    return value


def _normalized_candidate_labels(
    labels: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not isinstance(labels, list) or not labels:
        raise KnowledgeWorkbenchError("批次标签不能为空")
    if len(labels) > 200:
        raise KnowledgeWorkbenchError("单批次标签不能超过 200 条")
    normalized = []
    seen: set[str] = set()
    for item in labels:
        if not isinstance(item, dict):
            raise KnowledgeWorkbenchError("批次标签必须是对象")
        candidate_id = item.get("candidate_id")
        if not isinstance(candidate_id, str) or not candidate_id:
            raise KnowledgeWorkbenchError("批次标签缺少 candidate_id")
        if candidate_id in seen:
            raise KnowledgeWorkbenchError(
                f"批次标签包含重复候选：{candidate_id}"
            )
        seen.add(candidate_id)
        expected_conflict = item.get("expected_conflict")
        expected_type = item.get("expected_type")
        if not isinstance(expected_conflict, bool):
            raise KnowledgeWorkbenchError(
                f"候选 {candidate_id} 必须明确标注冲突或非冲突"
            )
        if expected_conflict and expected_type not in _CONFLICT_TYPES:
            raise KnowledgeWorkbenchError(
                f"候选 {candidate_id} 标记冲突时必须指定有效类型"
            )
        if not expected_conflict and expected_type is not None:
            raise KnowledgeWorkbenchError(
                f"候选 {candidate_id} 标记非冲突时类型必须为 null"
            )
        normalized.append(
            {
                "candidate_id": candidate_id,
                "expected_conflict": expected_conflict,
                "expected_type": expected_type,
                "note": _candidate_note(
                    item.get("note"), required=False
                ),
            }
        )
    return normalized


def _normalized_candidate_reviews(
    decisions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not isinstance(decisions, list) or not decisions:
        raise KnowledgeWorkbenchError("批次复核决定不能为空")
    if len(decisions) > 200:
        raise KnowledgeWorkbenchError("单批次复核不能超过 200 条")
    normalized = []
    seen: set[str] = set()
    for item in decisions:
        if not isinstance(item, dict):
            raise KnowledgeWorkbenchError("批次复核决定必须是对象")
        candidate_id = item.get("candidate_id")
        if not isinstance(candidate_id, str) or not candidate_id:
            raise KnowledgeWorkbenchError("批次复核缺少 candidate_id")
        if candidate_id in seen:
            raise KnowledgeWorkbenchError(
                f"批次复核包含重复候选：{candidate_id}"
            )
        seen.add(candidate_id)
        decision = item.get("decision")
        if decision not in {"approved", "rejected"}:
            raise KnowledgeWorkbenchError(
                f"候选 {candidate_id} 必须选择批准或驳回"
            )
        normalized.append(
            {
                "candidate_id": candidate_id,
                "decision": decision,
                "note": _candidate_note(
                    item.get("note"),
                    required=decision == "rejected",
                ),
            }
        )
    return normalized


def _require_content_sha256(path: Path, expected: str) -> None:
    if not isinstance(expected, str) or len(expected) != 64:
        raise KnowledgeWorkbenchError("expected_content_sha256 无效")
    if sha256_file(path) != expected:
        raise KnowledgeWorkbenchError("候选包已被其他操作修改，请刷新后重试")


def _read_candidate_pack_at_sha256(
    path: Path, pack_id: str, expected_content_sha256: str
) -> tuple[dict[str, Any], str]:
    _require_content_sha256(path, expected_content_sha256)
    try:
        content_bytes = path.read_bytes()
        if hashlib.sha256(content_bytes).hexdigest() != expected_content_sha256:
            raise KnowledgeWorkbenchError(
                "候选包已被其他操作修改，请刷新后重试"
            )
        content = content_bytes.decode("utf-8")
        pack = json.loads(content)
    except KnowledgeWorkbenchError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise KnowledgeWorkbenchError("冲突候选包无法安全读取") from exc
    validate_conflict_candidate_pack(pack)
    _validate_pack_identity(pack)
    if pack["pack_id"] != pack_id:
        raise KnowledgeWorkbenchError("候选包身份在读取期间发生变化")
    return pack, content


def _mutate_candidate_pack(
    database: Database,
    paths: WorkspacePaths,
    pack_id: str,
    *,
    expected_content_sha256: str,
    actor: str,
    mutate,
) -> dict[str, Any]:
    with _CANDIDATE_PACK_WRITE_LOCK:
        path, _ = _candidate_pack_by_id(paths, pack_id)
        pack, original_content = _read_candidate_pack_at_sha256(
            path, pack_id, expected_content_sha256
        )
        _validate_pack_provenance(database, pack)
        event = mutate(pack)
        _validate_pack_identity(pack)
        validate_conflict_candidate_pack(pack)
        content = json.dumps(pack, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        before_sha256 = sha256_text(original_content)
        after_sha256 = sha256_text(content)
        _require_content_sha256(path, expected_content_sha256)
        write_text_atomic(path, content)
        try:
            with database.transaction() as connection:
                details = dict(event["details"])
                details.update(
                    {
                        "content_sha256_before": before_sha256,
                        "content_sha256_after": after_sha256,
                    }
                )
                record_event(
                    connection,
                    event["event_type"],
                    "conflict_candidate_pack",
                    pack_id,
                    actor=actor,
                    details=details,
                )
        except Exception:
            write_text_atomic(path, original_content)
            raise
        submission, drifted = _candidate_submission_state(database, pack)
        result = {
            "pack_id": pack_id,
            "content_sha256": after_sha256,
            "phase": _candidate_pack_phase(
                submission, drifted, _candidate_counts(pack)
            ),
            "actor": actor,
        }
        for key in ("candidate_id", "candidate_ids", "plan_id", "batch_id"):
            if key in event["details"]:
                result[key] = event["details"][key]
        return result


def _validated_labels(pack: dict[str, Any]) -> list[dict[str, Any]]:
    cases = []
    for candidate in pack["candidates"]:
        label = candidate["label"]
        expected_conflict = label["expected_conflict"]
        expected_type = label["expected_type"]
        if expected_conflict is None:
            raise KnowledgeWorkbenchError(
                f"候选 {candidate['candidate_id']} 尚未填写冲突标注"
            )
        if expected_conflict and expected_type is None:
            raise KnowledgeWorkbenchError(
                f"候选 {candidate['candidate_id']} 标记为冲突时必须填写类型"
            )
        if not expected_conflict and expected_type is not None:
            raise KnowledgeWorkbenchError(
                f"候选 {candidate['candidate_id']} 标记为非冲突时类型必须为 null"
            )
        cases.append(
            {
                "case_id": candidate["candidate_id"],
                "older": candidate["left"]["excerpt"],
                "newer": candidate["right"]["excerpt"],
                "expected_conflict": expected_conflict,
                "expected_type": expected_type,
            }
        )
    if not cases:
        raise KnowledgeWorkbenchError("候选包不包含可固化的用例")
    return cases


def _read_candidate_pack(paths: WorkspacePaths, pack_path: Path) -> dict[str, Any]:
    pack_path = pack_path.expanduser().resolve()
    try:
        pack_path.relative_to(paths.evaluations.resolve())
    except ValueError as exc:
        raise KnowledgeWorkbenchError(
            "冲突候选包只能从当前 workspace/evaluations 内读取"
        ) from exc
    try:
        pack = json.loads(pack_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise KnowledgeWorkbenchError(f"冲突候选包不存在：{pack_path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise KnowledgeWorkbenchError(f"冲突候选包不是有效 JSON：{exc}") from exc
    validate_conflict_candidate_pack(pack)
    return pack


def _pack_id(pack: dict[str, Any]) -> str:
    return "cpack_" + candidate_pack_identity_sha256(pack)[:24]


def candidate_pack_identity_sha256(pack: dict[str, Any]) -> str:
    """Hash immutable candidate/source metadata, excluding human decisions."""

    identity = {
        key: value
        for key, value in pack.items()
        if key not in {"pack_id", "candidates"}
    }
    identity["candidates"] = [
        {
            key: value
            for key, value in candidate.items()
            if key not in {"label", "review"}
        }
        for candidate in pack["candidates"]
    ]
    canonical = json.dumps(
        identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return sha256_text(canonical)


def _validate_pack_identity(pack: dict[str, Any]) -> None:
    if pack["pack_id"] != _pack_id(pack):
        raise KnowledgeWorkbenchError("候选包身份校验失败；候选内容或来源元数据已被修改")


def _validate_pack_provenance(database: Database, pack: dict[str, Any]) -> None:
    expected: dict[str, dict[str, Any]] = {}
    for candidate in pack["candidates"]:
        for side in (candidate["left"], candidate["right"]):
            evidence_id = side["evidence_id"]
            existing = expected.get(evidence_id)
            if existing is not None and existing != side:
                raise KnowledgeWorkbenchError(
                    f"候选包中的证据 {evidence_id} 出现不一致来源投影"
                )
            expected[evidence_id] = side
    with database.connect() as connection:
        created = connection.execute(
            """
            SELECT 1 FROM audit_log
            WHERE event_type = 'conflict_candidate_pack_created'
              AND entity_type = 'conflict_candidate_pack'
              AND entity_id = ?
            LIMIT 1
            """,
            (pack["pack_id"],),
        ).fetchone()
        if not created:
            raise KnowledgeWorkbenchError("候选包缺少生成审计，不能提交或固化")
        current_rows = []
        evidence_ids = sorted(expected)
        for start in range(0, len(evidence_ids), 500):
            batch = evidence_ids[start : start + 500]
            placeholders = ",".join("?" for _ in batch)
            current_rows.extend(
                connection.execute(
                    f"""
                    SELECT e.id, e.excerpt, e.run_ordinal,
                           d.id AS document_id, d.original_name, d.classification,
                           dv.id AS document_version_id,
                           pr.id AS processing_run_id
                    FROM evidence e
                    JOIN processing_runs pr
                      ON pr.id = e.processing_run_id AND pr.is_current = 1
                    JOIN document_versions dv ON dv.id = e.document_version_id
                    JOIN documents d
                      ON d.id = dv.document_id AND d.current_version_id = dv.id
                    WHERE e.id IN ({placeholders})
                      AND e.status NOT IN ('deprecated', 'archived')
                      AND d.classification != 'restricted'
                    """,
                    tuple(batch),
                ).fetchall()
            )
        locators = _location_map(connection, evidence_ids)
    current = {
        row["id"]: _evidence_projection(row, locators) for row in current_rows
    }
    for evidence_id, expected_projection in expected.items():
        if current.get(evidence_id) != expected_projection:
            raise KnowledgeWorkbenchError(
                f"候选证据 {evidence_id} 已过期、受限或来源内容发生变化"
            )


def _annotation_sha256(pack: dict[str, Any]) -> str:
    annotation = [
        {"candidate_id": item["candidate_id"], "label": item["label"]}
        for item in pack["candidates"]
    ]
    canonical = json.dumps(
        annotation, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return sha256_text(canonical)


def _submitted_annotator(
    database: Database,
    *,
    pack_id: str,
    annotation_sha256: str,
) -> str | None:
    with database.connect() as connection:
        rows = connection.execute(
            """
            SELECT actor, details_json FROM audit_log
            WHERE event_type = 'conflict_candidate_annotations_submitted'
              AND entity_type = 'conflict_candidate_pack'
              AND entity_id = ?
            ORDER BY id DESC
            """,
            (pack_id,),
        ).fetchall()
    for row in rows:
        try:
            details = json.loads(row["details_json"])
        except json.JSONDecodeError:
            continue
        if details.get("annotation_sha256") == annotation_sha256:
            return row["actor"]
    return None


def _cross_document_candidates(rows, locators, *, minimum_similarity: float) -> list[dict]:
    blocked_pairs: set[tuple[int, int]] = set()
    blocks: dict[str, list[int]] = defaultdict(list)
    for index, row in enumerate(rows):
        signature = _comparison_signature(row["excerpt"])
        if len(signature) >= 4:
            blocks[f"signature:{signature}"].append(index)
        for shingle in _shingles(_normalize(row["excerpt"])):
            blocks[f"shingle:{shingle}"].append(index)
    for indexes in blocks.values():
        if len(indexes) > 200:
            continue
        for left_index, right_index in combinations(indexes, 2):
            if rows[left_index]["document_id"] == rows[right_index]["document_id"]:
                continue
            blocked_pairs.add((left_index, right_index))

    output = []
    for left_index, right_index in blocked_pairs:
        left = rows[left_index]
        right = rows[right_index]
        left_normalized = _normalize(left["excerpt"])
        right_normalized = _normalize(right["excerpt"])
        similarity = SequenceMatcher(None, left_normalized, right_normalized).ratio()
        if similarity < minimum_similarity:
            continue
        classified = _classify_conflict(left["excerpt"], right["excerpt"])
        evidence_ids = sorted((left["id"], right["id"]))
        candidate_id = "xdoc_" + sha256_text(":".join(evidence_ids))[:20]
        output.append(
            {
                "candidate_id": candidate_id,
                "left": _evidence_projection(left, locators),
                "right": _evidence_projection(right, locators),
                "predicted_conflict": classified is not None,
                "predicted_type": classified[0] if classified else None,
                "similarity": round(similarity, 6),
                "reason": classified[2] if classified else None,
                "label": {
                    "expected_conflict": None,
                    "expected_type": None,
                    "note": None,
                },
                "review": {"decision": None, "note": None},
            }
        )
    output.sort(
        key=lambda item: (
            not item["predicted_conflict"],
            -item["similarity"],
            item["candidate_id"],
        )
    )
    return output


def _evidence_projection(row, locators) -> dict[str, Any]:
    return {
        "evidence_id": row["id"],
        "document_id": row["document_id"],
        "document_name": row["original_name"],
        "document_version_id": row["document_version_id"],
        "processing_run_id": row["processing_run_id"],
        "classification": row["classification"],
        "excerpt": row["excerpt"],
        "locators": locators.get(row["id"], []),
    }


def _location_map(connection, evidence_ids: list[str]) -> dict[str, list[dict]]:
    if not evidence_ids:
        return {}
    output: dict[str, list[dict]] = defaultdict(list)
    for start in range(0, len(evidence_ids), 500):
        batch = evidence_ids[start : start + 500]
        placeholders = ",".join("?" for _ in batch)
        rows = connection.execute(
            f"""
            SELECT evidence_id, locator_json FROM evidence_locations
            WHERE evidence_id IN ({placeholders})
            ORDER BY evidence_id, location_ordinal
            """,
            tuple(batch),
        ).fetchall()
        for row in rows:
            output[row["evidence_id"]].append(json.loads(row["locator_json"]))
    return dict(output)


def _shingles(value: str) -> set[str]:
    if len(value) < 3:
        return {value} if value else set()
    return {value[index : index + 3] for index in range(len(value) - 2)}


def _validated_output(paths: WorkspacePaths, output: Path) -> Path:
    output = output.expanduser().resolve()
    try:
        output.relative_to(paths.raw.resolve())
    except ValueError:
        pass
    else:
        raise KnowledgeWorkbenchError("不能向只读原始资料目录写入冲突标注产物")
    try:
        output.relative_to(paths.evaluations.resolve())
    except ValueError as exc:
        raise KnowledgeWorkbenchError(
            "冲突候选包和评测数据集只能保存到当前 workspace/evaluations 内"
        ) from exc
    if output.exists():
        raise KnowledgeWorkbenchError(f"输出文件已存在，不会覆盖：{output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    return output


def _required_actor(value: str) -> str:
    if not isinstance(value, str):
        raise KnowledgeWorkbenchError("actor 必须是字符串")
    actor = value.strip()
    if not actor:
        raise KnowledgeWorkbenchError("actor 不能为空")
    if len(actor) > 80:
        raise KnowledgeWorkbenchError("actor 不能超过 80 个字符")
    return actor
