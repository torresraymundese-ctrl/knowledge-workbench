from __future__ import annotations

import json
import os
import re
from pathlib import Path

from .audit import record_event
from .config import WorkspacePaths
from .database import Database
from .errors import InvalidTransitionError, KnowledgeWorkbenchError
from .schema_validation import validate_evaluation_dataset
from .utils import new_id, sha256_file, sha256_text, utc_now
from .wiki import write_text_atomic


def create_labeling_session(
    database: Database,
    template_path: Path,
    *,
    actor: str,
    name: str | None = None,
    minimum_required_per_case: int = 3,
) -> str:
    actor = _required_actor(actor)
    if minimum_required_per_case < 1:
        raise KnowledgeWorkbenchError("每个用例的最少必要证据数必须至少为 1")
    template_path = template_path.expanduser().resolve()
    template = _load_template(template_path)
    session_id = new_id("labels")
    now = utc_now()
    prepared_cases: list[tuple] = []
    with database.connect() as connection:
        for case in template["cases"]:
            source = (template_path.parent / case["source_path"]).resolve()
            if not source.is_file():
                raise KnowledgeWorkbenchError(
                    f"标注用例 {case['case_id']} 的文件不存在：{source}"
                )
            digest = sha256_file(source)
            current = connection.execute(
                """
                SELECT dv.id AS document_version_id, d.classification
                FROM document_versions dv
                JOIN documents d
                  ON d.id = dv.document_id AND d.current_version_id = dv.id
                JOIN processing_runs pr
                  ON pr.document_version_id = dv.id AND pr.is_current = 1
                WHERE dv.sha256 = ?
                """,
                (digest,),
            ).fetchone()
            if not current:
                raise KnowledgeWorkbenchError(
                    f"标注用例 {case['case_id']} 尚未导入或没有当前处理运行"
                )
            if current["classification"] != case["classification"]:
                raise KnowledgeWorkbenchError(
                    f"标注用例 {case['case_id']} 的密级与数据库不一致"
                )
            prepared_cases.append(
                (
                    new_id("lcase"),
                    session_id,
                    case["case_id"],
                    str(source),
                    digest,
                    current["document_version_id"],
                    case["classification"],
                    case["max_duplicate_rate"],
                )
            )
    with database.transaction() as connection:
        connection.execute(
            """
            INSERT INTO labeling_sessions(
                id, name, template_path, status, minimum_required_per_case,
                created_by, created_at, updated_at
            ) VALUES (?, ?, ?, 'draft', ?, ?, ?, ?)
            """,
            (
                session_id,
                (name or template["name"]).strip(),
                str(template_path),
                minimum_required_per_case,
                actor,
                now,
                now,
            ),
        )
        connection.executemany(
            """
            INSERT INTO labeling_cases(
                id, session_id, case_id, source_path, source_sha256,
                document_version_id, classification, max_duplicate_rate
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            prepared_cases,
        )
        record_event(
            connection,
            "labeling_session_created",
            "labeling_session",
            session_id,
            actor=actor,
            details={
                "case_count": len(prepared_cases),
                "minimum_required_per_case": minimum_required_per_case,
            },
        )
    return session_id


def select_expected_evidence(
    database: Database,
    session_id: str,
    case_id: str,
    evidence_id: str,
    *,
    actor: str,
) -> None:
    select_expected_evidence_batch(
        database,
        session_id,
        case_id,
        [evidence_id],
        actor=actor,
    )


def select_expected_evidence_batch(
    database: Database,
    session_id: str,
    case_id: str,
    evidence_ids: list[str],
    *,
    actor: str,
) -> dict:
    actor = _required_actor(actor)
    unique_ids = list(
        dict.fromkeys(value.strip() for value in evidence_ids if value.strip())
    )
    if not unique_ids:
        raise KnowledgeWorkbenchError("至少需要提供一条 evidence_id")
    if len(unique_ids) > 100:
        raise KnowledgeWorkbenchError("单次最多选择 100 条证据")
    now = utc_now()
    with database.transaction() as connection:
        session, case = _editable_case(connection, session_id, case_id, actor)
        for evidence_id in unique_ids:
            _ensure_evidence_is_current_for_case(connection, case, evidence_id)
        placeholders = ",".join("?" for _ in unique_ids)
        existing = {
            row["evidence_id"]
            for row in connection.execute(
                f"""
                SELECT evidence_id FROM labeling_expected_evidence
                WHERE case_row_id = ? AND evidence_id IN ({placeholders})
                """,
                (case["id"], *unique_ids),
            ).fetchall()
        }
        added = [evidence_id for evidence_id in unique_ids if evidence_id not in existing]
        if added:
            connection.executemany(
                """
                INSERT INTO labeling_expected_evidence(
                    case_row_id, evidence_id, selected_by, created_at
                ) VALUES (?, ?, ?, ?)
                """,
                [
                    (case["id"], evidence_id, actor, now)
                    for evidence_id in added
                ],
            )
            connection.execute(
                "UPDATE labeling_sessions SET updated_at = ? WHERE id = ?",
                (now, session["id"]),
            )
            event_type = (
                "labeling_evidence_selected"
                if len(added) == 1
                else "labeling_evidence_batch_selected"
            )
            details = {"case_id": case_id, "evidence_ids": added}
            if len(added) == 1:
                details["evidence_id"] = added[0]
            record_event(
                connection,
                event_type,
                "labeling_session",
                session_id,
                actor=actor,
                details=details,
            )
    return {
        "requested_count": len(evidence_ids),
        "unique_count": len(unique_ids),
        "added_count": len(added),
        "already_selected_count": len(existing),
    }


def select_expected_evidence_by_ordinals(
    database: Database,
    session_id: str,
    case_id: str,
    ordinals: list[int],
    *,
    actor: str,
) -> dict:
    actor = _required_actor(actor)
    unique_ordinals = list(dict.fromkeys(ordinals))
    if not unique_ordinals:
        raise KnowledgeWorkbenchError("至少需要提供一个候选编号")
    if any(value < 1 for value in unique_ordinals):
        raise KnowledgeWorkbenchError("候选编号必须是大于 0 的整数")
    if len(unique_ordinals) > 100:
        raise KnowledgeWorkbenchError("单次最多选择 100 个候选编号")

    with database.connect() as connection:
        _, case = _editable_case(connection, session_id, case_id, actor)
        _ensure_case_source_current(connection, case)
        placeholders = ",".join("?" for _ in unique_ordinals)
        rows = connection.execute(
            f"""
            SELECT e.id, e.run_ordinal
            FROM evidence e
            JOIN processing_runs pr
              ON pr.id = e.processing_run_id AND pr.is_current = 1
            WHERE e.document_version_id = ?
              AND e.run_ordinal IN ({placeholders})
              AND e.status NOT IN ('conflicted', 'deprecated', 'archived')
            """,
            (case["document_version_id"], *unique_ordinals),
        ).fetchall()
    evidence_by_ordinal = {row["run_ordinal"]: row["id"] for row in rows}
    missing = [value for value in unique_ordinals if value not in evidence_by_ordinal]
    if missing:
        raise KnowledgeWorkbenchError(
            "以下候选编号不属于该用例的当前处理运行："
            + ", ".join(str(value) for value in missing)
        )

    result = select_expected_evidence_batch(
        database,
        session_id,
        case_id,
        [evidence_by_ordinal[value] for value in unique_ordinals],
        actor=actor,
    )
    return {
        **result,
        "requested_ordinal_count": len(ordinals),
        "unique_ordinal_count": len(unique_ordinals),
    }


def remove_expected_evidence(
    database: Database,
    session_id: str,
    case_id: str,
    evidence_id: str,
    *,
    actor: str,
) -> None:
    actor = _required_actor(actor)
    now = utc_now()
    with database.transaction() as connection:
        session, case = _editable_case(connection, session_id, case_id, actor)
        cursor = connection.execute(
            """
            DELETE FROM labeling_expected_evidence
            WHERE case_row_id = ? AND evidence_id = ?
            """,
            (case["id"], evidence_id),
        )
        if not cursor.rowcount:
            raise KnowledgeWorkbenchError("该用例没有选择指定证据")
        connection.execute(
            "UPDATE labeling_sessions SET updated_at = ? WHERE id = ?",
            (now, session["id"]),
        )
        record_event(
            connection,
            "labeling_evidence_removed",
            "labeling_session",
            session_id,
            actor=actor,
            details={"case_id": case_id, "evidence_id": evidence_id},
        )


def add_forbidden_substring(
    database: Database,
    session_id: str,
    case_id: str,
    value: str,
    *,
    actor: str,
) -> None:
    actor = _required_actor(actor)
    value = value.strip()
    if not value:
        raise KnowledgeWorkbenchError("禁止内容不能为空")
    now = utc_now()
    with database.transaction() as connection:
        session, case = _editable_case(connection, session_id, case_id, actor)
        cursor = connection.execute(
            """
            INSERT OR IGNORE INTO labeling_forbidden_substrings(
                id, case_row_id, value, created_by, created_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (new_id("forbid"), case["id"], value, actor, now),
        )
        connection.execute(
            "UPDATE labeling_sessions SET updated_at = ? WHERE id = ?",
            (now, session["id"]),
        )
        if cursor.rowcount:
            record_event(
                connection,
                "labeling_forbidden_added",
                "labeling_session",
                session_id,
                actor=actor,
                details={"case_id": case_id, "value_sha256": sha256_text(value)},
            )


def remove_forbidden_substring(
    database: Database,
    session_id: str,
    case_id: str,
    value: str,
    *,
    actor: str,
) -> None:
    actor = _required_actor(actor)
    value = value.strip()
    now = utc_now()
    with database.transaction() as connection:
        session, case = _editable_case(connection, session_id, case_id, actor)
        cursor = connection.execute(
            """
            DELETE FROM labeling_forbidden_substrings
            WHERE case_row_id = ? AND value = ?
            """,
            (case["id"], value),
        )
        if not cursor.rowcount:
            raise KnowledgeWorkbenchError("该用例没有指定的禁止内容")
        connection.execute(
            "UPDATE labeling_sessions SET updated_at = ? WHERE id = ?",
            (now, session["id"]),
        )
        record_event(
            connection,
            "labeling_forbidden_removed",
            "labeling_session",
            session_id,
            actor=actor,
            details={"case_id": case_id, "value_sha256": sha256_text(value)},
        )


def submit_labeling_session(database: Database, session_id: str, *, actor: str) -> None:
    actor = _required_actor(actor)
    now = utc_now()
    with database.transaction() as connection:
        session = _get_session(connection, session_id)
        if session["status"] != "draft":
            raise InvalidTransitionError("只有 draft 标注集可以提交复核")
        if session["created_by"] != actor:
            raise InvalidTransitionError("只有标注集创建者可以提交复核")
        _ensure_session_ready(connection, session)
        connection.execute(
            """
            UPDATE labeling_sessions
            SET status = 'reviewing', submitted_by = ?, approved_by = NULL, updated_at = ?
            WHERE id = ?
            """,
            (actor, now, session_id),
        )
        record_event(
            connection,
            "labeling_session_submitted",
            "labeling_session",
            session_id,
            actor=actor,
        )


def review_labeling_case(
    database: Database,
    session_id: str,
    case_id: str,
    decision: str,
    *,
    actor: str,
    note: str | None = None,
) -> None:
    actor = _required_actor(actor)
    decision = decision.strip().lower()
    note = (note or "").strip() or None
    if decision not in {"approved", "rejected"}:
        raise KnowledgeWorkbenchError("复核决定必须是 approved 或 rejected")
    if decision == "rejected" and not note:
        raise KnowledgeWorkbenchError("驳回单个用例必须填写原因")
    now = utc_now()
    with database.transaction() as connection:
        session = _get_session(connection, session_id)
        if session["status"] != "reviewing":
            raise InvalidTransitionError("只有 reviewing 标注集可以逐项复核")
        if session["submitted_by"] == actor:
            raise InvalidTransitionError("复核人必须与提交人不同")
        case = connection.execute(
            "SELECT * FROM labeling_cases WHERE session_id = ? AND case_id = ?",
            (session_id, case_id),
        ).fetchone()
        if not case:
            raise KnowledgeWorkbenchError(f"标注用例不存在：{case_id}")
        _ensure_case_ready(connection, session, case)
        connection.execute(
            """
            INSERT INTO labeling_case_reviews(
                case_row_id, reviewer, decision, note, reviewed_at
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(case_row_id) DO UPDATE SET
                reviewer = excluded.reviewer,
                decision = excluded.decision,
                note = excluded.note,
                reviewed_at = excluded.reviewed_at
            """,
            (case["id"], actor, decision, note, now),
        )
        connection.execute(
            "UPDATE labeling_sessions SET updated_at = ? WHERE id = ?",
            (now, session_id),
        )
        details = {"case_id": case_id, "decision": decision}
        if note:
            details["note_sha256"] = sha256_text(note)
        record_event(
            connection,
            "labeling_case_reviewed",
            "labeling_session",
            session_id,
            actor=actor,
            details=details,
        )


def approve_labeling_session(database: Database, session_id: str, *, actor: str) -> None:
    actor = _required_actor(actor)
    now = utc_now()
    with database.transaction() as connection:
        session = _get_session(connection, session_id)
        if session["status"] != "reviewing":
            raise InvalidTransitionError("只有 reviewing 标注集可以批准")
        if session["submitted_by"] == actor:
            raise InvalidTransitionError("批准人必须与提交人不同")
        _ensure_session_ready(connection, session)
        _ensure_case_reviews(connection, session, actor)
        connection.execute(
            """
            UPDATE labeling_sessions
            SET status = 'approved', approved_by = ?, updated_at = ?
            WHERE id = ?
            """,
            (actor, now, session_id),
        )
        record_event(
            connection,
            "labeling_session_approved",
            "labeling_session",
            session_id,
            actor=actor,
        )


def reject_labeling_session(
    database: Database,
    session_id: str,
    *,
    actor: str,
    note: str,
) -> None:
    actor = _required_actor(actor)
    note = note.strip()
    if not note:
        raise KnowledgeWorkbenchError("驳回标注集必须填写原因")
    now = utc_now()
    with database.transaction() as connection:
        session = _get_session(connection, session_id)
        if session["status"] != "reviewing":
            raise InvalidTransitionError("只有 reviewing 标注集可以驳回")
        if session["submitted_by"] == actor:
            raise InvalidTransitionError("复核人必须与提交人不同")
        connection.execute(
            """
            DELETE FROM labeling_case_reviews
            WHERE case_row_id IN (
                SELECT id FROM labeling_cases WHERE session_id = ?
            )
            """,
            (session_id,),
        )
        connection.execute(
            """
            UPDATE labeling_sessions
            SET status = 'draft', submitted_by = NULL, approved_by = NULL, updated_at = ?
            WHERE id = ?
            """,
            (now, session_id),
        )
        record_event(
            connection,
            "labeling_session_rejected",
            "labeling_session",
            session_id,
            actor=actor,
            details={"note": note},
        )


def export_labeling_annotation_pack(
    database: Database,
    paths: WorkspacePaths,
    session_id: str,
    output: Path,
    *,
    actor: str,
    limit_per_case: int = 0,
) -> Path:
    actor = _required_actor(actor)
    if limit_per_case < 0 or limit_per_case > 5000:
        raise KnowledgeWorkbenchError("每个用例的候选上限必须在 0 到 5000 之间")
    output = output.expanduser().resolve()
    if output.suffix.lower() != ".md":
        raise KnowledgeWorkbenchError("标注工作包必须使用 .md 文件")
    if output.exists():
        raise KnowledgeWorkbenchError(f"标注工作包已存在，不允许静默覆盖：{output}")

    with database.connect() as connection:
        session = _get_session(connection, session_id)
        if session["status"] != "draft":
            raise InvalidTransitionError("只有 draft 标注集可以生成标注工作包")
        if session["created_by"] != actor:
            raise InvalidTransitionError("只有标注集创建人可以生成标注工作包")
        cases = connection.execute(
            "SELECT * FROM labeling_cases WHERE session_id = ? ORDER BY case_id",
            (session_id,),
        ).fetchall()
        _ensure_labeling_output_allowed(paths, cases, output, "标注工作包")
        generated_at = utc_now()
        powershell_output = "'" + str(output).replace("'", "''") + "'"
        lines = [
            "---",
            "type: labeling-annotation-pack",
            f"session_id: {session_id}",
            "status: draft",
            f"generated_at: {generated_at}",
            "---",
            "",
            f"# {session['name']}：标注工作包",
            "",
            f"- 标注人：`{actor}`",
            f"- 每个用例最低证据数：`{session['minimum_required_per_case']}`",
            "- 本文件只用于本地人工定位；勾选 Markdown 不会自动修改数据库。",
            "- 选择前必须回到来源文件核对原文、上下文和适用范围。",
            "",
            "## 应用已勾选项",
            "",
            "保存本文件后运行以下命令。该操作只增量添加勾选项，不会删除未勾选项。",
            "",
            "```powershell",
            (
                f'.\\.venv\\Scripts\\knowledge.exe --workspace .\\workspace '
                f"label apply-annotation-pack {powershell_output} --actor {actor}"
            ),
            "```",
            "",
        ]
        candidate_count = 0
        truncated_case_count = 0
        for case in cases:
            _ensure_case_source_current(connection, case)
            total = connection.execute(
                """
                SELECT COUNT(*)
                FROM evidence e
                JOIN processing_runs pr
                  ON pr.id = e.processing_run_id AND pr.is_current = 1
                WHERE e.document_version_id = ?
                  AND e.status NOT IN ('conflicted', 'deprecated', 'archived')
                """,
                (case["document_version_id"],),
            ).fetchone()[0]
            limit_clause = " LIMIT ?" if limit_per_case else ""
            parameters: tuple = (case["id"], case["document_version_id"])
            if limit_per_case:
                parameters = (*parameters, limit_per_case)
            evidence = connection.execute(
                f"""
                SELECT e.id, e.run_ordinal, e.excerpt, e.locator_json, e.status,
                       pr.parser_name, pr.parser_version,
                       CASE WHEN lee.evidence_id IS NULL THEN 0 ELSE 1 END AS selected
                FROM evidence e
                JOIN processing_runs pr
                  ON pr.id = e.processing_run_id AND pr.is_current = 1
                LEFT JOIN labeling_expected_evidence lee
                  ON lee.case_row_id = ? AND lee.evidence_id = e.id
                WHERE e.document_version_id = ?
                  AND e.status NOT IN ('conflicted', 'deprecated', 'archived')
                ORDER BY e.run_ordinal, e.id{limit_clause}
                """,
                parameters,
            ).fetchall()
            candidate_count += len(evidence)
            if len(evidence) < total:
                truncated_case_count += 1
            lines.extend(
                [
                    f"## {case['case_id']}",
                    "",
                    f"- 密级：`{case['classification']}`",
                    f"- 来源：`{_markdown_code(case['source_path'])}`",
                    f"- 来源 SHA-256：`{case['source_sha256']}`",
                    f"- 当前候选：显示 `{len(evidence)}` / 共 `{total}` 条",
                    "",
                ]
            )
            for row in evidence:
                marker = "x" if row["selected"] else " "
                lines.extend(
                    [
                        f"### #{row['run_ordinal']} `{row['id']}`",
                        "",
                        f"- [{marker}] 选择此证据",
                        f"- 状态：`{row['status']}`",
                        f"- 定位：`{_markdown_code(row['locator_json'])}`",
                        f"- 解析器：`{row['parser_name']}:{row['parser_version']}`",
                        "",
                        *_blockquote(row["excerpt"]),
                        "",
                    ]
                )
            lines.extend(
                [
                    "### 写入本用例选择",
                    "",
                    "```powershell",
                    "$Ordinals = @() # 核对原文后填写候选 #编号",
                    (
                        f"if ($Ordinals.Count -lt {session['minimum_required_per_case']}) "
                        f'{{ throw "请至少填写{session["minimum_required_per_case"]}个候选编号" }}'
                    ),
                    (
                        f'.\\.venv\\Scripts\\knowledge.exe --workspace .\\workspace '
                        f"label add-ordinals {session_id} {case['case_id']} "
                        f"$Ordinals --actor {actor}"
                    ),
                    "```",
                    "",
                ]
            )

    content = "\n".join(lines).rstrip() + "\n"
    write_text_atomic(output, content)
    try:
        with database.transaction() as connection:
            record_event(
                connection,
                "labeling_annotation_pack_exported",
                "labeling_session",
                session_id,
                actor=actor,
                details={
                    "output_path": str(output),
                    "content_sha256": sha256_text(content),
                    "case_count": len(cases),
                    "candidate_count": candidate_count,
                    "limit_per_case": limit_per_case,
                    "truncated_case_count": truncated_case_count,
                },
            )
    except Exception:
        output.unlink(missing_ok=True)
        raise
    return output


def apply_labeling_annotation_pack(
    database: Database,
    paths: WorkspacePaths,
    pack_path: Path,
    *,
    actor: str,
) -> dict:
    actor = _required_actor(actor)
    pack_path = pack_path.expanduser().resolve()
    if pack_path.suffix.lower() != ".md":
        raise KnowledgeWorkbenchError("标注工作包必须使用 .md 文件")
    try:
        pack_path.relative_to(paths.evaluations)
    except ValueError as exc:
        raise KnowledgeWorkbenchError(
            "标注工作包只能从当前 workspace/evaluations 读取"
        ) from exc
    try:
        content = pack_path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise KnowledgeWorkbenchError(f"标注工作包不存在：{pack_path}") from exc
    session_id, checked = _parse_labeling_annotation_pack(content)
    if not checked:
        raise KnowledgeWorkbenchError("标注工作包中没有已勾选的证据")

    now = utc_now()
    with database.transaction() as connection:
        session = _get_session(connection, session_id)
        if session["status"] != "draft":
            raise InvalidTransitionError("只有 draft 标注集可以应用标注工作包")
        if session["created_by"] != actor:
            raise InvalidTransitionError("只有标注集创建人可以应用标注工作包")
        provenance_rows = connection.execute(
            """
            SELECT details_json FROM audit_log
            WHERE entity_id = ? AND event_type = ? AND actor = ?
            """,
            (session_id, "labeling_annotation_pack_exported", actor),
        ).fetchall()
        if not any(
            json.loads(row["details_json"]).get("output_path") == str(pack_path)
            for row in provenance_rows
        ):
            raise KnowledgeWorkbenchError("标注工作包没有匹配的系统导出审计记录")

        cases = {
            row["case_id"]: row
            for row in connection.execute(
                "SELECT * FROM labeling_cases WHERE session_id = ?",
                (session_id,),
            ).fetchall()
        }
        validated: dict[str, list[str]] = {}
        for case_id, items in checked.items():
            case = cases.get(case_id)
            if not case:
                raise KnowledgeWorkbenchError(f"标注工作包包含未知用例：{case_id}")
            _ensure_case_source_current(connection, case)
            evidence_ids = []
            for ordinal, evidence_id in items:
                evidence = connection.execute(
                    """
                    SELECT e.id
                    FROM evidence e
                    JOIN processing_runs pr
                      ON pr.id = e.processing_run_id AND pr.is_current = 1
                    WHERE e.id = ? AND e.run_ordinal = ?
                      AND e.document_version_id = ?
                      AND e.status NOT IN ('conflicted', 'deprecated', 'archived')
                    """,
                    (evidence_id, ordinal, case["document_version_id"]),
                ).fetchone()
                if not evidence:
                    raise KnowledgeWorkbenchError(
                        f"用例 {case_id} 的候选 #{ordinal} 与当前证据不匹配："
                        f"{evidence_id}"
                    )
                evidence_ids.append(evidence_id)
            validated[case_id] = evidence_ids

        added_count = 0
        already_selected_count = 0
        selected_details: dict[str, list[str]] = {}
        for case_id, evidence_ids in validated.items():
            case = cases[case_id]
            placeholders = ",".join("?" for _ in evidence_ids)
            existing = {
                row["evidence_id"]
                for row in connection.execute(
                    f"""
                    SELECT evidence_id FROM labeling_expected_evidence
                    WHERE case_row_id = ? AND evidence_id IN ({placeholders})
                    """,
                    (case["id"], *evidence_ids),
                ).fetchall()
            }
            added = [value for value in evidence_ids if value not in existing]
            if added:
                connection.executemany(
                    """
                    INSERT INTO labeling_expected_evidence(
                        case_row_id, evidence_id, selected_by, created_at
                    ) VALUES (?, ?, ?, ?)
                    """,
                    [(case["id"], value, actor, now) for value in added],
                )
            added_count += len(added)
            already_selected_count += len(existing)
            selected_details[case_id] = evidence_ids
        if added_count:
            connection.execute(
                "UPDATE labeling_sessions SET updated_at = ? WHERE id = ?",
                (now, session_id),
            )
        record_event(
            connection,
            "labeling_annotation_pack_applied",
            "labeling_session",
            session_id,
            actor=actor,
            details={
                "pack_path": str(pack_path),
                "content_sha256": sha256_text(content),
                "case_count": len(validated),
                "checked_count": sum(len(values) for values in validated.values()),
                "added_count": added_count,
                "already_selected_count": already_selected_count,
                "evidence_ids_by_case": selected_details,
                "mode": "additive",
            },
        )
    return {
        "session_id": session_id,
        "case_count": len(validated),
        "checked_count": sum(len(values) for values in validated.values()),
        "added_count": added_count,
        "already_selected_count": already_selected_count,
    }


def export_labeling_review_pack(
    database: Database,
    paths: WorkspacePaths,
    session_id: str,
    output: Path,
    *,
    actor: str,
) -> Path:
    actor = _required_actor(actor)
    output = output.expanduser().resolve()
    if output.suffix.lower() != ".md":
        raise KnowledgeWorkbenchError("复核包必须使用 .md 文件")
    if output.exists():
        raise KnowledgeWorkbenchError(f"复核包已存在，不允许静默覆盖：{output}")
    with database.connect() as connection:
        session = _get_session(connection, session_id)
        if session["status"] != "reviewing":
            raise InvalidTransitionError("只有 reviewing 标注集可以生成复核包")
        if session["submitted_by"] == actor:
            raise InvalidTransitionError("复核包执行人必须与提交人不同")
        _ensure_session_ready(connection, session)
        cases = connection.execute(
            "SELECT * FROM labeling_cases WHERE session_id = ? ORDER BY case_id",
            (session_id,),
        ).fetchall()
        _ensure_labeling_output_allowed(paths, cases, output, "复核包")
        generated_at = utc_now()
        powershell_output = "'" + str(output).replace("'", "''") + "'"
        lines = [
            "---",
            "type: labeling-review-pack",
            f"session_id: {session_id}",
            f"reviewer: {actor}",
            f"generated_at: {generated_at}",
            "---",
            "",
            f"# {session['name']}：复核包",
            "",
            f"- 会话ID：`{session_id}`",
            f"- 提交人：`{session['submitted_by']}`",
            f"- 复核人：`{actor}`",
            f"- 生成时间：`{generated_at}`",
            "- 说明：本文件只用于人工回源复核，不代表已经批准。",
            "",
            "## 应用逐项复核",
            "",
            "每个用例必须且只能选择一个决定；驳回时必须填写原因。保存后运行：",
            "",
            "```powershell",
            (
                f'.\\.venv\\Scripts\\knowledge.exe --workspace .\\workspace '
                f"label apply-review-pack {powershell_output} --actor {actor}"
            ),
            "```",
            "",
        ]
        evidence_count = 0
        for case in cases:
            evidence = connection.execute(
                """
                SELECT e.id, e.run_ordinal, e.excerpt, e.locator_json, e.status,
                       lee.selected_by, lee.created_at
                FROM labeling_expected_evidence lee
                JOIN evidence e ON e.id = lee.evidence_id
                WHERE lee.case_row_id = ?
                ORDER BY e.run_ordinal, e.id
                """,
                (case["id"],),
            ).fetchall()
            forbidden = connection.execute(
                """
                SELECT value FROM labeling_forbidden_substrings
                WHERE case_row_id = ? ORDER BY created_at, id
                """,
                (case["id"],),
            ).fetchall()
            evidence_count += len(evidence)
            lines.extend(
                [
                    f"## {case['case_id']}",
                    "",
                    f"- 密级：`{case['classification']}`",
                    f"- 来源：`{_markdown_code(case['source_path'])}`",
                    f"- 来源SHA-256：`{case['source_sha256']}`",
                    f"- 文件版本：`{case['document_version_id']}`",
                    "",
                ]
            )
            for row in evidence:
                lines.extend(
                    [
                        f"### #{row['run_ordinal']} `{row['id']}`",
                        "",
                        f"- 状态：`{row['status']}`",
                        f"- 定位：`{_markdown_code(row['locator_json'])}`",
                        f"- 选择人：`{row['selected_by']}`",
                        "",
                        *_blockquote(row["excerpt"]),
                        "",
                    ]
                )
            if forbidden:
                lines.extend(["### 禁止内容", ""])
                for row in forbidden:
                    lines.extend([*_blockquote(row["value"]), ""])
            lines.extend(
                [
                    "### 复核决定",
                    "",
                    "- [ ] 批准本用例",
                    "- [ ] 驳回本用例",
                    "- 驳回原因：",
                    "",
                ]
            )
    content = "\n".join(lines).rstrip() + "\n"
    write_text_atomic(output, content)
    try:
        with database.transaction() as connection:
            record_event(
                connection,
                "labeling_review_pack_exported",
                "labeling_session",
                session_id,
                actor=actor,
                details={
                    "output_path": str(output),
                    "content_sha256": sha256_text(content),
                    "case_count": len(cases),
                    "evidence_count": evidence_count,
                },
            )
    except Exception:
        output.unlink(missing_ok=True)
        raise
    return output


def apply_labeling_review_pack(
    database: Database,
    paths: WorkspacePaths,
    pack_path: Path,
    *,
    actor: str,
) -> dict:
    actor = _required_actor(actor)
    pack_path = pack_path.expanduser().resolve()
    if pack_path.suffix.lower() != ".md":
        raise KnowledgeWorkbenchError("复核包必须使用 .md 文件")
    try:
        pack_path.relative_to(paths.evaluations)
    except ValueError as exc:
        raise KnowledgeWorkbenchError("复核包只能从当前 workspace/evaluations 读取") from exc
    try:
        content = pack_path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise KnowledgeWorkbenchError(f"复核包不存在：{pack_path}") from exc
    session_id, declared_reviewer, decisions = _parse_labeling_review_pack(content)
    if declared_reviewer != actor:
        raise KnowledgeWorkbenchError("复核包声明的复核人与当前 actor 不一致")

    now = utc_now()
    with database.transaction() as connection:
        session = _get_session(connection, session_id)
        if session["status"] != "reviewing":
            raise InvalidTransitionError("只有 reviewing 标注集可以应用复核包")
        if session["submitted_by"] == actor:
            raise InvalidTransitionError("复核人必须与提交人不同")
        provenance_rows = connection.execute(
            """
            SELECT details_json FROM audit_log
            WHERE entity_id = ? AND event_type = ? AND actor = ?
            """,
            (session_id, "labeling_review_pack_exported", actor),
        ).fetchall()
        if not any(
            json.loads(row["details_json"]).get("output_path") == str(pack_path)
            for row in provenance_rows
        ):
            raise KnowledgeWorkbenchError("复核包没有匹配的系统导出审计记录")

        cases = {
            row["case_id"]: row
            for row in connection.execute(
                "SELECT * FROM labeling_cases WHERE session_id = ?",
                (session_id,),
            ).fetchall()
        }
        missing_cases = sorted(set(cases) - set(decisions))
        unknown_cases = sorted(set(decisions) - set(cases))
        if missing_cases:
            raise KnowledgeWorkbenchError(
                "以下用例尚未填写复核决定：" + ", ".join(missing_cases)
            )
        if unknown_cases:
            raise KnowledgeWorkbenchError(
                "复核包包含未知用例：" + ", ".join(unknown_cases)
            )

        validated = {}
        for case_id, case in cases.items():
            _ensure_case_ready(connection, session, case)
            decision = decisions[case_id]
            checked_count = int(decision["approved"]) + int(decision["rejected"])
            if checked_count != 1:
                raise KnowledgeWorkbenchError(
                    f"用例 {case_id} 必须且只能选择一个复核决定"
                )
            resolved = "approved" if decision["approved"] else "rejected"
            note = decision["note"].strip() or None
            if resolved == "rejected" and not note:
                raise KnowledgeWorkbenchError(f"用例 {case_id} 驳回时必须填写原因")
            validated[case_id] = (resolved, note)

        for case_id, (decision, note) in validated.items():
            connection.execute(
                """
                INSERT INTO labeling_case_reviews(
                    case_row_id, reviewer, decision, note, reviewed_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(case_row_id) DO UPDATE SET
                    reviewer = excluded.reviewer,
                    decision = excluded.decision,
                    note = excluded.note,
                    reviewed_at = excluded.reviewed_at
                """,
                (cases[case_id]["id"], actor, decision, note, now),
            )
        connection.execute(
            "UPDATE labeling_sessions SET updated_at = ? WHERE id = ?",
            (now, session_id),
        )
        record_event(
            connection,
            "labeling_review_pack_applied",
            "labeling_session",
            session_id,
            actor=actor,
            details={
                "pack_path": str(pack_path),
                "content_sha256": sha256_text(content),
                "case_count": len(validated),
                "decisions_by_case": {
                    case_id: decision for case_id, (decision, _) in validated.items()
                },
                "note_sha256_by_case": {
                    case_id: sha256_text(note)
                    for case_id, (_, note) in validated.items()
                    if note
                },
            },
        )
    return {
        "session_id": session_id,
        "case_count": len(validated),
        "approved_count": sum(
            decision == "approved" for decision, _ in validated.values()
        ),
        "rejected_count": sum(
            decision == "rejected" for decision, _ in validated.values()
        ),
    }


def export_labeling_dataset(
    database: Database,
    paths: WorkspacePaths,
    session_id: str,
    output: Path,
    *,
    actor: str,
) -> Path:
    actor = _required_actor(actor)
    output = output.expanduser().resolve()
    if output.exists():
        raise KnowledgeWorkbenchError(f"导出文件已存在，不允许静默覆盖：{output}")
    with database.connect() as connection:
        session = _get_session(connection, session_id)
        if session["status"] != "approved":
            raise InvalidTransitionError("只有 approved 标注集可以导出")
        _ensure_session_ready(connection, session)
        cases = connection.execute(
            "SELECT * FROM labeling_cases WHERE session_id = ? ORDER BY case_id",
            (session_id,),
        ).fetchall()
        _ensure_labeling_output_allowed(paths, cases, output, "评测集")
        dataset_cases = []
        for case in cases:
            evidence = connection.execute(
                """
                SELECT e.excerpt
                FROM labeling_expected_evidence lee
                JOIN evidence e ON e.id = lee.evidence_id
                WHERE lee.case_row_id = ?
                ORDER BY e.run_ordinal
                """,
                (case["id"],),
            ).fetchall()
            forbidden = connection.execute(
                """
                SELECT value FROM labeling_forbidden_substrings
                WHERE case_row_id = ? ORDER BY created_at, id
                """,
                (case["id"],),
            ).fetchall()
            dataset_cases.append(
                {
                    "case_id": case["case_id"],
                    "source_path": _relative_source_path(
                        Path(case["source_path"]), output.parent
                    ),
                    "classification": case["classification"],
                    "expected_evidence": [
                        {"text": row["excerpt"], "required": True} for row in evidence
                    ],
                    "forbidden_substrings": [row["value"] for row in forbidden],
                    "max_duplicate_rate": case["max_duplicate_rate"],
                }
            )
    dataset = {
        "schema_version": "1.0",
        "name": session["name"],
        "cases": dataset_cases,
    }
    validate_evaluation_dataset(dataset, require_ready=True)
    content = json.dumps(dataset, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    write_text_atomic(output, content)
    try:
        with database.transaction() as connection:
            record_event(
                connection,
                "labeling_dataset_exported",
                "labeling_session",
                session_id,
                actor=actor,
                details={
                    "output_path": str(output),
                    "content_sha256": sha256_text(content),
                    "case_count": len(dataset_cases),
                },
            )
    except Exception:
        output.unlink(missing_ok=True)
        raise
    return output


def labeling_session_summary(database: Database, session_id: str) -> dict:
    with database.connect() as connection:
        session = _get_session(connection, session_id)
        cases = connection.execute(
            """
            SELECT lc.case_id, lc.classification,
                   COUNT(DISTINCT lee.evidence_id) AS selected_evidence_count,
                   GROUP_CONCAT(DISTINCT lee.evidence_id) AS selected_evidence_ids,
                   COUNT(DISTINCT lfs.id) AS forbidden_count,
                   lcr.decision AS review_decision,
                   lcr.reviewer AS reviewer,
                   lcr.reviewed_at AS reviewed_at
            FROM labeling_cases lc
            LEFT JOIN labeling_expected_evidence lee ON lee.case_row_id = lc.id
            LEFT JOIN labeling_forbidden_substrings lfs ON lfs.case_row_id = lc.id
            LEFT JOIN labeling_case_reviews lcr ON lcr.case_row_id = lc.id
            WHERE lc.session_id = ?
            GROUP BY lc.id ORDER BY lc.case_id
            """,
            (session_id,),
        ).fetchall()
    return {"session": dict(session), "cases": [dict(case) for case in cases]}


def list_labeling_candidates(
    database: Database,
    session_id: str,
    case_id: str,
    *,
    limit: int = 20,
    offset: int = 0,
    only_unselected: bool = False,
    only_selected: bool = False,
) -> dict:
    if limit < 1 or limit > 200:
        raise KnowledgeWorkbenchError("候选证据分页大小必须在 1 到 200 之间")
    if offset < 0:
        raise KnowledgeWorkbenchError("候选证据分页偏移不能小于 0")
    if only_unselected and only_selected:
        raise KnowledgeWorkbenchError("不能同时指定 only_unselected 和 only_selected")
    with database.connect() as connection:
        session = _get_session(connection, session_id)
        case = connection.execute(
            "SELECT * FROM labeling_cases WHERE session_id = ? AND case_id = ?",
            (session_id, case_id),
        ).fetchone()
        if not case:
            raise KnowledgeWorkbenchError(f"标注用例不存在：{case_id}")
        _ensure_case_source_current(connection, case)
        if only_unselected:
            selection_filter = "AND lee.evidence_id IS NULL"
        elif only_selected:
            selection_filter = "AND lee.evidence_id IS NOT NULL"
        else:
            selection_filter = ""
        total = connection.execute(
            f"""
            SELECT COUNT(*)
            FROM evidence e
            JOIN processing_runs pr
              ON pr.id = e.processing_run_id AND pr.is_current = 1
            LEFT JOIN labeling_expected_evidence lee
              ON lee.case_row_id = ? AND lee.evidence_id = e.id
            WHERE e.document_version_id = ?
              AND e.status NOT IN ('conflicted', 'deprecated', 'archived')
              {selection_filter}
            """,
            (case["id"], case["document_version_id"]),
        ).fetchone()[0]
        rows = connection.execute(
            f"""
            SELECT e.id, e.run_ordinal, e.excerpt, e.locator_json, e.status,
                   pr.id AS processing_run_id, pr.parser_name, pr.parser_version,
                   CASE WHEN lee.evidence_id IS NULL THEN 0 ELSE 1 END AS selected
            FROM evidence e
            JOIN processing_runs pr
              ON pr.id = e.processing_run_id AND pr.is_current = 1
            LEFT JOIN labeling_expected_evidence lee
              ON lee.case_row_id = ? AND lee.evidence_id = e.id
            WHERE e.document_version_id = ?
              AND e.status NOT IN ('conflicted', 'deprecated', 'archived')
              {selection_filter}
            ORDER BY e.run_ordinal, e.id
            LIMIT ? OFFSET ?
            """,
            (case["id"], case["document_version_id"], limit, offset),
        ).fetchall()
    return {
        "session": {
            "id": session["id"],
            "status": session["status"],
            "created_by": session["created_by"],
        },
        "case": {
            "case_id": case["case_id"],
            "source_path": case["source_path"],
            "source_sha256": case["source_sha256"],
            "classification": case["classification"],
            "document_version_id": case["document_version_id"],
        },
        "total": total,
        "limit": limit,
        "offset": offset,
        "only_unselected": only_unselected,
        "only_selected": only_selected,
        "candidates": [
            {
                **dict(row),
                "locator": json.loads(row["locator_json"]),
            }
            for row in rows
        ],
    }


def list_labeling_sessions(database: Database) -> list[dict]:
    with database.connect() as connection:
        rows = connection.execute(
            """
            SELECT ls.id, ls.name, ls.status, ls.minimum_required_per_case,
                   ls.created_by, ls.submitted_by, ls.approved_by, ls.updated_at,
                   COUNT(DISTINCT lc.id) AS case_count,
                   COUNT(DISTINCT lee.evidence_id) AS selected_evidence_count
            FROM labeling_sessions ls
            LEFT JOIN labeling_cases lc ON lc.session_id = ls.id
            LEFT JOIN labeling_expected_evidence lee ON lee.case_row_id = lc.id
            GROUP BY ls.id
            ORDER BY ls.updated_at DESC, ls.id
            """
        ).fetchall()
    return [dict(row) for row in rows]


def labeling_session_readiness(database: Database, session_id: str) -> dict:
    with database.connect() as connection:
        session = _get_session(connection, session_id)
        cases = connection.execute(
            "SELECT * FROM labeling_cases WHERE session_id = ? ORDER BY case_id",
            (session_id,),
        ).fetchall()
        results = []
        for case in cases:
            issues = []
            try:
                _ensure_case_source_current(connection, case)
            except KnowledgeWorkbenchError as exc:
                issues.append(
                    {
                        "code": "source_or_processing_stale",
                        "message": str(exc),
                    }
                )
            selected = connection.execute(
                """
                SELECT evidence_id FROM labeling_expected_evidence
                WHERE case_row_id = ? ORDER BY created_at, evidence_id
                """,
                (case["id"],),
            ).fetchall()
            selected_ids = [row["evidence_id"] for row in selected]
            if len(selected_ids) < session["minimum_required_per_case"]:
                issues.append(
                    {
                        "code": "insufficient_evidence",
                        "message": (
                            f"已选择 {len(selected_ids)} 条，至少需要 "
                            f"{session['minimum_required_per_case']} 条"
                        ),
                    }
                )
            for evidence_id in selected_ids:
                try:
                    _ensure_evidence_is_current_for_case(
                        connection, case, evidence_id
                    )
                except KnowledgeWorkbenchError as exc:
                    issues.append(
                        {
                            "code": "selected_evidence_invalid",
                            "evidence_id": evidence_id,
                            "message": str(exc),
                        }
                    )
            review = connection.execute(
                """
                SELECT reviewer, decision, reviewed_at
                FROM labeling_case_reviews WHERE case_row_id = ?
                """,
                (case["id"],),
            ).fetchone()
            if session["status"] in {"reviewing", "approved"}:
                if not review:
                    issues.append(
                        {
                            "code": "case_review_missing",
                            "message": "该用例尚未记录复核决定",
                        }
                    )
                elif review["decision"] != "approved":
                    issues.append(
                        {
                            "code": "case_review_rejected",
                            "message": "该用例的当前复核决定为 rejected",
                        }
                    )
                elif (
                    session["status"] == "approved"
                    and review["reviewer"] != session["approved_by"]
                ):
                    issues.append(
                        {
                            "code": "case_reviewer_mismatch",
                            "message": "用例复核人与会话批准人不一致",
                        }
                    )
            results.append(
                {
                    "case_id": case["case_id"],
                    "classification": case["classification"],
                    "selected_evidence_count": len(selected_ids),
                    "minimum_required": session["minimum_required_per_case"],
                    "review_decision": review["decision"] if review else None,
                    "reviewer": review["reviewer"] if review else None,
                    "ready": not issues,
                    "issues": issues,
                }
            )
    ready = all(case["ready"] for case in results)
    status = session["status"]
    reviewers = {
        case["reviewer"] for case in results if case["reviewer"] is not None
    }
    review_consistent = (
        len(reviewers) == 1 and session["submitted_by"] not in reviewers
    )
    return {
        "session_id": session["id"],
        "status": status,
        "ready": ready,
        "case_count": len(results),
        "ready_case_count": sum(case["ready"] for case in results),
        "reviewed_case_count": sum(
            case["review_decision"] is not None for case in results
        ),
        "review_consistent": review_consistent,
        "reviewer": next(iter(reviewers)) if len(reviewers) == 1 else None,
        "issue_count": sum(len(case["issues"]) for case in results),
        "can_submit": ready and status == "draft",
        "can_approve": ready and status == "reviewing" and review_consistent,
        "can_export": ready and status == "approved" and review_consistent,
        "cases": results,
    }


def validate_labeling_session_ready(database: Database, session_id: str) -> None:
    with database.connect() as connection:
        session = _get_session(connection, session_id)
        _ensure_session_ready(connection, session)
        if session["status"] == "approved":
            _ensure_case_reviews(connection, session, session["approved_by"])


def _load_template(path: Path) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise KnowledgeWorkbenchError(f"标注模板不存在：{path}") from exc
    except json.JSONDecodeError as exc:
        raise KnowledgeWorkbenchError(f"标注模板不是有效 JSON：{exc}") from exc
    validate_evaluation_dataset(payload)
    return payload


def _get_session(connection, session_id: str):
    session = connection.execute(
        "SELECT * FROM labeling_sessions WHERE id = ?", (session_id,)
    ).fetchone()
    if not session:
        raise KnowledgeWorkbenchError(f"标注集不存在：{session_id}")
    return session


def _editable_case(connection, session_id: str, case_id: str, actor: str):
    session = _get_session(connection, session_id)
    if session["status"] != "draft":
        raise InvalidTransitionError("只有 draft 标注集可以修改")
    if session["created_by"] != actor:
        raise InvalidTransitionError("只有标注集创建者可以修改")
    case = connection.execute(
        "SELECT * FROM labeling_cases WHERE session_id = ? AND case_id = ?",
        (session_id, case_id),
    ).fetchone()
    if not case:
        raise KnowledgeWorkbenchError(f"标注用例不存在：{case_id}")
    return session, case


def _ensure_evidence_is_current_for_case(connection, case, evidence_id: str) -> None:
    evidence = connection.execute(
        """
        SELECT e.id
        FROM evidence e
        JOIN processing_runs pr
          ON pr.id = e.processing_run_id AND pr.is_current = 1
        JOIN document_versions dv ON dv.id = e.document_version_id
        JOIN documents d
          ON d.id = dv.document_id AND d.current_version_id = dv.id
        WHERE e.id = ? AND e.document_version_id = ?
          AND e.status NOT IN ('conflicted', 'deprecated', 'archived')
        """,
        (evidence_id, case["document_version_id"]),
    ).fetchone()
    if not evidence:
        raise KnowledgeWorkbenchError(
            f"证据 {evidence_id} 不属于该用例的当前文件版本和当前处理运行"
        )


def _ensure_session_ready(connection, session) -> None:
    cases = connection.execute(
        "SELECT * FROM labeling_cases WHERE session_id = ? ORDER BY case_id",
        (session["id"],),
    ).fetchall()
    for case in cases:
        _ensure_case_ready(connection, session, case)


def _ensure_case_ready(connection, session, case) -> None:
    _ensure_case_source_current(connection, case)
    selected = connection.execute(
        """
        SELECT lee.evidence_id
        FROM labeling_expected_evidence lee
        WHERE lee.case_row_id = ?
        """,
        (case["id"],),
    ).fetchall()
    if len(selected) < session["minimum_required_per_case"]:
        raise KnowledgeWorkbenchError(
            f"标注用例 {case['case_id']} 只有 {len(selected)} 条必要证据，"
            f"至少需要 {session['minimum_required_per_case']} 条"
        )
    for row in selected:
        _ensure_evidence_is_current_for_case(connection, case, row["evidence_id"])


def _ensure_case_reviews(connection, session, actor: str) -> None:
    rows = connection.execute(
        """
        SELECT lc.case_id, lcr.reviewer, lcr.decision
        FROM labeling_cases lc
        LEFT JOIN labeling_case_reviews lcr ON lcr.case_row_id = lc.id
        WHERE lc.session_id = ? ORDER BY lc.case_id
        """,
        (session["id"],),
    ).fetchall()
    missing = [row["case_id"] for row in rows if row["decision"] is None]
    rejected = [row["case_id"] for row in rows if row["decision"] == "rejected"]
    wrong_reviewer = [
        row["case_id"]
        for row in rows
        if row["decision"] == "approved" and row["reviewer"] != actor
    ]
    if missing:
        raise KnowledgeWorkbenchError(
            "以下用例尚未逐项复核：" + ", ".join(missing)
        )
    if rejected:
        raise KnowledgeWorkbenchError(
            "以下用例复核未通过：" + ", ".join(rejected)
        )
    if wrong_reviewer:
        raise KnowledgeWorkbenchError(
            "批准人必须与所有用例的复核人一致：" + ", ".join(wrong_reviewer)
        )


def _ensure_case_source_current(connection, case) -> None:
    source_path = Path(case["source_path"])
    if not source_path.is_file() or sha256_file(source_path) != case["source_sha256"]:
        raise KnowledgeWorkbenchError(
            f"标注用例 {case['case_id']} 的来源文件内容已经变化"
        )
    current = connection.execute(
        """
        SELECT 1
        FROM document_versions dv
        JOIN documents d
          ON d.id = dv.document_id AND d.current_version_id = dv.id
        JOIN processing_runs pr
          ON pr.document_version_id = dv.id AND pr.is_current = 1
        WHERE dv.id = ? AND dv.sha256 = ? AND d.classification = ?
        """,
        (
            case["document_version_id"],
            case["source_sha256"],
            case["classification"],
        ),
    ).fetchone()
    if not current:
        raise KnowledgeWorkbenchError(
            f"标注用例 {case['case_id']} 的来源或处理运行已经过期"
        )


def _parse_labeling_annotation_pack(
    content: str,
) -> tuple[str, dict[str, list[tuple[int, str]]]]:
    lines = content.splitlines()
    if not lines or lines[0].strip() != "---":
        raise KnowledgeWorkbenchError("标注工作包缺少 YAML Frontmatter")
    try:
        frontmatter_end = next(
            index for index, line in enumerate(lines[1:], start=1) if line.strip() == "---"
        )
    except StopIteration as exc:
        raise KnowledgeWorkbenchError("标注工作包的 YAML Frontmatter 未闭合") from exc
    metadata = {}
    for line in lines[1:frontmatter_end]:
        if ":" in line:
            key, value = line.split(":", 1)
            metadata[key.strip()] = value.strip()
    if metadata.get("type") != "labeling-annotation-pack":
        raise KnowledgeWorkbenchError("文件不是系统生成的标注工作包")
    session_id = metadata.get("session_id", "")
    if not session_id:
        raise KnowledgeWorkbenchError("标注工作包缺少 session_id")

    case_pattern = re.compile(r"^##\s+(.+?)\s*$")
    candidate_pattern = re.compile(r"^###\s+#(\d+)\s+`(ev_[A-Za-z0-9]+)`\s*$")
    legacy_pattern = re.compile(
        r"^###\s+\[([ xX])\]\s+#(\d+)\s+`(ev_[A-Za-z0-9]+)`\s*$"
    )
    task_pattern = re.compile(r"^-\s+\[([ xX])\]\s+选择此证据\s*$")
    checked: dict[str, list[tuple[int, str]]] = {}
    seen: set[tuple[str, int, str]] = set()
    current_case: str | None = None
    current_candidate: tuple[int, str] | None = None

    def add_checked(ordinal: int, evidence_id: str) -> None:
        if current_case is None:
            raise KnowledgeWorkbenchError("已勾选证据出现在用例标题之前")
        key = (current_case, ordinal, evidence_id)
        if key in seen:
            raise KnowledgeWorkbenchError(
                f"标注工作包包含重复勾选：{current_case} #{ordinal}"
            )
        seen.add(key)
        checked.setdefault(current_case, []).append((ordinal, evidence_id))

    for line in lines[frontmatter_end + 1 :]:
        case_match = case_pattern.match(line)
        if case_match:
            current_case = case_match.group(1)
            current_candidate = None
            continue
        legacy_match = legacy_pattern.match(line)
        if legacy_match:
            current_candidate = (int(legacy_match.group(2)), legacy_match.group(3))
            if legacy_match.group(1).lower() == "x":
                add_checked(*current_candidate)
            continue
        candidate_match = candidate_pattern.match(line)
        if candidate_match:
            current_candidate = (int(candidate_match.group(1)), candidate_match.group(2))
            continue
        task_match = task_pattern.match(line)
        if task_match and task_match.group(1).lower() == "x":
            if current_candidate is None:
                raise KnowledgeWorkbenchError("已勾选任务缺少对应的候选证据标题")
            add_checked(*current_candidate)
    return session_id, checked


def _parse_labeling_review_pack(
    content: str,
) -> tuple[str, str, dict[str, dict[str, bool | str]]]:
    lines = content.splitlines()
    if not lines or lines[0].strip() != "---":
        raise KnowledgeWorkbenchError("复核包缺少 YAML Frontmatter")
    try:
        frontmatter_end = next(
            index for index, line in enumerate(lines[1:], start=1) if line.strip() == "---"
        )
    except StopIteration as exc:
        raise KnowledgeWorkbenchError("复核包的 YAML Frontmatter 未闭合") from exc
    metadata = {}
    for line in lines[1:frontmatter_end]:
        if ":" in line:
            key, value = line.split(":", 1)
            metadata[key.strip()] = value.strip()
    if metadata.get("type") != "labeling-review-pack":
        raise KnowledgeWorkbenchError("文件不是系统生成的复核包")
    session_id = metadata.get("session_id", "")
    reviewer = metadata.get("reviewer", "")
    if not session_id or not reviewer:
        raise KnowledgeWorkbenchError("复核包缺少 session_id 或 reviewer")

    case_pattern = re.compile(r"^##\s+(.+?)\s*$")
    approved_pattern = re.compile(r"^-\s+\[([ xX])\]\s+批准本用例\s*$")
    rejected_pattern = re.compile(r"^-\s+\[([ xX])\]\s+驳回本用例\s*$")
    note_pattern = re.compile(r"^-\s+驳回原因：(.*)$")
    decisions: dict[str, dict[str, bool | str]] = {}
    current_case: str | None = None

    def current_decision() -> dict[str, bool | str]:
        if current_case is None:
            raise KnowledgeWorkbenchError("复核决定出现在用例标题之前")
        return decisions.setdefault(
            current_case,
            {"approved": False, "rejected": False, "note": ""},
        )

    for line in lines[frontmatter_end + 1 :]:
        case_match = case_pattern.match(line)
        if case_match:
            current_case = case_match.group(1)
            continue
        approved_match = approved_pattern.match(line)
        if approved_match:
            current_decision()["approved"] = approved_match.group(1).lower() == "x"
            continue
        rejected_match = rejected_pattern.match(line)
        if rejected_match:
            current_decision()["rejected"] = rejected_match.group(1).lower() == "x"
            continue
        note_match = note_pattern.match(line)
        if note_match:
            current_decision()["note"] = note_match.group(1).strip()
    return session_id, reviewer, decisions


def _relative_source_path(source: Path, output_parent: Path) -> str:
    try:
        return Path(os.path.relpath(source, output_parent)).as_posix()
    except ValueError:
        return str(source)


def _ensure_labeling_output_allowed(
    paths: WorkspacePaths, cases, output: Path, output_kind: str
) -> None:
    if any(case["classification"] == "restricted" for case in cases):
        raise KnowledgeWorkbenchError(f"restricted 标注集暂不允许导出{output_kind}")
    if any(case["classification"] != "public" for case in cases):
        try:
            output.relative_to(paths.evaluations)
        except ValueError as exc:
            raise KnowledgeWorkbenchError(
                f"非公开资料的{output_kind}只能导出到当前 workspace/evaluations 内"
            ) from exc


def _markdown_code(value: str) -> str:
    return value.replace("`", "'")


def _blockquote(value: str) -> list[str]:
    return [f"> {line}" if line else ">" for line in value.splitlines() or [""]]


def _required_actor(value: str) -> str:
    actor = value.strip()
    if not actor:
        raise KnowledgeWorkbenchError("actor 不能为空")
    return actor
