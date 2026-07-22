from __future__ import annotations

import json
import os
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
        lines = [
            f"# {session['name']}：复核包",
            "",
            f"- 会话ID：`{session_id}`",
            f"- 提交人：`{session['submitted_by']}`",
            f"- 复核人：`{actor}`",
            f"- 生成时间：`{utc_now()}`",
            "- 说明：本文件只用于人工回源复核，不代表已经批准。",
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
                        f"### [ ] #{row['run_ordinal']} `{row['id']}`",
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
            output.relative_to(paths.root)
        except ValueError as exc:
            raise KnowledgeWorkbenchError(
                f"非公开资料的{output_kind}只能导出到当前 workspace 内"
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
