from __future__ import annotations

import re
from difflib import SequenceMatcher

from .audit import record_event
from .database import Database
from .errors import InvalidTransitionError, KnowledgeWorkbenchError
from .models import ConflictStatus
from .utils import new_id, utc_now


CONFLICT_TRANSITIONS = {
    ConflictStatus.PENDING: {ConflictStatus.REVIEWING, ConflictStatus.DISMISSED},
    ConflictStatus.REVIEWING: {ConflictStatus.RESOLVED, ConflictStatus.DISMISSED},
    ConflictStatus.RESOLVED: set(),
    ConflictStatus.DISMISSED: set(),
}


def detect_version_conflicts(
    connection,
    *,
    document_id: str,
    older_version_id: str | None,
    newer_version_id: str,
    actor: str = "system",
) -> list[str]:
    if not older_version_id:
        return []
    older = connection.execute(
        "SELECT id, excerpt FROM evidence WHERE document_version_id = ?",
        (older_version_id,),
    ).fetchall()
    newer = connection.execute(
        "SELECT id, excerpt FROM evidence WHERE document_version_id = ?",
        (newer_version_id,),
    ).fetchall()
    created: list[str] = []
    now = utc_now()
    for old in older:
        for new in newer:
            conflict = _classify_conflict(old["excerpt"], new["excerpt"])
            if not conflict:
                continue
            conflict_type, similarity, reason = conflict
            conflict_id = new_id("conflict")
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO conflicts(
                    id, document_id, older_evidence_id, newer_evidence_id,
                    conflict_type, similarity_score, reason, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                """,
                (
                    conflict_id,
                    document_id,
                    old["id"],
                    new["id"],
                    conflict_type,
                    similarity,
                    reason,
                    now,
                    now,
                ),
            )
            if cursor.rowcount:
                created.append(conflict_id)
                record_event(
                    connection,
                    "potential_conflict_queued",
                    "conflict",
                    conflict_id,
                    actor=actor,
                    details={
                        "older_evidence_id": old["id"],
                        "newer_evidence_id": new["id"],
                        "type": conflict_type,
                        "similarity": similarity,
                    },
                )
    return created


def transition_conflict(
    database: Database,
    conflict_id: str,
    target: ConflictStatus,
    *,
    actor: str,
    note: str | None = None,
) -> None:
    now = utc_now()
    with database.transaction() as connection:
        row = connection.execute(
            "SELECT status FROM conflicts WHERE id = ?", (conflict_id,)
        ).fetchone()
        if not row:
            raise KnowledgeWorkbenchError(f"冲突不存在：{conflict_id}")
        current = ConflictStatus(row["status"])
        if current == target:
            return
        if target not in CONFLICT_TRANSITIONS[current]:
            raise InvalidTransitionError(
                f"冲突状态不能从 {current.value} 直接变为 {target.value}"
            )
        if target is ConflictStatus.RESOLVED and not (note or "").strip():
            raise KnowledgeWorkbenchError("解决冲突时必须填写 resolution note")
        connection.execute(
            """
            UPDATE conflicts
            SET status = ?, resolution_note = COALESCE(?, resolution_note), updated_at = ?
            WHERE id = ?
            """,
            (target.value, note, now, conflict_id),
        )
        record_event(
            connection,
            "conflict_status_changed",
            "conflict",
            conflict_id,
            actor=actor,
            details={"from": current.value, "to": target.value, "note": note},
        )


def _classify_conflict(older: str, newer: str):
    old_normalized = _normalize(older)
    new_normalized = _normalize(newer)
    if old_normalized == new_normalized:
        return None
    similarity = SequenceMatcher(None, old_normalized, new_normalized).ratio()
    old_polarity = _polarity(old_normalized)
    new_polarity = _polarity(new_normalized)
    if similarity >= 0.55 and old_polarity and new_polarity and old_polarity != new_polarity:
        return (
            "polarity_change",
            round(similarity, 6),
            "相似原子证据出现允许/禁止或肯定/否定方向变化，需人工确认。",
        )
    old_numbers = re.findall(r"\d+(?:\.\d+)?%?", old_normalized)
    new_numbers = re.findall(r"\d+(?:\.\d+)?%?", new_normalized)
    if similarity >= 0.65 and old_numbers and new_numbers and old_numbers != new_numbers:
        return (
            "value_change",
            round(similarity, 6),
            "相似原子证据中的数值发生变化，需确认适用版本和时间。",
        )
    return None


def _normalize(value: str) -> str:
    return re.sub(r"[\W_]+", "", value.lower(), flags=re.UNICODE)


def _polarity(value: str) -> str | None:
    negative = ("禁止", "不得", "不允许", "不应", "无需", "不必", "不是", "禁用")
    positive = ("允许", "可以", "应当", "应该", "必须", "需要", "启用", "是")
    if any(token in value for token in negative):
        return "negative"
    if any(token in value for token in positive):
        return "positive"
    return None

