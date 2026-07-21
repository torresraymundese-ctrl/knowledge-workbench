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
        """
        SELECT e.id, e.excerpt, e.run_ordinal
        FROM evidence e
        JOIN processing_runs pr
          ON pr.id = e.processing_run_id AND pr.is_current = 1
        WHERE e.document_version_id = ?
        """,
        (older_version_id,),
    ).fetchall()
    newer = connection.execute(
        """
        SELECT e.id, e.excerpt, e.run_ordinal
        FROM evidence e
        JOIN processing_runs pr
          ON pr.id = e.processing_run_id AND pr.is_current = 1
        WHERE e.document_version_id = ?
        """,
        (newer_version_id,),
    ).fetchall()
    created: list[str] = []
    now = utc_now()
    for old, new in _candidate_pairs(older, newer):
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
    old_values = _meaningful_values(older)
    new_values = _meaningful_values(newer)
    if similarity >= 0.65 and (old_values or new_values) and old_values != new_values:
        return (
            "value_change",
            round(similarity, 6),
            "相似原子证据中的金额、比例、期限或数量发生变化，需确认适用范围。",
        )
    return None


def _normalize(value: str) -> str:
    return re.sub(r"[\W_]+", "", value.lower(), flags=re.UNICODE)


def _polarity(value: str) -> str | None:
    if any(token in value for token in _NEGATIVE_POLARITY):
        return "negative"
    if any(token in value for token in _POSITIVE_POLARITY):
        return "positive"
    return None


_DATE_PATTERN = re.compile(
    r"(?:\d{4}[年./-]\d{1,2}(?:[月./-]\d{1,2}日?)?|\d{1,2}月\d{1,2}日)"
)
_VERSION_PATTERN = re.compile(
    r"(?:\b[vV]\s*\d+(?:\.\d+){1,3}\b|版本\s*\d+(?:\.\d+){0,3})"
)
_REFERENCE_PATTERN = re.compile(r"第\s*\d+\s*[章节条款项]")
_MEANINGFUL_VALUE_PATTERN = re.compile(
    r"\d+(?:\.\d+)?\s*(?:%|％|亿元|万元|元|个工作日|工作日|天|小时|分钟|秒|人|份|次|项|套|台|个)"
)
_NEGATIVE_POLARITY = ("禁止", "不得", "不允许", "不应", "无需", "不必", "不是", "禁用")
_POSITIVE_POLARITY = ("允许", "可以", "应当", "应该", "必须", "需要", "启用")


def _meaningful_values(value: str) -> tuple[str, ...]:
    without_metadata = _DATE_PATTERN.sub("", value)
    without_metadata = _VERSION_PATTERN.sub("", without_metadata)
    without_metadata = _REFERENCE_PATTERN.sub("", without_metadata)
    return tuple(
        sorted(
            re.sub(r"\s+", "", match).replace("％", "%")
            for match in _MEANINGFUL_VALUE_PATTERN.findall(without_metadata)
        )
    )


def _comparison_signature(value: str) -> str:
    stable = _DATE_PATTERN.sub("", value)
    stable = _VERSION_PATTERN.sub("", stable)
    stable = _REFERENCE_PATTERN.sub("", stable)
    stable = _MEANINGFUL_VALUE_PATTERN.sub("", stable)
    for token in (*_NEGATIVE_POLARITY, *_POSITIVE_POLARITY):
        stable = stable.replace(token, "")
    return _normalize(stable)


def _candidate_pairs(older, newer):
    newer_by_ordinal = {row["run_ordinal"]: row for row in newer}
    newer_by_signature: dict[str, list] = {}
    for row in newer:
        signature = _comparison_signature(row["excerpt"])
        if len(signature) >= 4:
            newer_by_signature.setdefault(signature, []).append(row)

    yielded: set[tuple[str, str]] = set()
    for old in older:
        candidates = [
            newer_by_ordinal[ordinal]
            for ordinal in range(old["run_ordinal"] - 2, old["run_ordinal"] + 3)
            if ordinal in newer_by_ordinal
        ]
        signature = _comparison_signature(old["excerpt"])
        signature_matches = newer_by_signature.get(signature, ())
        candidates.extend(
            sorted(
                signature_matches,
                key=lambda row: abs(row["run_ordinal"] - old["run_ordinal"]),
            )[:3]
        )
        for new in candidates:
            pair = (old["id"], new["id"])
            if pair in yielded:
                continue
            yielded.add(pair)
            yield old, new
