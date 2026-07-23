from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .audit import record_event
from .config import WorkspacePaths
from .conflict_labeling_plan import list_conflict_labeling_plans
from .database import Database
from .entity_relationships import project_business_relationship_graph
from .errors import KnowledgeWorkbenchError
from .graph_evaluation import evaluate_graph_dataset
from .graph_pilot import list_graph_pilot_packs
from .labeling import (
    labeling_session_readiness,
    list_labeling_sessions,
)
from .linting import lint_workspace
from .schema_validation import validate_graph_evaluation_dataset
from .utils import sha256_text, utc_now
from .wiki import write_text_atomic


DEFAULT_GOLD_DOCUMENT_TARGET = 20
MINIMUM_GOLD_DOCUMENT_BASELINE = 10


def build_quality_closure_status(
    database: Database,
    paths: WorkspacePaths,
    *,
    target_gold_documents: int = DEFAULT_GOLD_DOCUMENT_TARGET,
) -> dict[str, Any]:
    target_gold_documents = _validated_gold_target(
        target_gold_documents
    )
    lint = lint_workspace(database, paths)
    gold = _gold_status(database)
    conflict = _conflict_status(database, paths)
    graph = _graph_status(database, paths)
    work_packs = _work_pack_status(database)

    gates = [
        _gate(
            "workspace_integrity",
            lint["passed"],
            required={"lint_error_count": 0},
            actual={
                "lint_error_count": lint["summary"]["error_count"],
                "lint_warning_count": lint["summary"]["warning_count"],
            },
            next_action="修复全部 Lint error；warning 保持显式可见",
            phase="system",
        ),
        _gate(
            "gold_baseline",
            (
                gold["approved_current_document_count"]
                >= MINIMUM_GOLD_DOCUMENT_BASELINE
                and gold["invalid_approved_session_count"] == 0
            ),
            required={
                "minimum_approved_current_documents":
                MINIMUM_GOLD_DOCUMENT_BASELINE,
                "invalid_approved_sessions": 0,
            },
            actual={
                "approved_current_document_count": gold[
                    "approved_current_document_count"
                ],
                "invalid_approved_session_count": gold[
                    "invalid_approved_session_count"
                ],
            },
            next_action="完成至少 10 份当前资料的双人黄金标注",
            phase="human_data",
        ),
        _gate(
            "gold_expansion",
            (
                gold["approved_current_document_count"]
                >= target_gold_documents
                and gold["invalid_approved_session_count"] == 0
            ),
            required={
                "target_approved_current_documents":
                target_gold_documents,
                "invalid_approved_sessions": 0,
            },
            actual={
                "approved_current_document_count": gold[
                    "approved_current_document_count"
                ],
                "remaining_document_count": max(
                    0,
                    target_gold_documents
                    - gold["approved_current_document_count"],
                ),
            },
            next_action="扩充并异人复核真实黄金资料",
            phase="human_data",
        ),
        _gate(
            "conflict_labeling_plan",
            (
                conflict["valid_plan_count"] > 0
                and conflict["invalid_plan_count"] == 0
            ),
            required={
                "minimum_valid_plan_count": 1,
                "invalid_plan_count": 0,
            },
            actual={
                "valid_plan_count": conflict["valid_plan_count"],
                "invalid_plan_count": conflict["invalid_plan_count"],
                "candidate_count": conflict["candidate_count"],
                "batch_count": conflict["batch_count"],
            },
            next_action="生成覆盖完整候选包的经审计分层计划",
            phase="system",
        ),
        _gate(
            "conflict_annotation",
            conflict["annotation_complete"],
            required={"annotation_complete": True},
            actual={
                "labeled_count": conflict["labeled_count"],
                "candidate_count": conflict["candidate_count"],
                "unlabeled_count": conflict["unlabeled_count"],
            },
            next_action="逐批人工回源标注跨文档冲突候选",
            phase="human_data",
        ),
        _gate(
            "conflict_independent_review",
            conflict["review_complete"],
            required={"review_complete": True},
            actual={
                "approved_count": conflict["approved_count"],
                "candidate_count": conflict["candidate_count"],
                "unreviewed_count": conflict["unreviewed_count"],
            },
            next_action="提交整包后由不同 actor 逐批复核",
            phase="human_data",
        ),
        _gate(
            "graph_pilot_snapshot",
            (
                graph["valid_pack_count"] > 0
                and graph["invalid_pack_count"] == 0
                and graph["source_snapshot_passed"]
                and graph["classification_boundary_passed"]
            ),
            required={
                "minimum_valid_pack_count": 1,
                "invalid_pack_count": 0,
                "source_snapshot_passed": True,
                "classification_boundary_passed": True,
            },
            actual={
                "valid_pack_count": graph["valid_pack_count"],
                "invalid_pack_count": graph["invalid_pack_count"],
                "candidate_count": graph["candidate_count"],
                "snapshot_valid_count": graph["snapshot_valid_count"],
                "restricted_leak_count": graph[
                    "restricted_leak_count"
                ],
            },
            next_action="重建来源过期或密级越界的图谱试点包",
            phase="system",
        ),
        _gate(
            "graph_evidence_review",
            (
                graph["evidence_review_complete"]
                and graph["verified_evidence_count"] > 0
            ),
            required={
                "evidence_review_complete": True,
                "minimum_verified_evidence_count": 1,
            },
            actual={
                "evidence_review_complete": graph[
                    "evidence_review_complete"
                ],
                "verified_evidence_count": graph[
                    "verified_evidence_count"
                ],
                "status_counts": graph["status_counts"],
            },
            next_action="完成试点证据初审与异人验证或明确暂缓处置",
            phase="human_data",
        ),
        _gate(
            "graph_entity_mentions",
            graph["verified_with_two_entities_count"] > 0,
            required={
                "minimum_verified_with_two_entities_count": 1
            },
            actual={
                "active_entity_count": graph["active_entity_count"],
                "entity_mention_count": graph["entity_mention_count"],
                "verified_with_entity_count": graph[
                    "verified_with_entity_count"
                ],
                "verified_with_two_entities_count": graph[
                    "verified_with_two_entities_count"
                ],
            },
            next_action="人工创建规范实体并逐字绑定 verified 证据提及",
            phase="human_data",
        ),
        _gate(
            "graph_business_relationship",
            (
                graph["graph_gold_prerequisites_met"]
                and graph["projectable_relationship_count"] > 0
            ),
            required={
                "graph_gold_prerequisites_met": True,
                "minimum_projectable_relationship_count": 1,
            },
            actual={
                "active_relation_type_count": graph[
                    "active_relation_type_count"
                ],
                "active_relationship_count": graph[
                    "active_relationship_count"
                ],
                "projectable_relationship_count": graph[
                    "projectable_relationship_count"
                ],
                "graph_gold_prerequisites_met": graph[
                    "graph_gold_prerequisites_met"
                ],
            },
            next_action="人工登记关系类型和共同 verified 证据支撑的业务关系",
            phase="human_data",
        ),
        _gate(
            "graph_gold_evaluation",
            graph["passing_graph_gold_dataset_count"] > 0,
            required={
                "minimum_passing_graph_gold_dataset_count": 1,
                "finalization_audit_required": True,
            },
            actual={
                "valid_graph_gold_dataset_count": graph[
                    "valid_graph_gold_dataset_count"
                ],
                "stale_graph_gold_dataset_count": graph[
                    "stale_graph_gold_dataset_count"
                ],
                "passing_graph_gold_dataset_count": graph[
                    "passing_graph_gold_dataset_count"
                ],
                "unaudited_graph_gold_dataset_count": graph[
                    "unaudited_graph_gold_dataset_count"
                ],
            },
            next_action="通过受控工作包异人复核、固化并运行 graph-evaluate",
            phase="human_data",
        ),
    ]
    passed_count = sum(item["status"] == "passed" for item in gates)
    pending = [
        {
            "gate_id": item["gate_id"],
            "phase": item["phase"],
            "next_action": item["next_action"],
        }
        for item in gates
        if item["status"] != "passed"
    ]
    return {
        "schema_version": "1.0",
        "kind": "quality-closure-status",
        "checked_at": utc_now(),
        "configuration": {
            "minimum_gold_document_baseline":
            MINIMUM_GOLD_DOCUMENT_BASELINE,
            "target_gold_documents": target_gold_documents,
        },
        "summary": {
            "complete": passed_count == len(gates),
            "gate_count": len(gates),
            "passed_gate_count": passed_count,
            "pending_gate_count": len(gates) - passed_count,
            "human_action_required": any(
                item["phase"] == "human_data" for item in pending
            ),
        },
        "metrics": {
            "workspace": {
                **lint["summary"],
                "warning_codes": sorted(
                    {
                        issue["code"]
                        for issue in lint["issues"]
                        if issue["severity"] == "warning"
                    }
                ),
            },
            "gold": gold,
            "conflict": conflict,
            "graph": graph,
            "work_packs": work_packs,
        },
        "gates": gates,
        "pending_actions": pending,
    }


def save_quality_closure_status(
    database: Database,
    paths: WorkspacePaths,
    output: Path,
    *,
    actor: str,
    target_gold_documents: int = DEFAULT_GOLD_DOCUMENT_TARGET,
) -> dict[str, Any]:
    actor = _required_actor(actor)
    output = _validated_output(paths, output)
    report = build_quality_closure_status(
        database,
        paths,
        target_gold_documents=target_gold_documents,
    )
    content = json.dumps(
        report, ensure_ascii=False, indent=2, sort_keys=True
    ) + "\n"
    write_text_atomic(output, content)
    try:
        with database.transaction() as connection:
            record_event(
                connection,
                "quality_closure_status_saved",
                "quality_closure_status",
                sha256_text(content)[:32],
                actor=actor,
                details={
                    "output": output.relative_to(
                        paths.root.resolve()
                    ).as_posix(),
                    "content_sha256": sha256_text(content),
                    "complete": report["summary"]["complete"],
                    "passed_gate_count": report["summary"][
                        "passed_gate_count"
                    ],
                    "pending_gate_count": report["summary"][
                        "pending_gate_count"
                    ],
                    "pending_gate_ids": [
                        item["gate_id"]
                        for item in report["pending_actions"]
                    ],
                    "target_gold_documents": target_gold_documents,
                },
            )
    except Exception:
        output.unlink(missing_ok=True)
        raise
    return report


def _gold_status(database: Database) -> dict[str, Any]:
    sessions = list_labeling_sessions(database)
    approved = [
        session for session in sessions if session["status"] == "approved"
    ]
    invalid = 0
    approved_case_count = 0
    for session in approved:
        try:
            readiness = labeling_session_readiness(
                database, session["id"]
            )
        except KnowledgeWorkbenchError:
            invalid += 1
            continue
        if not readiness["can_export"]:
            invalid += 1
        approved_case_count += readiness["case_count"]
    with database.connect() as connection:
        row = connection.execute(
            """
            SELECT COUNT(DISTINCT dv.document_id) AS document_count
            FROM labeling_sessions ls
            JOIN labeling_cases lc ON lc.session_id = ls.id
            JOIN document_versions dv
              ON dv.id = lc.document_version_id
            JOIN documents d
              ON d.id = dv.document_id
             AND d.current_version_id = dv.id
            WHERE ls.status = 'approved'
            """
        ).fetchone()
    return {
        "session_count": len(sessions),
        "approved_session_count": len(approved),
        "invalid_approved_session_count": invalid,
        "approved_case_count": approved_case_count,
        "approved_current_document_count": row["document_count"],
    }


def _conflict_status(
    database: Database, paths: WorkspacePaths
) -> dict[str, Any]:
    listing = list_conflict_labeling_plans(database, paths)
    latest = listing["items"][0] if listing["items"] else None
    summary = latest["summary"] if latest else {}
    return {
        "valid_plan_count": listing["total"],
        "invalid_plan_count": listing["invalid_plan_count"],
        "latest_plan_id": latest["plan_id"] if latest else None,
        "source_pack_id": latest["source_pack_id"] if latest else None,
        "candidate_count": summary.get("candidate_count", 0),
        "batch_count": summary.get("batch_count", 0),
        "labeled_count": summary.get("labeled_count", 0),
        "approved_count": summary.get("approved_count", 0),
        "rejected_count": summary.get("rejected_count", 0),
        "unlabeled_count": summary.get("unlabeled_count", 0),
        "unreviewed_count": summary.get("unreviewed_count", 0),
        "annotation_complete": summary.get(
            "annotation_complete", False
        ),
        "review_complete": summary.get("review_complete", False),
    }


def _graph_status(
    database: Database, paths: WorkspacePaths
) -> dict[str, Any]:
    listing = list_graph_pilot_packs(database, paths)
    latest = listing["items"][0] if listing["items"] else None
    summary = latest["summary"] if latest else {}
    with database.connect() as connection:
        counts = connection.execute(
            """
            SELECT
              (SELECT COUNT(*) FROM canonical_entities
               WHERE status = 'active') AS active_entity_count,
              (SELECT COUNT(*) FROM evidence_entity_mentions)
                AS entity_mention_count,
              (SELECT COUNT(*) FROM entity_relation_types
               WHERE status = 'active') AS active_relation_type_count,
              (SELECT COUNT(*) FROM entity_relationships
               WHERE status = 'active') AS active_relationship_count
            """
        ).fetchone()
    business = project_business_relationship_graph(database)
    datasets = _graph_gold_dataset_status(database, paths)
    return {
        "valid_pack_count": listing["total"],
        "invalid_pack_count": listing["invalid_pack_count"],
        "latest_pack_id": latest["pack_id"] if latest else None,
        "candidate_count": summary.get("candidate_count", 0),
        "snapshot_valid_count": summary.get(
            "snapshot_valid_count", 0
        ),
        "restricted_leak_count": summary.get(
            "restricted_leak_count", 0
        ),
        "status_counts": summary.get("status_counts", {}),
        "verified_evidence_count": summary.get(
            "verified_evidence_count", 0
        ),
        "verified_with_entity_count": summary.get(
            "verified_with_entity_count", 0
        ),
        "verified_with_two_entities_count": summary.get(
            "verified_with_two_entities_count", 0
        ),
        "source_snapshot_passed": summary.get(
            "source_snapshot_passed", False
        ),
        "classification_boundary_passed": summary.get(
            "classification_boundary_passed", False
        ),
        "evidence_review_complete": summary.get(
            "evidence_review_complete", False
        ),
        "graph_gold_prerequisites_met": summary.get(
            "graph_gold_prerequisites_met", False
        ),
        "active_entity_count": counts["active_entity_count"],
        "entity_mention_count": counts["entity_mention_count"],
        "active_relation_type_count": counts[
            "active_relation_type_count"
        ],
        "active_relationship_count": counts[
            "active_relationship_count"
        ],
        "projectable_relationship_count": business["summary"][
            "eligible_edge_count"
        ],
        **datasets,
    }


def _graph_gold_dataset_status(
    database: Database, paths: WorkspacePaths
) -> dict[str, int]:
    valid = 0
    stale = 0
    passing = 0
    unaudited = 0
    with database.connect() as connection:
        audit_rows = connection.execute(
            """
            SELECT details_json FROM audit_log
            WHERE event_type = 'graph_gold_dataset_finalized'
              AND entity_type = 'graph_gold_candidate'
            """
        ).fetchall()
    finalized_outputs: dict[str, set[str]] = {}
    for row in audit_rows:
        try:
            details = json.loads(row["details_json"])
        except json.JSONDecodeError:
            continue
        output = details.get("output")
        content_sha256 = details.get("content_sha256")
        if isinstance(output, str) and isinstance(content_sha256, str):
            finalized_outputs.setdefault(output, set()).add(content_sha256)
    for path in sorted(paths.evaluations.glob("*.json")):
        try:
            content = path.read_text(encoding="utf-8")
            payload = json.loads(content)
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if not _looks_like_graph_gold_dataset(payload):
            continue
        try:
            validate_graph_evaluation_dataset(payload)
        except KnowledgeWorkbenchError:
            continue
        relative_path = path.resolve().relative_to(
            paths.root.resolve()
        ).as_posix()
        if sha256_text(content) not in finalized_outputs.get(
            relative_path, set()
        ):
            unaudited += 1
            continue
        valid += 1
        try:
            report = evaluate_graph_dataset(database, path)
        except KnowledgeWorkbenchError:
            stale += 1
            continue
        aggregate = report["aggregate"]
        if (
            report["safety"]["passed"]
            and aggregate["pass_rate"] == 1.0
            and aggregate["truncated_path_case_count"] == 0
        ):
            passing += 1
    return {
        "valid_graph_gold_dataset_count": valid,
        "stale_graph_gold_dataset_count": stale,
        "passing_graph_gold_dataset_count": passing,
        "unaudited_graph_gold_dataset_count": unaudited,
    }


def _work_pack_status(database: Database) -> dict[str, int]:
    with database.connect() as connection:
        rows = connection.execute(
            """
            SELECT event_type, COUNT(*) AS count
            FROM audit_log
            WHERE event_type IN (
              'conflict_batch_annotation_pack_exported',
              'conflict_batch_review_pack_exported',
              'graph_pilot_triage_pack_exported',
              'graph_pilot_verification_pack_exported',
              'graph_pilot_entity_curation_pack_exported',
              'graph_pilot_entity_curation_applied',
              'graph_pilot_relationship_curation_pack_exported',
              'graph_pilot_relationship_curation_applied',
              'graph_gold_annotation_pack_exported',
              'graph_gold_candidate_saved',
              'graph_gold_review_pack_exported',
              'graph_gold_dataset_finalized'
            )
            GROUP BY event_type
            """
        ).fetchall()
    counts = {row["event_type"]: row["count"] for row in rows}
    return {
        "conflict_annotation_export_count": counts.get(
            "conflict_batch_annotation_pack_exported", 0
        ),
        "conflict_review_export_count": counts.get(
            "conflict_batch_review_pack_exported", 0
        ),
        "graph_triage_export_count": counts.get(
            "graph_pilot_triage_pack_exported", 0
        ),
        "graph_verification_export_count": counts.get(
            "graph_pilot_verification_pack_exported", 0
        ),
        "graph_entity_curation_export_count": counts.get(
            "graph_pilot_entity_curation_pack_exported", 0
        ),
        "graph_entity_curation_applied_count": counts.get(
            "graph_pilot_entity_curation_applied", 0
        ),
        "graph_relationship_curation_export_count": counts.get(
            "graph_pilot_relationship_curation_pack_exported", 0
        ),
        "graph_relationship_curation_applied_count": counts.get(
            "graph_pilot_relationship_curation_applied", 0
        ),
        "graph_gold_annotation_export_count": counts.get(
            "graph_gold_annotation_pack_exported", 0
        ),
        "graph_gold_candidate_saved_count": counts.get(
            "graph_gold_candidate_saved", 0
        ),
        "graph_gold_review_export_count": counts.get(
            "graph_gold_review_pack_exported", 0
        ),
        "graph_gold_dataset_finalized_count": counts.get(
            "graph_gold_dataset_finalized", 0
        ),
    }


def _gate(
    gate_id: str,
    passed: bool,
    *,
    required: dict[str, Any],
    actual: dict[str, Any],
    next_action: str,
    phase: str,
) -> dict[str, Any]:
    return {
        "gate_id": gate_id,
        "status": "passed" if passed else "pending",
        "phase": phase,
        "required": required,
        "actual": actual,
        "next_action": None if passed else next_action,
    }


def _looks_like_graph_gold_dataset(payload: Any) -> bool:
    return (
        isinstance(payload, dict)
        and "relation_cases" in payload
        and "path_cases" in payload
        and "provenance" in payload
    )


def _validated_gold_target(value: int) -> int:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or value < MINIMUM_GOLD_DOCUMENT_BASELINE
        or value > 1000
    ):
        raise KnowledgeWorkbenchError(
            "黄金资料目标必须是 10 到 1000 之间的整数"
        )
    return value


def _validated_output(
    paths: WorkspacePaths, output: Path
) -> Path:
    output = output.expanduser().resolve()
    if output.suffix.lower() != ".json":
        raise KnowledgeWorkbenchError(
            "质量闭环状态报告必须使用 .json 文件"
        )
    try:
        output.relative_to(paths.evaluations.resolve())
    except ValueError as exc:
        raise KnowledgeWorkbenchError(
            "质量闭环状态报告只能保存到当前 workspace/evaluations 内"
        ) from exc
    if output.exists():
        raise KnowledgeWorkbenchError(
            f"质量闭环状态报告已存在，不允许静默覆盖：{output}"
        )
    return output


def _required_actor(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise KnowledgeWorkbenchError("actor 不能为空")
    actor = value.strip()
    if len(actor) > 80:
        raise KnowledgeWorkbenchError("actor 不能超过 80 个字符")
    if any(character in actor for character in "\r\n"):
        raise KnowledgeWorkbenchError("actor 不能包含换行")
    return actor
