from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any

from .audit import record_event
from .config import WorkspacePaths
from .database import Database
from .entities import (
    ENTITY_TYPES,
    _add_entity_alias_in_transaction,
    _create_entity_in_transaction,
    _link_evidence_entity_in_transaction,
    normalize_entity_name,
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


PACK_TYPE = "graph-pilot-entity-curation-pack"
EXPORT_EVENT = "graph_pilot_entity_curation_pack_exported"
APPLY_EVENT = "graph_pilot_entity_curation_applied"
ENTITY_ID_PATTERN = re.compile(r"^entity_[a-f0-9]{32}$")
EVIDENCE_ID_PATTERN = re.compile(r"^ev_[a-f0-9]{32}$")


def export_graph_pilot_entity_curation_work_pack(
    database: Database,
    paths: WorkspacePaths,
    source_pack_path: Path,
    output: Path,
    *,
    actor: str,
) -> Path:
    actor = _required_actor(actor)
    output = _validated_output(paths, output, "图谱试点实体裁决工作包")
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
    candidates = [
        item
        for item in source_pack["candidates"]
        if item["evidence_id"] in verified_ids
    ]
    if not candidates:
        raise InvalidTransitionError(
            "图谱试点包没有 verified 证据可进行实体裁决"
        )
    records = [
        {
            "evidence_id": item["evidence_id"],
            "expected_status": "verified",
        }
        for item in candidates
    ]
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
        f"# 图谱试点实体裁决：`{source_pack['pack_id']}`",
        "",
        f"- 裁决人：`{_markdown_code(actor)}`",
        f"- verified 证据：`{len(candidates)}`",
        "- 每条证据必须且只能选择“登记实体”或“当前无实体”。",
        "- 登记实体时填写 JSON 数组；mention 必须逐字出现在原文。",
        "- 已有实体使用 entity_id；新实体使用 canonical_name 与 entity_type。",
        "- 同一类型规范名在本包内可重复引用并只创建一次；数据库已有名称必须显式填写 entity_id。",
        "- 当前无实体时数组必须为空，并填写说明。整包一次原子应用。",
        "",
        "## 应用本批实体裁决",
        "",
        "```powershell",
        (
            f".\\.venv\\Scripts\\knowledge.exe graph "
            f"pilot-entity-apply {_powershell_literal(str(output))} "
            f"--actor {_powershell_literal(actor)}"
        ),
        "```",
        "",
    ]
    for candidate in candidates:
        lines.extend(
            [
                f"## `{candidate['evidence_id']}`",
                "",
                "- 导出状态：`verified`",
                "- [ ] 登记实体",
                "- [ ] 当前无实体",
                "- 实体裁决 JSON：[]",
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
                    "template_sha256": _template_sha256(content),
                    "content_sha256": sha256_text(content),
                },
            )
    except Exception:
        output.unlink(missing_ok=True)
        raise
    return output


def apply_graph_pilot_entity_curation_work_pack(
    database: Database,
    paths: WorkspacePaths,
    work_pack_path: Path,
    *,
    actor: str,
) -> dict[str, Any]:
    actor = _required_actor(actor)
    work_pack_path, content = _read_work_pack(
        paths, work_pack_path, "图谱试点实体裁决工作包"
    )
    metadata, decisions = _parse_work_pack(content)
    if metadata["curator"] != actor:
        raise KnowledgeWorkbenchError(
            "工作包声明的实体裁决人与当前 actor 不一致"
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
    work_pack_sha256 = sha256_text(content)
    created_by_key: dict[tuple[str, str], str] = {}
    created_entity_ids: list[str] = []
    linked_pairs: list[tuple[str, str]] = []
    no_entity_ids: list[str] = []
    try:
        with database.transaction() as connection:
            already_applied = connection.execute(
                """
                SELECT 1 FROM audit_log
                WHERE event_type = ?
                  AND entity_type = 'graph_pilot_pack'
                  AND entity_id = ?
                  AND json_extract(details_json, '$.work_pack_path') = ?
                LIMIT 1
                """,
                (
                    APPLY_EVENT,
                    source_pack["pack_id"],
                    work_pack_path.relative_to(
                        paths.root.resolve()
                    ).as_posix(),
                ),
            ).fetchone()
            if already_applied:
                raise InvalidTransitionError("该实体裁决工作包已经应用")
            for record in records:
                candidate = candidates.get(record["evidence_id"])
                if candidate is None:
                    raise KnowledgeWorkbenchError(
                        f"证据不在来源图谱试点包中：{record['evidence_id']}"
                    )
                _validate_candidate_snapshot(
                    connection, candidate, expected_status="verified"
                )
            for decision in decisions:
                evidence_id = decision["evidence_id"]
                if decision["decision"] == "none":
                    no_entity_ids.append(evidence_id)
                    continue
                seen_mentions: set[str] = set()
                for item in decision["entities"]:
                    normalized_mention = normalize_entity_name(
                        item["mention"]
                    )
                    if normalized_mention in seen_mentions:
                        raise KnowledgeWorkbenchError(
                            f"证据 {evidence_id} 包含重复实体提及裁决"
                        )
                    seen_mentions.add(normalized_mention)
                    entity_id = item.get("entity_id")
                    if entity_id is None:
                        key = (
                            item["entity_type"],
                            normalize_entity_name(item["canonical_name"]),
                        )
                        entity_id = created_by_key.get(key)
                        if entity_id is None:
                            entity_id = _create_entity_in_transaction(
                                connection,
                                item["canonical_name"],
                                item["entity_type"],
                                actor=actor,
                            )
                            created_by_key[key] = entity_id
                            created_entity_ids.append(entity_id)
                    _add_entity_alias_in_transaction(
                        connection,
                        entity_id,
                        item["mention"],
                        actor=actor,
                    )
                    linked = _link_evidence_entity_in_transaction(
                        connection,
                        entity_id,
                        evidence_id,
                        item["mention"],
                        actor=actor,
                    )
                    if linked:
                        linked_pairs.append((evidence_id, entity_id))
            record_event(
                connection,
                APPLY_EVENT,
                "graph_pilot_pack",
                source_pack["pack_id"],
                actor=actor,
                details={
                    "work_pack_path": work_pack_path.relative_to(
                        paths.root.resolve()
                    ).as_posix(),
                    "work_pack_sha256": work_pack_sha256,
                    "scope_sha256": _scope_sha256(records),
                    "evidence_count": len(records),
                    "evidence_ids": [
                        item["evidence_id"] for item in records
                    ],
                    "created_entity_ids": created_entity_ids,
                    "created_entity_count": len(created_entity_ids),
                    "linked_evidence_entity_count": len(linked_pairs),
                    "linked_evidence_entity_sha256": sha256_text(
                        json.dumps(
                            linked_pairs,
                            separators=(",", ":"),
                            sort_keys=True,
                        )
                    ),
                    "no_entity_evidence_ids": no_entity_ids,
                    "no_entity_count": len(no_entity_ids),
                    "note_sha256_by_evidence": {
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
            f"实体裁决失败，整包未写入：{exc}"
        ) from exc
    except sqlite3.IntegrityError as exc:
        raise KnowledgeWorkbenchError(
            "实体名称、别名或证据关联与当前数据冲突，整包未写入"
        ) from exc
    return {
        "graph_pilot_pack_id": source_pack["pack_id"],
        "evidence_count": len(records),
        "created_entity_count": len(created_entity_ids),
        "linked_evidence_entity_count": len(linked_pairs),
        "no_entity_count": len(no_entity_ids),
        "actor": actor,
    }


def _parse_work_pack(
    content: str,
) -> tuple[dict[str, str], list[dict[str, Any]]]:
    lines = content.splitlines()
    metadata, start = _parse_frontmatter(lines)
    heading = re.compile(r"^##\s+`(ev_[a-f0-9]{32})`\s*$")
    status = re.compile(r"^-\s+导出状态：`([^`]+)`\s*$")
    curate = re.compile(r"^-\s+\[([ xX])\]\s+登记实体\s*$")
    none = re.compile(r"^-\s+\[([ xX])\]\s+当前无实体\s*$")
    entities = re.compile(r"^-\s+实体裁决 JSON：(.*)$")
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
                "curate": False,
                "none": False,
                "entities": None,
                "note": None,
                "seen": set(),
            }
            records.append(current)
            continue
        if current is None:
            continue
        for key, pattern in (
            ("status", status),
            ("curate", curate),
            ("none", none),
            ("entities", entities),
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
            elif key in {"curate", "none"}:
                current[key] = value.lower() == "x"
            elif key == "entities":
                current["entities"] = _entity_json(
                    value, current["evidence_id"]
                )
            else:
                current["note"] = _note_json(
                    value, current["evidence_id"]
                )
            break
    if not records:
        raise KnowledgeWorkbenchError("工作包不包含证据")
    required = {"status", "curate", "none", "entities", "note"}
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
        if int(record["curate"]) + int(record["none"]) != 1:
            raise KnowledgeWorkbenchError(
                f"证据 {record['evidence_id']} 必须且只能选择一种实体裁决"
            )
        decision = "curate" if record["curate"] else "none"
        if decision == "curate" and not record["entities"]:
            raise KnowledgeWorkbenchError(
                f"证据 {record['evidence_id']} 登记实体时 JSON 不能为空"
            )
        if decision == "none" and record["entities"]:
            raise KnowledgeWorkbenchError(
                f"证据 {record['evidence_id']} 选择当前无实体时 JSON 必须为空"
            )
        if decision == "none" and not record["note"]:
            raise KnowledgeWorkbenchError(
                f"证据 {record['evidence_id']} 选择当前无实体时必须填写说明"
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
        raise KnowledgeWorkbenchError("文件不是图谱试点实体裁决工作包")
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


def _entity_json(value: str, evidence_id: str) -> list[dict[str, str]]:
    try:
        payload = json.loads(value.strip())
    except json.JSONDecodeError as exc:
        raise KnowledgeWorkbenchError(
            f"证据 {evidence_id} 的实体裁决不是有效 JSON"
        ) from exc
    if not isinstance(payload, list) or len(payload) > 50:
        raise KnowledgeWorkbenchError(
            f"证据 {evidence_id} 的实体裁决必须是最多 50 项的 JSON 数组"
        )
    result = []
    for index, item in enumerate(payload, start=1):
        if not isinstance(item, dict):
            raise KnowledgeWorkbenchError(
                f"证据 {evidence_id} 的第 {index} 个实体裁决必须是对象"
            )
        allowed = {
            "mention",
            "entity_id",
            "canonical_name",
            "entity_type",
        }
        if set(item) - allowed:
            raise KnowledgeWorkbenchError(
                f"证据 {evidence_id} 的第 {index} 个实体裁决包含未知字段"
            )
        mention = _required_text(
            item.get("mention"), "mention", evidence_id, maximum=500
        )
        entity_id = item.get("entity_id")
        canonical_name = item.get("canonical_name")
        entity_type = item.get("entity_type")
        if entity_id is not None:
            if not isinstance(entity_id, str) or not ENTITY_ID_PATTERN.fullmatch(
                entity_id
            ):
                raise KnowledgeWorkbenchError(
                    f"证据 {evidence_id} 的 entity_id 无效"
                )
            if canonical_name is not None or entity_type is not None:
                raise KnowledgeWorkbenchError(
                    f"证据 {evidence_id} 的已有实体不能同时填写新实体字段"
                )
            result.append({"mention": mention, "entity_id": entity_id})
            continue
        canonical_name = _required_text(
            canonical_name,
            "canonical_name",
            evidence_id,
            maximum=500,
        )
        if entity_type not in ENTITY_TYPES:
            raise KnowledgeWorkbenchError(
                f"证据 {evidence_id} 的 entity_type 无效"
            )
        result.append(
            {
                "mention": mention,
                "canonical_name": canonical_name,
                "entity_type": entity_type,
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
            and details.get("template_sha256") == _template_sha256(content)
        ):
            return
    raise KnowledgeWorkbenchError(
        "工作包缺少匹配的系统导出审计记录，或修改了受保护内容"
    )


def _template_sha256(content: str) -> str:
    checkbox = re.compile(
        r"^-\s+\[[ xX]\]\s+(登记实体|当前无实体)\s*$"
    )
    entities = re.compile(r"^-\s+实体裁决 JSON：.*$")
    note = re.compile(r"^-\s+裁决说明 JSON：.*$")
    normalized = []
    for line in content.splitlines():
        if match := checkbox.match(line):
            normalized.append(f"- [ ] {match.group(1)}")
        elif entities.match(line):
            normalized.append("- 实体裁决 JSON：[]")
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
