from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .audit import record_event
from .config import WorkspacePaths
from .conflict_batch_workpacks import (
    ANNOTATION_PACK_TYPE as CONFLICT_ANNOTATION_PACK_TYPE,
    REVIEW_PACK_TYPE as CONFLICT_REVIEW_PACK_TYPE,
    inspect_conflict_batch_work_pack,
)
from .conflict_labeling_plan import list_conflict_labeling_plans
from .database import Database
from .entity_relationships import project_business_relationship_graph
from .errors import KnowledgeWorkbenchError
from .graph_evaluation import evaluate_graph_dataset
from .graph_gold_workpacks import (
    ANNOTATION_PACK_TYPE,
    REVIEW_PACK_TYPE,
    inspect_graph_gold_work_pack,
)
from .graph_pilot import list_graph_pilot_packs
from .labeling import (
    labeling_session_readiness,
    list_labeling_sessions,
)
from .linting import lint_workspace
from .review_assurance import (
    INDEPENDENT_REVIEW_MODE,
    SOLO_ATTESTED_REVIEW_MODE,
    review_actor_policy_valid,
)
from .schema_validation import validate_graph_evaluation_dataset
from .utils import sha256_text, utc_now
from .wiki import write_text_atomic


DEFAULT_GOLD_DOCUMENT_TARGET = 20
MINIMUM_GOLD_DOCUMENT_BASELINE = 10
DEFAULT_CONFLICT_SAMPLE_SIZE = 100
DEFAULT_MINIMUM_KNOWN_CONFLICTS = 5


def build_quality_closure_status(
    database: Database,
    paths: WorkspacePaths,
    *,
    target_gold_documents: int = DEFAULT_GOLD_DOCUMENT_TARGET,
    conflict_sample_size: int = DEFAULT_CONFLICT_SAMPLE_SIZE,
    minimum_known_conflicts: int = DEFAULT_MINIMUM_KNOWN_CONFLICTS,
) -> dict[str, Any]:
    target_gold_documents = _validated_gold_target(
        target_gold_documents
    )
    conflict_sample_size = _validated_conflict_sample_size(
        conflict_sample_size
    )
    minimum_known_conflicts = _validated_minimum_known_conflicts(
        minimum_known_conflicts,
        conflict_sample_size=conflict_sample_size,
    )
    lint = lint_workspace(database, paths)
    gold = _gold_status(database)
    governance = _document_governance_status(database)
    gold["quality_role"] = "development_regression_only"
    gold["counts_as_enterprise_business_baseline"] = False
    conflict = _conflict_status(database, paths)
    conflict_sampling = _conflict_sampling_status(
        conflict,
        requested_sample_size=conflict_sample_size,
        minimum_known_conflicts=minimum_known_conflicts,
    )
    conflict["sampling"] = conflict_sampling
    graph = _graph_status(database, paths)
    work_packs = _work_pack_status(database, paths)

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
            "development_regression_baseline",
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
            next_action="完成至少 10 份开发样例的回归标注；它不代表企业业务基线",
            phase="system",
        ),
        _gate(
            "business_corpus_scope",
            (
                governance["production_in_scope_document_count"] > 0
                and governance["production_authoritative_document_count"] > 0
                and governance["production_unreviewed_document_count"] == 0
            ),
            required={
                "minimum_production_in_scope_documents": 1,
                "minimum_production_authoritative_documents": 1,
                "production_unreviewed_documents": 0,
            },
            actual=governance,
            next_action=(
                "先生成全库资料地图，再由熟悉业务的人确认资料范围、"
                "现行版本和权威性；不要逐条审批普通证据"
            ),
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
            next_action=(
                "生成覆盖完整候选清单的经审计分层抽样框；"
                "完整清单不要求全量人工标注"
            ),
            phase="system",
        ),
        _gate(
            "conflict_annotation",
            (
                conflict_sampling["annotation_complete"]
                and conflict_sampling["known_conflict_labeling_complete"]
            ),
            required={
                "stratified_sample_annotation_complete": True,
                "minimum_known_conflicts":
                minimum_known_conflicts,
            },
            actual={
                "candidate_universe_count": conflict["candidate_count"],
                "requested_sample_size": conflict_sampling[
                    "requested_sample_size"
                ],
                "effective_sample_size": conflict_sampling[
                    "effective_sample_size"
                ],
                "sample_labeled_count": conflict_sampling[
                    "sample_labeled_count"
                ],
                "remaining_sample_label_count": conflict_sampling[
                    "remaining_sample_label_count"
                ],
                "known_conflict_labeled_count": conflict_sampling[
                    "known_conflict_labeled_count"
                ],
                "remaining_known_conflict_label_count": conflict_sampling[
                    "remaining_known_conflict_label_count"
                ],
                "strata": conflict_sampling["strata"],
                "annotation_work_pack_ready_count": work_packs[
                    "conflict_annotation_ready_count"
                ],
                "annotation_work_pack_incomplete_count": work_packs[
                    "conflict_annotation_incomplete_count"
                ],
                "annotation_work_pack_invalid_count": work_packs[
                    "conflict_annotation_invalid_count"
                ],
                "annotation_work_pack_applied_count": work_packs[
                    "conflict_annotation_applied_count"
                ],
            },
            next_action=_conflict_annotation_next_action(
                conflict_sampling, work_packs
            ),
            phase="human_data",
        ),
        _gate(
            "conflict_review",
            (
                conflict_sampling["human_attested_review_complete"]
                and conflict_sampling["known_conflict_review_complete"]
            ),
            required={
                "stratified_sample_human_attested_review_complete": True,
                "minimum_human_attested_known_conflicts":
                minimum_known_conflicts,
                "accepted_review_modes": [
                    "independent",
                    "solo_attested",
                ],
            },
            actual={
                "candidate_universe_count": conflict["candidate_count"],
                "effective_sample_size": conflict_sampling[
                    "effective_sample_size"
                ],
                "sample_human_attested_review_count": conflict_sampling[
                    "sample_human_attested_review_count"
                ],
                "remaining_sample_review_count": conflict_sampling[
                    "remaining_sample_review_count"
                ],
                "human_attested_known_conflict_count": conflict_sampling[
                    "human_attested_known_conflict_count"
                ],
                "remaining_known_conflict_review_count": conflict_sampling[
                    "remaining_known_conflict_review_count"
                ],
                "human_attested_approved_count": conflict[
                    "human_attested_approved_count"
                ],
                "independent_approved_count": conflict[
                    "independent_approved_count"
                ],
                "solo_attested_approved_count": conflict[
                    "solo_attested_approved_count"
                ],
                "unattributed_approved_count": conflict[
                    "unattributed_approved_count"
                ],
                "independent_review_complete": conflict[
                    "independent_review_complete"
                ],
                "review_audit_current": conflict[
                    "review_audit_current"
                ],
                "strata": conflict_sampling["strata"],
                "review_work_pack_ready_count": work_packs[
                    "conflict_review_ready_count"
                ],
                "review_work_pack_incomplete_count": work_packs[
                    "conflict_review_incomplete_count"
                ],
                "review_work_pack_invalid_count": work_packs[
                    "conflict_review_invalid_count"
                ],
                "review_work_pack_applied_count": work_packs[
                    "conflict_review_applied_count"
                ],
            },
            next_action=_conflict_review_next_action(
                conflict_sampling, work_packs
            ),
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
                and graph[
                    "human_attested_verified_evidence_count"
                ] > 0
            ),
            required={
                "evidence_review_complete": True,
                "minimum_human_attested_verified_evidence_count": 1,
                "accepted_review_modes": [
                    "independent",
                    "solo_attested",
                ],
            },
            actual={
                "evidence_review_complete": graph[
                    "evidence_review_complete"
                ],
                "verified_evidence_count": graph[
                    "verified_evidence_count"
                ],
                "human_attested_verified_evidence_count": graph[
                    "human_attested_verified_evidence_count"
                ],
                "independent_verified_evidence_count": graph[
                    "independent_verified_evidence_count"
                ],
                "solo_attested_verified_evidence_count": graph[
                    "solo_attested_verified_evidence_count"
                ],
                "unattributed_verified_evidence_count": graph[
                    "unattributed_verified_evidence_count"
                ],
                "independent_review_complete": graph[
                    "independent_review_complete"
                ],
                "status_counts": graph["status_counts"],
            },
            next_action=(
                "完成试点证据人工验证；单人模式必须显式记录"
                " solo_attested，不能冒充独立复核"
            ),
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
            graph[
                "human_attested_passing_graph_gold_dataset_count"
            ] > 0,
            required={
                "minimum_human_attested_passing_dataset_count": 1,
                "accepted_review_modes": [
                    "independent",
                    "solo_attested",
                ],
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
                "human_attested_passing_graph_gold_dataset_count": graph[
                    "human_attested_passing_graph_gold_dataset_count"
                ],
                "independent_passing_graph_gold_dataset_count": graph[
                    "independent_passing_graph_gold_dataset_count"
                ],
                "solo_attested_passing_graph_gold_dataset_count": graph[
                    "solo_attested_passing_graph_gold_dataset_count"
                ],
                "unattributed_passing_graph_gold_dataset_count": graph[
                    "unattributed_passing_graph_gold_dataset_count"
                ],
                "unaudited_graph_gold_dataset_count": graph[
                    "unaudited_graph_gold_dataset_count"
                ],
                "annotation_work_pack_ready_count": work_packs[
                    "graph_gold_annotation_ready_count"
                ],
                "annotation_work_pack_incomplete_count": work_packs[
                    "graph_gold_annotation_incomplete_count"
                ],
                "annotation_work_pack_invalid_count": work_packs[
                    "graph_gold_annotation_invalid_count"
                ],
                "review_work_pack_ready_count": work_packs[
                    "graph_gold_review_ready_count"
                ],
                "review_work_pack_incomplete_count": work_packs[
                    "graph_gold_review_incomplete_count"
                ],
                "review_work_pack_invalid_count": work_packs[
                    "graph_gold_review_invalid_count"
                ],
            },
            next_action=_graph_gold_next_action(work_packs),
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
    gold_advisory = _gold_coverage_advisory(
        gold, recommended_document_count=target_gold_documents
    )
    return {
        "schema_version": "3.0",
        "kind": "quality-closure-status",
        "checked_at": utc_now(),
        "configuration": {
            "minimum_gold_document_baseline":
            MINIMUM_GOLD_DOCUMENT_BASELINE,
            "recommended_gold_documents": target_gold_documents,
            "conflict_sample_size": conflict_sample_size,
            "minimum_known_conflicts": minimum_known_conflicts,
        },
        "summary": {
            "complete": passed_count == len(gates),
            "gate_count": len(gates),
            "passed_gate_count": passed_count,
            "pending_gate_count": len(gates) - passed_count,
            "human_action_required": any(
                item["phase"] == "human_data" for item in pending
            ),
            "advisory_count": 1,
            "unmet_advisory_count": (
                0 if gold_advisory["status"] == "met" else 1
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
            "document_governance": governance,
            "material_flow": _material_flow_status(database, gold),
            "conflict": conflict,
            "graph": graph,
            "work_packs": work_packs,
        },
        "gates": gates,
        "advisories": [gold_advisory],
        "pending_actions": pending,
    }


def save_quality_closure_status(
    database: Database,
    paths: WorkspacePaths,
    output: Path,
    *,
    actor: str,
    target_gold_documents: int = DEFAULT_GOLD_DOCUMENT_TARGET,
    conflict_sample_size: int = DEFAULT_CONFLICT_SAMPLE_SIZE,
    minimum_known_conflicts: int = DEFAULT_MINIMUM_KNOWN_CONFLICTS,
) -> dict[str, Any]:
    actor = _required_actor(actor)
    output = _validated_output(paths, output)
    report = build_quality_closure_status(
        database,
        paths,
        target_gold_documents=target_gold_documents,
        conflict_sample_size=conflict_sample_size,
        minimum_known_conflicts=minimum_known_conflicts,
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
                    "recommended_gold_documents":
                    target_gold_documents,
                    "conflict_sample_size": conflict_sample_size,
                    "minimum_known_conflicts":
                    minimum_known_conflicts,
                },
            )
    except Exception:
        output.unlink(missing_ok=True)
        raise
    return report


def _document_governance_status(database: Database) -> dict[str, int]:
    with database.connect() as connection:
        rows = connection.execute(
            """
            SELECT purpose, scope_status, authority_status, COUNT(*) AS count
            FROM document_governance
            GROUP BY purpose, scope_status, authority_status
            """
        ).fetchall()
    metrics = {
        "development_fixture_document_count": 0,
        "candidate_document_count": 0,
        "production_document_count": 0,
        "production_in_scope_document_count": 0,
        "production_authoritative_document_count": 0,
        "production_unreviewed_document_count": 0,
    }
    for row in rows:
        purpose = row["purpose"]
        count = row["count"]
        if purpose == "development_fixture":
            metrics["development_fixture_document_count"] += count
        elif purpose == "candidate":
            metrics["candidate_document_count"] += count
        elif purpose == "production":
            metrics["production_document_count"] += count
            if row["scope_status"] == "in_scope":
                metrics["production_in_scope_document_count"] += count
            if (
                row["scope_status"] == "in_scope"
                and row["authority_status"] == "authoritative"
            ):
                metrics["production_authoritative_document_count"] += count
            if row["scope_status"] == "unreviewed":
                metrics["production_unreviewed_document_count"] += count
    return metrics


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
            SELECT COUNT(DISTINCT dv.document_id) AS approved_document_count,
                   (SELECT COUNT(*) FROM documents
                    WHERE current_version_id IS NOT NULL)
                     AS current_document_count
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
        unapproved_rows = connection.execute(
            """
            SELECT d.id
            FROM documents d
            WHERE d.current_version_id IS NOT NULL
              AND NOT EXISTS (
                SELECT 1
                FROM labeling_sessions ls
                JOIN labeling_cases lc ON lc.session_id = ls.id
                WHERE ls.status = 'approved'
                  AND lc.document_version_id = d.current_version_id
              )
            ORDER BY d.id
            """
        ).fetchall()
    return {
        "session_count": len(sessions),
        "approved_session_count": len(approved),
        "invalid_approved_session_count": invalid,
        "approved_case_count": approved_case_count,
        "approved_current_document_count": row[
            "approved_document_count"
        ],
        "current_document_count": row["current_document_count"],
        "unapproved_current_document_count": len(unapproved_rows),
        "unapproved_current_document_ids": [
            item["id"] for item in unapproved_rows
        ],
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
        "human_attested_review_complete": summary.get(
            "human_attested_review_complete", False
        ),
        "independent_review_complete": summary.get(
            "independent_review_complete", False
        ),
        "human_attested_approved_count": summary.get(
            "human_attested_approved_count", 0
        ),
        "independent_approved_count": summary.get(
            "independent_approved_count", 0
        ),
        "solo_attested_approved_count": summary.get(
            "solo_attested_approved_count", 0
        ),
        "unattributed_approved_count": summary.get(
            "unattributed_approved_count", 0
        ),
        "review_audit_current": summary.get(
            "review_audit_current", True
        ),
        "known_conflict_labeled_count": summary.get(
            "known_conflict_labeled_count", 0
        ),
        "human_attested_known_conflict_count": summary.get(
            "human_attested_known_conflict_count", 0
        ),
        "stratum_status": summary.get("stratum_status", {}),
    }


def _conflict_sampling_status(
    conflict: dict[str, Any],
    *,
    requested_sample_size: int,
    minimum_known_conflicts: int,
) -> dict[str, Any]:
    candidate_count = conflict["candidate_count"]
    effective_sample_size = min(requested_sample_size, candidate_count)
    stratum_status = conflict["stratum_status"]
    universe_by_stratum = {
        stratum: status["candidate_count"]
        for stratum, status in stratum_status.items()
        if status["candidate_count"] > 0
    }
    quotas = _allocate_stratified_quotas(
        universe_by_stratum,
        effective_sample_size,
    )
    strata = {}
    sample_labeled_count = 0
    sample_human_attested_review_count = 0
    for stratum, quota in quotas.items():
        status = stratum_status[stratum]
        labeled = min(status["labeled_count"], quota)
        reviewed = min(
            status["human_attested_approved_count"], quota
        )
        sample_labeled_count += labeled
        sample_human_attested_review_count += reviewed
        strata[stratum] = {
            "candidate_count": status["candidate_count"],
            "sample_quota": quota,
            "labeled_count": status["labeled_count"],
            "sample_labeled_count": labeled,
            "human_attested_approved_count": status[
                "human_attested_approved_count"
            ],
            "sample_human_attested_review_count": reviewed,
            "remaining_label_count": quota - labeled,
            "remaining_review_count": quota - reviewed,
        }
    known_conflict_labeled_count = conflict[
        "known_conflict_labeled_count"
    ]
    human_attested_known_conflict_count = conflict[
        "human_attested_known_conflict_count"
    ]
    return {
        "selection_method": (
            "quota_stratified_by_predicted_type_and_similarity_band"
        ),
        "candidate_universe_count": candidate_count,
        "requested_sample_size": requested_sample_size,
        "effective_sample_size": effective_sample_size,
        "minimum_known_conflicts": minimum_known_conflicts,
        "sample_labeled_count": sample_labeled_count,
        "remaining_sample_label_count": (
            effective_sample_size - sample_labeled_count
        ),
        "sample_human_attested_review_count": (
            sample_human_attested_review_count
        ),
        "remaining_sample_review_count": (
            effective_sample_size
            - sample_human_attested_review_count
        ),
        "known_conflict_labeled_count": (
            known_conflict_labeled_count
        ),
        "remaining_known_conflict_label_count": max(
            0,
            minimum_known_conflicts - known_conflict_labeled_count,
        ),
        "human_attested_known_conflict_count": (
            human_attested_known_conflict_count
        ),
        "remaining_known_conflict_review_count": max(
            0,
            minimum_known_conflicts
            - human_attested_known_conflict_count,
        ),
        "annotation_complete": (
            effective_sample_size > 0
            and sample_labeled_count == effective_sample_size
        ),
        "known_conflict_labeling_complete": (
            known_conflict_labeled_count >= minimum_known_conflicts
        ),
        "human_attested_review_complete": (
            effective_sample_size > 0
            and sample_human_attested_review_count
            == effective_sample_size
        ),
        "known_conflict_review_complete": (
            human_attested_known_conflict_count
            >= minimum_known_conflicts
        ),
        "strata": strata,
    }


def _allocate_stratified_quotas(
    universe_by_stratum: dict[str, int],
    sample_size: int,
) -> dict[str, int]:
    quotas = {
        stratum: 0 for stratum in sorted(universe_by_stratum)
    }
    for _ in range(sample_size):
        eligible = [
            stratum
            for stratum, count in universe_by_stratum.items()
            if quotas[stratum] < count
        ]
        if not eligible:
            break
        selected = min(
            eligible,
            key=lambda stratum: (
                quotas[stratum] / universe_by_stratum[stratum],
                -universe_by_stratum[stratum],
                stratum,
            ),
        )
        quotas[selected] += 1
    return {
        stratum: quota
        for stratum, quota in quotas.items()
        if quota > 0
    }


def _gold_coverage_advisory(
    gold: dict[str, Any],
    *,
    recommended_document_count: int,
) -> dict[str, Any]:
    approved_count = gold["approved_current_document_count"]
    return {
        "advisory_id": "gold_coverage_recommendation",
        "status": (
            "met"
            if approved_count >= recommended_document_count
            else "suggested"
        ),
        "blocks_quality_closure": False,
        "recommendation": {
            "recommended_approved_current_documents":
            recommended_document_count,
        },
        "actual": {
            "approved_current_document_count": approved_count,
            "imported_current_document_count": gold[
                "current_document_count"
            ],
            "unapproved_current_document_count": gold[
                "unapproved_current_document_count"
            ],
            "unapproved_current_document_ids": gold[
                "unapproved_current_document_ids"
            ],
            "remaining_recommended_document_count": max(
                0, recommended_document_count - approved_count
            ),
            "minimum_new_document_import_count": max(
                0,
                recommended_document_count
                - gold["current_document_count"],
            ),
        },
        "next_action": (
            None
            if approved_count >= recommended_document_count
            else (
                "仅在格式、业务类型或风险覆盖确有缺口时扩充黄金样本；"
                "不要为达到建议数量吸收无关 NAS 资料"
            )
        ),
    }


def _material_flow_status(
    database: Database,
    gold: dict[str, Any],
) -> dict[str, Any]:
    with database.connect() as connection:
        nas_counts = connection.execute(
            """
            SELECT
                SUM(CASE WHEN status = 'discovered' THEN 1 ELSE 0 END)
                    AS discovered_count,
                SUM(CASE WHEN status IN ('admitted', 'imported')
                         THEN 1 ELSE 0 END)
                    AS admitted_count
            FROM nas_discoveries
            """
        ).fetchone()
    return {
        "nas_discovered": {
            "available": True,
            "count": nas_counts["discovered_count"] or 0,
            "definition": "只读扫描发现、尚未做准入决定的文件",
        },
        "nas_admitted": {
            "available": True,
            "count": nas_counts["admitted_count"] or 0,
            "definition": "已明确批准吸收、允许进入导入链路的文件",
        },
        "imported": {
            "available": True,
            "count": gold["current_document_count"],
            "definition": "已进入 SHA-256 导入链路的当前资料",
        },
        "gold_sample": {
            "available": True,
            "count": gold["approved_current_document_count"],
            "definition": (
                "来源仍为当前版本的开发回归样本；不代表企业业务知识已批准"
            ),
        },
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
        "human_attested_verified_evidence_count": summary.get(
            "human_attested_verified_evidence_count", 0
        ),
        "independent_verified_evidence_count": summary.get(
            "independent_verified_evidence_count", 0
        ),
        "solo_attested_verified_evidence_count": summary.get(
            "solo_attested_verified_evidence_count", 0
        ),
        "unattributed_verified_evidence_count": summary.get(
            "unattributed_verified_evidence_count", 0
        ),
        "independent_review_complete": summary.get(
            "independent_review_complete", False
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
    passing_by_assurance = {
        INDEPENDENT_REVIEW_MODE: 0,
        SOLO_ATTESTED_REVIEW_MODE: 0,
        "unattributed": 0,
    }
    with database.connect() as connection:
        audit_rows = connection.execute(
            """
            SELECT details_json FROM audit_log
            WHERE event_type = 'graph_gold_dataset_finalized'
              AND entity_type = 'graph_gold_candidate'
            """
        ).fetchall()
    finalized_outputs: dict[str, dict[str, set[str]]] = {}
    for row in audit_rows:
        try:
            details = json.loads(row["details_json"])
        except json.JSONDecodeError:
            continue
        output = details.get("output")
        content_sha256 = details.get("content_sha256")
        if isinstance(output, str) and isinstance(content_sha256, str):
            mode = details.get("review_mode")
            finalized_outputs.setdefault(output, {}).setdefault(
                content_sha256, set()
            ).add(mode if isinstance(mode, str) else "")
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
        content_sha256 = sha256_text(content)
        audit_modes = finalized_outputs.get(
            relative_path, {}
        ).get(content_sha256)
        if not audit_modes:
            unaudited += 1
            continue
        assurance = _graph_dataset_review_assurance(
            payload, audit_modes
        )
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
            passing_by_assurance[assurance] += 1
    return {
        "valid_graph_gold_dataset_count": valid,
        "stale_graph_gold_dataset_count": stale,
        "passing_graph_gold_dataset_count": passing,
        "human_attested_passing_graph_gold_dataset_count": (
            passing_by_assurance[INDEPENDENT_REVIEW_MODE]
            + passing_by_assurance[SOLO_ATTESTED_REVIEW_MODE]
        ),
        "independent_passing_graph_gold_dataset_count": (
            passing_by_assurance[INDEPENDENT_REVIEW_MODE]
        ),
        "solo_attested_passing_graph_gold_dataset_count": (
            passing_by_assurance[SOLO_ATTESTED_REVIEW_MODE]
        ),
        "unattributed_passing_graph_gold_dataset_count": (
            passing_by_assurance["unattributed"]
        ),
        "unaudited_graph_gold_dataset_count": unaudited,
    }


def _graph_dataset_review_assurance(
    payload: dict[str, Any],
    audit_modes: set[str],
) -> str:
    provenance = payload["provenance"]
    payload_mode = provenance.get("review_mode")
    explicit_modes = {
        mode
        for mode in audit_modes
        if mode in {
            INDEPENDENT_REVIEW_MODE,
            SOLO_ATTESTED_REVIEW_MODE,
        }
    }
    if len(explicit_modes) > 1:
        return "unattributed"
    if explicit_modes:
        mode = next(iter(explicit_modes))
        if payload_mode != mode:
            return "unattributed"
        expected_independent = mode == INDEPENDENT_REVIEW_MODE
        if provenance.get("independent_review") is not expected_independent:
            return "unattributed"
    elif payload_mode in {
        INDEPENDENT_REVIEW_MODE,
        SOLO_ATTESTED_REVIEW_MODE,
    }:
        return "unattributed"
    else:
        mode = (
            INDEPENDENT_REVIEW_MODE
            if provenance["annotator"] != provenance["reviewer"]
            else "unattributed"
        )
    if mode == "unattributed":
        return mode
    if not review_actor_policy_valid(
        submitter=provenance["annotator"],
        reviewer=provenance["reviewer"],
        review_mode=mode,
    ):
        return "unattributed"
    return mode


def _work_pack_status(
    database: Database, paths: WorkspacePaths
) -> dict[str, int]:
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
    graph_gold_work_packs = _graph_gold_work_pack_status(
        database, paths
    )
    conflict_work_packs = _conflict_work_pack_status(
        database, paths
    )
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
        **conflict_work_packs,
        **graph_gold_work_packs,
    }


def _conflict_work_pack_status(
    database: Database,
    paths: WorkspacePaths,
) -> dict[str, int]:
    metrics = {
        "conflict_annotation_work_pack_count": 0,
        "conflict_annotation_ready_count": 0,
        "conflict_annotation_incomplete_count": 0,
        "conflict_annotation_invalid_count": 0,
        "conflict_annotation_applied_count": 0,
        "conflict_review_work_pack_count": 0,
        "conflict_review_ready_count": 0,
        "conflict_review_incomplete_count": 0,
        "conflict_review_invalid_count": 0,
        "conflict_review_applied_count": 0,
    }
    if not paths.evaluations.exists():
        return metrics
    declarations = {
        CONFLICT_ANNOTATION_PACK_TYPE: "annotation",
        CONFLICT_REVIEW_PACK_TYPE: "review",
    }
    invalid_issue_codes = {
        "invalid_work_pack_format",
        "scope_invalid",
        "export_audit_or_template_invalid",
        "source_content_drift",
        "source_phase_invalid",
        "review_actor_policy_conflict",
        "annotator_audit_mismatch",
    }
    for path in sorted(paths.evaluations.glob("*.md")):
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
        work_pack_type = _declared_work_pack_type(
            content, declarations
        )
        if work_pack_type is None:
            continue
        prefix = f"conflict_{work_pack_type}"
        metrics[f"{prefix}_work_pack_count"] += 1
        try:
            status = inspect_conflict_batch_work_pack(
                database, paths, path
            )
        except KnowledgeWorkbenchError:
            metrics[f"{prefix}_invalid_count"] += 1
            continue
        if status.get("already_applied", False):
            metrics[f"{prefix}_applied_count"] += 1
        elif invalid_issue_codes.intersection(
            status.get("issue_codes", [])
        ):
            metrics[f"{prefix}_invalid_count"] += 1
        elif status["apply_ready"]:
            metrics[f"{prefix}_ready_count"] += 1
        else:
            metrics[f"{prefix}_incomplete_count"] += 1
    return metrics


def _graph_gold_work_pack_status(
    database: Database,
    paths: WorkspacePaths,
) -> dict[str, int]:
    metrics = {
        "graph_gold_annotation_work_pack_count": 0,
        "graph_gold_annotation_ready_count": 0,
        "graph_gold_annotation_incomplete_count": 0,
        "graph_gold_annotation_invalid_count": 0,
        "graph_gold_annotation_applied_count": 0,
        "graph_gold_review_work_pack_count": 0,
        "graph_gold_review_ready_count": 0,
        "graph_gold_review_incomplete_count": 0,
        "graph_gold_review_invalid_count": 0,
        "graph_gold_review_applied_count": 0,
    }
    if not paths.evaluations.exists():
        return metrics
    for path in sorted(paths.evaluations.glob("*.md")):
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
        work_pack_type = _declared_work_pack_type(
            content,
            {
                ANNOTATION_PACK_TYPE: "annotation",
                REVIEW_PACK_TYPE: "review",
            },
        )
        if work_pack_type is None:
            continue
        prefix = f"graph_gold_{work_pack_type}"
        metrics[f"{prefix}_work_pack_count"] += 1
        try:
            status = inspect_graph_gold_work_pack(
                database, paths, path
            )
        except KnowledgeWorkbenchError:
            metrics[f"{prefix}_invalid_count"] += 1
            continue
        if status["already_applied"]:
            metrics[f"{prefix}_applied_count"] += 1
        elif not status["integrity_valid"]:
            metrics[f"{prefix}_invalid_count"] += 1
        elif status["apply_ready"]:
            metrics[f"{prefix}_ready_count"] += 1
        else:
            metrics[f"{prefix}_incomplete_count"] += 1
    return metrics


def _declared_work_pack_type(
    content: str,
    declarations: dict[str, str],
) -> str | None:
    lines = content.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    try:
        end = next(
            index
            for index, line in enumerate(lines[1:], start=1)
            if line.strip() == "---"
        )
    except StopIteration:
        return None
    declared_type = None
    for line in lines[1:end]:
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        if key.strip() == "type":
            declared_type = value.strip()
    return declarations.get(declared_type)


def _conflict_annotation_next_action(
    sampling: dict[str, Any],
    work_packs: dict[str, int],
) -> str:
    if work_packs["conflict_annotation_ready_count"] > 0:
        return "应用已完成的冲突标注批次工作包"
    if work_packs["conflict_annotation_incomplete_count"] > 0:
        return (
            "继续逐条回源填写现有冲突标注批次，再运行只读预检"
        )
    if work_packs["conflict_annotation_invalid_count"] > 0:
        return "重新导出来源漂移或完整性失效的冲突标注批次"
    if sampling["remaining_known_conflict_label_count"] > 0:
        return (
            "继续按分层配额标注代表性候选，并主动加入至少 "
            f"{sampling['remaining_known_conflict_label_count']} 条"
            "人工确认的真冲突；不要求标完候选全集"
        )
    return (
        "继续按分层配额标注代表性候选；"
        f"还需 {sampling['remaining_sample_label_count']} 条，"
        "不要求标完候选全集"
    )


def _conflict_review_next_action(
    sampling: dict[str, Any],
    work_packs: dict[str, int],
) -> str:
    if not (
        sampling["annotation_complete"]
        and sampling["known_conflict_labeling_complete"]
    ):
        return "先完成分层样本和已知真冲突标注，不需要处理候选全集"
    if work_packs["conflict_review_ready_count"] > 0:
        return "应用已完成的冲突人工复核批次工作包"
    if work_packs["conflict_review_incomplete_count"] > 0:
        return (
            "继续逐案填写现有冲突复核批次，再运行只读预检"
        )
    if work_packs["conflict_review_invalid_count"] > 0:
        return "重新导出来源漂移或完整性失效的冲突复核批次"
    return (
        "复核达到分层配额的样本和已知真冲突；未抽样候选不形成欠账，"
        "单人模式必须显式记录 solo_attested"
    )


def _graph_gold_next_action(work_packs: dict[str, int]) -> str:
    if work_packs["graph_gold_review_ready_count"] > 0:
        return (
            "应用已完成的图谱黄金复核工作包，固化并运行 "
            "graph-evaluate"
        )
    if work_packs["graph_gold_review_incomplete_count"] > 0:
        return (
            "逐案完成图谱黄金复核工作包后再次运行只读预检"
        )
    if work_packs["graph_gold_annotation_ready_count"] > 0:
        return (
            "应用已通过预检的图谱黄金标注工作包并导出复核包"
        )
    if work_packs["graph_gold_annotation_incomplete_count"] > 0:
        return (
            "填写现有图谱黄金标注包的名称和至少一个真实用例，"
            "再运行只读预检"
        )
    if (
        work_packs["graph_gold_annotation_invalid_count"] > 0
        or work_packs["graph_gold_review_invalid_count"] > 0
    ):
        return (
            "废弃来源漂移或完整性失效的图谱黄金工作包并重新导出"
        )
    return "导出图谱黄金标注工作包并人工填写真实用例"


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


def _validated_conflict_sample_size(value: int) -> int:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or value < 10
        or value > 1000
    ):
        raise KnowledgeWorkbenchError(
            "冲突分层样本建议规模必须是 10 到 1000 之间的整数"
        )
    return value


def _validated_minimum_known_conflicts(
    value: int,
    *,
    conflict_sample_size: int,
) -> int:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or value < 1
        or value > conflict_sample_size
    ):
        raise KnowledgeWorkbenchError(
            "已知真冲突最少数量必须是 1 到冲突样本规模之间的整数"
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
