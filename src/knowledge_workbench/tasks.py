from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from .audit import record_event
from .database import Database
from .errors import InvalidTransitionError, KnowledgeWorkbenchError
from .models import ClaimedTask, TaskStatus
from .utils import new_id, utc_now


def enqueue_task(
    database: Database,
    task_type: str,
    payload: dict[str, Any],
    *,
    idempotency_key: str | None = None,
    priority: int = 0,
    max_attempts: int = 3,
    actor: str = "system",
) -> str:
    if max_attempts < 1:
        raise KnowledgeWorkbenchError("max_attempts 必须大于 0")
    now = utc_now()
    task_id = new_id("task")
    with database.transaction() as connection:
        if idempotency_key:
            existing = connection.execute(
                "SELECT id FROM tasks WHERE idempotency_key = ?", (idempotency_key,)
            ).fetchone()
            if existing:
                return existing["id"]
        connection.execute(
            """
            INSERT INTO tasks(
                id, task_type, status, payload_json, idempotency_key, priority,
                attempts, max_attempts, created_at, updated_at
            ) VALUES (?, ?, 'pending', ?, ?, ?, 0, ?, ?, ?)
            """,
            (
                task_id,
                task_type,
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
                idempotency_key,
                priority,
                max_attempts,
                now,
                now,
            ),
        )
        record_event(
            connection,
            "task_enqueued",
            "task",
            task_id,
            actor=actor,
            details={"task_type": task_type, "idempotency_key": idempotency_key},
        )
    return task_id


def claim_next_task(
    database: Database,
    *,
    worker: str,
    lease_seconds: int = 300,
) -> ClaimedTask | None:
    if lease_seconds < 1:
        raise KnowledgeWorkbenchError("lease_seconds 必须大于 0")
    now = utc_now()
    lease_expires = _after_seconds(lease_seconds)
    with database.transaction() as connection:
        _recover_expired_in_transaction(connection, now, actor=worker)
        row = connection.execute(
            """
            SELECT * FROM tasks
            WHERE status IN ('pending', 'retrying')
              AND attempts < max_attempts
              AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
            ORDER BY priority DESC, created_at
            LIMIT 1
            """,
            (now,),
        ).fetchone()
        if not row:
            return None
        connection.execute(
            """
            UPDATE tasks
            SET status = 'running', attempts = attempts + 1,
                lease_expires_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (lease_expires, now, row["id"]),
        )
        record_event(
            connection,
            "task_claimed",
            "task",
            row["id"],
            actor=worker,
            details={"attempt": row["attempts"] + 1, "lease_expires_at": lease_expires},
        )
        return ClaimedTask(
            id=row["id"],
            task_type=row["task_type"],
            payload=json.loads(row["payload_json"]),
            attempts=row["attempts"] + 1,
            max_attempts=row["max_attempts"],
        )


def complete_task(
    database: Database,
    task_id: str,
    result: dict[str, Any] | None,
    *,
    worker: str,
) -> None:
    now = utc_now()
    with database.transaction() as connection:
        row = _task(connection, task_id)
        if TaskStatus(row["status"]) is not TaskStatus.RUNNING:
            raise InvalidTransitionError("只有 running 任务可以完成")
        connection.execute(
            """
            UPDATE tasks
            SET status = 'done', result_json = ?, lease_expires_at = NULL,
                last_error = NULL, updated_at = ?
            WHERE id = ?
            """,
            (json.dumps(result or {}, ensure_ascii=False, sort_keys=True), now, task_id),
        )
        record_event(
            connection, "task_completed", "task", task_id, actor=worker
        )


def fail_task(
    database: Database,
    task_id: str,
    error: str,
    *,
    worker: str,
    base_delay_seconds: int = 30,
    retryable: bool = True,
) -> TaskStatus:
    now = utc_now()
    with database.transaction() as connection:
        row = _task(connection, task_id)
        if TaskStatus(row["status"]) is not TaskStatus.RUNNING:
            raise InvalidTransitionError("只有 running 任务可以报告失败")
        if retryable and row["attempts"] < row["max_attempts"]:
            target = TaskStatus.RETRYING
            delay = base_delay_seconds * (2 ** max(row["attempts"] - 1, 0))
            next_attempt_at = _after_seconds(delay)
        else:
            target = TaskStatus.FAILED
            next_attempt_at = None
        connection.execute(
            """
            UPDATE tasks
            SET status = ?, next_attempt_at = ?, lease_expires_at = NULL,
                last_error = ?, updated_at = ?
            WHERE id = ?
            """,
            (target.value, next_attempt_at, error[:4000], now, task_id),
        )
        record_event(
            connection,
            "task_failed" if target is TaskStatus.FAILED else "task_retry_scheduled",
            "task",
            task_id,
            actor=worker,
            details={"attempt": row["attempts"], "next_attempt_at": next_attempt_at},
        )
        return target


def retry_failed_task(database: Database, task_id: str, *, actor: str) -> None:
    now = utc_now()
    with database.transaction() as connection:
        row = _task(connection, task_id)
        if TaskStatus(row["status"]) is not TaskStatus.FAILED:
            raise InvalidTransitionError("只有 failed 任务可以人工重试")
        connection.execute(
            """
            UPDATE tasks
            SET status = 'retrying', attempts = 0, next_attempt_at = ?,
                lease_expires_at = NULL, updated_at = ?
            WHERE id = ?
            """,
            (now, now, task_id),
        )
        record_event(
            connection, "task_manual_retry", "task", task_id, actor=actor
        )


def recover_expired_tasks(database: Database, *, actor: str = "recovery") -> int:
    now = utc_now()
    with database.transaction() as connection:
        return _recover_expired_in_transaction(connection, now, actor=actor)


def _recover_expired_in_transaction(connection, now: str, *, actor: str) -> int:
    rows = connection.execute(
        """
        SELECT * FROM tasks
        WHERE status = 'running' AND lease_expires_at IS NOT NULL AND lease_expires_at <= ?
        """,
        (now,),
    ).fetchall()
    for row in rows:
        target = "retrying" if row["attempts"] < row["max_attempts"] else "failed"
        connection.execute(
            """
            UPDATE tasks
            SET status = ?, next_attempt_at = ?, lease_expires_at = NULL,
                last_error = 'worker lease expired', updated_at = ?
            WHERE id = ?
            """,
            (target, now if target == "retrying" else None, now, row["id"]),
        )
        record_event(
            connection,
            "task_lease_recovered",
            "task",
            row["id"],
            actor=actor,
            details={"to": target},
        )
    return len(rows)


def _task(connection, task_id: str):
    row = connection.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if not row:
        raise KnowledgeWorkbenchError(f"任务不存在：{task_id}")
    return row


def _after_seconds(seconds: int) -> str:
    return (datetime.now(UTC) + timedelta(seconds=seconds)).isoformat(timespec="seconds")
