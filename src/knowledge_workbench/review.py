from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

from .audit import record_event
from .config import WorkspacePaths
from .database import Database
from .errors import InvalidTransitionError, KnowledgeWorkbenchError
from .models import EvidenceStatus, RevisionStatus
from .utils import sha256_text, utc_now
from .wiki import write_text_atomic


EVIDENCE_TRANSITIONS: dict[EvidenceStatus, set[EvidenceStatus]] = {
    EvidenceStatus.DRAFT: {EvidenceStatus.REVIEWING, EvidenceStatus.ARCHIVED},
    EvidenceStatus.REVIEWING: {
        EvidenceStatus.DRAFT,
        EvidenceStatus.VERIFIED,
        EvidenceStatus.CONFLICTED,
        EvidenceStatus.ARCHIVED,
    },
    EvidenceStatus.VERIFIED: {
        EvidenceStatus.CONFLICTED,
        EvidenceStatus.DEPRECATED,
        EvidenceStatus.ARCHIVED,
    },
    EvidenceStatus.CONFLICTED: {
        EvidenceStatus.REVIEWING,
        EvidenceStatus.DEPRECATED,
        EvidenceStatus.ARCHIVED,
    },
    EvidenceStatus.DEPRECATED: {EvidenceStatus.ARCHIVED},
    EvidenceStatus.ARCHIVED: set(),
}


def transition_evidence(
    database: Database,
    evidence_id: str,
    target: EvidenceStatus,
    *,
    actor: str,
) -> None:
    actor = _required_actor(actor)
    with database.transaction() as connection:
        _transition_evidence_in_transaction(
            connection,
            evidence_id,
            target,
            actor=actor,
        )


def finalize_technical_validation(
    connection: sqlite3.Connection,
    processing_run_id: str,
    *,
    actor: str = "system:faithful-ingest-v1",
) -> dict[str, int]:
    """Promote faithfully validated current evidence without human approval."""

    actor = _required_actor(actor)
    run = connection.execute(
        """
        SELECT pr.is_current,
               (d.current_version_id = pr.document_version_id) AS version_is_current
        FROM processing_runs pr
        JOIN document_versions dv ON dv.id = pr.document_version_id
        JOIN documents d ON d.id = dv.document_id
        WHERE pr.id = ?
        """,
        (processing_run_id,),
    ).fetchone()
    if not run:
        raise KnowledgeWorkbenchError(f"处理运行不存在：{processing_run_id}")
    if not run["is_current"] or not run["version_is_current"]:
        raise InvalidTransitionError("只有当前文件版本的当前处理运行可以完成技术校验")

    rows = connection.execute(
        """
        SELECT e.id, e.status, etv.status AS validation_status
        FROM evidence e
        LEFT JOIN evidence_technical_validation etv ON etv.evidence_id = e.id
        WHERE e.processing_run_id = ?
        ORDER BY e.run_ordinal, e.id
        """,
        (processing_run_id,),
    ).fetchall()
    invalid = [
        row["id"] for row in rows if row["validation_status"] != "passed"
    ]
    if invalid:
        raise InvalidTransitionError(
            f"仍有 {len(invalid)} 条依据未通过自动技术校验，不能进入知识检索"
        )

    promoted = 0
    already_ready = 0
    preserved_exception = 0
    context = {
        "automation": "technical_validation",
        "validator": "faithful-ingest-v1",
    }
    for row in rows:
        current = EvidenceStatus(row["status"])
        if current is EvidenceStatus.DRAFT:
            _transition_evidence_in_transaction(
                connection,
                row["id"],
                EvidenceStatus.REVIEWING,
                actor=actor,
                expected_status=EvidenceStatus.DRAFT,
                context=context,
            )
            current = EvidenceStatus.REVIEWING
        if current is EvidenceStatus.REVIEWING:
            _transition_evidence_in_transaction(
                connection,
                row["id"],
                EvidenceStatus.VERIFIED,
                actor=actor,
                expected_status=EvidenceStatus.REVIEWING,
                context=context,
            )
            promoted += 1
        elif current is EvidenceStatus.VERIFIED:
            already_ready += 1
        else:
            # Never erase a conflict, deprecation or archival decision on re-import.
            preserved_exception += 1

    return {
        "total": len(rows),
        "promoted": promoted,
        "already_ready": already_ready,
        "preserved_exception": preserved_exception,
    }


def _transition_evidence_in_transaction(
    connection: sqlite3.Connection,
    evidence_id: str,
    target: EvidenceStatus,
    *,
    actor: str,
    expected_status: EvidenceStatus | None = None,
    context: dict[str, str] | None = None,
) -> EvidenceStatus:
    row = connection.execute(
        """
        SELECT e.status,
               pr.is_current AS processing_run_is_current,
               (d.current_version_id = dv.id) AS document_version_is_current
        FROM evidence e
        LEFT JOIN processing_runs pr ON pr.id = e.processing_run_id
        JOIN document_versions dv ON dv.id = e.document_version_id
        JOIN documents d ON d.id = dv.document_id
        WHERE e.id = ?
        """,
        (evidence_id,),
    ).fetchone()
    if not row:
        raise KnowledgeWorkbenchError(f"原子证据不存在：{evidence_id}")
    current = EvidenceStatus(row["status"])
    if expected_status is not None and current is not expected_status:
        raise InvalidTransitionError(
            f"证据 {evidence_id} 状态已变化；"
            f"预期 {expected_status.value}，当前 {current.value}"
        )
    if current is target:
        return current
    if target in {EvidenceStatus.REVIEWING, EvidenceStatus.VERIFIED} and (
        not row["processing_run_is_current"]
        or not row["document_version_is_current"]
    ):
        raise InvalidTransitionError(
            "历史文件版本或已被替代处理运行的证据不能进入审核或正式状态"
        )
    if target not in EVIDENCE_TRANSITIONS[current]:
        raise InvalidTransitionError(
            f"证据状态不能从 {current.value} 直接变为 {target.value}"
        )
    connection.execute(
        "UPDATE evidence SET status = ?, updated_at = ? WHERE id = ?",
        (target.value, utc_now(), evidence_id),
    )
    details = {"from": current.value, "to": target.value}
    details.update(context or {})
    record_event(
        connection,
        "evidence_status_changed",
        "evidence",
        evidence_id,
        actor=actor,
        details=details,
    )
    if current is EvidenceStatus.VERIFIED and target is not EvidenceStatus.VERIFIED:
        _mark_revisions_revalidation_for_evidence(
            connection,
            evidence_id=evidence_id,
            actor=actor,
            reason=f"evidence_status:{target.value}",
        )
    return current


def request_revision_review(
    database: Database,
    revision_id: str,
    *,
    actor: str,
    paths: WorkspacePaths | None = None,
) -> None:
    actor = _required_actor(actor)
    paths = paths or WorkspacePaths(database.path.parent)
    now = utc_now()
    with database.transaction() as connection:
        revision = _get_revision(connection, revision_id)
        _ensure_revision_is_current(connection, revision)
        content, _ = _read_revision_artifact(paths, revision)
        _validate_topic_revision_content(connection, revision, content)
        current = RevisionStatus(revision["status"])
        if current is not RevisionStatus.DRAFT:
            raise InvalidTransitionError(
                f"只有 draft 修订可提交审核，当前为 {current.value}"
            )
        connection.execute(
            "UPDATE wiki_revisions SET status = 'reviewing', updated_at = ? WHERE id = ?",
            (now, revision_id),
        )
        record_event(
            connection,
            "wiki_revision_submitted",
            "wiki_revision",
            revision_id,
            actor=actor,
        )


def reject_revision(
    database: Database,
    revision_id: str,
    *,
    actor: str,
    note: str,
) -> None:
    actor = _required_actor(actor)
    note = note.strip()
    if not note:
        raise KnowledgeWorkbenchError("驳回 Wiki 修订必须填写复核意见")
    if len(note) > 2000:
        raise KnowledgeWorkbenchError("Wiki 修订复核意见不能超过 2000 个字符")
    now = utc_now()
    with database.transaction() as connection:
        revision = _get_revision(connection, revision_id)
        _ensure_revision_is_latest(connection, revision)
        current = RevisionStatus(revision["status"])
        if current is not RevisionStatus.REVIEWING:
            raise InvalidTransitionError(
                f"只有 reviewing 修订可以驳回，当前为 {current.value}"
            )
        connection.execute(
            "UPDATE wiki_revisions SET status = 'rejected', updated_at = ? WHERE id = ?",
            (now, revision_id),
        )
        record_event(
            connection,
            "wiki_revision_rejected",
            "wiki_revision",
            revision_id,
            actor=actor,
            details={"note": note},
        )


def publish_revision(
    database: Database,
    paths: WorkspacePaths,
    revision_id: str,
    *,
    actor: str,
) -> Path:
    actor = _required_actor(actor)
    now = utc_now()
    source_path: Path | None = None
    target_path: Path | None = None
    target_written = False
    try:
        with database.transaction() as connection:
            revision = _get_revision(connection, revision_id)
            _ensure_revision_is_current(connection, revision)
            if RevisionStatus(revision["status"]) is not RevisionStatus.REVIEWING:
                raise InvalidTransitionError("只有 reviewing 修订可以发布")
            evidence_count = connection.execute(
                "SELECT COUNT(*) FROM revision_evidence WHERE revision_id = ?",
                (revision_id,),
            ).fetchone()[0]
            if evidence_count == 0:
                raise InvalidTransitionError("发布前必须至少引用一条证据")

            content, source_path = _read_revision_artifact(paths, revision)
            _validate_topic_revision_content(connection, revision, content)
            content = content.replace("status: draft", "status: verified", 1)
            target_path = paths.wiki_verified / source_path.name
            if target_path.exists() and target_path != source_path:
                raise KnowledgeWorkbenchError("正式 Wiki 文件已存在，不能静默覆盖")
            write_text_atomic(target_path, content)
            target_written = target_path != source_path

            previous = connection.execute(
                "SELECT current_verified_revision_id FROM wiki_pages WHERE id = ?",
                (revision["page_id"],),
            ).fetchone()[0]
            if previous:
                connection.execute(
                    """
                    UPDATE wiki_revisions
                    SET status = 'superseded', updated_at = ?
                    WHERE id = ?
                    """,
                    (now, previous),
                )
            relative_target = target_path.relative_to(paths.root).as_posix()
            connection.execute(
                """
                UPDATE wiki_revisions
                SET status = 'verified', markdown_path = ?,
                    content_sha256 = ?, updated_at = ?
                WHERE id = ?
                """,
                (relative_target, sha256_text(content), now, revision_id),
            )
            connection.execute(
                """
                UPDATE wiki_pages
                SET status = 'verified', current_verified_revision_id = ?,
                    needs_revalidation = 0, updated_at = ?
                WHERE id = ?
                """,
                (revision_id, now, revision["page_id"]),
            )
            record_event(
                connection,
                "wiki_revision_published",
                "wiki_revision",
                revision_id,
                actor=actor,
                details={"superseded_revision_id": previous},
            )
    except Exception:
        if target_written and target_path is not None:
            try:
                target_path.unlink(missing_ok=True)
            except OSError:
                pass
        raise

    if (
        source_path is not None
        and target_path is not None
        and source_path != target_path
    ):
        try:
            source_path.unlink(missing_ok=True)
        except OSError:
            # Database state and verified artifact are already committed. A
            # leftover draft is harmless and can be cleaned by maintenance.
            pass
    assert target_path is not None
    return target_path


def _get_revision(connection: sqlite3.Connection, revision_id: str) -> sqlite3.Row:
    row = connection.execute(
        """
        SELECT wr.*, wp.source_document_id, wp.needs_revalidation,
               pr.is_current AS processing_run_is_current,
               (d.current_version_id = dv.id) AS document_version_is_current
        FROM wiki_revisions wr
        JOIN wiki_pages wp ON wp.id = wr.page_id
        LEFT JOIN processing_runs pr ON pr.id = wr.processing_run_id
        LEFT JOIN document_versions dv ON dv.id = pr.document_version_id
        LEFT JOIN documents d ON d.id = dv.document_id
        WHERE wr.id = ?
        """,
        (revision_id,),
    ).fetchone()
    if not row:
        raise KnowledgeWorkbenchError(f"Wiki 修订不存在：{revision_id}")
    return row


def _ensure_revision_is_current(
    connection: sqlite3.Connection,
    revision: sqlite3.Row,
) -> None:
    if revision["source_document_id"] is not None and (
        not revision["processing_run_is_current"]
        or not revision["document_version_is_current"]
    ):
        raise InvalidTransitionError(
            "历史文件版本或已被替代处理运行的 Wiki 修订不能提交审核或发布"
        )
    _ensure_revision_is_latest(connection, revision)
    if revision["source_document_id"] is not None:
        _ensure_document_revision_sources(connection, revision)
        return

    if revision["processing_run_id"] is not None:
        raise InvalidTransitionError("聚合主题页不能绑定单一处理运行")
    expected_evidence_count = connection.execute(
        """
        SELECT COUNT(*)
        FROM revision_evidence
        WHERE revision_id = ?
        """,
        (revision["id"],),
    ).fetchone()[0]
    sources = connection.execute(
        """
        SELECT e.id AS evidence_id, e.status AS evidence_status,
               etv.status AS validation_status,
               dv.document_id, pr.status AS run_status,
               pr.is_current AS run_is_current,
               (d.current_version_id = dv.id) AS version_is_current,
               (pr.document_version_id = e.document_version_id)
                   AS run_version_matches,
               d.classification, dg.purpose, dg.scope_status,
               dg.authority_status
        FROM revision_evidence re
        JOIN evidence e ON e.id = re.evidence_id
        JOIN document_versions dv ON dv.id = e.document_version_id
        JOIN documents d ON d.id = dv.document_id
        LEFT JOIN evidence_technical_validation etv ON etv.evidence_id = e.id
        LEFT JOIN processing_runs pr ON pr.id = e.processing_run_id
        LEFT JOIN document_governance dg ON dg.document_id = d.id
        WHERE re.revision_id = ?
        ORDER BY e.id
        """,
        (revision["id"],),
    ).fetchall()
    if not sources or len(sources) != expected_evidence_count:
        raise InvalidTransitionError("聚合主题页至少需要引用两份资料")
    if len({row["document_id"] for row in sources}) < 2:
        raise InvalidTransitionError("聚合主题页至少需要引用两份不同资料")
    invalid = [
        row
        for row in sources
        if row["evidence_status"] != "verified"
        or row["validation_status"] != "passed"
        or row["run_status"] != "completed"
        or not row["run_is_current"]
        or not row["version_is_current"]
        or not row["run_version_matches"]
        or row["classification"] == "restricted"
        or row["purpose"] != "production"
        or row["scope_status"] != "in_scope"
        or row["authority_status"] not in {"reference", "authoritative"}
    ]
    if invalid:
        raise InvalidTransitionError(
            f"聚合主题页有 {len(invalid)} 条来源依据已失效，不能提交审核或发布"
        )


def _ensure_document_revision_sources(
    connection: sqlite3.Connection,
    revision: sqlite3.Row,
) -> None:
    if not revision["processing_run_id"]:
        raise InvalidTransitionError("单资料 Wiki 修订必须绑定处理运行")
    expected_evidence_count = connection.execute(
        "SELECT COUNT(*) FROM revision_evidence WHERE revision_id = ?",
        (revision["id"],),
    ).fetchone()[0]
    sources = connection.execute(
        """
        SELECT e.id AS evidence_id, e.status AS evidence_status,
               e.processing_run_id AS evidence_run_id,
               etv.status AS validation_status,
               dv.document_id,
               pr.status AS run_status,
               pr.is_current AS run_is_current,
               (pr.document_version_id = e.document_version_id)
                   AS run_version_matches,
               (d.current_version_id = e.document_version_id)
                   AS version_is_current,
               d.classification, dg.purpose, dg.scope_status,
               dg.authority_status
        FROM revision_evidence re
        JOIN evidence e ON e.id = re.evidence_id
        JOIN document_versions dv ON dv.id = e.document_version_id
        JOIN documents d ON d.id = dv.document_id
        LEFT JOIN evidence_technical_validation etv
          ON etv.evidence_id = e.id
        LEFT JOIN processing_runs pr
          ON pr.id = e.processing_run_id
        LEFT JOIN document_governance dg
          ON dg.document_id = d.id
        WHERE re.revision_id = ?
        ORDER BY e.id
        """,
        (revision["id"],),
    ).fetchall()
    if not sources or len(sources) != expected_evidence_count:
        raise InvalidTransitionError("单资料 Wiki 修订必须至少引用一条有效依据")
    invalid = [
        row
        for row in sources
        if row["document_id"] != revision["source_document_id"]
        or row["evidence_run_id"] != revision["processing_run_id"]
        or row["evidence_status"] != "verified"
        or row["validation_status"] != "passed"
        or row["run_status"] != "completed"
        or not row["run_is_current"]
        or not row["run_version_matches"]
        or not row["version_is_current"]
        or row["classification"] == "restricted"
        or row["purpose"] != "production"
        or row["scope_status"] != "in_scope"
        or row["authority_status"] not in {"reference", "authoritative"}
    ]
    if invalid:
        raise InvalidTransitionError(
            f"单资料 Wiki 有 {len(invalid)} 条来源依据未完成准入或已失效，"
            "不能提交审核或发布"
        )


_TOPIC_MARKER = re.compile(
    r"<!-- topic-section-evidence: (?P<ids>\[.*\]) -->"
)


def _read_revision_artifact(
    paths: WorkspacePaths,
    revision: sqlite3.Row,
) -> tuple[str, Path]:
    root = paths.root.resolve()
    source_path = (root / revision["markdown_path"]).resolve()
    try:
        source_path.relative_to(root)
    except ValueError as exc:
        raise KnowledgeWorkbenchError("Wiki 修订文件位置无效") from exc
    if not source_path.is_file():
        raise KnowledgeWorkbenchError(f"修订文件不存在：{source_path}")
    content = source_path.read_text(encoding="utf-8")
    if sha256_text(content) != revision["content_sha256"]:
        raise KnowledgeWorkbenchError(
            "Wiki 修订文件已被外部修改，请先通过受控流程同步后再发布"
        )
    return content, source_path


def _validate_topic_revision_content(
    connection: sqlite3.Connection,
    revision: sqlite3.Row,
    content: str,
) -> None:
    if revision["source_document_id"] is not None:
        return
    if revision["generator"] != "topic-extractive-v1":
        raise InvalidTransitionError("主题知识页生成器不符合逐字摘录基线")
    if not re.search(
        r'^generator:\s*"topic-extractive-v1"\s*$',
        content,
        flags=re.MULTILINE,
    ):
        raise InvalidTransitionError("主题知识页缺少可信的生成器声明")

    evidence_rows = connection.execute(
        """
        SELECT e.id, e.excerpt
        FROM revision_evidence re
        JOIN evidence e ON e.id = re.evidence_id
        WHERE re.revision_id = ?
        ORDER BY e.id
        """,
        (revision["id"],),
    ).fetchall()
    expected = {row["id"]: row["excerpt"].strip() for row in evidence_rows}
    lines = content.splitlines()
    heading_indexes = [
        index for index, line in enumerate(lines) if line.startswith("## ")
    ]
    if not heading_indexes:
        raise InvalidTransitionError("主题知识页缺少可审核章节")

    declared: set[str] = set()
    source_declared: set[str] = set()
    for position, heading_index in enumerate(heading_indexes):
        if heading_index + 1 >= len(lines):
            raise InvalidTransitionError("主题知识页章节后缺少依据声明")
        marker_match = _TOPIC_MARKER.fullmatch(lines[heading_index + 1].strip())
        if not marker_match:
            raise InvalidTransitionError("主题知识页每个二级章节必须紧邻声明依据")
        try:
            marker_ids = json.loads(marker_match.group("ids"))
        except json.JSONDecodeError as exc:
            raise InvalidTransitionError("主题知识页章节依据声明格式无效") from exc
        if (
            not isinstance(marker_ids, list)
            or any(not isinstance(item, str) for item in marker_ids)
        ):
            raise InvalidTransitionError("主题知识页章节依据声明必须是 ID 列表")
        if len(marker_ids) != len(set(marker_ids)):
            raise InvalidTransitionError("主题知识页章节依据声明不能包含重复项")
        unknown = set(marker_ids) - set(expected)
        if unknown:
            raise InvalidTransitionError("主题知识页章节引用了不属于本修订的依据")
        declared.update(marker_ids)

        if not lines[heading_index].startswith("## 原文依据："):
            continue
        if not marker_ids:
            raise InvalidTransitionError("主题知识页原文依据章节不能为空")
        source_declared.update(marker_ids)
        next_heading = (
            heading_indexes[position + 1]
            if position + 1 < len(heading_indexes)
            else len(lines)
        )
        section_lines = lines[heading_index + 2 : next_heading]
        section_text = "\n".join(
            line[2:] if line.startswith("> ") else line
            for line in section_lines
        )
        if any(expected[evidence_id] not in section_text for evidence_id in marker_ids):
            raise InvalidTransitionError(
                "主题知识页原文依据章节与声明的逐字摘录不一致"
            )

    if declared != set(expected):
        raise InvalidTransitionError("主题知识页章节依据与修订引用不一致")
    if source_declared != set(expected):
        raise InvalidTransitionError("主题知识页并未逐项展示全部来源依据")


def _ensure_revision_is_latest(
    connection: sqlite3.Connection,
    revision: sqlite3.Row,
) -> None:
    latest = connection.execute(
        """
        SELECT id
        FROM wiki_revisions
        WHERE page_id = ?
        ORDER BY revision_number DESC, created_at DESC, id DESC
        LIMIT 1
        """,
        (revision["page_id"],),
    ).fetchone()
    if not latest or latest["id"] != revision["id"]:
        raise InvalidTransitionError("只有页面最新修订可以提交审核或发布")


def _mark_revisions_revalidation_for_evidence(
    connection: sqlite3.Connection,
    *,
    evidence_id: str,
    actor: str,
    reason: str,
) -> None:
    now = utc_now()
    pages = connection.execute(
        """
        SELECT DISTINCT wp.id
        FROM wiki_pages wp
        JOIN revision_evidence re
          ON re.revision_id = wp.current_verified_revision_id
        WHERE re.evidence_id = ?
          AND wp.needs_revalidation = 0
        """,
        (evidence_id,),
    ).fetchall()
    for page in pages:
        connection.execute(
            """
            UPDATE wiki_pages
            SET needs_revalidation = 1, updated_at = ?
            WHERE id = ?
            """,
            (now, page["id"]),
        )
        record_event(
            connection,
            "wiki_revalidation_required",
            "wiki_page",
            page["id"],
            actor=actor,
            details={"reason": reason, "cause_evidence_id": evidence_id},
        )


def _required_actor(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise KnowledgeWorkbenchError("actor 不能为空")
    actor = value.strip()
    if len(actor) > 80:
        raise KnowledgeWorkbenchError("actor 不能超过 80 个字符")
    if any(character in actor for character in "\r\n"):
        raise KnowledgeWorkbenchError("actor 不能包含换行")
    return actor
