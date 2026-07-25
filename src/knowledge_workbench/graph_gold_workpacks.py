from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .audit import record_event
from .config import WorkspacePaths
from .database import Database
from .entity_relationships import project_business_relationship_graph
from .errors import InvalidTransitionError, KnowledgeWorkbenchError
from .graph_evaluation import (
    _validate_current_references,
    evaluate_graph_dataset,
)
from .graph_pilot import inspect_graph_pilot_pack, resolve_graph_pilot_pack
from .graph_pilot_review_workpacks import (
    _blockquote,
    _markdown_code,
    _powershell_literal,
    _read_work_pack,
    _require_safe_status,
    _validated_output,
)
from .review_assurance import (
    INDEPENDENT_REVIEW_MODE,
    SOLO_ATTESTATION_PHRASE,
    SOLO_ATTESTED_REVIEW_MODE,
    required_review_mode,
    review_actor_policy_valid,
    review_audit_context,
    review_mode_from_metadata,
    validate_review_actor_policy,
    validate_review_attestation,
)
from .schema_validation import validate_graph_gold_candidate_pack
from .utils import new_id, sha256_text, utc_now
from .wiki import write_text_atomic


ANNOTATION_PACK_TYPE = "graph-gold-annotation-work-pack"
REVIEW_PACK_TYPE = "graph-gold-review-work-pack"
ANNOTATION_EXPORT_EVENT = "graph_gold_annotation_pack_exported"
CANDIDATE_SAVED_EVENT = "graph_gold_candidate_saved"
REVIEW_EXPORT_EVENT = "graph_gold_review_pack_exported"
REVIEW_APPLIED_EVENT = "graph_gold_review_applied"
DATASET_FINALIZED_EVENT = "graph_gold_dataset_finalized"


def inspect_graph_gold_work_pack(
    database: Database,
    paths: WorkspacePaths,
    work_pack_path: Path,
) -> dict[str, Any]:
    work_pack_path, content = _read_work_pack(
        paths, work_pack_path, "图谱黄金工作包"
    )
    metadata, _, _ = _parse_frontmatter_metadata(content)
    pack_type = metadata.get("type")
    if pack_type == ANNOTATION_PACK_TYPE:
        return _inspect_annotation_work_pack(
            database, paths, work_pack_path, content
        )
    if pack_type == REVIEW_PACK_TYPE:
        return _inspect_review_work_pack(
            database, paths, work_pack_path, content
        )
    raise KnowledgeWorkbenchError("工作包类型不正确")


def export_graph_gold_annotation_work_pack(
    database: Database,
    paths: WorkspacePaths,
    source_pack_path: Path,
    output: Path,
    *,
    actor: str,
) -> Path:
    actor = _required_actor(actor)
    output = _validated_output(paths, output, "图谱黄金标注工作包")
    context = _graph_context(database, paths, source_pack_path)
    if not context["projectable_relationships"]:
        raise InvalidTransitionError(
            "当前试点没有可投影业务关系，不能开始图谱黄金标注"
        )
    lines = [
        "---",
        f"type: {ANNOTATION_PACK_TYPE}",
        f"graph_pilot_pack_id: {context['pack']['pack_id']}",
        f"source_pack_path: {context['source_relative']}",
        f"source_content_sha256: {context['source_sha256']}",
        f"graph_snapshot_sha256: {context['graph_snapshot_sha256']}",
        f"annotator: {actor}",
        f"generated_at: {utc_now()}",
        "---",
        "",
        f"# 图谱黄金用例标注：`{context['pack']['pack_id']}`",
        "",
        f"- 标注人：`{_markdown_code(actor)}`",
        "- 标注人必须独立判断正例、负例、方向、路径和黄金证据。",
        "- 当前业务图仅用于候选定位，不能自动成为黄金答案。",
        "- 所有实体必须来自本试点人工提及范围；正例证据必须来自本试点。",
        "- 至少填写一个关系或路径用例，全局 case_id 必须唯一。",
        "- 标注 JSON 是本工作包唯一可编辑内容；其余目录、原文和快照均受保护。",
        "",
        "## 应用标注并保存不可变候选包",
        "",
        "```powershell",
        (
            f".\\.venv\\Scripts\\knowledge.exe graph "
            f"pilot-gold-annotation-apply {_powershell_literal(str(output))} "
            ".\\workspace\\evaluations\\graph-gold-candidate.json "
            f"--actor {_powershell_literal(actor)}"
        ),
        "```",
        "",
        "## 图谱黄金标注 JSON",
        "",
        "- 图谱黄金标注 JSON："
        + json.dumps(
            {"name": "", "relation_cases": [], "path_cases": []},
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        "",
        "## 当前安全业务图候选",
        "",
        "- 图投影 JSON："
        + json.dumps(
            context["graph"],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
        "",
        "## 试点实体范围",
        "",
        "- 实体 JSON："
        + json.dumps(
            context["entity_catalog"],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
        "",
        "## 已验证试点证据",
        "",
    ]
    for candidate in context["verified_candidates"]:
        lines.extend(
            [
                f"### `{candidate['evidence_id']}`",
                "",
                f"- 来源：`{_markdown_code(candidate['document_name'])}`",
                f"- 密级：`{candidate['classification']}`",
                "- 定位 JSON："
                + json.dumps(candidate["locators"], ensure_ascii=False),
                "",
                *_blockquote(candidate["excerpt"]),
                "",
            ]
        )
    content = "\n".join(lines).rstrip() + "\n"
    write_text_atomic(output, content)
    try:
        with database.transaction() as connection:
            record_event(
                connection,
                ANNOTATION_EXPORT_EVENT,
                "graph_pilot_pack",
                context["pack"]["pack_id"],
                actor=actor,
                details={
                    "output": _relative(paths, output),
                    "source_pack_path": context["source_relative"],
                    "source_content_sha256": context["source_sha256"],
                    "graph_snapshot_sha256": context[
                        "graph_snapshot_sha256"
                    ],
                    "entity_scope_sha256": context[
                        "entity_scope_sha256"
                    ],
                    "evidence_scope_sha256": context[
                        "evidence_scope_sha256"
                    ],
                    "template_sha256": _annotation_template_sha256(
                        content
                    ),
                    "content_sha256": sha256_text(content),
                },
            )
    except Exception:
        output.unlink(missing_ok=True)
        raise
    return output


def apply_graph_gold_annotation_work_pack(
    database: Database,
    paths: WorkspacePaths,
    work_pack_path: Path,
    output: Path,
    *,
    actor: str,
) -> dict[str, Any]:
    actor = _required_actor(actor)
    output = _validated_json_output(paths, output, "图谱黄金候选包")
    work_pack_path, content = _read_work_pack(
        paths, work_pack_path, "图谱黄金标注工作包"
    )
    metadata, annotation = _parse_annotation_pack(content)
    if metadata["annotator"] != actor:
        raise KnowledgeWorkbenchError(
            "工作包声明的图谱标注人与当前 actor 不一致"
        )
    source_path = _source_path(paths, metadata["source_pack_path"])
    context = _graph_context(database, paths, source_path)
    _validate_context_metadata(metadata, context)
    _validate_annotation_export(
        database,
        paths,
        work_pack_path,
        content,
        metadata,
        context,
        actor=actor,
    )
    candidate = {
        "schema_version": "1.0",
        "pack_type": "graph-gold-candidate-pack",
        "candidate_id": new_id("graphgold"),
        "status": "reviewing",
        "name": annotation["name"],
        "source": {
            "graph_pilot_pack_id": context["pack"]["pack_id"],
            "graph_pilot_pack_path": context["source_relative"],
            "graph_pilot_pack_sha256": context["source_sha256"],
            "graph_snapshot_sha256": context["graph_snapshot_sha256"],
        },
        "annotator": actor,
        "annotated_at": utc_now(),
        "relation_cases": annotation["relation_cases"],
        "path_cases": annotation["path_cases"],
    }
    _validate_candidate(database, context, candidate)
    _ensure_annotation_not_applied(
        database,
        _relative(paths, work_pack_path),
    )
    candidate_content = (
        json.dumps(
            candidate,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )
    write_text_atomic(output, candidate_content)
    try:
        with database.transaction() as connection:
            record_event(
                connection,
                CANDIDATE_SAVED_EVENT,
                "graph_gold_candidate",
                candidate["candidate_id"],
                actor=actor,
                details={
                    "output": _relative(paths, output),
                    "content_sha256": sha256_text(candidate_content),
                    "graph_pilot_pack_id": context["pack"]["pack_id"],
                    "graph_snapshot_sha256": context[
                        "graph_snapshot_sha256"
                    ],
                    "annotation_work_pack_path": _relative(
                        paths, work_pack_path
                    ),
                    "annotation_work_pack_sha256": sha256_text(content),
                    "relation_case_count": len(
                        candidate["relation_cases"]
                    ),
                    "path_case_count": len(candidate["path_cases"]),
                    "case_scope_sha256": _case_scope_sha256(candidate),
                },
            )
    except Exception:
        output.unlink(missing_ok=True)
        raise
    return {
        "candidate_id": candidate["candidate_id"],
        "output": _relative(paths, output),
        "relation_case_count": len(candidate["relation_cases"]),
        "path_case_count": len(candidate["path_cases"]),
        "annotator": actor,
    }


def export_graph_gold_review_work_pack(
    database: Database,
    paths: WorkspacePaths,
    candidate_path: Path,
    output: Path,
    *,
    actor: str,
    review_mode: str = INDEPENDENT_REVIEW_MODE,
) -> Path:
    actor = _required_actor(actor)
    review_mode = required_review_mode(review_mode)
    output = _validated_output(paths, output, "图谱黄金人工复核工作包")
    candidate_path, candidate_content, candidate, context = (
        _resolve_candidate(database, paths, candidate_path)
    )
    try:
        validate_review_actor_policy(
            submitter=candidate["annotator"],
            reviewer=actor,
            review_mode=review_mode,
        )
    except KnowledgeWorkbenchError as exc:
        raise InvalidTransitionError(str(exc)) from exc
    cases = _candidate_cases(candidate)
    evidence = _evidence_details(
        database,
        {
            evidence_id
            for _, case in cases
            for evidence_id in case[
                "expected_supporting_evidence_ids"
            ]
        },
    )
    entities = _entity_details(
        database,
        {
            entity_id
            for kind, case in cases
            for entity_id in _case_entity_ids(kind, case)
        },
    )
    lines = [
        "---",
        f"type: {REVIEW_PACK_TYPE}",
        f"graph_gold_candidate_id: {candidate['candidate_id']}",
        f"candidate_path: {_relative(paths, candidate_path)}",
        f"candidate_content_sha256: {sha256_text(candidate_content)}",
        f"graph_snapshot_sha256: {context['graph_snapshot_sha256']}",
        f"annotator: {candidate['annotator']}",
        f"reviewer: {actor}",
        f"review_mode: {review_mode}",
        f"generated_at: {utc_now()}",
        "---",
        "",
        f"# 图谱黄金人工复核：`{candidate['candidate_id']}`",
        "",
        f"- 标注人：`{_markdown_code(candidate['annotator'])}`",
        f"- 复核人：`{_markdown_code(actor)}`",
        (
            "- 复核模式：`independent`（标注人与复核人不同）。"
            if review_mode == INDEPENDENT_REVIEW_MODE
            else (
                "- 复核模式：`solo_attested`（同一责任人二次确认，"
                "不构成独立复核）。"
            )
        ),
        f"- 用例数：`{len(cases)}`",
        "- 复核人必须逐案回到实体、方向、黄金证据和当前来源判断。",
        "- 每案必须且只能批准或驳回；驳回意见必填。",
        "- 只有全部批准才会固化最终数据集并立即运行 graph-evaluate。",
        "",
        "## 应用复核",
        "",
        "```powershell",
        (
            f".\\.venv\\Scripts\\knowledge.exe graph "
            f"pilot-gold-review-apply {_powershell_literal(str(output))} "
            ".\\workspace\\evaluations\\graph-gold-v1.json "
            f"--actor {_powershell_literal(actor)}"
            + (
                " --solo-attestation "
                + _powershell_literal(SOLO_ATTESTATION_PHRASE)
                if review_mode == SOLO_ATTESTED_REVIEW_MODE
                else ""
            )
        ),
        "```",
        "",
    ]
    for index, (kind, case) in enumerate(cases, start=1):
        lines.extend(
            [
                f"## Case {index:04d}",
                "",
                "- Case ID JSON："
                + json.dumps(case["case_id"], ensure_ascii=False),
                "- Case 类型：`" + kind + "`",
                "- [ ] 批准",
                "- [ ] 驳回",
                '- 复核意见 JSON：""',
                "- 用例 JSON："
                + json.dumps(
                    case,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                "- 实体 JSON："
                + json.dumps(
                    [
                        entities[entity_id]
                        for entity_id in _case_entity_ids(kind, case)
                    ],
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                "",
            ]
        )
        for evidence_id in case["expected_supporting_evidence_ids"]:
            item = evidence[evidence_id]
            lines.extend(
                [
                    f"### 证据 `{evidence_id}`",
                    "",
                    f"- 来源：`{_markdown_code(item['document_name'])}`",
                    "- 定位 JSON："
                    + json.dumps(item["locators"], ensure_ascii=False),
                    "",
                    *_blockquote(item["excerpt"]),
                    "",
                ]
            )
    content = "\n".join(lines).rstrip() + "\n"
    write_text_atomic(output, content)
    try:
        with database.transaction() as connection:
            record_event(
                connection,
                REVIEW_EXPORT_EVENT,
                "graph_gold_candidate",
                candidate["candidate_id"],
                actor=actor,
                details={
                    "output": _relative(paths, output),
                    "candidate_path": _relative(paths, candidate_path),
                    "candidate_content_sha256": sha256_text(
                        candidate_content
                    ),
                    "graph_snapshot_sha256": context[
                        "graph_snapshot_sha256"
                    ],
                    "case_scope_sha256": _case_scope_sha256(candidate),
                    "case_count": len(cases),
                    "template_sha256": _review_template_sha256(content),
                    "content_sha256": sha256_text(content),
                    "annotator": candidate["annotator"],
                    **review_audit_context(review_mode, None),
                },
            )
    except Exception:
        output.unlink(missing_ok=True)
        raise
    return output


def apply_graph_gold_review_work_pack(
    database: Database,
    paths: WorkspacePaths,
    work_pack_path: Path,
    output: Path,
    *,
    actor: str,
    solo_attestation: str | None = None,
) -> dict[str, Any]:
    actor = _required_actor(actor)
    work_pack_path, content = _read_work_pack(
        paths, work_pack_path, "图谱黄金人工复核工作包"
    )
    metadata, decisions = _parse_review_pack(content)
    review_mode = metadata["review_mode"]
    attestation_sha256 = validate_review_attestation(
        review_mode, solo_attestation
    )
    if metadata["reviewer"] != actor:
        raise KnowledgeWorkbenchError(
            "工作包声明的图谱复核人与当前 actor 不一致"
        )
    validate_review_actor_policy(
        submitter=metadata["annotator"],
        reviewer=actor,
        review_mode=review_mode,
    )
    candidate_path = _source_path(paths, metadata["candidate_path"])
    candidate_path, candidate_content, candidate, context = (
        _resolve_candidate(database, paths, candidate_path)
    )
    if candidate["candidate_id"] != metadata["graph_gold_candidate_id"]:
        raise KnowledgeWorkbenchError("图谱黄金候选包身份不一致")
    if sha256_text(candidate_content) != metadata[
        "candidate_content_sha256"
    ]:
        raise KnowledgeWorkbenchError("图谱黄金候选包内容哈希已变化")
    if context["graph_snapshot_sha256"] != metadata[
        "graph_snapshot_sha256"
    ]:
        raise KnowledgeWorkbenchError("业务图快照已变化")
    _validate_review_export(
        database,
        paths,
        work_pack_path,
        content,
        metadata,
        candidate,
        actor=actor,
    )
    expected_cases = _candidate_cases(candidate)
    expected_ids = [case["case_id"] for _, case in expected_cases]
    if [item["case_id"] for item in decisions] != expected_ids:
        raise KnowledgeWorkbenchError("复核工作包用例范围或顺序不一致")
    rejected = [item for item in decisions if not item["approved"]]
    work_pack_sha256 = sha256_text(content)
    relative_work_pack = _relative(paths, work_pack_path)
    _ensure_review_not_applied(
        database,
        candidate["candidate_id"],
        relative_work_pack,
    )
    if rejected:
        with database.transaction() as connection:
            _record_review_applied(
                connection,
                candidate,
                actor,
                relative_work_pack,
                work_pack_sha256,
                decisions,
                approved=False,
                output=None,
                review_mode=review_mode,
                attestation_sha256=attestation_sha256,
            )
        return {
            "candidate_id": candidate["candidate_id"],
            "approved": False,
            "rejected_case_count": len(rejected),
            "rejected_case_ids": [
                item["case_id"] for item in rejected
            ],
            "dataset_written": False,
            "reviewer": actor,
            **review_audit_context(review_mode, attestation_sha256),
        }
    output = _validated_json_output(paths, output, "图谱黄金评测数据集")
    dataset = {
        "schema_version": "1.0",
        "name": candidate["name"],
        "provenance": {
            "annotator": candidate["annotator"],
            "reviewer": actor,
            "reviewed_at": utc_now(),
            "decision": "approved",
            "review_mode": review_mode,
            "independent_review": (
                review_mode == INDEPENDENT_REVIEW_MODE
            ),
        },
        "relation_cases": candidate["relation_cases"],
        "path_cases": candidate["path_cases"],
    }
    dataset_content = (
        json.dumps(
            dataset,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )
    write_text_atomic(output, dataset_content)
    try:
        report = evaluate_graph_dataset(database, output)
        with database.transaction() as connection:
            _record_review_applied(
                connection,
                candidate,
                actor,
                relative_work_pack,
                work_pack_sha256,
                decisions,
                approved=True,
                output=_relative(paths, output),
                review_mode=review_mode,
                attestation_sha256=attestation_sha256,
            )
            record_event(
                connection,
                DATASET_FINALIZED_EVENT,
                "graph_gold_candidate",
                candidate["candidate_id"],
                actor=actor,
                details={
                    "output": _relative(paths, output),
                    "content_sha256": sha256_text(dataset_content),
                    "graph_snapshot_sha256": context[
                        "graph_snapshot_sha256"
                    ],
                    "case_scope_sha256": _case_scope_sha256(candidate),
                    "relation_case_count": len(
                        candidate["relation_cases"]
                    ),
                    "path_case_count": len(candidate["path_cases"]),
                    "evaluation_pass_rate": report["aggregate"][
                        "pass_rate"
                    ],
                    "restricted_support_leak_count": report["safety"][
                        "restricted_support_leak_count"
                    ],
                    **review_audit_context(
                        review_mode, attestation_sha256
                    ),
                },
            )
    except Exception:
        output.unlink(missing_ok=True)
        raise
    return {
        "candidate_id": candidate["candidate_id"],
        "approved": True,
        "dataset_written": True,
        "output": _relative(paths, output),
        "reviewer": actor,
        **review_audit_context(review_mode, attestation_sha256),
        "evaluation": {
            "case_count": report["aggregate"]["case_count"],
            "passed_cases": report["aggregate"]["passed_cases"],
            "pass_rate": report["aggregate"]["pass_rate"],
            "restricted_support_leak_count": report["safety"][
                "restricted_support_leak_count"
            ],
        },
    }


def _graph_context(
    database: Database,
    paths: WorkspacePaths,
    source_pack_path: Path,
) -> dict[str, Any]:
    source_path, source_content, pack = resolve_graph_pilot_pack(
        database, paths, source_pack_path
    )
    status = inspect_graph_pilot_pack(database, paths, source_path)
    _require_safe_status(status)
    verified_ids = {
        item["evidence_id"]
        for item in status["items"]
        if item["status"] == "verified"
    }
    verified_candidates = [
        item
        for item in pack["candidates"]
        if item["evidence_id"] in verified_ids
    ]
    if not verified_candidates:
        raise InvalidTransitionError(
            "图谱试点没有 verified 证据，不能构建黄金用例"
        )
    entity_catalog = _pilot_entity_catalog(database, verified_ids)
    if len(entity_catalog) < 2:
        raise InvalidTransitionError(
            "图谱试点至少需要两个 active 人工实体"
        )
    graph = project_business_relationship_graph(database, limit=500)
    if graph["summary"]["truncated"]:
        raise KnowledgeWorkbenchError(
            "当前业务图超过 500 条边，不能构建完整黄金快照"
        )
    graph_snapshot_sha256 = _canonical_sha256(graph)
    return {
        "source_path": source_path,
        "source_relative": _relative(paths, source_path),
        "source_content": source_content,
        "source_sha256": sha256_text(source_content),
        "pack": pack,
        "status": status,
        "verified_ids": verified_ids,
        "verified_candidates": verified_candidates,
        "entity_catalog": entity_catalog,
        "entity_ids": {item["entity_id"] for item in entity_catalog},
        "entity_scope_sha256": _canonical_sha256(entity_catalog),
        "evidence_scope_sha256": _canonical_sha256(
            sorted(verified_ids)
        ),
        "graph": graph,
        "graph_snapshot_sha256": graph_snapshot_sha256,
        "projectable_relationships": graph["edges"],
    }


def _pilot_entity_catalog(
    database: Database, evidence_ids: set[str]
) -> list[dict[str, str]]:
    placeholders = ", ".join("?" for _ in evidence_ids)
    with database.connect() as connection:
        rows = connection.execute(
            f"""
            SELECT DISTINCT ce.id AS entity_id, ce.canonical_name,
                            ce.entity_type
            FROM evidence_entity_mentions eem
            JOIN canonical_entities ce ON ce.id = eem.entity_id
            WHERE eem.evidence_id IN ({placeholders})
              AND ce.status = 'active'
            ORDER BY ce.id
            """,
            sorted(evidence_ids),
        ).fetchall()
    return [dict(row) for row in rows]


def _validate_candidate(
    database: Database, context: dict[str, Any], candidate: dict
) -> None:
    validate_graph_gold_candidate_pack(candidate)
    dataset = _candidate_dataset(candidate, reviewer="__scope_reviewer__")
    _validate_current_references(database, dataset)
    case_entity_ids = {
        entity_id
        for kind, case in _candidate_cases(candidate)
        for entity_id in _case_entity_ids(kind, case)
    }
    if not case_entity_ids.issubset(context["entity_ids"]):
        missing = sorted(case_entity_ids - context["entity_ids"])
        raise KnowledgeWorkbenchError(
            "图谱黄金用例引用了试点范围外实体：" + ", ".join(missing)
        )
    case_evidence_ids = {
        evidence_id
        for _, case in _candidate_cases(candidate)
        for evidence_id in case["expected_supporting_evidence_ids"]
    }
    if not case_evidence_ids.issubset(context["verified_ids"]):
        missing = sorted(case_evidence_ids - context["verified_ids"])
        raise KnowledgeWorkbenchError(
            "图谱黄金用例引用了试点范围外证据：" + ", ".join(missing)
        )
    relation_keys = {
        case["relation_key"] for case in candidate["relation_cases"]
    }
    relation_keys.update(
        relation_key
        for case in candidate["path_cases"]
        for relation_key in case["expected_relation_keys"]
    )
    if relation_keys:
        placeholders = ", ".join("?" for _ in relation_keys)
        with database.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT relation_key FROM entity_relation_types
                WHERE relation_key IN ({placeholders})
                  AND status = 'active'
                """,
                sorted(relation_keys),
            ).fetchall()
        active_keys = {row["relation_key"] for row in rows}
        if active_keys != relation_keys:
            raise KnowledgeWorkbenchError(
                "图谱黄金用例引用了不存在或非 active 的关系类型："
                + ", ".join(sorted(relation_keys - active_keys))
            )


def _resolve_candidate(
    database: Database,
    paths: WorkspacePaths,
    candidate_path: Path,
) -> tuple[Path, str, dict, dict[str, Any]]:
    candidate_path = _validated_existing_json(
        paths, candidate_path, "图谱黄金候选包"
    )
    try:
        content = candidate_path.read_text(encoding="utf-8")
        candidate = json.loads(content)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise KnowledgeWorkbenchError(
            "图谱黄金候选包无法安全读取或不是有效 JSON"
        ) from exc
    validate_graph_gold_candidate_pack(candidate)
    source_path = _source_path(
        paths, candidate["source"]["graph_pilot_pack_path"]
    )
    context = _graph_context(database, paths, source_path)
    if (
        candidate["source"]["graph_pilot_pack_id"]
        != context["pack"]["pack_id"]
        or candidate["source"]["graph_pilot_pack_sha256"]
        != context["source_sha256"]
        or candidate["source"]["graph_snapshot_sha256"]
        != context["graph_snapshot_sha256"]
    ):
        raise KnowledgeWorkbenchError(
            "图谱黄金候选包来源或业务图快照已变化"
        )
    _validate_candidate(database, context, candidate)
    relative = _relative(paths, candidate_path)
    with database.connect() as connection:
        rows = connection.execute(
            """
            SELECT actor, details_json FROM audit_log
            WHERE event_type = ?
              AND entity_type = 'graph_gold_candidate'
              AND entity_id = ?
            """,
            (CANDIDATE_SAVED_EVENT, candidate["candidate_id"]),
        ).fetchall()
    for row in rows:
        try:
            details = json.loads(row["details_json"])
        except json.JSONDecodeError:
            continue
        if (
            row["actor"] == candidate["annotator"]
            and details.get("output") == relative
            and details.get("content_sha256") == sha256_text(content)
            and details.get("case_scope_sha256")
            == _case_scope_sha256(candidate)
        ):
            return candidate_path, content, candidate, context
    raise KnowledgeWorkbenchError(
        "图谱黄金候选包缺少匹配的保存审计记录"
    )


def _parse_annotation_pack(
    content: str,
) -> tuple[dict[str, str], dict[str, Any]]:
    metadata, lines, _ = _parse_frontmatter(
        content, ANNOTATION_PACK_TYPE
    )
    pattern = re.compile(r"^-\s+图谱黄金标注 JSON：(.*)$")
    values = [match.group(1) for line in lines if (match := pattern.match(line))]
    if len(values) != 1:
        raise KnowledgeWorkbenchError(
            "图谱黄金标注工作包必须且只能包含一个标注 JSON"
        )
    try:
        annotation = json.loads(values[0])
    except json.JSONDecodeError as exc:
        raise KnowledgeWorkbenchError("图谱黄金标注不是有效 JSON") from exc
    if not isinstance(annotation, dict) or set(annotation) != {
        "name",
        "relation_cases",
        "path_cases",
    }:
        raise KnowledgeWorkbenchError("图谱黄金标注 JSON 字段无效")
    return metadata, annotation


def _parse_review_pack(
    content: str,
) -> tuple[dict[str, str], list[dict[str, Any]]]:
    metadata, lines, start = _parse_frontmatter(
        content, REVIEW_PACK_TYPE
    )
    heading = re.compile(r"^##\s+Case\s+(\d{4})\s*$")
    case_id_pattern = re.compile(r"^-\s+Case ID JSON：(.*)$")
    approve_pattern = re.compile(r"^-\s+\[([ xX])\]\s+批准\s*$")
    reject_pattern = re.compile(r"^-\s+\[([ xX])\]\s+驳回\s*$")
    note_pattern = re.compile(r"^-\s+复核意见 JSON：(.*)$")
    records = []
    current = None
    for line in lines[start:]:
        if match := heading.match(line):
            expected_index = len(records) + 1
            if int(match.group(1)) != expected_index:
                raise KnowledgeWorkbenchError("复核用例编号不连续")
            current = {
                "case_id": None,
                "approve": False,
                "reject": False,
                "note": None,
                "seen": set(),
            }
            records.append(current)
            continue
        if current is None:
            continue
        for key, pattern in (
            ("case_id", case_id_pattern),
            ("approve", approve_pattern),
            ("reject", reject_pattern),
            ("note", note_pattern),
        ):
            match = pattern.match(line)
            if not match:
                continue
            if key in current["seen"]:
                raise KnowledgeWorkbenchError(
                    f"复核用例包含重复字段：{key}"
                )
            current["seen"].add(key)
            value = match.group(1)
            if key == "case_id":
                try:
                    current["case_id"] = json.loads(value)
                except json.JSONDecodeError as exc:
                    raise KnowledgeWorkbenchError(
                        "复核用例 case_id 不是有效 JSON 字符串"
                    ) from exc
                if not isinstance(current["case_id"], str):
                    raise KnowledgeWorkbenchError(
                        "复核用例 case_id 必须是字符串"
                    )
            elif key in {"approve", "reject"}:
                current[key] = value.lower() == "x"
            else:
                current["note"] = _json_note(value)
            break
    if not records:
        raise KnowledgeWorkbenchError("复核工作包不包含用例")
    decisions = []
    for record in records:
        if record["seen"] != {"case_id", "approve", "reject", "note"}:
            raise KnowledgeWorkbenchError("复核用例字段不完整")
        if int(record["approve"]) + int(record["reject"]) != 1:
            raise KnowledgeWorkbenchError(
                f"用例 {record['case_id']} 必须且只能批准或驳回"
            )
        if record["reject"] and not record["note"]:
            raise KnowledgeWorkbenchError(
                f"用例 {record['case_id']} 驳回时必须填写意见"
            )
        decisions.append(
            {
                "case_id": record["case_id"],
                "approved": record["approve"],
                "note": record["note"],
            }
        )
    metadata["review_mode"] = review_mode_from_metadata(metadata)
    return metadata, decisions


def _parse_frontmatter(
    content: str, expected_type: str
) -> tuple[dict[str, str], list[str], int]:
    metadata, lines, start = _parse_frontmatter_metadata(content)
    if metadata.get("type") != expected_type:
        raise KnowledgeWorkbenchError("工作包类型不正确")
    return metadata, lines, start


def _parse_frontmatter_metadata(
    content: str,
) -> tuple[dict[str, str], list[str], int]:
    lines = content.splitlines()
    if not lines or lines[0].strip() != "---":
        raise KnowledgeWorkbenchError("工作包缺少 YAML Frontmatter")
    try:
        end = next(
            index
            for index, line in enumerate(lines[1:], start=1)
            if line.strip() == "---"
        )
    except StopIteration as exc:
        raise KnowledgeWorkbenchError(
            "工作包 YAML Frontmatter 未闭合"
        ) from exc
    metadata = {}
    for line in lines[1:end]:
        if ":" in line:
            key, value = line.split(":", 1)
            metadata[key.strip()] = value.strip()
    return metadata, lines, end + 1


def _inspect_annotation_work_pack(
    database: Database,
    paths: WorkspacePaths,
    work_pack_path: Path,
    content: str,
) -> dict[str, Any]:
    issue_codes: list[str] = []
    metadata: dict[str, str] = {}
    annotation: dict[str, Any] = {}
    structure_valid = True
    try:
        metadata, annotation = _parse_annotation_pack(content)
    except KnowledgeWorkbenchError:
        structure_valid = False
        issue_codes.append("annotation_structure_invalid")

    context = None
    source_snapshot_valid = False
    export_audit_valid = False
    if structure_valid:
        try:
            source_path = _source_path(
                paths, metadata["source_pack_path"]
            )
            context = _graph_context(database, paths, source_path)
            _validate_context_metadata(metadata, context)
            source_snapshot_valid = True
        except (KeyError, KnowledgeWorkbenchError):
            issue_codes.append("source_snapshot_invalid")
        if context is not None and source_snapshot_valid:
            try:
                _validate_annotation_export(
                    database,
                    paths,
                    work_pack_path,
                    content,
                    metadata,
                    context,
                    actor=metadata["annotator"],
                )
                export_audit_valid = True
            except (KeyError, KnowledgeWorkbenchError):
                issue_codes.append(
                    "protected_content_or_export_audit_invalid"
                )

    name_present = bool(
        isinstance(annotation.get("name"), str)
        and annotation["name"].strip()
    )
    relation_cases = annotation.get("relation_cases")
    path_cases = annotation.get("path_cases")
    relation_case_count = (
        len(relation_cases) if isinstance(relation_cases, list) else 0
    )
    path_case_count = (
        len(path_cases) if isinstance(path_cases, list) else 0
    )
    case_count = relation_case_count + path_case_count
    if structure_valid and not name_present:
        issue_codes.append("dataset_name_missing")
    if structure_valid and case_count == 0:
        issue_codes.append("case_missing")

    annotation_valid = False
    if (
        structure_valid
        and source_snapshot_valid
        and name_present
        and case_count > 0
        and context is not None
    ):
        preview = {
            "schema_version": "1.0",
            "pack_type": "graph-gold-candidate-pack",
            "candidate_id": "graphgold_" + "0" * 32,
            "status": "reviewing",
            "name": annotation["name"],
            "source": {
                "graph_pilot_pack_id": context["pack"]["pack_id"],
                "graph_pilot_pack_path": context["source_relative"],
                "graph_pilot_pack_sha256": context["source_sha256"],
                "graph_snapshot_sha256": context[
                    "graph_snapshot_sha256"
                ],
            },
            "annotator": metadata.get("annotator", ""),
            "annotated_at": metadata.get("generated_at", utc_now()),
            "relation_cases": relation_cases,
            "path_cases": path_cases,
        }
        try:
            _validate_candidate(database, context, preview)
            annotation_valid = True
        except KnowledgeWorkbenchError:
            issue_codes.append("annotation_semantics_invalid")

    already_applied = False
    if structure_valid:
        try:
            _ensure_annotation_not_applied(
                database, _relative(paths, work_pack_path)
            )
        except InvalidTransitionError:
            already_applied = True
            issue_codes.append("work_pack_already_applied")

    integrity_valid = (
        structure_valid
        and source_snapshot_valid
        and export_audit_valid
    )
    return {
        "schema_version": "1.0",
        "kind": "graph-gold-work-pack-status",
        "work_pack_path": _relative(paths, work_pack_path),
        "content_sha256": sha256_text(content),
        "work_pack_type": "annotation",
        "graph_pilot_pack_id": metadata.get(
            "graph_pilot_pack_id"
        ),
        "actor_role": "annotator",
        "actor": metadata.get("annotator"),
        "structure_valid": structure_valid,
        "source_snapshot_valid": source_snapshot_valid,
        "export_audit_valid": export_audit_valid,
        "integrity_valid": integrity_valid,
        "dataset_name_present": name_present,
        "relation_case_count": relation_case_count,
        "path_case_count": path_case_count,
        "case_count": case_count,
        "annotation_valid": annotation_valid,
        "already_applied": already_applied,
        "issue_codes": list(dict.fromkeys(issue_codes)),
        "apply_ready": (
            integrity_valid
            and annotation_valid
            and not already_applied
        ),
    }


def _inspect_review_work_pack(
    database: Database,
    paths: WorkspacePaths,
    work_pack_path: Path,
    content: str,
) -> dict[str, Any]:
    issue_codes: list[str] = []
    try:
        metadata, records = _inspect_review_records(content)
        structure_valid = True
    except KnowledgeWorkbenchError:
        metadata = {}
        records = []
        structure_valid = False
        issue_codes.append("review_structure_invalid")

    review_mode = None
    actor_policy_valid = False
    if structure_valid:
        try:
            review_mode = review_mode_from_metadata(metadata)
            actor_policy_valid = review_actor_policy_valid(
                submitter=metadata["annotator"],
                reviewer=metadata["reviewer"],
                review_mode=review_mode,
            )
        except (KeyError, KnowledgeWorkbenchError):
            actor_policy_valid = False
        if not actor_policy_valid:
            issue_codes.append("review_actor_policy_invalid")

    source_snapshot_valid = False
    export_audit_valid = False
    case_scope_valid = False
    candidate = None
    if structure_valid:
        try:
            candidate_path = _source_path(
                paths, metadata["candidate_path"]
            )
            (
                _,
                candidate_content,
                candidate,
                context,
            ) = _resolve_candidate(database, paths, candidate_path)
            if (
                candidate["candidate_id"]
                != metadata["graph_gold_candidate_id"]
                or sha256_text(candidate_content)
                != metadata["candidate_content_sha256"]
                or context["graph_snapshot_sha256"]
                != metadata["graph_snapshot_sha256"]
            ):
                raise KnowledgeWorkbenchError(
                    "图谱黄金候选包或业务图快照已变化"
                )
            source_snapshot_valid = True
            expected_ids = [
                case["case_id"]
                for _, case in _candidate_cases(candidate)
            ]
            case_scope_valid = [
                item["case_id"] for item in records
            ] == expected_ids
            if not case_scope_valid:
                issue_codes.append("case_scope_invalid")
            try:
                _validate_review_export(
                    database,
                    paths,
                    work_pack_path,
                    content,
                    metadata,
                    candidate,
                    actor=metadata["reviewer"],
                )
                export_audit_valid = True
            except KnowledgeWorkbenchError:
                issue_codes.append(
                    "protected_content_or_export_audit_invalid"
                )
        except (KeyError, KnowledgeWorkbenchError):
            issue_codes.append("source_snapshot_invalid")

    decision_counts = {
        "approved": 0,
        "rejected": 0,
        "undecided": 0,
        "conflicting": 0,
    }
    missing_note_count = 0
    for record in records:
        if record["approved"] is True:
            decision_counts["approved"] += 1
        elif record["approved"] is False:
            decision_counts["rejected"] += 1
            if not record["note"]:
                missing_note_count += 1
        elif record["decision_conflicting"]:
            decision_counts["conflicting"] += 1
        else:
            decision_counts["undecided"] += 1
    if decision_counts["undecided"]:
        issue_codes.append("decision_missing")
    if decision_counts["conflicting"]:
        issue_codes.append("decision_conflicting")
    if missing_note_count:
        issue_codes.append("required_note_missing")

    already_applied = False
    if candidate is not None:
        try:
            _ensure_review_not_applied(
                database,
                candidate["candidate_id"],
                _relative(paths, work_pack_path),
            )
        except InvalidTransitionError:
            already_applied = True
            issue_codes.append("work_pack_already_applied")

    decisions_complete = (
        len(records) > 0
        and decision_counts["undecided"] == 0
        and decision_counts["conflicting"] == 0
        and missing_note_count == 0
    )
    integrity_valid = (
        structure_valid
        and source_snapshot_valid
        and export_audit_valid
        and case_scope_valid
        and actor_policy_valid
    )
    return {
        "schema_version": "1.0",
        "kind": "graph-gold-work-pack-status",
        "work_pack_path": _relative(paths, work_pack_path),
        "content_sha256": sha256_text(content),
        "work_pack_type": "review",
        "graph_gold_candidate_id": metadata.get(
            "graph_gold_candidate_id"
        ),
        "actor_role": "reviewer",
        "actor": metadata.get("reviewer"),
        "review_mode": review_mode,
        "independent_review": (
            review_mode == INDEPENDENT_REVIEW_MODE
            if review_mode is not None
            else None
        ),
        "solo_attestation_required": (
            review_mode == SOLO_ATTESTED_REVIEW_MODE
        ),
        "review_actor_policy_valid": actor_policy_valid,
        "structure_valid": structure_valid,
        "source_snapshot_valid": source_snapshot_valid,
        "export_audit_valid": export_audit_valid,
        "case_scope_valid": case_scope_valid,
        "integrity_valid": integrity_valid,
        "case_count": len(records),
        "decision_counts": decision_counts,
        "missing_required_note_count": missing_note_count,
        "decisions_complete": decisions_complete,
        "already_applied": already_applied,
        "issue_codes": list(dict.fromkeys(issue_codes)),
        "apply_ready": (
            integrity_valid
            and decisions_complete
            and not already_applied
        ),
    }


def _inspect_review_records(
    content: str,
) -> tuple[dict[str, str], list[dict[str, Any]]]:
    metadata, lines, start = _parse_frontmatter(
        content, REVIEW_PACK_TYPE
    )
    heading = re.compile(r"^##\s+Case\s+(\d{4})\s*$")
    case_id_pattern = re.compile(r"^-\s+Case ID JSON：(.*)$")
    approve_pattern = re.compile(r"^-\s+\[([ xX])\]\s+批准\s*$")
    reject_pattern = re.compile(r"^-\s+\[([ xX])\]\s+驳回\s*$")
    note_pattern = re.compile(r"^-\s+复核意见 JSON：(.*)$")
    blocks: list[list[str]] = []
    current: list[str] | None = None
    for line in lines[start:]:
        if match := heading.match(line):
            if int(match.group(1)) != len(blocks) + 1:
                raise KnowledgeWorkbenchError("复核用例编号不连续")
            current = []
            blocks.append(current)
            continue
        if current is not None:
            current.append(line)
    if not blocks:
        raise KnowledgeWorkbenchError("复核工作包不包含用例")

    records = []
    for block in blocks:
        case_values = [
            match.group(1)
            for line in block
            if (match := case_id_pattern.match(line))
        ]
        approve_values = [
            match.group(1)
            for line in block
            if (match := approve_pattern.match(line))
        ]
        reject_values = [
            match.group(1)
            for line in block
            if (match := reject_pattern.match(line))
        ]
        note_values = [
            match.group(1)
            for line in block
            if (match := note_pattern.match(line))
        ]
        if not (
            len(case_values)
            == len(approve_values)
            == len(reject_values)
            == len(note_values)
            == 1
        ):
            raise KnowledgeWorkbenchError("复核用例字段不完整或重复")
        try:
            case_id = json.loads(case_values[0])
        except json.JSONDecodeError as exc:
            raise KnowledgeWorkbenchError(
                "复核用例 case_id 不是有效 JSON 字符串"
            ) from exc
        if not isinstance(case_id, str):
            raise KnowledgeWorkbenchError(
                "复核用例 case_id 必须是字符串"
            )
        note = _json_note(note_values[0])
        approve = approve_values[0].lower() == "x"
        reject = reject_values[0].lower() == "x"
        records.append(
            {
                "case_id": case_id,
                "approved": (
                    True
                    if approve and not reject
                    else False
                    if reject and not approve
                    else None
                ),
                "decision_conflicting": approve and reject,
                "note": note,
            }
        )
    return metadata, records


def _validate_context_metadata(
    metadata: dict[str, str], context: dict[str, Any]
) -> None:
    required = (
        "graph_pilot_pack_id",
        "source_pack_path",
        "source_content_sha256",
        "graph_snapshot_sha256",
        "annotator",
    )
    missing = [key for key in required if not metadata.get(key)]
    if missing:
        raise KnowledgeWorkbenchError(
            "图谱黄金标注工作包缺少元数据：" + ", ".join(missing)
        )
    if (
        metadata["graph_pilot_pack_id"] != context["pack"]["pack_id"]
        or metadata["source_pack_path"] != context["source_relative"]
        or metadata["source_content_sha256"] != context["source_sha256"]
        or metadata["graph_snapshot_sha256"]
        != context["graph_snapshot_sha256"]
    ):
        raise KnowledgeWorkbenchError(
            "图谱黄金标注工作包来源或业务图快照已变化"
        )


def _validate_annotation_export(
    database: Database,
    paths: WorkspacePaths,
    work_pack_path: Path,
    content: str,
    metadata: dict[str, str],
    context: dict[str, Any],
    *,
    actor: str,
) -> None:
    with database.connect() as connection:
        rows = connection.execute(
            """
            SELECT details_json FROM audit_log
            WHERE event_type = ?
              AND entity_type = 'graph_pilot_pack'
              AND entity_id = ?
              AND actor = ?
            """,
            (
                ANNOTATION_EXPORT_EVENT,
                context["pack"]["pack_id"],
                actor,
            ),
        ).fetchall()
    for row in rows:
        try:
            details = json.loads(row["details_json"])
        except json.JSONDecodeError:
            continue
        if (
            details.get("output") == _relative(paths, work_pack_path)
            and details.get("source_content_sha256")
            == context["source_sha256"]
            and details.get("graph_snapshot_sha256")
            == context["graph_snapshot_sha256"]
            and details.get("entity_scope_sha256")
            == context["entity_scope_sha256"]
            and details.get("evidence_scope_sha256")
            == context["evidence_scope_sha256"]
            and details.get("template_sha256")
            == _annotation_template_sha256(content)
        ):
            return
    raise KnowledgeWorkbenchError(
        "图谱黄金标注工作包缺少匹配导出审计或受保护内容已修改"
    )


def _validate_review_export(
    database: Database,
    paths: WorkspacePaths,
    work_pack_path: Path,
    content: str,
    metadata: dict[str, str],
    candidate: dict,
    *,
    actor: str,
) -> None:
    required = (
        "graph_gold_candidate_id",
        "candidate_path",
        "candidate_content_sha256",
        "graph_snapshot_sha256",
        "annotator",
        "reviewer",
    )
    if any(not metadata.get(key) for key in required):
        raise KnowledgeWorkbenchError("图谱黄金复核工作包元数据不完整")
    with database.connect() as connection:
        rows = connection.execute(
            """
            SELECT details_json FROM audit_log
            WHERE event_type = ?
              AND entity_type = 'graph_gold_candidate'
              AND entity_id = ?
              AND actor = ?
            """,
            (REVIEW_EXPORT_EVENT, candidate["candidate_id"], actor),
        ).fetchall()
    for row in rows:
        try:
            details = json.loads(row["details_json"])
        except json.JSONDecodeError:
            continue
        if (
            details.get("output") == _relative(paths, work_pack_path)
            and details.get("candidate_path")
            == metadata["candidate_path"]
            and details.get("candidate_content_sha256")
            == metadata["candidate_content_sha256"]
            and details.get("case_scope_sha256")
            == _case_scope_sha256(candidate)
            and details.get("template_sha256")
            == _review_template_sha256(content)
            and details.get("annotator") == candidate["annotator"]
        ):
            return
    raise KnowledgeWorkbenchError(
        "图谱黄金复核工作包缺少匹配导出审计或受保护内容已修改"
    )


def _annotation_template_sha256(content: str) -> str:
    pattern = re.compile(r"^-\s+图谱黄金标注 JSON：.*$")
    normalized = [
        (
            '- 图谱黄金标注 JSON：{"name":"","relation_cases":[],"path_cases":[]}'
            if pattern.match(line)
            else line
        )
        for line in content.splitlines()
    ]
    return sha256_text("\n".join(normalized).rstrip() + "\n")


def _review_template_sha256(content: str) -> str:
    checkbox = re.compile(r"^-\s+\[[ xX]\]\s+(批准|驳回)\s*$")
    note = re.compile(r"^-\s+复核意见 JSON：.*$")
    normalized = []
    for line in content.splitlines():
        if match := checkbox.match(line):
            normalized.append(f"- [ ] {match.group(1)}")
        elif note.match(line):
            normalized.append('- 复核意见 JSON：""')
        else:
            normalized.append(line)
    return sha256_text("\n".join(normalized).rstrip() + "\n")


def _candidate_dataset(candidate: dict, *, reviewer: str) -> dict:
    if reviewer == candidate["annotator"]:
        reviewer = "__candidate_scope_reviewer__"
    return {
        "schema_version": "1.0",
        "name": candidate["name"],
        "provenance": {
            "annotator": candidate["annotator"],
            "reviewer": reviewer,
            "reviewed_at": candidate["annotated_at"],
            "decision": "approved",
        },
        "relation_cases": candidate["relation_cases"],
        "path_cases": candidate["path_cases"],
    }


def _candidate_cases(candidate: dict) -> list[tuple[str, dict]]:
    return [
        *(("relation", case) for case in candidate["relation_cases"]),
        *(("path", case) for case in candidate["path_cases"]),
    ]


def _case_entity_ids(kind: str, case: dict) -> list[str]:
    ids = [case["source_entity_id"], case["target_entity_id"]]
    if kind == "path":
        ids.extend(case["expected_entity_ids"])
    return list(dict.fromkeys(ids))


def _case_scope_sha256(candidate: dict) -> str:
    return _canonical_sha256(
        {
            "name": candidate["name"],
            "relation_cases": candidate["relation_cases"],
            "path_cases": candidate["path_cases"],
        }
    )


def _evidence_details(
    database: Database, evidence_ids: set[str]
) -> dict[str, dict[str, Any]]:
    if not evidence_ids:
        return {}
    placeholders = ", ".join("?" for _ in evidence_ids)
    with database.connect() as connection:
        rows = connection.execute(
            f"""
            SELECT e.id, e.excerpt, d.original_name AS document_name
            FROM evidence e
            JOIN document_versions dv ON dv.id = e.document_version_id
            JOIN documents d ON d.id = dv.document_id
            WHERE e.id IN ({placeholders})
            """,
            sorted(evidence_ids),
        ).fetchall()
        location_rows = connection.execute(
            f"""
            SELECT evidence_id, locator_json
            FROM evidence_locations
            WHERE evidence_id IN ({placeholders})
            ORDER BY evidence_id, location_ordinal
            """,
            sorted(evidence_ids),
        ).fetchall()
    locations: dict[str, list[dict]] = {}
    for row in location_rows:
        locations.setdefault(row["evidence_id"], []).append(
            json.loads(row["locator_json"])
        )
    return {
        row["id"]: {
            "evidence_id": row["id"],
            "excerpt": row["excerpt"],
            "document_name": row["document_name"],
            "locators": locations.get(row["id"], []),
        }
        for row in rows
    }


def _entity_details(
    database: Database, entity_ids: set[str]
) -> dict[str, dict[str, str]]:
    placeholders = ", ".join("?" for _ in entity_ids)
    with database.connect() as connection:
        rows = connection.execute(
            f"""
            SELECT id, canonical_name, entity_type
            FROM canonical_entities
            WHERE id IN ({placeholders}) AND status = 'active'
            """,
            sorted(entity_ids),
        ).fetchall()
    return {row["id"]: dict(row) for row in rows}


def _ensure_review_not_applied(
    database: Database, candidate_id: str, work_pack_path: str
) -> None:
    with database.connect() as connection:
        rows = connection.execute(
            """
            SELECT details_json FROM audit_log
            WHERE event_type = ?
              AND entity_type = 'graph_gold_candidate'
              AND entity_id = ?
            """,
            (REVIEW_APPLIED_EVENT, candidate_id),
        ).fetchall()
    for row in rows:
        try:
            details = json.loads(row["details_json"])
        except json.JSONDecodeError:
            continue
        if details.get("work_pack_path") == work_pack_path:
            raise InvalidTransitionError(
                "该图谱黄金复核工作包已经应用"
            )


def _ensure_annotation_not_applied(
    database: Database, work_pack_path: str
) -> None:
    with database.connect() as connection:
        rows = connection.execute(
            """
            SELECT details_json FROM audit_log
            WHERE event_type = ?
              AND entity_type = 'graph_gold_candidate'
            """,
            (CANDIDATE_SAVED_EVENT,),
        ).fetchall()
    for row in rows:
        try:
            details = json.loads(row["details_json"])
        except json.JSONDecodeError:
            continue
        if details.get("annotation_work_pack_path") == work_pack_path:
            raise InvalidTransitionError(
                "该图谱黄金标注工作包已经应用"
            )


def _record_review_applied(
    connection,
    candidate: dict,
    actor: str,
    work_pack_path: str,
    work_pack_sha256: str,
    decisions: list[dict[str, Any]],
    *,
    approved: bool,
    output: str | None,
    review_mode: str,
    attestation_sha256: str | None,
) -> None:
    record_event(
        connection,
        REVIEW_APPLIED_EVENT,
        "graph_gold_candidate",
        candidate["candidate_id"],
        actor=actor,
        details={
            "work_pack_path": work_pack_path,
            "work_pack_sha256": work_pack_sha256,
            "case_scope_sha256": _case_scope_sha256(candidate),
            "approved": approved,
            "approved_case_count": sum(
                item["approved"] for item in decisions
            ),
            "rejected_case_count": sum(
                not item["approved"] for item in decisions
            ),
            "rejected_case_ids": [
                item["case_id"]
                for item in decisions
                if not item["approved"]
            ],
            "note_sha256_by_case": {
                item["case_id"]: sha256_text(item["note"])
                for item in decisions
                if item["note"]
            },
            "output": output,
            **review_audit_context(
                review_mode, attestation_sha256
            ),
        },
    )


def _validated_json_output(
    paths: WorkspacePaths, output: Path, label: str
) -> Path:
    output = output.expanduser().resolve()
    if output.suffix.lower() != ".json":
        raise KnowledgeWorkbenchError(f"{label}必须使用 .json 文件")
    try:
        output.relative_to(paths.evaluations.resolve())
    except ValueError as exc:
        raise KnowledgeWorkbenchError(
            f"{label}只能保存到当前 workspace/evaluations 内"
        ) from exc
    if output.exists():
        raise KnowledgeWorkbenchError(
            f"{label}已存在，不允许静默覆盖：{output}"
        )
    return output


def _validated_existing_json(
    paths: WorkspacePaths, path: Path, label: str
) -> Path:
    path = path.expanduser().resolve()
    if path.suffix.lower() != ".json":
        raise KnowledgeWorkbenchError(f"{label}必须使用 .json 文件")
    try:
        path.relative_to(paths.evaluations.resolve())
    except ValueError as exc:
        raise KnowledgeWorkbenchError(
            f"{label}只能从当前 workspace/evaluations 内读取"
        ) from exc
    if not path.is_file():
        raise KnowledgeWorkbenchError(f"{label}不存在：{path}")
    return path


def _source_path(paths: WorkspacePaths, relative_path: str) -> Path:
    path = (paths.root.resolve() / relative_path).resolve()
    try:
        path.relative_to(paths.evaluations.resolve())
    except ValueError as exc:
        raise KnowledgeWorkbenchError("来源路径无效") from exc
    return path


def _relative(paths: WorkspacePaths, path: Path) -> str:
    return path.resolve().relative_to(paths.root.resolve()).as_posix()


def _canonical_sha256(value: Any) -> str:
    return sha256_text(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )


def _json_note(value: str) -> str:
    try:
        note = json.loads(value.strip())
    except json.JSONDecodeError as exc:
        raise KnowledgeWorkbenchError(
            "复核意见不是有效 JSON 字符串"
        ) from exc
    if not isinstance(note, str) or len(note.strip()) > 2000:
        raise KnowledgeWorkbenchError(
            "复核意见必须是最多 2000 字符的 JSON 字符串"
        )
    return note.strip()


def _required_actor(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise KnowledgeWorkbenchError("actor 不能为空")
    value = value.strip()
    if len(value) > 80 or any(char in value for char in "\r\n"):
        raise KnowledgeWorkbenchError(
            "actor 不能超过 80 个字符或包含换行"
        )
    return value
