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
from .schema_validation import validate_conflict_labeling_plan
from .utils import sha256_text, utc_now
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
) -> dict[str, Any]:
    actor = _required_actor(actor)
    batch_size = _validated_batch_size(batch_size)
    seed = _validated_seed(seed)
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

    batches = _partition_candidates(
        source_pack["candidates"],
        batch_size=batch_size,
        seed=seed,
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
    _, source_pack = _candidate_pack_by_id(
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
    for batch in plan["batches"]:
        counts = Counter()
        for candidate_id in batch["candidate_ids"]:
            candidate = candidates[candidate_id]
            if candidate["label"]["expected_conflict"] is not None:
                counts["labeled"] += 1
            decision = candidate["review"]["decision"]
            if decision is not None:
                counts["reviewed"] += 1
                counts[decision] += 1
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
                "annotation_complete": counts["labeled"] == counts["total"],
                "review_complete": counts["approved"] == counts["total"],
                "stratum_counts": batch["stratum_counts"],
            }
        )
    candidate_count = len(candidates)
    return {
        "schema_version": "1.0",
        "kind": "cross-document-conflict-labeling-plan-status",
        "checked_at": utc_now(),
        "plan_id": plan["plan_id"],
        "source_pack_id": source_pack["pack_id"],
        "summary": {
            "candidate_count": candidate_count,
            "batch_count": len(items),
            "batch_size": plan["batch_size"],
            "labeled_count": totals["labeled"],
            "approved_count": totals["approved"],
            "rejected_count": totals["rejected"],
            "unlabeled_count": candidate_count - totals["labeled"],
            "unreviewed_count": candidate_count - totals["reviewed"],
            "annotation_complete": totals["labeled"] == candidate_count,
            "review_complete": totals["approved"] == candidate_count,
        },
        "items": items,
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
