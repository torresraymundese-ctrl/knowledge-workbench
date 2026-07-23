from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .audit import record_event
from .config import WorkspacePaths
from .conflict_candidates import (
    _candidate_counts,
    _candidate_pack_by_id,
    _candidate_pack_phase,
    _candidate_submission_state,
    apply_cross_document_candidate_label_batch,
    apply_cross_document_candidate_review_batch,
)
from .conflict_labeling_plan import resolve_conflict_labeling_batch
from .database import Database
from .errors import InvalidTransitionError, KnowledgeWorkbenchError
from .utils import sha256_file, sha256_text, utc_now
from .wiki import write_text_atomic


_ANNOTATION_PACK_TYPE = "conflict-batch-annotation-pack"
_REVIEW_PACK_TYPE = "conflict-batch-review-pack"


def export_conflict_batch_annotation_pack(
    database: Database,
    paths: WorkspacePaths,
    plan_id: str,
    batch_id: str,
    output: Path,
    *,
    actor: str,
) -> Path:
    actor = _required_actor(actor)
    output = _validated_output(paths, output, "冲突批次标注工作包")
    batch, source_path, source_pack = _resolved_source(
        database, paths, plan_id, batch_id
    )
    submission, drifted = _candidate_submission_state(database, source_pack)
    phase = _candidate_pack_phase(
        submission, drifted, _candidate_counts(source_pack)
    )
    if phase != "labeling":
        raise InvalidTransitionError(
            "只有 labeling 阶段可以导出冲突批次标注工作包"
        )
    source_content_sha256 = sha256_file(source_path)
    candidates = _ordered_candidates(source_pack, batch["candidate_ids"])
    generated_at = utc_now()
    lines = [
        "---",
        f"type: {_ANNOTATION_PACK_TYPE}",
        f"plan_id: {plan_id}",
        f"batch_id: {batch_id}",
        f"source_pack_id: {source_pack['pack_id']}",
        f"source_content_sha256: {source_content_sha256}",
        f"annotator: {actor}",
        f"generated_at: {generated_at}",
        "---",
        "",
        f"# 跨文档冲突标注：{batch_id}",
        "",
        f"- 计划：`{plan_id}`",
        f"- 来源候选包：`{source_pack['pack_id']}`",
        f"- 标注人：`{actor}`",
        f"- 本批候选：`{len(candidates)}`",
        "- 规则预测只用于召回与排序，不是人工标签，不得机械照抄。",
        "- 必须回到左右来源及上下文核对适用范围；本文件只在本机使用。",
        "- 每个候选必须且只能勾选“冲突”或“非冲突”；冲突时必须填写类型。",
        "- 工作包按来源候选包 SHA-256 锁定；其他页面保存标签后必须重新导出。",
        "",
        "## 应用本批标签",
        "",
        "完成全部候选后运行：",
        "",
        "```powershell",
        (
            f".\\.venv\\Scripts\\knowledge.exe conflict "
            "batch-annotation-apply "
            f"{_powershell_literal(str(output))} "
            f"--actor {_powershell_literal(actor)}"
        ),
        "```",
        "",
    ]
    for candidate in candidates:
        label = candidate["label"]
        expected_conflict = label["expected_conflict"]
        lines.extend(
            [
                f"## `{candidate['candidate_id']}`",
                "",
                (
                    "- [x] 冲突"
                    if expected_conflict is True
                    else "- [ ] 冲突"
                ),
                (
                    "- [x] 非冲突"
                    if expected_conflict is False
                    else "- [ ] 非冲突"
                ),
                f"- 冲突类型：`{label['expected_type'] or 'null'}`",
                "- 标注依据 JSON："
                + json.dumps(
                    label["note"] or "", ensure_ascii=False
                ),
                f"- 规则预测：`{candidate['predicted_type'] or 'none'}`",
                f"- 相似度：`{candidate['similarity']}`",
                "",
                *_candidate_side_lines("左侧", candidate["left"]),
                *_candidate_side_lines("右侧", candidate["right"]),
            ]
        )
    content = "\n".join(lines).rstrip() + "\n"
    _write_export(
        database,
        paths,
        output,
        content,
        event_type="conflict_batch_annotation_pack_exported",
        plan_id=plan_id,
        batch_id=batch_id,
        source_pack_id=source_pack["pack_id"],
        source_content_sha256=source_content_sha256,
        candidate_count=len(candidates),
        actor=actor,
    )
    return output


def apply_conflict_batch_annotation_pack(
    database: Database,
    paths: WorkspacePaths,
    pack_path: Path,
    *,
    actor: str,
) -> dict[str, Any]:
    actor = _required_actor(actor)
    pack_path, content = _read_work_pack(
        paths, pack_path, "冲突批次标注工作包"
    )
    metadata, labels = _parse_annotation_pack(content)
    if metadata["annotator"] != actor:
        raise KnowledgeWorkbenchError(
            "工作包声明的标注人与当前 actor 不一致"
        )
    batch = resolve_conflict_labeling_batch(
        database,
        paths,
        metadata["plan_id"],
        metadata["batch_id"],
    )
    _validate_work_pack_scope(metadata, batch, labels)
    _validate_export_audit(
        database,
        paths,
        pack_path,
        metadata,
        actor=actor,
        event_type="conflict_batch_annotation_pack_exported",
    )
    source_path, _ = _candidate_pack_by_id(
        paths, metadata["source_pack_id"]
    )
    _require_source_sha256(source_path, metadata["source_content_sha256"])
    return apply_cross_document_candidate_label_batch(
        database,
        paths,
        metadata["source_pack_id"],
        labels,
        expected_content_sha256=metadata["source_content_sha256"],
        plan_id=metadata["plan_id"],
        batch_id=metadata["batch_id"],
        work_pack_path=pack_path.relative_to(
            paths.root.resolve()
        ).as_posix(),
        work_pack_sha256=sha256_text(content),
        actor=actor,
    )


def export_conflict_batch_review_pack(
    database: Database,
    paths: WorkspacePaths,
    plan_id: str,
    batch_id: str,
    output: Path,
    *,
    actor: str,
) -> Path:
    actor = _required_actor(actor)
    output = _validated_output(paths, output, "冲突批次复核工作包")
    batch, source_path, source_pack = _resolved_source(
        database, paths, plan_id, batch_id
    )
    submission, drifted = _candidate_submission_state(database, source_pack)
    phase = _candidate_pack_phase(
        submission, drifted, _candidate_counts(source_pack)
    )
    if phase not in {"reviewing", "reviewed"} or submission is None:
        raise InvalidTransitionError(
            "只有已提交标签的候选包可以导出冲突批次复核工作包"
        )
    if submission["actor"] == actor:
        raise InvalidTransitionError("标注人与复核人必须不同")
    source_content_sha256 = sha256_file(source_path)
    candidates = _ordered_candidates(source_pack, batch["candidate_ids"])
    generated_at = utc_now()
    lines = [
        "---",
        f"type: {_REVIEW_PACK_TYPE}",
        f"plan_id: {plan_id}",
        f"batch_id: {batch_id}",
        f"source_pack_id: {source_pack['pack_id']}",
        f"source_content_sha256: {source_content_sha256}",
        f"annotator: {submission['actor']}",
        f"reviewer: {actor}",
        f"generated_at: {generated_at}",
        "---",
        "",
        f"# 跨文档冲突复核：{batch_id}",
        "",
        f"- 计划：`{plan_id}`",
        f"- 来源候选包：`{source_pack['pack_id']}`",
        f"- 标注人：`{submission['actor']}`",
        f"- 复核人：`{actor}`",
        f"- 本批候选：`{len(candidates)}`",
        "- 复核人必须独立回源；规则预测与标注结论都不能代替来源核对。",
        "- 每个候选必须且只能勾选“批准人工标签”或“驳回人工标签”。",
        "- 驳回必须填写复核意见；工作包按来源候选包 SHA-256 锁定。",
        "",
        "## 应用本批复核",
        "",
        "完成全部候选后运行：",
        "",
        "```powershell",
        (
            f".\\.venv\\Scripts\\knowledge.exe conflict "
            "batch-review-apply "
            f"{_powershell_literal(str(output))} "
            f"--actor {_powershell_literal(actor)}"
        ),
        "```",
        "",
    ]
    for candidate in candidates:
        label = candidate["label"]
        review = candidate["review"]
        lines.extend(
            [
                f"## `{candidate['candidate_id']}`",
                "",
                (
                    "- [x] 批准人工标签"
                    if review["decision"] == "approved"
                    else "- [ ] 批准人工标签"
                ),
                (
                    "- [x] 驳回人工标签"
                    if review["decision"] == "rejected"
                    else "- [ ] 驳回人工标签"
                ),
                "- 复核意见 JSON："
                + json.dumps(
                    review["note"] or "", ensure_ascii=False
                ),
                (
                    "- 人工标签：`"
                    + (
                        label["expected_type"]
                        if label["expected_conflict"]
                        else "non_conflict"
                    )
                    + "`"
                ),
                f"- 标注依据 JSON：{json.dumps(label['note'] or '', ensure_ascii=False)}",
                f"- 规则预测：`{candidate['predicted_type'] or 'none'}`",
                "",
                *_candidate_side_lines("左侧", candidate["left"]),
                *_candidate_side_lines("右侧", candidate["right"]),
            ]
        )
    content = "\n".join(lines).rstrip() + "\n"
    _write_export(
        database,
        paths,
        output,
        content,
        event_type="conflict_batch_review_pack_exported",
        plan_id=plan_id,
        batch_id=batch_id,
        source_pack_id=source_pack["pack_id"],
        source_content_sha256=source_content_sha256,
        candidate_count=len(candidates),
        actor=actor,
        extra_details={"annotator": submission["actor"]},
    )
    return output


def apply_conflict_batch_review_pack(
    database: Database,
    paths: WorkspacePaths,
    pack_path: Path,
    *,
    actor: str,
) -> dict[str, Any]:
    actor = _required_actor(actor)
    pack_path, content = _read_work_pack(
        paths, pack_path, "冲突批次复核工作包"
    )
    metadata, decisions = _parse_review_pack(content)
    if metadata["reviewer"] != actor:
        raise KnowledgeWorkbenchError(
            "工作包声明的复核人与当前 actor 不一致"
        )
    if metadata["annotator"] == actor:
        raise KnowledgeWorkbenchError("标注人与复核人必须不同")
    batch = resolve_conflict_labeling_batch(
        database,
        paths,
        metadata["plan_id"],
        metadata["batch_id"],
    )
    _validate_work_pack_scope(metadata, batch, decisions)
    _validate_export_audit(
        database,
        paths,
        pack_path,
        metadata,
        actor=actor,
        event_type="conflict_batch_review_pack_exported",
    )
    source_path, source_pack = _candidate_pack_by_id(
        paths, metadata["source_pack_id"]
    )
    submission, _ = _candidate_submission_state(database, source_pack)
    if (
        submission is None
        or submission["actor"] != metadata["annotator"]
    ):
        raise KnowledgeWorkbenchError(
            "来源候选包的已审计标注人与复核工作包不一致"
        )
    _require_source_sha256(source_path, metadata["source_content_sha256"])
    return apply_cross_document_candidate_review_batch(
        database,
        paths,
        metadata["source_pack_id"],
        decisions,
        expected_content_sha256=metadata["source_content_sha256"],
        plan_id=metadata["plan_id"],
        batch_id=metadata["batch_id"],
        work_pack_path=pack_path.relative_to(
            paths.root.resolve()
        ).as_posix(),
        work_pack_sha256=sha256_text(content),
        actor=actor,
    )


def _resolved_source(
    database: Database,
    paths: WorkspacePaths,
    plan_id: str,
    batch_id: str,
) -> tuple[dict[str, Any], Path, dict[str, Any]]:
    batch = resolve_conflict_labeling_batch(
        database, paths, plan_id, batch_id
    )
    source_path, source_pack = _candidate_pack_by_id(
        paths, batch["source_pack_id"]
    )
    return batch, source_path, source_pack


def _ordered_candidates(
    source_pack: dict[str, Any], candidate_ids: list[str]
) -> list[dict[str, Any]]:
    by_id = {
        candidate["candidate_id"]: candidate
        for candidate in source_pack["candidates"]
    }
    return [by_id[candidate_id] for candidate_id in candidate_ids]


def _candidate_side_lines(
    label: str, side: dict[str, Any]
) -> list[str]:
    return [
        f"### {label}",
        "",
        f"- 资料：`{_markdown_code(side['document_name'])}`",
        f"- 证据：`{side['evidence_id']}`",
        f"- 密级：`{side['classification']}`",
        "- 定位 JSON："
        + json.dumps(side["locators"], ensure_ascii=False),
        "",
        *_blockquote(side["excerpt"]),
        "",
    ]


def _parse_annotation_pack(
    content: str,
) -> tuple[dict[str, str], list[dict[str, Any]]]:
    metadata, lines, start = _parse_frontmatter(
        content, _ANNOTATION_PACK_TYPE
    )
    _require_metadata(
        metadata,
        (
            "plan_id",
            "batch_id",
            "source_pack_id",
            "source_content_sha256",
            "annotator",
        ),
    )
    records = _parse_candidate_sections(
        lines,
        start,
        approved_label="冲突",
        rejected_label="非冲突",
        type_label="冲突类型",
        note_label="标注依据 JSON",
    )
    labels = []
    for candidate_id, record in records:
        checked_count = int(record["approved"]) + int(
            record["rejected"]
        )
        if checked_count != 1:
            raise KnowledgeWorkbenchError(
                f"候选 {candidate_id} 必须且只能选择冲突或非冲突"
            )
        expected_conflict = bool(record["approved"])
        expected_type = record["type"]
        if expected_conflict and expected_type not in {
            "polarity_change",
            "value_change",
        }:
            raise KnowledgeWorkbenchError(
                f"候选 {candidate_id} 标记冲突时必须填写有效类型"
            )
        if not expected_conflict and expected_type != "null":
            raise KnowledgeWorkbenchError(
                f"候选 {candidate_id} 标记非冲突时类型必须为 null"
            )
        labels.append(
            {
                "candidate_id": candidate_id,
                "expected_conflict": expected_conflict,
                "expected_type": (
                    expected_type if expected_conflict else None
                ),
                "note": record["note"] or None,
            }
        )
    return metadata, labels


def _parse_review_pack(
    content: str,
) -> tuple[dict[str, str], list[dict[str, Any]]]:
    metadata, lines, start = _parse_frontmatter(
        content, _REVIEW_PACK_TYPE
    )
    _require_metadata(
        metadata,
        (
            "plan_id",
            "batch_id",
            "source_pack_id",
            "source_content_sha256",
            "annotator",
            "reviewer",
        ),
    )
    records = _parse_candidate_sections(
        lines,
        start,
        approved_label="批准人工标签",
        rejected_label="驳回人工标签",
        type_label=None,
        note_label="复核意见 JSON",
    )
    decisions = []
    for candidate_id, record in records:
        checked_count = int(record["approved"]) + int(
            record["rejected"]
        )
        if checked_count != 1:
            raise KnowledgeWorkbenchError(
                f"候选 {candidate_id} 必须且只能选择一个复核决定"
            )
        decision = "approved" if record["approved"] else "rejected"
        if decision == "rejected" and not record["note"].strip():
            raise KnowledgeWorkbenchError(
                f"候选 {candidate_id} 驳回时必须填写复核意见"
            )
        decisions.append(
            {
                "candidate_id": candidate_id,
                "decision": decision,
                "note": record["note"] or None,
            }
        )
    return metadata, decisions


def _parse_frontmatter(
    content: str, expected_type: str
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
            "工作包的 YAML Frontmatter 未闭合"
        ) from exc
    metadata = {}
    for line in lines[1:end]:
        if ":" in line:
            key, value = line.split(":", 1)
            metadata[key.strip()] = value.strip()
    if metadata.get("type") != expected_type:
        raise KnowledgeWorkbenchError("文件不是预期的系统工作包")
    return metadata, lines, end + 1


def _parse_candidate_sections(
    lines: list[str],
    start: int,
    *,
    approved_label: str,
    rejected_label: str,
    type_label: str | None,
    note_label: str,
) -> list[tuple[str, dict[str, Any]]]:
    candidate_pattern = re.compile(r"^##\s+`(xdoc_[a-f0-9]{20})`\s*$")
    approved_pattern = re.compile(
        rf"^-\s+\[([ xX])\]\s+{re.escape(approved_label)}\s*$"
    )
    rejected_pattern = re.compile(
        rf"^-\s+\[([ xX])\]\s+{re.escape(rejected_label)}\s*$"
    )
    type_pattern = (
        re.compile(
            rf"^-\s+{re.escape(type_label)}：`([^`]+)`\s*$"
        )
        if type_label
        else None
    )
    note_pattern = re.compile(
        rf"^-\s+{re.escape(note_label)}：(.*)$"
    )
    records: list[tuple[str, dict[str, Any]]] = []
    by_id: set[str] = set()
    current: dict[str, Any] | None = None
    current_id: str | None = None
    for line in lines[start:]:
        candidate_match = candidate_pattern.match(line)
        if candidate_match:
            current_id = candidate_match.group(1)
            if current_id in by_id:
                raise KnowledgeWorkbenchError(
                    f"工作包包含重复候选：{current_id}"
                )
            by_id.add(current_id)
            current = {
                "approved": False,
                "rejected": False,
                "type": "null",
                "note": "",
                "approved_seen": False,
                "rejected_seen": False,
                "type_seen": False,
                "note_seen": False,
            }
            records.append((current_id, current))
            continue
        if current is None:
            continue
        approved_match = approved_pattern.match(line)
        if approved_match:
            if current["approved_seen"]:
                raise KnowledgeWorkbenchError(
                    f"候选 {current_id} 包含重复的 {approved_label} 选项"
                )
            current["approved_seen"] = True
            current["approved"] = (
                approved_match.group(1).lower() == "x"
            )
            continue
        rejected_match = rejected_pattern.match(line)
        if rejected_match:
            if current["rejected_seen"]:
                raise KnowledgeWorkbenchError(
                    f"候选 {current_id} 包含重复的 {rejected_label} 选项"
                )
            current["rejected_seen"] = True
            current["rejected"] = (
                rejected_match.group(1).lower() == "x"
            )
            continue
        if type_pattern and (type_match := type_pattern.match(line)):
            if current["type_seen"]:
                raise KnowledgeWorkbenchError(
                    f"候选 {current_id} 包含重复的 {type_label}"
                )
            current["type_seen"] = True
            current["type"] = type_match.group(1).strip()
            continue
        note_match = note_pattern.match(line)
        if note_match:
            if current["note_seen"]:
                raise KnowledgeWorkbenchError(
                    f"候选 {current_id} 包含重复的 {note_label}"
                )
            current["note_seen"] = True
            try:
                note = json.loads(note_match.group(1).strip())
            except json.JSONDecodeError as exc:
                raise KnowledgeWorkbenchError(
                    f"候选 {current_id} 的意见不是有效 JSON 字符串"
                ) from exc
            if not isinstance(note, str):
                raise KnowledgeWorkbenchError(
                    f"候选 {current_id} 的意见必须是 JSON 字符串"
                )
            if len(note) > 2000:
                raise KnowledgeWorkbenchError(
                    f"候选 {current_id} 的意见不能超过 2000 个字符"
                )
            current["note"] = note.strip()
    if not records:
        raise KnowledgeWorkbenchError("工作包不包含候选")
    for candidate_id, record in records:
        required_fields = {
            approved_label: record["approved_seen"],
            rejected_label: record["rejected_seen"],
            note_label: record["note_seen"],
        }
        if type_label:
            required_fields[type_label] = record["type_seen"]
        missing = [
            label for label, present in required_fields.items() if not present
        ]
        if missing:
            raise KnowledgeWorkbenchError(
                f"候选 {candidate_id} 缺少字段：" + "、".join(missing)
            )
    return records


def _validate_work_pack_scope(
    metadata: dict[str, str],
    batch: dict[str, Any],
    records: list[dict[str, Any]],
) -> None:
    if metadata["source_pack_id"] != batch["source_pack_id"]:
        raise KnowledgeWorkbenchError(
            "工作包来源候选包与批次计划不一致"
        )
    candidate_ids = [item["candidate_id"] for item in records]
    if candidate_ids != batch["candidate_ids"]:
        raise KnowledgeWorkbenchError(
            "工作包候选顺序或范围与经审计批次计划不一致"
        )


def _validate_export_audit(
    database: Database,
    paths: WorkspacePaths,
    pack_path: Path,
    metadata: dict[str, str],
    *,
    actor: str,
    event_type: str,
) -> None:
    relative_path = pack_path.relative_to(
        paths.root.resolve()
    ).as_posix()
    entity_id = _batch_entity_id(
        metadata["plan_id"], metadata["batch_id"]
    )
    with database.connect() as connection:
        rows = connection.execute(
            """
            SELECT details_json FROM audit_log
            WHERE event_type = ?
              AND entity_type = 'conflict_labeling_batch'
              AND entity_id = ?
              AND actor = ?
            """,
            (event_type, entity_id, actor),
        ).fetchall()
    for row in rows:
        try:
            details = json.loads(row["details_json"])
        except json.JSONDecodeError:
            continue
        if (
            details.get("output") == relative_path
            and details.get("source_pack_id")
            == metadata["source_pack_id"]
            and details.get("source_content_sha256")
            == metadata["source_content_sha256"]
        ):
            return
    raise KnowledgeWorkbenchError(
        "工作包缺少匹配的系统导出审计记录"
    )


def _write_export(
    database: Database,
    paths: WorkspacePaths,
    output: Path,
    content: str,
    *,
    event_type: str,
    plan_id: str,
    batch_id: str,
    source_pack_id: str,
    source_content_sha256: str,
    candidate_count: int,
    actor: str,
    extra_details: dict[str, Any] | None = None,
) -> None:
    write_text_atomic(output, content)
    try:
        with database.transaction() as connection:
            details = {
                "output": output.relative_to(
                    paths.root.resolve()
                ).as_posix(),
                "content_sha256": sha256_text(content),
                "plan_id": plan_id,
                "batch_id": batch_id,
                "source_pack_id": source_pack_id,
                "source_content_sha256": source_content_sha256,
                "candidate_count": candidate_count,
            }
            details.update(extra_details or {})
            record_event(
                connection,
                event_type,
                "conflict_labeling_batch",
                _batch_entity_id(plan_id, batch_id),
                actor=actor,
                details=details,
            )
    except Exception:
        output.unlink(missing_ok=True)
        raise


def _batch_entity_id(plan_id: str, batch_id: str) -> str:
    return f"{plan_id}:{batch_id}"


def _require_metadata(
    metadata: dict[str, str], keys: tuple[str, ...]
) -> None:
    missing = [key for key in keys if not metadata.get(key)]
    if missing:
        raise KnowledgeWorkbenchError(
            "工作包缺少元数据：" + ", ".join(missing)
        )
    value = metadata.get("source_content_sha256", "")
    if not re.fullmatch(r"[a-f0-9]{64}", value):
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
    paths: WorkspacePaths, pack_path: Path, label: str
) -> tuple[Path, str]:
    pack_path = pack_path.expanduser().resolve()
    if pack_path.suffix.lower() != ".md":
        raise KnowledgeWorkbenchError(f"{label}必须使用 .md 文件")
    try:
        pack_path.relative_to(paths.evaluations.resolve())
    except ValueError as exc:
        raise KnowledgeWorkbenchError(
            f"{label}只能从当前 workspace/evaluations 内读取"
        ) from exc
    try:
        return pack_path, pack_path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise KnowledgeWorkbenchError(f"{label}不存在：{pack_path}") from exc
    except (OSError, UnicodeError) as exc:
        raise KnowledgeWorkbenchError(f"{label}无法安全读取") from exc


def _require_source_sha256(source_path: Path, expected: str) -> None:
    if sha256_file(source_path) != expected:
        raise KnowledgeWorkbenchError(
            "来源候选包已被其他审核操作修改；请重新导出当前批次工作包"
        )


def _required_actor(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise KnowledgeWorkbenchError("actor 不能为空")
    actor = value.strip()
    if len(actor) > 80:
        raise KnowledgeWorkbenchError("actor 不能超过 80 个字符")
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
