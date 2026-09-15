from __future__ import annotations

import json
import math
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from .audit import record_event
from .config import WorkspacePaths
from .conflict_candidates import (
    _candidate_pack_by_id,
    _read_candidate_pack,
    _validate_pack_identity,
    _validate_pack_provenance,
    candidate_pack_identity_sha256,
)
from .database import Database
from .errors import KnowledgeWorkbenchError
from .review_assurance import (
    INDEPENDENT_REVIEW_MODE,
    SOLO_ATTESTED_REVIEW_MODE,
)
from .schema_validation import validate_conflict_labeling_plan
from .utils import sha256_file, sha256_text, utc_now
from .wiki import write_text_atomic


DEFAULT_CONFLICT_LABELING_PLAN_SEED = "conflict-plan-v1"
_SEED_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,80}$")


def create_conflict_labeling_plan(
    database: Database,
    paths: WorkspacePaths,
    source_pack_path: Path,
    output: Path,
    *,
    actor: str,
    batch_size: int = 60,
    seed: str = DEFAULT_CONFLICT_LABELING_PLAN_SEED,
    focus_predicted_type: str | None = None,
    focus_limit: int | None = None,
) -> dict[str, Any]:
    actor = _required_actor(actor)
    batch_size = _validated_batch_size(batch_size)
    seed = _validated_seed(seed)
    focus_predicted_type = _validated_focus_predicted_type(
        focus_predicted_type
    )
    focus_limit = _validated_focus_limit(
        focus_limit,
        batch_size=batch_size,
        focus_enabled=focus_predicted_type is not None,
    )
    output = _validated_output(paths, output)
    source_pack = _read_candidate_pack(paths, source_pack_path)
    _validate_pack_identity(source_pack)
    _validate_pack_provenance(database, source_pack)
    if source_pack["statistics"]["truncated"]:
        raise KnowledgeWorkbenchError(
            "只有未截断的完整冲突候选包可以生成完整标注批次计划"
        )
    if not source_pack["candidates"]:
        raise KnowledgeWorkbenchError("冲突候选包没有可分配的候选")

    focus_candidate_ids: list[str] = []
    if focus_predicted_type is None:
        batches = _partition_candidates(
            source_pack["candidates"],
            batch_size=batch_size,
            seed=seed,
        )
    else:
        batches, focus_candidate_ids = _partition_focused_candidates(
            source_pack["candidates"],
            batch_size=batch_size,
            seed=seed,
            predicted_type=focus_predicted_type,
            focus_limit=focus_limit,
        )
    plan = {
        "schema_version": "1.0",
        "kind": "cross-document-conflict-labeling-plan",
        "plan_id": "pending",
        "generated_at": utc_now(),
        "generated_by": actor,
        "source_pack": {
            "pack_id": source_pack["pack_id"],
            "identity_sha256": candidate_pack_identity_sha256(source_pack),
            "candidate_count": len(source_pack["candidates"]),
        },
        "batch_size": batch_size,
        "seed": seed,
        "strata": {
            "similarity_bands": {
                "high": "similarity >= 0.85",
                "medium": "0.70 <= similarity < 0.85",
                "low": "similarity < 0.70",
            },
            "dimensions": ["predicted_type", "similarity_band"],
        },
        "batches": batches,
    }
    if focus_predicted_type is not None:
        plan["focus"] = {
            "predicted_type": focus_predicted_type,
            "requested_limit": focus_limit,
            "candidate_count": len(focus_candidate_ids),
            "selection_method": (
                "unlabeled_similarity_desc_then_seeded_sha256"
            ),
        }
    plan["plan_id"] = _plan_id(plan)
    validate_conflict_labeling_plan(plan)
    content = json.dumps(
        plan, ensure_ascii=False, indent=2, sort_keys=True
    ) + "\n"
    write_text_atomic(output, content)
    try:
        with database.transaction() as connection:
            record_event(
                connection,
                "conflict_labeling_plan_created",
                "conflict_labeling_plan",
                plan["plan_id"],
                actor=actor,
                details={
                    "output": output.relative_to(
                        paths.root.resolve()
                    ).as_posix(),
                    "content_sha256": sha256_text(content),
                    "source_pack_id": source_pack["pack_id"],
                    "source_pack_identity_sha256": plan["source_pack"][
                        "identity_sha256"
                    ],
                    "candidate_count": len(source_pack["candidates"]),
                    "batch_count": len(batches),
                    "batch_size": batch_size,
                    "seed_sha256": sha256_text(seed),
                    "focus_predicted_type": focus_predicted_type,
                    "focus_requested_limit": focus_limit,
                    "focus_candidate_count": len(
                        focus_candidate_ids
                    ),
                    "focus_candidate_ids_sha256": (
                        sha256_text(
                            json.dumps(
                                focus_candidate_ids,
                                separators=(",", ":"),
                            )
                        )
                        if focus_candidate_ids
                        else None
                    ),
                },
            )
    except Exception:
        output.unlink(missing_ok=True)
        raise
    return plan


def inspect_conflict_labeling_plan(
    database: Database,
    paths: WorkspacePaths,
    plan_path: Path,
) -> dict[str, Any]:
    plan_path, content, plan = _read_plan(paths, plan_path)
    _validate_plan_audit(database, paths, plan_path, content, plan)
    source_path, source_pack = _candidate_pack_by_id(
        paths, plan["source_pack"]["pack_id"]
    )
    _validate_pack_provenance(database, source_pack)
    current_identity_sha256 = candidate_pack_identity_sha256(source_pack)
    if current_identity_sha256 != plan["source_pack"]["identity_sha256"]:
        raise KnowledgeWorkbenchError(
            "冲突候选包不可变来源身份与标注计划不一致"
        )

    candidates = {
        item["candidate_id"]: item for item in source_pack["candidates"]
    }
    review_assurance, review_audit_current = (
        _review_assurance_by_candidate(
            database,
            source_pack["pack_id"],
            current_content_sha256=sha256_file(source_path),
        )
    )
    planned_ids = [
        candidate_id
        for batch in plan["batches"]
        for candidate_id in batch["candidate_ids"]
    ]
    if set(planned_ids) != set(candidates):
        raise KnowledgeWorkbenchError(
            "冲突标注计划没有完整覆盖当前来源候选包"
        )

    items = []
    totals = Counter()
    stratum_totals: dict[str, Counter[str]] = defaultdict(Counter)
    for batch in plan["batches"]:
        counts = Counter()
        for candidate_id in batch["candidate_ids"]:
            candidate = candidates[candidate_id]
            stratum = _candidate_stratum(candidate)
            stratum_totals[stratum]["candidate_count"] += 1
            if candidate["label"]["expected_conflict"] is not None:
                counts["labeled"] += 1
                stratum_totals[stratum]["labeled_count"] += 1
                if candidate["label"]["expected_conflict"]:
                    counts["known_conflict_labeled"] += 1
                    stratum_totals[stratum][
                        "known_conflict_labeled_count"
                    ] += 1
            decision = candidate["review"]["decision"]
            if decision is not None:
                counts["reviewed"] += 1
                counts[decision] += 1
            if decision == "approved":
                audited = review_assurance.get(candidate_id)
                assurance = (
                    audited[0]
                    if audited is not None
                    and audited[1] == decision
                    else "unattributed"
                )
                counts[f"{assurance}_approved"] += 1
                stratum_totals[stratum]["approved_count"] += 1
                stratum_totals[stratum][
                    f"{assurance}_approved_count"
                ] += 1
                if (
                    assurance
                    in {
                        INDEPENDENT_REVIEW_MODE,
                        SOLO_ATTESTED_REVIEW_MODE,
                    }
                    and candidate["label"]["expected_conflict"] is True
                ):
                    counts["human_attested_known_conflict"] += 1
                    stratum_totals[stratum][
                        "human_attested_known_conflict_count"
                    ] += 1
        counts["total"] = len(batch["candidate_ids"])
        totals.update(counts)
        items.append(
            {
                "batch_id": batch["batch_id"],
                "ordinal": batch["ordinal"],
                "candidate_count": counts["total"],
                "labeled_count": counts["labeled"],
                "approved_count": counts["approved"],
                "rejected_count": counts["rejected"],
                "human_attested_approved_count": (
                    counts["independent_approved"]
                    + counts["solo_attested_approved"]
                ),
                "independent_approved_count": counts[
                    "independent_approved"
                ],
                "solo_attested_approved_count": counts[
                    "solo_attested_approved"
                ],
                "unattributed_approved_count": counts[
                    "unattributed_approved"
                ],
                "annotation_complete": counts["labeled"] == counts["total"],
                "review_complete": counts["approved"] == counts["total"],
                "independent_review_complete": (
                    counts["independent_approved"] == counts["total"]
                ),
                "stratum_counts": batch["stratum_counts"],
            }
        )
    candidate_count = len(candidates)
    return {
        "schema_version": "1.0",
        "kind": "cross-document-conflict-labeling-plan-status",
        "checked_at": utc_now(),
        "plan_id": plan["plan_id"],
        "generated_at": plan["generated_at"],
        "generated_by": plan["generated_by"],
        "source_pack_id": source_pack["pack_id"],
        "summary": {
            "candidate_count": candidate_count,
            "batch_count": len(items),
            "batch_size": plan["batch_size"],
            "labeled_count": totals["labeled"],
            "approved_count": totals["approved"],
            "rejected_count": totals["rejected"],
            "human_attested_approved_count": (
                totals["independent_approved"]
                + totals["solo_attested_approved"]
            ),
            "independent_approved_count": totals[
                "independent_approved"
            ],
            "solo_attested_approved_count": totals[
                "solo_attested_approved"
            ],
            "unattributed_approved_count": totals[
                "unattributed_approved"
            ],
            "unlabeled_count": candidate_count - totals["labeled"],
            "unreviewed_count": candidate_count - totals["reviewed"],
            "known_conflict_labeled_count": totals[
                "known_conflict_labeled"
            ],
            "human_attested_known_conflict_count": totals[
                "human_attested_known_conflict"
            ],
            "annotation_complete": totals["labeled"] == candidate_count,
            "review_complete": totals["approved"] == candidate_count,
            "human_attested_review_complete": (
                totals["approved"] == candidate_count
                and (
                    totals["independent_approved"]
                    + totals["solo_attested_approved"]
                )
                == candidate_count
            ),
            "independent_review_complete": (
                totals["independent_approved"] == candidate_count
            ),
            "review_audit_current": review_audit_current,
            "stratum_status": {
                stratum: {
                    "candidate_count": counts["candidate_count"],
                    "labeled_count": counts["labeled_count"],
                    "approved_count": counts["approved_count"],
                    "human_attested_approved_count": (
                        counts["independent_approved_count"]
                        + counts["solo_attested_approved_count"]
                    ),
                    "independent_approved_count": counts[
                        "independent_approved_count"
                    ],
                    "solo_attested_approved_count": counts[
                        "solo_attested_approved_count"
                    ],
                    "unattributed_approved_count": counts[
                        "unattributed_approved_count"
                    ],
                    "known_conflict_labeled_count": counts[
                        "known_conflict_labeled_count"
                    ],
                    "human_attested_known_conflict_count": counts[
                        "human_attested_known_conflict_count"
                    ],
                }
                for stratum, counts in sorted(stratum_totals.items())
            },
        },
        "items": items,
    }


def _review_assurance_by_candidate(
    database: Database,
    pack_id: str,
    *,
    current_content_sha256: str,
) -> tuple[dict[str, tuple[str, str | None]], bool]:
    with database.connect() as connection:
        rows = connection.execute(
            """
            SELECT actor, event_type, details_json
            FROM audit_log
            WHERE entity_type = 'conflict_candidate_pack'
              AND entity_id = ?
              AND event_type IN (
                'conflict_candidate_label_updated',
                'conflict_candidate_label_batch_applied',
                'conflict_candidate_review_updated',
                'conflict_candidate_review_batch_applied'
              )
            ORDER BY id DESC
            """,
            (pack_id,),
        ).fetchall()
    if not rows:
        return {}, True

    assurance: dict[str, tuple[str, str | None]] = {}
    resolved_candidates: set[str] = set()
    expected_after_sha256 = current_content_sha256
    for index, row in enumerate(rows):
        try:
            details = json.loads(row["details_json"])
        except (TypeError, json.JSONDecodeError):
            return {}, False
        if not isinstance(details.get("content_sha256_before"), str):
            return {}, False
        if details.get("content_sha256_after") != expected_after_sha256:
            if index == 0:
                return {}, False
            break
        expected_after_sha256 = details["content_sha256_before"]

        is_batch = row["event_type"] in {
            "conflict_candidate_label_batch_applied",
            "conflict_candidate_review_batch_applied",
        }
        candidate_ids = (
            details.get("candidate_ids")
            if is_batch
            else [details.get("candidate_id")]
        )
        if not isinstance(candidate_ids, list):
            return {}, False
        candidate_ids = [
            candidate_id
            for candidate_id in candidate_ids
            if isinstance(candidate_id, str)
        ]
        if not candidate_ids:
            return {}, False

        if row["event_type"] in {
            "conflict_candidate_label_updated",
            "conflict_candidate_label_batch_applied",
        }:
            resolved_candidates.update(candidate_ids)
            continue

        annotator = details.get("annotator")
        mode = details.get("review_mode")
        if mode not in {
            INDEPENDENT_REVIEW_MODE,
            SOLO_ATTESTED_REVIEW_MODE,
        }:
            mode = (
                INDEPENDENT_REVIEW_MODE
                if isinstance(annotator, str)
                and row["actor"] != annotator
                else "unattributed"
            )
        if (
            mode == INDEPENDENT_REVIEW_MODE
            and row["actor"] == annotator
        ) or (
            mode == SOLO_ATTESTED_REVIEW_MODE
            and row["actor"] != annotator
        ):
            mode = "unattributed"
        decision_by_candidate = details.get("decision_by_candidate")
        if not isinstance(decision_by_candidate, dict):
            decision_by_candidate = {}
        uniform_batch_decision = None
        if is_batch:
            candidate_count = details.get("candidate_count")
            if (
                isinstance(candidate_count, int)
                and details.get("approved_count") == candidate_count
            ):
                uniform_batch_decision = "approved"
            elif (
                isinstance(candidate_count, int)
                and details.get("rejected_count") == candidate_count
            ):
                uniform_batch_decision = "rejected"
        for candidate_id in candidate_ids:
            if candidate_id not in resolved_candidates:
                decision = (
                    decision_by_candidate.get(candidate_id)
                    if is_batch
                    else details.get("decision")
                )
                if decision not in {"approved", "rejected"}:
                    decision = uniform_batch_decision
                assurance[candidate_id] = (mode, decision)
                resolved_candidates.add(candidate_id)
    return assurance, True


def list_conflict_labeling_plans(
    database: Database,
    paths: WorkspacePaths,
    *,
    source_pack_id: str | None = None,
) -> dict[str, Any]:
    normalized_source_pack_id = (source_pack_id or "").strip() or None
    items = []
    invalid_plan_count = 0
    seen: set[str] = set()
    for plan_path in sorted(paths.evaluations.glob("*.json")):
        try:
            raw = json.loads(plan_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if (
            not isinstance(raw, dict)
            or raw.get("kind")
            != "cross-document-conflict-labeling-plan"
        ):
            continue
        try:
            status = inspect_conflict_labeling_plan(
                database, paths, plan_path
            )
        except KnowledgeWorkbenchError:
            invalid_plan_count += 1
            continue
        if status["plan_id"] in seen:
            invalid_plan_count += 1
            continue
        seen.add(status["plan_id"])
        if (
            normalized_source_pack_id is not None
            and status["source_pack_id"] != normalized_source_pack_id
        ):
            continue
        items.append(
            {
                "plan_id": status["plan_id"],
                "generated_at": status["generated_at"],
                "generated_by": status["generated_by"],
                "source_pack_id": status["source_pack_id"],
                "summary": status["summary"],
                "batches": status["items"],
            }
        )
    items.sort(
        key=lambda item: (item["generated_at"], item["plan_id"]),
        reverse=True,
    )
    return {
        "items": items,
        "total": len(items),
        "invalid_plan_count": invalid_plan_count,
    }


def resolve_conflict_labeling_batch(
    database: Database,
    paths: WorkspacePaths,
    plan_id: str,
    batch_id: str,
) -> dict[str, Any]:
    plan_path = _find_plan_path(paths, plan_id)
    status = inspect_conflict_labeling_plan(database, paths, plan_path)
    _, _, plan = _read_plan(paths, plan_path)
    batches = {
        item["batch_id"]: item for item in plan["batches"]
    }
    batch = batches.get(batch_id)
    if batch is None:
        raise KnowledgeWorkbenchError(
            f"冲突标注批次不存在：{batch_id}"
        )
    status_items = {
        item["batch_id"]: item for item in status["items"]
    }
    return {
        "plan_id": plan["plan_id"],
        "source_pack_id": plan["source_pack"]["pack_id"],
        "batch_id": batch["batch_id"],
        "candidate_ids": batch["candidate_ids"],
        "status": status_items[batch["batch_id"]],
    }


def _partition_candidates(
    candidates: list[dict[str, Any]],
    *,
    batch_size: int,
    seed: str,
) -> list[dict[str, Any]]:
    batch_count = math.ceil(len(candidates) / batch_size)
    even_size, larger_batch_count = divmod(len(candidates), batch_count)
    capacities = [
        even_size + (1 if index < larger_batch_count else 0)
        for index in range(batch_count)
    ]
    assignments: list[list[str]] = [[] for _ in range(batch_count)]
    stratum_counts: list[Counter[str]] = [
        Counter() for _ in range(batch_count)
    ]
    grouped: dict[str, list[str]] = defaultdict(list)
    for candidate in candidates:
        grouped[_candidate_stratum(candidate)].append(
            candidate["candidate_id"]
        )

    for stratum in sorted(grouped):
        candidate_ids = sorted(
            grouped[stratum],
            key=lambda candidate_id: (
                sha256_text(f"{seed}:{candidate_id}"),
                candidate_id,
            ),
        )
        start = int(sha256_text(f"{seed}:{stratum}")[:8], 16) % batch_count
        for candidate_id in candidate_ids:
            eligible = [
                index
                for index in range(batch_count)
                if len(assignments[index]) < capacities[index]
            ]
            selected = min(
                eligible,
                key=lambda index: (
                    stratum_counts[index][stratum],
                    len(assignments[index]),
                    (index - start) % batch_count,
                ),
            )
            assignments[selected].append(candidate_id)
            stratum_counts[selected][stratum] += 1

    batches = []
    width = max(3, len(str(batch_count)))
    for index, candidate_ids in enumerate(assignments, start=1):
        ordered_ids = sorted(
            candidate_ids,
            key=lambda candidate_id: (
                sha256_text(f"{seed}:batch:{index}:{candidate_id}"),
                candidate_id,
            ),
        )
        batches.append(
            {
                "batch_id": f"batch_{index:0{width}d}",
                "ordinal": index,
                "candidate_ids": ordered_ids,
                "stratum_counts": dict(
                    sorted(stratum_counts[index - 1].items())
                ),
            }
        )
    return batches


def _partition_focused_candidates(
    candidates: list[dict[str, Any]],
    *,
    batch_size: int,
    seed: str,
    predicted_type: str,
    focus_limit: int,
) -> tuple[list[dict[str, Any]], list[str]]:
    eligible = [
        candidate
        for candidate in candidates
        if candidate["predicted_type"] == predicted_type
        and candidate["label"]["expected_conflict"] is None
    ]
    if not eligible:
        raise KnowledgeWorkbenchError(
            "没有符合聚焦类型且尚未标注的冲突候选"
        )
    selected = sorted(
        eligible,
        key=lambda candidate: (
            -float(candidate["similarity"]),
            sha256_text(f"{seed}:focus:{candidate['candidate_id']}"),
            candidate["candidate_id"],
        ),
    )[:focus_limit]
    focus_candidate_ids = [
        candidate["candidate_id"] for candidate in selected
    ]
    focus_id_set = set(focus_candidate_ids)
    remaining = [
        candidate
        for candidate in candidates
        if candidate["candidate_id"] not in focus_id_set
    ]
    tail = (
        _partition_candidates(
            remaining,
            batch_size=batch_size,
            seed=seed,
        )
        if remaining
        else []
    )
    grouped_ids = [
        focus_candidate_ids,
        *(batch["candidate_ids"] for batch in tail),
    ]
    by_id = {
        candidate["candidate_id"]: candidate
        for candidate in candidates
    }
    width = max(3, len(str(len(grouped_ids))))
    batches = []
    for ordinal, candidate_ids in enumerate(grouped_ids, start=1):
        counts = Counter(
            _candidate_stratum(by_id[candidate_id])
            for candidate_id in candidate_ids
        )
        batches.append(
            {
                "batch_id": f"batch_{ordinal:0{width}d}",
                "ordinal": ordinal,
                "candidate_ids": candidate_ids,
                "stratum_counts": dict(sorted(counts.items())),
            }
        )
    return batches, focus_candidate_ids


def _validated_focus_predicted_type(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    if normalized not in {"value_change", "polarity_change"}:
        raise KnowledgeWorkbenchError(
            "聚焦预测类型必须是 value_change 或 polarity_change"
        )
    return normalized


def _validated_focus_limit(
    value: int | None,
    *,
    batch_size: int,
    focus_enabled: bool,
) -> int | None:
    if not focus_enabled:
        if value is not None:
            raise KnowledgeWorkbenchError(
                "focus_limit 只能与 focus_predicted_type 一起使用"
            )
        return None
    if value is None:
        return batch_size
    if isinstance(value, bool) or not isinstance(value, int):
        raise KnowledgeWorkbenchError("focus_limit 必须是整数")
    if value < 1 or value > batch_size:
        raise KnowledgeWorkbenchError(
            "focus_limit 必须介于 1 与 batch_size 之间"
        )
    return value


def _candidate_stratum(candidate: dict[str, Any]) -> str:
    predicted_type = candidate["predicted_type"] or "none"
    similarity = candidate["similarity"]
    if similarity >= 0.85:
        band = "high"
    elif similarity >= 0.70:
        band = "medium"
    else:
        band = "low"
    return f"{predicted_type}:{band}"


def _validated_batch_size(value: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise KnowledgeWorkbenchError("batch_size 必须是整数")
    if value < 10 or value > 200:
        raise KnowledgeWorkbenchError("batch_size 必须在 10 到 200 之间")
    return value


def _validated_seed(value: str) -> str:
    if not isinstance(value, str) or not _SEED_PATTERN.fullmatch(value):
        raise KnowledgeWorkbenchError(
            "seed 只能包含字母、数字、点、下划线或连字符，长度 1 到 80"
        )
    return value


def _required_actor(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise KnowledgeWorkbenchError("actor 不能为空")
    actor = value.strip()
    if len(actor) > 80:
        raise KnowledgeWorkbenchError("actor 不能超过 80 个字符")
    return actor


def _validated_output(paths: WorkspacePaths, output: Path) -> Path:
    output = output.expanduser().resolve()
    if output.suffix.lower() != ".json":
        raise KnowledgeWorkbenchError("冲突标注计划必须使用 .json 文件")
    try:
        output.relative_to(paths.evaluations.resolve())
    except ValueError as exc:
        raise KnowledgeWorkbenchError(
            "冲突标注计划只能保存到当前 workspace/evaluations 内"
        ) from exc
    if output.exists():
        raise KnowledgeWorkbenchError(
            f"冲突标注计划已存在，不允许静默覆盖：{output}"
        )
    return output


def _read_plan(
    paths: WorkspacePaths, plan_path: Path
) -> tuple[Path, str, dict[str, Any]]:
    plan_path = plan_path.expanduser().resolve()
    try:
        plan_path.relative_to(paths.evaluations.resolve())
    except ValueError as exc:
        raise KnowledgeWorkbenchError(
            "冲突标注计划只能从当前 workspace/evaluations 内读取"
        ) from exc
    try:
        content = plan_path.read_text(encoding="utf-8")
        plan = json.loads(content)
    except FileNotFoundError as exc:
        raise KnowledgeWorkbenchError(
            f"冲突标注计划不存在：{plan_path}"
        ) from exc
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise KnowledgeWorkbenchError(
            f"冲突标注计划不是有效 JSON：{exc}"
        ) from exc
    validate_conflict_labeling_plan(plan)
    _validate_plan_identity(plan)
    return plan_path, content, plan


def _find_plan_path(paths: WorkspacePaths, plan_id: str) -> Path:
    if (
        not isinstance(plan_id, str)
        or not re.fullmatch(r"cplan_[a-f0-9]{24}", plan_id)
    ):
        raise KnowledgeWorkbenchError("冲突标注计划 ID 无效")
    matched = []
    for plan_path in sorted(paths.evaluations.glob("*.json")):
        try:
            raw = json.loads(plan_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if (
            isinstance(raw, dict)
            and raw.get("kind")
            == "cross-document-conflict-labeling-plan"
            and raw.get("plan_id") == plan_id
        ):
            matched.append(plan_path)
    if not matched:
        raise KnowledgeWorkbenchError(
            "冲突标注计划不存在或校验失败"
        )
    if len(matched) > 1:
        raise KnowledgeWorkbenchError(
            "冲突标注计划 ID 重复，无法安全读取"
        )
    return matched[0]


def _plan_id(plan: dict[str, Any]) -> str:
    identity = {
        key: value for key, value in plan.items() if key != "plan_id"
    }
    canonical = json.dumps(
        identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return "cplan_" + sha256_text(canonical)[:24]


def _validate_plan_identity(plan: dict[str, Any]) -> None:
    if plan["plan_id"] != _plan_id(plan):
        raise KnowledgeWorkbenchError(
            "冲突标注计划身份校验失败；批次或来源元数据已被修改"
        )


def _validate_plan_audit(
    database: Database,
    paths: WorkspacePaths,
    plan_path: Path,
    content: str,
    plan: dict[str, Any],
) -> None:
    relative_path = plan_path.relative_to(paths.root.resolve()).as_posix()
    with database.connect() as connection:
        rows = connection.execute(
            """
            SELECT details_json FROM audit_log
            WHERE event_type = 'conflict_labeling_plan_created'
              AND entity_type = 'conflict_labeling_plan'
              AND entity_id = ?
            """,
            (plan["plan_id"],),
        ).fetchall()
    content_sha256 = sha256_text(content)
    for row in rows:
        try:
            details = json.loads(row["details_json"])
        except json.JSONDecodeError:
            continue
        if (
            details.get("content_sha256") == content_sha256
            and details.get("output") == relative_path
            and details.get("source_pack_id")
            == plan["source_pack"]["pack_id"]
        ):
            return
    raise KnowledgeWorkbenchError(
        "冲突标注计划缺少匹配的系统生成审计记录"
    )
