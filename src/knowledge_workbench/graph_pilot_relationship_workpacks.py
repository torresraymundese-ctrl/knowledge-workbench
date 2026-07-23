from __future__ import annotations

import json
import re
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Any

from .audit import record_event
from .config import WorkspacePaths
from .database import Database
from .entity_relationships import (
    RELATION_KEY_PATTERN,
    _create_entity_relationship_in_transaction,
    list_relation_types,
)
from .errors import InvalidTransitionError, KnowledgeWorkbenchError
from .graph_pilot import inspect_graph_pilot_pack, resolve_graph_pilot_pack
from .graph_pilot_review_workpacks import (
    _blockquote,
    _markdown_code,
    _powershell_literal,
    _read_work_pack,
    _require_safe_status,
    _validate_candidate_snapshot,
    _validated_output,
)
from .utils import sha256_text, utc_now
from .wiki import write_text_atomic


PACK_TYPE = "graph-pilot-relationship-curation-pack"
EXPORT_EVENT = "graph_pilot_relationship_curation_pack_exported"
APPLY_EVENT = "graph_pilot_relationship_curation_applied"
ENTITY_ID_PATTERN = re.compile(r"^entity_[a-f0-9]{32}$")
REFERENCE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$")


def export_graph_pilot_relationship_curation_work_pack(
    database: Database,
    paths: WorkspacePaths,
    source_pack_path: Path,
    output: Path,
    *,
    actor: str,
) -> Path:
    actor = _required_actor(actor)
    output = _validated_output(paths, output, "图谱试点业务关系裁决工作包")
    source_path, source_content, source_pack = resolve_graph_pilot_pack(
        database, paths, source_pack_path
    )
    status = inspect_graph_pilot_pack(database, paths, source_path)
    _require_safe_status(status)
    verified_ids = {
        item["evidence_id"]
        for item in status["items"]
        if item["status"] == "verified"
    }
    relation_types = _active_relation_types(database)
    if not relation_types:
        raise InvalidTransitionError(
            "尚未登记 active 业务关系类型，不能导出关系裁决工作包"
        )
    all_mentions = _mention_snapshots(database, verified_ids)
    candidates = []
    records = []
    for candidate in source_pack["candidates"]:
        evidence_id = candidate["evidence_id"]
        mentions = all_mentions.get(evidence_id, [])
        if evidence_id not in verified_ids:
            continue
        if len({item["entity_id"] for item in mentions}) < 2:
            continue
        candidates.append((candidate, mentions))
        records.append(
            {
                "evidence_id": evidence_id,
                "expected_status": "verified",
                "mention_scope_sha256": _mention_scope_sha256(mentions),
            }
        )
    if not candidates:
        raise InvalidTransitionError(
            "没有同时包含至少两个 active 人工实体提及的 verified 试点证据"
        )
    source_relative = source_path.relative_to(
        paths.root.resolve()
    ).as_posix()
    source_sha256 = sha256_text(source_content)
    lines = [
        "---",
        f"type: {PACK_TYPE}",
        f"graph_pilot_pack_id: {source_pack['pack_id']}",
        f"source_pack_path: {source_relative}",
        f"source_content_sha256: {source_sha256}",
        f"curator: {actor}",
        f"generated_at: {utc_now()}",
        "---",
        "",
        f"# 图谱试点业务关系裁决：`{source_pack['pack_id']}`",
        "",
        f"- 裁决人：`{_markdown_code(actor)}`",
        f"- 可裁决 verified 证据：`{len(candidates)}`",
        "- 每条证据必须且只能选择“登记关系”或“当前无关系”。",
        "- 关系只能使用下列人工登记类型，并显式选择证据内两个 active 实体。",
        "- relationship_ref 在本包内标识同一关系；跨证据重复引用会合并支持证据。",
        "- 同一 relationship_ref 的类型、方向、端点和说明必须完全一致。",
        "- 系统不会把共现、模型候选或多跳连通自动提升为业务关系。",
        "- 当前无关系时 JSON 必须为空且说明必填；整包一次原子应用。",
        "",
        "## 当前 active 关系类型",
        "",
    ]
    for relation_type in relation_types:
        lines.append(
            "- "
            + json.dumps(
                {
                    "relation_key": relation_type["relation_key"],
                    "label": relation_type["label"],
                    "inverse_label": relation_type["inverse_label"],
                    "directed": relation_type["directed"],
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
    lines.extend(
        [
            "",
            "## 应用本批关系裁决",
            "",
            "```powershell",
            (
                f".\\.venv\\Scripts\\knowledge.exe graph "
                f"pilot-relationship-apply {_powershell_literal(str(output))} "
                f"--actor {_powershell_literal(actor)}"
            ),
            "```",
            "",
        ]
    )
    for (candidate, mentions), record in zip(
        candidates, records, strict=True
    ):
        entity_catalog = [
            {
                "entity_id": item["entity_id"],
                "canonical_name": item["canonical_name"],
                "entity_type": item["entity_type"],
                "mention": item["mention_text"],
            }
            for item in mentions
        ]
        lines.extend(
            [
                f"## `{candidate['evidence_id']}`",
                "",
                "- 导出状态：`verified`",
                f"- 实体范围 SHA-256：`{record['mention_scope_sha256']}`",
                "- 实体清单 JSON："
                + json.dumps(
                    entity_catalog, ensure_ascii=False, sort_keys=True
                ),
                "- [ ] 登记关系",
                "- [ ] 当前无关系",
                "- 关系裁决 JSON：[]",
                '- 裁决说明 JSON：""',
                "",
                f"### 来源：`{_markdown_code(candidate['document_name'])}`",
                "",
                f"- 资料 ID：`{candidate['document_id']}`",
                f"- 文件版本：`{candidate['document_version_id']}`",
                f"- 处理运行：`{candidate['processing_run_id']}`",
                f"- 运行序号：`{candidate['run_ordinal']}`",
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
                EXPORT_EVENT,
                "graph_pilot_pack",
                source_pack["pack_id"],
                actor=actor,
                details={
                    "output": output.relative_to(
                        paths.root.resolve()
                    ).as_posix(),
                    "source_pack_path": source_relative,
                    "source_content_sha256": source_sha256,
                    "evidence_count": len(records),
                    "evidence_ids": [
                        item["evidence_id"] for item in records
                    ],
                    "scope_sha256": _scope_sha256(records),
                    "relation_type_scope_sha256": _relation_type_scope_sha256(
                        relation_types
                    ),
                    "template_sha256": _template_sha256(content),
                    "content_sha256": sha256_text(content),
                },
            )
    except Exception:
        output.unlink(missing_ok=True)
        raise
    return output


def apply_graph_pilot_relationship_curation_work_pack(
    database: Database,
    paths: WorkspacePaths,
    work_pack_path: Path,
    *,
    actor: str,
) -> dict[str, Any]:
    actor = _required_actor(actor)
    work_pack_path, content = _read_work_pack(
        paths, work_pack_path, "图谱试点业务关系裁决工作包"
    )
    metadata, decisions = _parse_work_pack(content)
    if metadata["curator"] != actor:
        raise KnowledgeWorkbenchError(
            "工作包声明的关系裁决人与当前 actor 不一致"
        )
    source_path = (
        paths.root.resolve() / metadata["source_pack_path"]
    ).resolve()
    try:
        source_path.relative_to(paths.evaluations.resolve())
    except ValueError as exc:
        raise KnowledgeWorkbenchError("来源图谱试点包路径无效") from exc
    source_path, source_content, source_pack = resolve_graph_pilot_pack(
        database, paths, source_path
    )
    if source_pack["pack_id"] != metadata["graph_pilot_pack_id"]:
        raise KnowledgeWorkbenchError("来源图谱试点包身份不一致")
    if sha256_text(source_content) != metadata["source_content_sha256"]:
        raise KnowledgeWorkbenchError("来源图谱试点包内容哈希已变化")
    status = inspect_graph_pilot_pack(database, paths, source_path)
    _require_safe_status(status)
    records = [
        {
            "evidence_id": item["evidence_id"],
            "expected_status": item["expected_status"],
            "mention_scope_sha256": item["mention_scope_sha256"],
        }
        for item in decisions
    ]
    _validate_export_audit(
        database,
        paths,
        work_pack_path,
        content,
        metadata,
        records,
        actor=actor,
    )
    candidates = {
        item["evidence_id"]: item for item in source_pack["candidates"]
    }
    grouped = _group_relationships(decisions)
    allowed_relation_keys = {
        item["relation_key"] for item in _active_relation_types(database)
    }
    for item in grouped:
        if item["relation_key"] not in allowed_relation_keys:
            raise KnowledgeWorkbenchError(
                f"关系类型不在导出时的 active 范围内：{item['relation_key']}"
            )
    work_pack_sha256 = sha256_text(content)
    relationship_ids: list[str] = []
    no_relationship_ids = [
        item["evidence_id"]
        for item in decisions
        if item["decision"] == "none"
    ]
    try:
        with database.transaction() as connection:
            relative_path = work_pack_path.relative_to(
                paths.root.resolve()
            ).as_posix()
            already_applied = connection.execute(
                """
                SELECT details_json FROM audit_log
                WHERE event_type = ?
                  AND entity_type = 'graph_pilot_pack'
                  AND entity_id = ?
                """,
                (APPLY_EVENT, source_pack["pack_id"]),
            ).fetchall()
            for row in already_applied:
                try:
                    details = json.loads(row["details_json"])
                except json.JSONDecodeError:
                    continue
                if details.get("work_pack_path") == relative_path:
                    raise InvalidTransitionError(
                        "该业务关系裁决工作包已经应用"
                    )
            for record in records:
                candidate = candidates.get(record["evidence_id"])
                if candidate is None:
                    raise KnowledgeWorkbenchError(
                        f"证据不在来源图谱试点包中：{record['evidence_id']}"
                    )
                _validate_candidate_snapshot(
                    connection, candidate, expected_status="verified"
                )
                mentions = _mention_snapshots_in_connection(
                    connection, {record["evidence_id"]}
                ).get(record["evidence_id"], [])
                if (
                    len({item["entity_id"] for item in mentions}) < 2
                    or _mention_scope_sha256(mentions)
                    != record["mention_scope_sha256"]
                ):
                    raise KnowledgeWorkbenchError(
                        f"证据实体提及范围已变化：{record['evidence_id']}"
                    )
            for item in grouped:
                relationship_id = _create_entity_relationship_in_transaction(
                    connection,
                    item["relation_key"],
                    item["source_entity_id"],
                    item["target_entity_id"],
                    item["evidence_ids"],
                    actor=actor,
                    note=item["note"],
                )
                relationship_ids.append(relationship_id)
            record_event(
                connection,
                APPLY_EVENT,
                "graph_pilot_pack",
                source_pack["pack_id"],
                actor=actor,
                details={
                    "work_pack_path": relative_path,
                    "work_pack_sha256": work_pack_sha256,
                    "scope_sha256": _scope_sha256(records),
                    "evidence_count": len(records),
                    "evidence_ids": [
                        item["evidence_id"] for item in records
                    ],
                    "created_relationship_ids": relationship_ids,
                    "created_relationship_count": len(relationship_ids),
                    "relationship_reference_sha256": sorted(
                        sha256_text(item["relationship_ref"])
                        for item in grouped
                    ),
                    "no_relationship_evidence_ids": no_relationship_ids,
                    "no_relationship_count": len(no_relationship_ids),
                    "decision_note_sha256_by_evidence": {
                        item["evidence_id"]: sha256_text(item["note"])
                        for item in decisions
                        if item["note"]
                    },
                },
            )
    except InvalidTransitionError:
        raise
    except KnowledgeWorkbenchError as exc:
        raise KnowledgeWorkbenchError(
            f"业务关系裁决失败，整包未写入：{exc}"
        ) from exc
    except sqlite3.IntegrityError as exc:
        raise KnowledgeWorkbenchError(
            "业务关系重复或完整性冲突，整包未写入"
        ) from exc
    return {
        "graph_pilot_pack_id": source_pack["pack_id"],
        "evidence_count": len(records),
        "created_relationship_count": len(relationship_ids),
        "no_relationship_count": len(no_relationship_ids),
        "actor": actor,
    }


def _parse_work_pack(
    content: str,
) -> tuple[dict[str, str], list[dict[str, Any]]]:
    lines = content.splitlines()
    metadata, start = _parse_frontmatter(lines)
    heading = re.compile(r"^##\s+`(ev_[a-f0-9]{32})`\s*$")
    status = re.compile(r"^-\s+导出状态：`([^`]+)`\s*$")
    scope = re.compile(r"^-\s+实体范围 SHA-256：`([a-f0-9]{64})`\s*$")
    catalog = re.compile(r"^-\s+实体清单 JSON：(.*)$")
    curate = re.compile(r"^-\s+\[([ xX])\]\s+登记关系\s*$")
    none = re.compile(r"^-\s+\[([ xX])\]\s+当前无关系\s*$")
    relationships = re.compile(r"^-\s+关系裁决 JSON：(.*)$")
    note = re.compile(r"^-\s+裁决说明 JSON：(.*)$")
    records: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for line in lines[start:]:
        if match := heading.match(line):
            evidence_id = match.group(1)
            if any(item["evidence_id"] == evidence_id for item in records):
                raise KnowledgeWorkbenchError(
                    f"工作包包含重复证据：{evidence_id}"
                )
            current = {
                "evidence_id": evidence_id,
                "expected_status": None,
                "mention_scope_sha256": None,
                "entity_ids": None,
                "curate": False,
                "none": False,
                "relationships": None,
                "note": None,
                "seen": set(),
            }
            records.append(current)
            continue
        if current is None:
            continue
        for key, pattern in (
            ("status", status),
            ("scope", scope),
            ("catalog", catalog),
            ("curate", curate),
            ("none", none),
            ("relationships", relationships),
            ("note", note),
        ):
            match = pattern.match(line)
            if not match:
                continue
            if key in current["seen"]:
                raise KnowledgeWorkbenchError(
                    f"证据 {current['evidence_id']} 包含重复字段：{key}"
                )
            current["seen"].add(key)
            value = match.group(1)
            if key == "status":
                current["expected_status"] = value.strip()
            elif key == "scope":
                current["mention_scope_sha256"] = value
            elif key == "catalog":
                current["entity_ids"] = _catalog_json(
                    value, current["evidence_id"]
                )
            elif key in {"curate", "none"}:
                current[key] = value.lower() == "x"
            elif key == "relationships":
                current["relationships"] = _relationship_json(
                    value, current["evidence_id"]
                )
            else:
                current["note"] = _note_json(
                    value, current["evidence_id"]
                )
            break
    if not records:
        raise KnowledgeWorkbenchError("工作包不包含证据")
    required = {
        "status",
        "scope",
        "catalog",
        "curate",
        "none",
        "relationships",
        "note",
    }
    decisions = []
    for record in records:
        missing = sorted(required - record["seen"])
        if missing:
            raise KnowledgeWorkbenchError(
                f"证据 {record['evidence_id']} 缺少字段："
                + ", ".join(missing)
            )
        if record["expected_status"] != "verified":
            raise KnowledgeWorkbenchError(
                f"证据 {record['evidence_id']} 导出状态必须为 verified"
            )
        if len(record["entity_ids"]) < 2:
            raise KnowledgeWorkbenchError(
                f"证据 {record['evidence_id']} 实体清单不足两个实体"
            )
        if int(record["curate"]) + int(record["none"]) != 1:
            raise KnowledgeWorkbenchError(
                f"证据 {record['evidence_id']} 必须且只能选择一种关系裁决"
            )
        decision = "curate" if record["curate"] else "none"
        if decision == "curate" and not record["relationships"]:
            raise KnowledgeWorkbenchError(
                f"证据 {record['evidence_id']} 登记关系时 JSON 不能为空"
            )
        if decision == "none" and record["relationships"]:
            raise KnowledgeWorkbenchError(
                f"证据 {record['evidence_id']} 选择当前无关系时 JSON 必须为空"
            )
        if decision == "none" and not record["note"]:
            raise KnowledgeWorkbenchError(
                f"证据 {record['evidence_id']} 选择当前无关系时必须填写说明"
            )
        for item in record["relationships"]:
            if (
                item["source_entity_id"] not in record["entity_ids"]
                or item["target_entity_id"] not in record["entity_ids"]
            ):
                raise KnowledgeWorkbenchError(
                    f"证据 {record['evidence_id']} 的关系端点不在实体清单中"
                )
        decisions.append({**record, "decision": decision})
    return metadata, decisions


def _parse_frontmatter(lines: list[str]) -> tuple[dict[str, str], int]:
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
    if metadata.get("type") != PACK_TYPE:
        raise KnowledgeWorkbenchError("文件不是图谱试点关系裁决工作包")
    required = (
        "graph_pilot_pack_id",
        "source_pack_path",
        "source_content_sha256",
        "curator",
    )
    missing = [key for key in required if not metadata.get(key)]
    if missing:
        raise KnowledgeWorkbenchError(
            "工作包缺少元数据：" + ", ".join(missing)
        )
    if not re.fullmatch(
        r"[a-f0-9]{64}", metadata["source_content_sha256"]
    ):
        raise KnowledgeWorkbenchError("source_content_sha256 无效")
    return metadata, end + 1


def _catalog_json(value: str, evidence_id: str) -> set[str]:
    try:
        payload = json.loads(value.strip())
    except json.JSONDecodeError as exc:
        raise KnowledgeWorkbenchError(
            f"证据 {evidence_id} 的实体清单不是有效 JSON"
        ) from exc
    if not isinstance(payload, list) or len(payload) > 100:
        raise KnowledgeWorkbenchError(
            f"证据 {evidence_id} 的实体清单格式无效"
        )
    ids = set()
    for item in payload:
        if not isinstance(item, dict):
            raise KnowledgeWorkbenchError(
                f"证据 {evidence_id} 的实体清单项必须是对象"
            )
        entity_id = item.get("entity_id")
        if not isinstance(entity_id, str) or not ENTITY_ID_PATTERN.fullmatch(
            entity_id
        ):
            raise KnowledgeWorkbenchError(
                f"证据 {evidence_id} 的实体清单 ID 无效"
            )
        ids.add(entity_id)
    return ids


def _relationship_json(
    value: str, evidence_id: str
) -> list[dict[str, str]]:
    try:
        payload = json.loads(value.strip())
    except json.JSONDecodeError as exc:
        raise KnowledgeWorkbenchError(
            f"证据 {evidence_id} 的关系裁决不是有效 JSON"
        ) from exc
    if not isinstance(payload, list) or len(payload) > 50:
        raise KnowledgeWorkbenchError(
            f"证据 {evidence_id} 的关系裁决必须是最多 50 项的 JSON 数组"
        )
    result = []
    seen_refs: set[str] = set()
    for index, item in enumerate(payload, start=1):
        if not isinstance(item, dict) or set(item) != {
            "relationship_ref",
            "relation_key",
            "source_entity_id",
            "target_entity_id",
            "note",
        }:
            raise KnowledgeWorkbenchError(
                f"证据 {evidence_id} 的第 {index} 个关系裁决字段无效"
            )
        relationship_ref = item["relationship_ref"]
        relation_key = item["relation_key"]
        source = item["source_entity_id"]
        target = item["target_entity_id"]
        note = item["note"]
        if not isinstance(
            relationship_ref, str
        ) or not REFERENCE_PATTERN.fullmatch(relationship_ref):
            raise KnowledgeWorkbenchError(
                f"证据 {evidence_id} 的 relationship_ref 无效"
            )
        if relationship_ref in seen_refs:
            raise KnowledgeWorkbenchError(
                f"证据 {evidence_id} 包含重复 relationship_ref"
            )
        seen_refs.add(relationship_ref)
        if not isinstance(
            relation_key, str
        ) or not RELATION_KEY_PATTERN.fullmatch(relation_key):
            raise KnowledgeWorkbenchError(
                f"证据 {evidence_id} 的 relation_key 无效"
            )
        if (
            not isinstance(source, str)
            or not ENTITY_ID_PATTERN.fullmatch(source)
            or not isinstance(target, str)
            or not ENTITY_ID_PATTERN.fullmatch(target)
            or source == target
        ):
            raise KnowledgeWorkbenchError(
                f"证据 {evidence_id} 的关系端点无效"
            )
        note = _required_text(
            note, "note", evidence_id, maximum=2000
        )
        result.append(
            {
                "relationship_ref": relationship_ref,
                "relation_key": relation_key,
                "source_entity_id": source,
                "target_entity_id": target,
                "note": note,
            }
        )
    return result


def _note_json(value: str, evidence_id: str) -> str:
    try:
        note = json.loads(value.strip())
    except json.JSONDecodeError as exc:
        raise KnowledgeWorkbenchError(
            f"证据 {evidence_id} 的裁决说明不是有效 JSON 字符串"
        ) from exc
    if not isinstance(note, str) or len(note.strip()) > 2000:
        raise KnowledgeWorkbenchError(
            f"证据 {evidence_id} 的裁决说明必须是最多 2000 字符的 JSON 字符串"
        )
    return note.strip()


def _group_relationships(
    decisions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    triples: dict[tuple[str, str, str], str] = {}
    for decision in decisions:
        for item in decision["relationships"]:
            relationship_ref = item["relationship_ref"]
            definition = {
                key: item[key]
                for key in (
                    "relationship_ref",
                    "relation_key",
                    "source_entity_id",
                    "target_entity_id",
                    "note",
                )
            }
            existing = grouped.get(relationship_ref)
            if existing is None:
                grouped[relationship_ref] = {
                    **definition,
                    "evidence_ids": [decision["evidence_id"]],
                }
            else:
                existing_definition = {
                    key: existing[key] for key in definition
                }
                if existing_definition != definition:
                    raise KnowledgeWorkbenchError(
                        f"relationship_ref 定义不一致：{relationship_ref}"
                    )
                existing["evidence_ids"].append(decision["evidence_id"])
            triple = (
                item["relation_key"],
                item["source_entity_id"],
                item["target_entity_id"],
            )
            other_ref = triples.setdefault(triple, relationship_ref)
            if other_ref != relationship_ref:
                raise KnowledgeWorkbenchError(
                    "相同类型、方向和端点不能使用不同 relationship_ref"
                )
    return [grouped[key] for key in sorted(grouped)]


def _mention_snapshots(
    database: Database, evidence_ids: set[str]
) -> dict[str, list[dict[str, Any]]]:
    with database.connect() as connection:
        return _mention_snapshots_in_connection(connection, evidence_ids)


def _mention_snapshots_in_connection(
    connection: sqlite3.Connection, evidence_ids: set[str]
) -> dict[str, list[dict[str, Any]]]:
    if not evidence_ids:
        return {}
    placeholders = ", ".join("?" for _ in evidence_ids)
    rows = connection.execute(
        f"""
        SELECT eem.evidence_id, eem.entity_id, eem.alias_id,
               eem.mention_text, ce.canonical_name, ce.entity_type,
               ce.status
        FROM evidence_entity_mentions eem
        JOIN canonical_entities ce ON ce.id = eem.entity_id
        WHERE eem.evidence_id IN ({placeholders})
          AND ce.status = 'active'
        ORDER BY eem.evidence_id, eem.entity_id, eem.alias_id,
                 eem.mention_text
        """,
        sorted(evidence_ids),
    ).fetchall()
    result: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        result[row["evidence_id"]].append(dict(row))
    return dict(result)


def _mention_scope_sha256(mentions: list[dict[str, Any]]) -> str:
    scope = [
        {
            "entity_id": item["entity_id"],
            "alias_id": item["alias_id"],
            "mention_text_sha256": sha256_text(item["mention_text"]),
            "canonical_name_sha256": sha256_text(item["canonical_name"]),
            "entity_type": item["entity_type"],
            "status": item["status"],
        }
        for item in mentions
    ]
    return sha256_text(
        json.dumps(
            scope,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )


def _validate_export_audit(
    database: Database,
    paths: WorkspacePaths,
    work_pack_path: Path,
    content: str,
    metadata: dict[str, str],
    records: list[dict[str, str]],
    *,
    actor: str,
) -> None:
    relative_path = work_pack_path.relative_to(
        paths.root.resolve()
    ).as_posix()
    current_relation_type_scope = _relation_type_scope_sha256(
        _active_relation_types(database)
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
            (EXPORT_EVENT, metadata["graph_pilot_pack_id"], actor),
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
            and details.get("evidence_ids")
            == [item["evidence_id"] for item in records]
            and details.get("evidence_count") == len(records)
            and details.get("scope_sha256") == _scope_sha256(records)
            and details.get("relation_type_scope_sha256")
            == current_relation_type_scope
            and details.get("template_sha256") == _template_sha256(content)
        ):
            return
    raise KnowledgeWorkbenchError(
        "工作包缺少匹配的系统导出审计记录，或修改了受保护内容"
    )


def _template_sha256(content: str) -> str:
    checkbox = re.compile(
        r"^-\s+\[[ xX]\]\s+(登记关系|当前无关系)\s*$"
    )
    relationships = re.compile(r"^-\s+关系裁决 JSON：.*$")
    note = re.compile(r"^-\s+裁决说明 JSON：.*$")
    normalized = []
    for line in content.splitlines():
        if match := checkbox.match(line):
            normalized.append(f"- [ ] {match.group(1)}")
        elif relationships.match(line):
            normalized.append("- 关系裁决 JSON：[]")
        elif note.match(line):
            normalized.append('- 裁决说明 JSON：""')
        else:
            normalized.append(line)
    return sha256_text("\n".join(normalized).rstrip() + "\n")


def _scope_sha256(records: list[dict[str, str]]) -> str:
    return sha256_text(
        json.dumps(
            records,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )


def _relation_type_scope_sha256(relation_types: list[dict]) -> str:
    scope = [
        {
            "relation_key": item["relation_key"],
            "label_sha256": sha256_text(item["label"]),
            "inverse_label_sha256": (
                sha256_text(item["inverse_label"])
                if item["inverse_label"]
                else None
            ),
            "directed": item["directed"],
            "status": item["status"],
        }
        for item in relation_types
    ]
    return sha256_text(
        json.dumps(
            scope,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )


def _active_relation_types(database: Database) -> list[dict]:
    with database.connect() as connection:
        count = connection.execute(
            """
            SELECT COUNT(*) FROM entity_relation_types
            WHERE status = 'active'
            """
        ).fetchone()[0]
    if count > 500:
        raise KnowledgeWorkbenchError(
            "active 关系类型超过 500 个，不能生成完整受控快照"
        )
    return list_relation_types(database, status="active", limit=500)


def _required_actor(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise KnowledgeWorkbenchError("actor 不能为空")
    value = value.strip()
    if len(value) > 80 or any(char in value for char in "\r\n"):
        raise KnowledgeWorkbenchError(
            "actor 不能超过 80 个字符或包含换行"
        )
    return value


def _required_text(
    value: Any,
    label: str,
    evidence_id: str,
    *,
    maximum: int,
) -> str:
    if not isinstance(value, str) or not value.strip():
        raise KnowledgeWorkbenchError(
            f"证据 {evidence_id} 的 {label} 不能为空"
        )
    value = value.strip()
    if len(value) > maximum:
        raise KnowledgeWorkbenchError(
            f"证据 {evidence_id} 的 {label} 不能超过 {maximum} 字符"
        )
    return value
