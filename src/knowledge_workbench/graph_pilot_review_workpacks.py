from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any

from .audit import record_event
from .config import WorkspacePaths
from .database import Database
from .errors import InvalidTransitionError, KnowledgeWorkbenchError
from .graph_pilot import (
    inspect_graph_pilot_pack,
    resolve_graph_pilot_pack,
)
from .models import EvidenceStatus
from .review import _transition_evidence_in_transaction
from .utils import sha256_text, utc_now
from .wiki import write_text_atomic


_TRIAGE_PACK_TYPE = "graph-pilot-evidence-triage-pack"
_VERIFICATION_PACK_TYPE = "graph-pilot-evidence-verification-pack"
_ELIGIBLE_TRIAGE_STATUSES = frozenset({"draft", "conflicted"})


def export_graph_pilot_triage_work_pack(
    database: Database,
    paths: WorkspacePaths,
    source_pack_path: Path,
    output: Path,
    *,
    actor: str,
) -> Path:
    actor = _required_actor(actor)
    output = _validated_output(paths, output, "图谱试点证据初审工作包")
    source_path, source_content, source_pack = resolve_graph_pilot_pack(
        database, paths, source_pack_path
    )
    status = inspect_graph_pilot_pack(database, paths, source_path)
    _require_safe_status(status)
    current_by_id = {
        item["evidence_id"]: item for item in status["items"]
    }
    candidates = [
        candidate
        for candidate in source_pack["candidates"]
        if current_by_id[candidate["evidence_id"]]["status"]
        in _ELIGIBLE_TRIAGE_STATUSES
    ]
    if not candidates:
        raise InvalidTransitionError(
            "图谱试点包没有 draft 或 conflicted 证据可提交初审"
        )
    records = [
        {
            "evidence_id": candidate["evidence_id"],
            "expected_status": current_by_id[candidate["evidence_id"]][
                "status"
            ],
        }
        for candidate in candidates
    ]
    source_relative = source_path.relative_to(
        paths.root.resolve()
    ).as_posix()
    source_sha256 = sha256_text(source_content)
    lines = _frontmatter(
        _TRIAGE_PACK_TYPE,
        source_pack,
        source_relative,
        source_sha256,
        role_key="curator",
        actor=actor,
    )
    lines.extend(
        [
            f"# 图谱试点证据初审：`{source_pack['pack_id']}`",
            "",
            f"- 初审人：`{_markdown_code(actor)}`",
            f"- 待处理证据：`{len(candidates)}`",
            "- 逐条核对来源正文与定位；黄金用例相关性不能代替证据保真审核。",
            "- 每条必须且只能选择“提交复核”或“暂缓”。",
            "- 暂缓必须填写原因；系统不会自动验证、归档或修改暂缓证据。",
            "- 全部决定一次原子应用；任何一条失效时整批零写入。",
            "",
            "## 应用本批初审",
            "",
            "```powershell",
            (
                f".\\.venv\\Scripts\\knowledge.exe graph "
                f"pilot-triage-apply {_powershell_literal(str(output))} "
                f"--actor {_powershell_literal(actor)}"
            ),
            "```",
            "",
        ]
    )
    for candidate, record in zip(candidates, records, strict=True):
        lines.extend(
            _candidate_header(candidate, record["expected_status"])
        )
        lines.extend(
            [
                "- [ ] 提交复核",
                "- [ ] 暂缓",
                '- 决策依据 JSON：""',
                "",
                *_candidate_source(candidate),
            ]
        )
    content = "\n".join(lines).rstrip() + "\n"
    _write_export(
        database,
        paths,
        output,
        content,
        event_type="graph_pilot_triage_pack_exported",
        source_pack=source_pack,
        source_path=source_relative,
        source_sha256=source_sha256,
        records=records,
        actor=actor,
    )
    return output


def apply_graph_pilot_triage_work_pack(
    database: Database,
    paths: WorkspacePaths,
    work_pack_path: Path,
    *,
    actor: str,
) -> dict[str, Any]:
    actor = _required_actor(actor)
    work_pack_path, content = _read_work_pack(
        paths, work_pack_path, "图谱试点证据初审工作包"
    )
    metadata, decisions = _parse_triage_pack(content)
    if metadata["curator"] != actor:
        raise KnowledgeWorkbenchError(
            "工作包声明的初审人与当前 actor 不一致"
        )
    source_path, source_pack, records = _validate_work_pack(
        database,
        paths,
        work_pack_path,
        content,
        metadata,
        decisions,
        actor=actor,
        event_type="graph_pilot_triage_pack_exported",
    )
    submitted = [
        item for item in decisions if item["decision"] == "submit"
    ]
    held = [item for item in decisions if item["decision"] == "hold"]
    work_pack_sha256 = sha256_text(content)
    context = {
        "graph_pilot_pack_id": source_pack["pack_id"],
        "work_pack_sha256": work_pack_sha256,
    }
    candidates = _candidates_by_id(source_pack)
    with database.transaction() as connection:
        for record in records:
            _validate_candidate_snapshot(
                connection,
                candidates[record["evidence_id"]],
                expected_status=record["expected_status"],
            )
        for item in submitted:
            _transition_evidence_in_transaction(
                connection,
                item["evidence_id"],
                EvidenceStatus.REVIEWING,
                actor=actor,
                expected_status=EvidenceStatus(item["expected_status"]),
                context=context,
            )
        record_event(
            connection,
            "graph_pilot_triage_applied",
            "graph_pilot_pack",
            source_pack["pack_id"],
            actor=actor,
            details=_application_details(
                paths,
                work_pack_path,
                work_pack_sha256,
                records,
                decisions,
                {
                    "submitted_count": len(submitted),
                    "held_count": len(held),
                    "submitted_evidence_ids": [
                        item["evidence_id"] for item in submitted
                    ],
                    "held_evidence_ids": [
                        item["evidence_id"] for item in held
                    ],
                },
            ),
        )
    return {
        "graph_pilot_pack_id": source_pack["pack_id"],
        "source_pack_path": source_path.relative_to(
            paths.root.resolve()
        ).as_posix(),
        "evidence_count": len(records),
        "submitted_count": len(submitted),
        "held_count": len(held),
        "actor": actor,
    }


def export_graph_pilot_verification_work_pack(
    database: Database,
    paths: WorkspacePaths,
    source_pack_path: Path,
    output: Path,
    *,
    actor: str,
) -> Path:
    actor = _required_actor(actor)
    output = _validated_output(
        paths, output, "图谱试点证据异人复核工作包"
    )
    source_path, source_content, source_pack = resolve_graph_pilot_pack(
        database, paths, source_pack_path
    )
    status = inspect_graph_pilot_pack(database, paths, source_path)
    _require_safe_status(status)
    current_by_id = {
        item["evidence_id"]: item for item in status["items"]
    }
    reviewing_candidates = [
        candidate
        for candidate in source_pack["candidates"]
        if current_by_id[candidate["evidence_id"]]["status"] == "reviewing"
    ]
    if not reviewing_candidates:
        raise InvalidTransitionError(
            "图谱试点包没有 reviewing 证据可进行异人复核"
        )
    with database.connect() as connection:
        candidates = []
        records = []
        for candidate in reviewing_candidates:
            submitter = _reviewing_submitter(
                connection, candidate["evidence_id"]
            )
            if submitter == actor:
                continue
            candidates.append(candidate)
            records.append(
                {
                    "evidence_id": candidate["evidence_id"],
                    "expected_status": "reviewing",
                    "submitter": submitter,
                }
            )
    if not candidates:
        raise InvalidTransitionError(
            "没有可由当前 actor 异人复核的 reviewing 证据"
        )
    source_relative = source_path.relative_to(
        paths.root.resolve()
    ).as_posix()
    source_sha256 = sha256_text(source_content)
    lines = _frontmatter(
        _VERIFICATION_PACK_TYPE,
        source_pack,
        source_relative,
        source_sha256,
        role_key="reviewer",
        actor=actor,
    )
    lines.extend(
        [
            f"# 图谱试点证据异人复核：`{source_pack['pack_id']}`",
            "",
            f"- 复核人：`{_markdown_code(actor)}`",
            f"- 待复核证据：`{len(candidates)}`",
            "- 复核人必须独立核对来源正文、定位和原子边界。",
            "- 每条必须且只能选择“验证通过”或“退回草稿”。",
            "- 退回草稿必须填写复核意见；验证通过不会自动创建实体或关系。",
            "- 全部决定一次原子应用；任何一条失效时整批零写入。",
            "",
            "## 应用本批复核",
            "",
            "```powershell",
            (
                f".\\.venv\\Scripts\\knowledge.exe graph "
                "pilot-verification-apply "
                f"{_powershell_literal(str(output))} "
                f"--actor {_powershell_literal(actor)}"
            ),
            "```",
            "",
        ]
    )
    for candidate, record in zip(candidates, records, strict=True):
        lines.extend(
            _candidate_header(candidate, record["expected_status"])
        )
        lines.extend(
            [
                f"- 提交人：`{_markdown_code(record['submitter'])}`",
                "- [ ] 验证通过",
                "- [ ] 退回草稿",
                '- 复核意见 JSON：""',
                "",
                *_candidate_source(candidate),
            ]
        )
    content = "\n".join(lines).rstrip() + "\n"
    _write_export(
        database,
        paths,
        output,
        content,
        event_type="graph_pilot_verification_pack_exported",
        source_pack=source_pack,
        source_path=source_relative,
        source_sha256=source_sha256,
        records=records,
        actor=actor,
    )
    return output


def apply_graph_pilot_verification_work_pack(
    database: Database,
    paths: WorkspacePaths,
    work_pack_path: Path,
    *,
    actor: str,
) -> dict[str, Any]:
    actor = _required_actor(actor)
    work_pack_path, content = _read_work_pack(
        paths, work_pack_path, "图谱试点证据异人复核工作包"
    )
    metadata, decisions = _parse_verification_pack(content)
    if metadata["reviewer"] != actor:
        raise KnowledgeWorkbenchError(
            "工作包声明的复核人与当前 actor 不一致"
        )
    source_path, source_pack, records = _validate_work_pack(
        database,
        paths,
        work_pack_path,
        content,
        metadata,
        decisions,
        actor=actor,
        event_type="graph_pilot_verification_pack_exported",
    )
    approved = [
        item for item in decisions if item["decision"] == "approve"
    ]
    returned = [
        item for item in decisions if item["decision"] == "return"
    ]
    work_pack_sha256 = sha256_text(content)
    context = {
        "graph_pilot_pack_id": source_pack["pack_id"],
        "work_pack_sha256": work_pack_sha256,
    }
    candidates = _candidates_by_id(source_pack)
    with database.transaction() as connection:
        for record in records:
            _validate_candidate_snapshot(
                connection,
                candidates[record["evidence_id"]],
                expected_status=record["expected_status"],
            )
            submitter = _reviewing_submitter(
                connection, record["evidence_id"]
            )
            if submitter == actor:
                raise InvalidTransitionError("证据提交人与复核人必须不同")
        by_id = {
            item["evidence_id"]: item for item in decisions
        }
        for record in records:
            item = by_id[record["evidence_id"]]
            target = (
                EvidenceStatus.VERIFIED
                if item["decision"] == "approve"
                else EvidenceStatus.DRAFT
            )
            _transition_evidence_in_transaction(
                connection,
                item["evidence_id"],
                target,
                actor=actor,
                expected_status=EvidenceStatus.REVIEWING,
                context=context,
            )
        record_event(
            connection,
            "graph_pilot_verification_applied",
            "graph_pilot_pack",
            source_pack["pack_id"],
            actor=actor,
            details=_application_details(
                paths,
                work_pack_path,
                work_pack_sha256,
                records,
                decisions,
                {
                    "approved_count": len(approved),
                    "returned_count": len(returned),
                    "approved_evidence_ids": [
                        item["evidence_id"] for item in approved
                    ],
                    "returned_evidence_ids": [
                        item["evidence_id"] for item in returned
                    ],
                },
            ),
        )
    return {
        "graph_pilot_pack_id": source_pack["pack_id"],
        "source_pack_path": source_path.relative_to(
            paths.root.resolve()
        ).as_posix(),
        "evidence_count": len(records),
        "approved_count": len(approved),
        "returned_count": len(returned),
        "actor": actor,
    }


def inspect_graph_pilot_review_work_pack(
    database: Database,
    paths: WorkspacePaths,
    work_pack_path: Path,
) -> dict[str, Any]:
    """Inspect a triage or verification work pack without changing state."""
    work_pack_path, content = _read_work_pack(
        paths, work_pack_path, "图谱试点证据审核工作包"
    )
    relative_path = work_pack_path.relative_to(
        paths.root.resolve()
    ).as_posix()
    base = {
        "schema_version": "1.0",
        "kind": "graph-pilot-review-work-pack-status",
        "work_pack_path": relative_path,
        "content_sha256": sha256_text(content),
    }
    try:
        metadata, lines, start = _parse_frontmatter(content, None)
        pack_type = metadata.get("type")
        if pack_type == _TRIAGE_PACK_TYPE:
            role_key = "curator"
            event_type = "graph_pilot_triage_pack_exported"
            first_label = "提交复核"
            second_label = "暂缓"
            note_label = "决策依据 JSON"
            first_key = "submit"
            second_key = "hold"
        elif pack_type == _VERIFICATION_PACK_TYPE:
            role_key = "reviewer"
            event_type = "graph_pilot_verification_pack_exported"
            first_label = "验证通过"
            second_label = "退回草稿"
            note_label = "复核意见 JSON"
            first_key = "approve"
            second_key = "return"
        else:
            raise KnowledgeWorkbenchError(
                "文件不是预期的图谱试点证据审核工作包"
            )
        _require_metadata(metadata, (role_key,))
        actor = _required_actor(metadata[role_key])
        records = _parse_sections(
            lines,
            start,
            first_label=first_label,
            second_label=second_label,
            note_label=note_label,
        )
    except KnowledgeWorkbenchError as exc:
        return {
            **base,
            "work_pack_type": "unknown",
            "integrity_valid": False,
            "apply_ready": False,
            "issue_codes": ["invalid_work_pack_format"],
            "error": str(exc),
        }

    first_only = [
        item for item in records if item["first"] and not item["second"]
    ]
    second_only = [
        item for item in records if item["second"] and not item["first"]
    ]
    undecided = [
        item for item in records if not item["first"] and not item["second"]
    ]
    conflicting = [
        item for item in records if item["first"] and item["second"]
    ]
    missing_note = [item for item in second_only if not item["note"]]
    complete_ids = {
        item["evidence_id"] for item in first_only
    } | {
        item["evidence_id"]
        for item in second_only
        if item["note"]
    }
    unresolved_ids = [
        item["evidence_id"]
        for item in records
        if item["evidence_id"] not in complete_ids
    ]

    integrity_valid = True
    integrity_error = None
    source_pack = None
    try:
        _, source_pack, _ = _validate_work_pack(
            database,
            paths,
            work_pack_path,
            content,
            metadata,
            records,
            actor=actor,
            event_type=event_type,
        )
    except KnowledgeWorkbenchError as exc:
        integrity_valid = False
        integrity_error = str(exc)

    snapshot_invalid_ids: list[str] = []
    actor_separation_conflict_ids: list[str] = []
    submitter_audit_invalid_ids: list[str] = []
    if integrity_valid and source_pack is not None:
        candidates = _candidates_by_id(source_pack)
        with database.connect() as connection:
            for record in records:
                candidate = candidates.get(record["evidence_id"])
                if candidate is None:
                    snapshot_invalid_ids.append(record["evidence_id"])
                    continue
                try:
                    _validate_candidate_snapshot(
                        connection,
                        candidate,
                        expected_status=record["expected_status"],
                    )
                except KnowledgeWorkbenchError:
                    snapshot_invalid_ids.append(record["evidence_id"])
                    continue
                if pack_type != _VERIFICATION_PACK_TYPE:
                    continue
                try:
                    submitter = _reviewing_submitter(
                        connection, record["evidence_id"]
                    )
                except KnowledgeWorkbenchError:
                    submitter_audit_invalid_ids.append(
                        record["evidence_id"]
                    )
                    continue
                if submitter == actor:
                    actor_separation_conflict_ids.append(
                        record["evidence_id"]
                    )

    issue_codes = []
    if not integrity_valid:
        issue_codes.append("integrity_invalid")
    if undecided:
        issue_codes.append("decision_missing")
    if conflicting:
        issue_codes.append("decision_conflicting")
    if missing_note:
        issue_codes.append("required_note_missing")
    if snapshot_invalid_ids:
        issue_codes.append("source_or_status_drift")
    if submitter_audit_invalid_ids:
        issue_codes.append("submitter_audit_missing")
    if actor_separation_conflict_ids:
        issue_codes.append("actor_separation_conflict")

    apply_ready = (
        integrity_valid
        and not unresolved_ids
        and not snapshot_invalid_ids
        and not submitter_audit_invalid_ids
        and not actor_separation_conflict_ids
    )
    payload = {
        **base,
        "work_pack_type": (
            "triage"
            if pack_type == _TRIAGE_PACK_TYPE
            else "verification"
        ),
        "graph_pilot_pack_id": metadata["graph_pilot_pack_id"],
        "actor_role": role_key,
        "actor": actor,
        "evidence_count": len(records),
        "decision_counts": {
            first_key: len(first_only),
            second_key: len(second_only),
            "undecided": len(undecided),
            "conflicting": len(conflicting),
        },
        "complete_decision_count": len(complete_ids),
        "remaining_decision_count": len(unresolved_ids),
        "missing_required_note_count": len(missing_note),
        "integrity_valid": integrity_valid,
        "source_snapshot_valid": (
            integrity_valid and not snapshot_invalid_ids
        ),
        "snapshot_invalid_count": len(snapshot_invalid_ids),
        "submitter_audit_invalid_count": len(
            submitter_audit_invalid_ids
        ),
        "actor_separation_valid": (
            pack_type != _VERIFICATION_PACK_TYPE
            or (
                integrity_valid
                and not submitter_audit_invalid_ids
                and not actor_separation_conflict_ids
            )
        ),
        "actor_separation_conflict_count": len(
            actor_separation_conflict_ids
        ),
        "next_unresolved_evidence_id": (
            unresolved_ids[0] if unresolved_ids else None
        ),
        "issue_codes": issue_codes,
        "apply_ready": apply_ready,
    }
    if integrity_error is not None:
        payload["integrity_error"] = integrity_error
    return payload


def _frontmatter(
    pack_type: str,
    source_pack: dict,
    source_path: str,
    source_sha256: str,
    *,
    role_key: str,
    actor: str,
) -> list[str]:
    return [
        "---",
        f"type: {pack_type}",
        f"graph_pilot_pack_id: {source_pack['pack_id']}",
        f"source_pack_path: {source_path}",
        f"source_content_sha256: {source_sha256}",
        f"{role_key}: {actor}",
        f"generated_at: {utc_now()}",
        "---",
        "",
    ]


def _candidate_header(
    candidate: dict, expected_status: str
) -> list[str]:
    return [
        f"## `{candidate['evidence_id']}`",
        "",
        f"- 导出状态：`{expected_status}`",
    ]


def _candidate_source(candidate: dict) -> list[str]:
    return [
        f"### 来源：`{_markdown_code(candidate['document_name'])}`",
        "",
        f"- 资料 ID：`{candidate['document_id']}`",
        f"- 文件版本：`{candidate['document_version_id']}`",
        f"- 处理运行：`{candidate['processing_run_id']}`",
        f"- 运行序号：`{candidate['run_ordinal']}`",
        f"- 密级：`{candidate['classification']}`",
        "- 黄金用例 JSON："
        + json.dumps(candidate["case_ids"], ensure_ascii=False),
        "- 定位 JSON："
        + json.dumps(candidate["locators"], ensure_ascii=False),
        "",
        *_blockquote(candidate["excerpt"]),
        "",
    ]


def _parse_triage_pack(
    content: str,
) -> tuple[dict[str, str], list[dict[str, Any]]]:
    metadata, lines, start = _parse_frontmatter(
        content, _TRIAGE_PACK_TYPE
    )
    _require_metadata(metadata, ("curator",))
    records = _parse_sections(
        lines,
        start,
        first_label="提交复核",
        second_label="暂缓",
        note_label="决策依据 JSON",
    )
    decisions = []
    for record in records:
        _require_one_decision(record, "提交复核或暂缓")
        decision = "submit" if record["first"] else "hold"
        if decision == "hold" and not record["note"]:
            raise KnowledgeWorkbenchError(
                f"证据 {record['evidence_id']} 暂缓时必须填写原因"
            )
        decisions.append({**record, "decision": decision})
    return metadata, decisions


def _parse_verification_pack(
    content: str,
) -> tuple[dict[str, str], list[dict[str, Any]]]:
    metadata, lines, start = _parse_frontmatter(
        content, _VERIFICATION_PACK_TYPE
    )
    _require_metadata(metadata, ("reviewer",))
    records = _parse_sections(
        lines,
        start,
        first_label="验证通过",
        second_label="退回草稿",
        note_label="复核意见 JSON",
    )
    decisions = []
    for record in records:
        _require_one_decision(record, "验证通过或退回草稿")
        decision = "approve" if record["first"] else "return"
        if decision == "return" and not record["note"]:
            raise KnowledgeWorkbenchError(
                f"证据 {record['evidence_id']} 退回时必须填写复核意见"
            )
        decisions.append({**record, "decision": decision})
    return metadata, decisions


def _parse_frontmatter(
    content: str, expected_type: str | None
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
    if expected_type is not None and metadata.get("type") != expected_type:
        raise KnowledgeWorkbenchError("文件不是预期的图谱试点工作包")
    return metadata, lines, end + 1


def _parse_sections(
    lines: list[str],
    start: int,
    *,
    first_label: str,
    second_label: str,
    note_label: str,
) -> list[dict[str, Any]]:
    heading_pattern = re.compile(r"^##\s+`(ev_[a-f0-9]{32})`\s*$")
    status_pattern = re.compile(r"^-\s+导出状态：`([^`]+)`\s*$")
    first_pattern = re.compile(
        rf"^-\s+\[([ xX])\]\s+{re.escape(first_label)}\s*$"
    )
    second_pattern = re.compile(
        rf"^-\s+\[([ xX])\]\s+{re.escape(second_label)}\s*$"
    )
    note_pattern = re.compile(
        rf"^-\s+{re.escape(note_label)}：(.*)$"
    )
    records = []
    seen_ids: set[str] = set()
    current = None
    for line in lines[start:]:
        heading = heading_pattern.match(line)
        if heading:
            evidence_id = heading.group(1)
            if evidence_id in seen_ids:
                raise KnowledgeWorkbenchError(
                    f"工作包包含重复证据：{evidence_id}"
                )
            seen_ids.add(evidence_id)
            current = {
                "evidence_id": evidence_id,
                "expected_status": None,
                "first": False,
                "second": False,
                "note": "",
                "status_seen": False,
                "first_seen": False,
                "second_seen": False,
                "note_seen": False,
            }
            records.append(current)
            continue
        if current is None:
            continue
        if match := status_pattern.match(line):
            _mark_once(current, "status_seen", "导出状态")
            current["expected_status"] = match.group(1).strip()
            continue
        if match := first_pattern.match(line):
            _mark_once(current, "first_seen", first_label)
            current["first"] = match.group(1).lower() == "x"
            continue
        if match := second_pattern.match(line):
            _mark_once(current, "second_seen", second_label)
            current["second"] = match.group(1).lower() == "x"
            continue
        if match := note_pattern.match(line):
            _mark_once(current, "note_seen", note_label)
            current["note"] = _json_note(
                match.group(1), current["evidence_id"]
            )
    if not records:
        raise KnowledgeWorkbenchError("工作包不包含证据")
    for record in records:
        missing = [
            label
            for label, key in (
                ("导出状态", "status_seen"),
                (first_label, "first_seen"),
                (second_label, "second_seen"),
                (note_label, "note_seen"),
            )
            if not record[key]
        ]
        if missing:
            raise KnowledgeWorkbenchError(
                f"证据 {record['evidence_id']} 缺少字段："
                + "、".join(missing)
            )
    return records


def _validate_work_pack(
    database: Database,
    paths: WorkspacePaths,
    work_pack_path: Path,
    work_pack_content: str,
    metadata: dict[str, str],
    decisions: list[dict[str, Any]],
    *,
    actor: str,
    event_type: str,
) -> tuple[Path, dict, list[dict[str, str]]]:
    source_path = (
        paths.root.resolve() / metadata["source_pack_path"]
    ).resolve()
    try:
        source_path.relative_to(paths.evaluations.resolve())
    except ValueError as exc:
        raise KnowledgeWorkbenchError(
            "工作包来源图谱试点包路径无效"
        ) from exc
    source_path, source_content, source_pack = resolve_graph_pilot_pack(
        database, paths, source_path
    )
    if source_pack["pack_id"] != metadata["graph_pilot_pack_id"]:
        raise KnowledgeWorkbenchError("工作包来源图谱试点包身份不一致")
    if sha256_text(source_content) != metadata["source_content_sha256"]:
        raise KnowledgeWorkbenchError("来源图谱试点包内容哈希已变化")
    status = inspect_graph_pilot_pack(database, paths, source_path)
    _require_safe_status(status)
    records = [
        {
            "evidence_id": item["evidence_id"],
            "expected_status": item["expected_status"],
        }
        for item in decisions
    ]
    _validate_export_audit(
        database,
        paths,
        work_pack_path,
        work_pack_content,
        metadata,
        records,
        actor=actor,
        event_type=event_type,
    )
    return source_path, source_pack, records


def _validate_export_audit(
    database: Database,
    paths: WorkspacePaths,
    work_pack_path: Path,
    work_pack_content: str,
    metadata: dict[str, str],
    records: list[dict[str, str]],
    *,
    actor: str,
    event_type: str,
) -> None:
    relative_path = work_pack_path.relative_to(
        paths.root.resolve()
    ).as_posix()
    scope_sha256 = _scope_sha256(records)
    template_sha256 = _editable_template_sha256(
        work_pack_content, event_type
    )
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
                event_type,
                metadata["graph_pilot_pack_id"],
                actor,
            ),
        ).fetchall()
    for row in rows:
        try:
            details = json.loads(row["details_json"])
        except json.JSONDecodeError:
            continue
        if (
            details.get("output") == relative_path
            and details.get("source_pack_path")
            == metadata["source_pack_path"]
            and details.get("source_content_sha256")
            == metadata["source_content_sha256"]
            and details.get("evidence_scope_sha256") == scope_sha256
            and details.get("evidence_count") == len(records)
            and details.get("evidence_ids")
            == [record["evidence_id"] for record in records]
            and details.get("template_sha256") == template_sha256
        ):
            return
    raise KnowledgeWorkbenchError(
        "工作包缺少匹配的系统导出审计记录，"
        "或修改了决定字段以外的受保护内容"
    )


def _write_export(
    database: Database,
    paths: WorkspacePaths,
    output: Path,
    content: str,
    *,
    event_type: str,
    source_pack: dict,
    source_path: str,
    source_sha256: str,
    records: list[dict[str, str]],
    actor: str,
) -> None:
    write_text_atomic(output, content)
    try:
        with database.transaction() as connection:
            record_event(
                connection,
                event_type,
                "graph_pilot_pack",
                source_pack["pack_id"],
                actor=actor,
                details={
                    "output": output.relative_to(
                        paths.root.resolve()
                    ).as_posix(),
                    "content_sha256": sha256_text(content),
                    "template_sha256": _editable_template_sha256(
                        content, event_type
                    ),
                    "source_pack_path": source_path,
                    "source_content_sha256": source_sha256,
                    "evidence_scope_sha256": _scope_sha256(records),
                    "evidence_count": len(records),
                    "evidence_ids": [
                        record["evidence_id"] for record in records
                    ],
                },
            )
    except Exception:
        output.unlink(missing_ok=True)
        raise


def _application_details(
    paths: WorkspacePaths,
    work_pack_path: Path,
    work_pack_sha256: str,
    records: list[dict[str, str]],
    decisions: list[dict[str, Any]],
    outcome_details: dict[str, Any],
) -> dict[str, Any]:
    notes = {
        item["evidence_id"]: sha256_text(item["note"])
        for item in decisions
        if item["note"]
    }
    return {
        "work_pack_path": work_pack_path.relative_to(
            paths.root.resolve()
        ).as_posix(),
        "work_pack_sha256": work_pack_sha256,
        "evidence_scope_sha256": _scope_sha256(records),
        "evidence_count": len(records),
        "evidence_ids": [
            record["evidence_id"] for record in records
        ],
        **outcome_details,
        "note_sha256_by_evidence": notes,
    }


def _validate_candidate_snapshot(
    connection: sqlite3.Connection,
    candidate: dict,
    *,
    expected_status: str,
) -> None:
    row = connection.execute(
        """
        SELECT e.excerpt, e.status, e.run_ordinal,
               e.processing_run_id, e.document_version_id,
               pr.is_current, d.id AS document_id,
               d.original_name, d.classification,
               CASE WHEN d.current_version_id = dv.id
                    THEN 1 ELSE 0 END AS version_is_current
        FROM evidence e
        JOIN processing_runs pr ON pr.id = e.processing_run_id
        JOIN document_versions dv ON dv.id = e.document_version_id
        JOIN documents d ON d.id = dv.document_id
        WHERE e.id = ?
        """,
        (candidate["evidence_id"],),
    ).fetchone()
    if not row:
        raise KnowledgeWorkbenchError(
            f"图谱试点证据不存在：{candidate['evidence_id']}"
        )
    locator_rows = connection.execute(
        """
        SELECT locator_json FROM evidence_locations
        WHERE evidence_id = ?
        ORDER BY location_ordinal
        """,
        (candidate["evidence_id"],),
    ).fetchall()
    locators = [json.loads(item["locator_json"]) for item in locator_rows]
    valid = (
        bool(row["is_current"])
        and bool(row["version_is_current"])
        and row["excerpt"] == candidate["excerpt"]
        and row["status"] == expected_status
        and row["run_ordinal"] == candidate["run_ordinal"]
        and row["processing_run_id"] == candidate["processing_run_id"]
        and row["document_version_id"] == candidate["document_version_id"]
        and row["document_id"] == candidate["document_id"]
        and row["original_name"] == candidate["document_name"]
        and row["classification"] == candidate["classification"]
        and row["classification"] != "restricted"
        and locators == candidate["locators"]
    )
    if not valid:
        raise KnowledgeWorkbenchError(
            f"图谱试点证据来源或状态已变化：{candidate['evidence_id']}"
        )


def _reviewing_submitter(
    connection: sqlite3.Connection, evidence_id: str
) -> str:
    rows = connection.execute(
        """
        SELECT actor, details_json FROM audit_log
        WHERE event_type = 'evidence_status_changed'
          AND entity_type = 'evidence'
          AND entity_id = ?
        ORDER BY id DESC
        """,
        (evidence_id,),
    ).fetchall()
    for row in rows:
        try:
            details = json.loads(row["details_json"])
        except json.JSONDecodeError:
            continue
        if details.get("to") == "reviewing":
            return row["actor"]
    raise KnowledgeWorkbenchError(
        f"证据 {evidence_id} 缺少进入 reviewing 的审计记录"
    )


def _require_safe_status(status: dict) -> None:
    summary = status["summary"]
    if not summary["source_snapshot_passed"]:
        raise KnowledgeWorkbenchError("图谱试点证据来源快照已失效")
    if not summary["classification_boundary_passed"]:
        raise KnowledgeWorkbenchError("图谱试点证据密级边界已失效")


def _candidates_by_id(source_pack: dict) -> dict[str, dict]:
    return {
        candidate["evidence_id"]: candidate
        for candidate in source_pack["candidates"]
    }


def _scope_sha256(records: list[dict[str, str]]) -> str:
    scope = [
        {
            "evidence_id": record["evidence_id"],
            "expected_status": record["expected_status"],
        }
        for record in records
    ]
    canonical = json.dumps(
        scope,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return sha256_text(canonical)


def _editable_template_sha256(
    content: str, event_type: str
) -> str:
    if event_type == "graph_pilot_triage_pack_exported":
        labels = ("提交复核", "暂缓")
        note_label = "决策依据 JSON"
    elif event_type == "graph_pilot_verification_pack_exported":
        labels = ("验证通过", "退回草稿")
        note_label = "复核意见 JSON"
    else:
        raise KnowledgeWorkbenchError("未知的图谱试点工作包类型")
    checkbox_patterns = [
        (
            re.compile(
                rf"^-\s+\[[ xX]\]\s+{re.escape(label)}\s*$"
            ),
            f"- [ ] {label}",
        )
        for label in labels
    ]
    note_pattern = re.compile(
        rf"^-\s+{re.escape(note_label)}：.*$"
    )
    normalized = []
    for line in content.splitlines():
        matched = False
        for pattern, replacement in checkbox_patterns:
            if pattern.match(line):
                normalized.append(replacement)
                matched = True
                break
        if matched:
            continue
        if note_pattern.match(line):
            normalized.append(f'- {note_label}：""')
            continue
        normalized.append(line)
    return sha256_text("\n".join(normalized).rstrip() + "\n")


def _require_one_decision(record: dict, label: str) -> None:
    if int(record["first"]) + int(record["second"]) != 1:
        raise KnowledgeWorkbenchError(
            f"证据 {record['evidence_id']} 必须且只能选择{label}"
        )


def _mark_once(record: dict, key: str, label: str) -> None:
    if record[key]:
        raise KnowledgeWorkbenchError(
            f"证据 {record['evidence_id']} 包含重复字段：{label}"
        )
    record[key] = True


def _json_note(value: str, evidence_id: str) -> str:
    try:
        note = json.loads(value.strip())
    except json.JSONDecodeError as exc:
        raise KnowledgeWorkbenchError(
            f"证据 {evidence_id} 的意见不是有效 JSON 字符串"
        ) from exc
    if not isinstance(note, str):
        raise KnowledgeWorkbenchError(
            f"证据 {evidence_id} 的意见必须是 JSON 字符串"
        )
    note = note.strip()
    if len(note) > 2000:
        raise KnowledgeWorkbenchError(
            f"证据 {evidence_id} 的意见不能超过 2000 个字符"
        )
    return note


def _require_metadata(
    metadata: dict[str, str], role_keys: tuple[str, ...]
) -> None:
    keys = (
        "graph_pilot_pack_id",
        "source_pack_path",
        "source_content_sha256",
        *role_keys,
    )
    missing = [key for key in keys if not metadata.get(key)]
    if missing:
        raise KnowledgeWorkbenchError(
            "工作包缺少元数据：" + ", ".join(missing)
        )
    if not re.fullmatch(
        r"[a-f0-9]{64}", metadata["source_content_sha256"]
    ):
        raise KnowledgeWorkbenchError(
            "工作包 source_content_sha256 无效"
        )


def _validated_output(
    paths: WorkspacePaths, output: Path, label: str
) -> Path:
    output = output.expanduser().resolve()
    if output.suffix.lower() != ".md":
        raise KnowledgeWorkbenchError(f"{label}必须使用 .md 文件")
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


def _read_work_pack(
    paths: WorkspacePaths, work_pack_path: Path, label: str
) -> tuple[Path, str]:
    work_pack_path = work_pack_path.expanduser().resolve()
    if work_pack_path.suffix.lower() != ".md":
        raise KnowledgeWorkbenchError(f"{label}必须使用 .md 文件")
    try:
        work_pack_path.relative_to(paths.evaluations.resolve())
    except ValueError as exc:
        raise KnowledgeWorkbenchError(
            f"{label}只能从当前 workspace/evaluations 内读取"
        ) from exc
    try:
        return work_pack_path, work_pack_path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise KnowledgeWorkbenchError(
            f"{label}不存在：{work_pack_path}"
        ) from exc
    except (OSError, UnicodeError) as exc:
        raise KnowledgeWorkbenchError(f"{label}无法安全读取") from exc


def _required_actor(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise KnowledgeWorkbenchError("actor 不能为空")
    actor = value.strip()
    if len(actor) > 80:
        raise KnowledgeWorkbenchError("actor 不能超过 80 个字符")
    if any(character in actor for character in "\r\n"):
        raise KnowledgeWorkbenchError("actor 不能包含换行")
    return actor


def _markdown_code(value: str) -> str:
    return value.replace("`", "ˋ")


def _powershell_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _blockquote(value: str) -> list[str]:
    return [
        "> " + line if line else ">"
        for line in value.splitlines()
    ]
