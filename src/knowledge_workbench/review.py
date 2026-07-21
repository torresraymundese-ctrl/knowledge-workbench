from __future__ import annotations

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
    now = utc_now()
    with database.transaction() as connection:
        row = connection.execute(
            "SELECT status FROM evidence WHERE id = ?", (evidence_id,)
        ).fetchone()
        if not row:
            raise KnowledgeWorkbenchError(f"原子证据不存在：{evidence_id}")
        current = EvidenceStatus(row["status"])
        if current == target:
            return
        if target not in EVIDENCE_TRANSITIONS[current]:
            raise InvalidTransitionError(
                f"证据状态不能从 {current.value} 直接变为 {target.value}"
            )
        connection.execute(
            "UPDATE evidence SET status = ?, updated_at = ? WHERE id = ?",
            (target.value, now, evidence_id),
        )
        record_event(
            connection,
            "evidence_status_changed",
            "evidence",
            evidence_id,
            actor=actor,
            details={"from": current.value, "to": target.value},
        )


def request_revision_review(database: Database, revision_id: str, *, actor: str) -> None:
    now = utc_now()
    with database.transaction() as connection:
        revision = _get_revision(connection, revision_id)
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


def publish_revision(
    database: Database,
    paths: WorkspacePaths,
    revision_id: str,
    *,
    actor: str,
) -> Path:
    now = utc_now()
    with database.transaction() as connection:
        revision = _get_revision(connection, revision_id)
        if RevisionStatus(revision["status"]) is not RevisionStatus.REVIEWING:
            raise InvalidTransitionError("只有 reviewing 修订可以发布")
        unverified = connection.execute(
            """
            SELECT e.id, e.status
            FROM revision_evidence re
            JOIN evidence e ON e.id = re.evidence_id
            WHERE re.revision_id = ? AND e.status != 'verified'
            ORDER BY e.ordinal
            """,
            (revision_id,),
        ).fetchall()
        if unverified:
            preview = ", ".join(f"{row['id']}({row['status']})" for row in unverified[:5])
            raise InvalidTransitionError(
                f"发布前必须审核全部引用证据；尚有 {len(unverified)} 条：{preview}"
            )

        source_path = paths.root / revision["markdown_path"]
        if not source_path.is_file():
            raise KnowledgeWorkbenchError(f"修订文件不存在：{source_path}")
        content = source_path.read_text(encoding="utf-8")
        content = content.replace("status: draft", "status: verified", 1)
        target_path = paths.wiki_verified / source_path.name
        write_text_atomic(target_path, content)

        previous = connection.execute(
            "SELECT current_verified_revision_id FROM wiki_pages WHERE id = ?",
            (revision["page_id"],),
        ).fetchone()[0]
        if previous:
            connection.execute(
                "UPDATE wiki_revisions SET status = 'superseded', updated_at = ? WHERE id = ?",
                (now, previous),
            )
        relative_target = target_path.relative_to(paths.root).as_posix()
        connection.execute(
            """
            UPDATE wiki_revisions
            SET status = 'verified', markdown_path = ?, content_sha256 = ?, updated_at = ?
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

    if source_path != target_path and source_path.exists():
        source_path.unlink()
    return target_path


def _get_revision(connection: sqlite3.Connection, revision_id: str) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM wiki_revisions WHERE id = ?", (revision_id,)
    ).fetchone()
    if not row:
        raise KnowledgeWorkbenchError(f"Wiki 修订不存在：{revision_id}")
    return row

