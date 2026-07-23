from __future__ import annotations

import sqlite3

from .audit import record_event
from .database import Database
from .errors import KnowledgeWorkbenchError
from .utils import new_id, sha256_text, utc_now


MERGE_REQUEST_STATUSES = ("reviewing", "merged", "rejected")
MERGE_REVIEW_DECISIONS = ("approve", "reject")


def propose_entity_merge(
    database: Database,
    source_entity_id: str,
    target_entity_id: str,
    *,
    actor: str,
    note: str,
) -> str:
    actor = _required_actor(actor)
    note_hash = sha256_text(_required_note(note, "合并提议说明"))
    if source_entity_id == target_entity_id:
        raise KnowledgeWorkbenchError("源实体和目标实体不能相同")
    request_id = new_id("entitymerge")
    now = utc_now()
    with database.transaction() as connection:
        source = _active_entity(connection, source_entity_id)
        target = _active_entity(connection, target_entity_id)
        if source["entity_type"] != target["entity_type"]:
            raise KnowledgeWorkbenchError("只有相同类型的规范实体可以提交合并复核")
        open_request = connection.execute(
            """
            SELECT id FROM entity_merge_requests
            WHERE status = 'reviewing'
              AND (
                  source_entity_id IN (?, ?)
                  OR target_entity_id IN (?, ?)
              )
            LIMIT 1
            """,
            (
                source_entity_id,
                target_entity_id,
                source_entity_id,
                target_entity_id,
            ),
        ).fetchone()
        if open_request:
            raise KnowledgeWorkbenchError(
                f"所选实体已有待复核合并请求：{open_request['id']}"
            )
        connection.execute(
            """
            INSERT INTO entity_merge_requests(
                id, source_entity_id, target_entity_id, entity_type,
                status, proposed_by, proposal_note_sha256,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, 'reviewing', ?, ?, ?, ?)
            """,
            (
                request_id,
                source_entity_id,
                target_entity_id,
                source["entity_type"],
                actor,
                note_hash,
                now,
                now,
            ),
        )
        record_event(
            connection,
            "entity_merge_proposed",
            "entity_merge_request",
            request_id,
            actor=actor,
            details={
                "source_entity_id": source_entity_id,
                "target_entity_id": target_entity_id,
                "entity_type": source["entity_type"],
                "proposal_note_sha256": note_hash,
            },
        )
    return request_id


def list_entity_merge_requests(
    database: Database,
    *,
    status: str = "reviewing",
    limit: int = 50,
) -> list[dict]:
    if status not in MERGE_REQUEST_STATUSES:
        raise KnowledgeWorkbenchError("实体合并请求状态无效")
    if limit <= 0 or limit > 500:
        raise KnowledgeWorkbenchError("实体合并请求 limit 必须在 1 到 500 之间")
    with database.connect() as connection:
        rows = connection.execute(
            """
            SELECT emr.id, emr.status, emr.source_entity_id,
                   source.canonical_name AS source_name,
                   source.status AS source_status,
                   emr.target_entity_id,
                   target.canonical_name AS target_name,
                   target.status AS target_status,
                   emr.entity_type, emr.proposed_by, emr.reviewed_by,
                   emr.created_at, emr.reviewed_at
            FROM entity_merge_requests emr
            JOIN canonical_entities source ON source.id = emr.source_entity_id
            JOIN canonical_entities target ON target.id = emr.target_entity_id
            WHERE emr.status = ?
            ORDER BY emr.created_at, emr.id
            LIMIT ?
            """,
            (status, limit),
        ).fetchall()
    return [dict(row) for row in rows]


def review_entity_merge(
    database: Database,
    request_id: str,
    decision: str,
    *,
    actor: str,
    note: str,
) -> dict:
    actor = _required_actor(actor)
    decision = decision.strip().casefold()
    if decision not in MERGE_REVIEW_DECISIONS:
        raise KnowledgeWorkbenchError("实体合并复核决定必须是 approve 或 reject")
    note_hash = sha256_text(_required_note(note, "合并复核意见"))
    try:
        with database.transaction() as connection:
            request = connection.execute(
                """
                SELECT * FROM entity_merge_requests
                WHERE id = ? AND status = 'reviewing'
                """,
                (request_id,),
            ).fetchone()
            if not request:
                raise KnowledgeWorkbenchError("实体合并请求不存在或已完成复核")
            if request["proposed_by"] == actor:
                raise KnowledgeWorkbenchError("实体合并必须由不同于提议人的操作者复核")
            now = utc_now()
            if decision == "reject":
                connection.execute(
                    """
                    UPDATE entity_merge_requests
                    SET status = 'rejected', reviewed_by = ?,
                        review_note_sha256 = ?, reviewed_at = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (actor, note_hash, now, now, request_id),
                )
                record_event(
                    connection,
                    "entity_merge_rejected",
                    "entity_merge_request",
                    request_id,
                    actor=actor,
                    details={
                        "source_entity_id": request["source_entity_id"],
                        "target_entity_id": request["target_entity_id"],
                        "review_note_sha256": note_hash,
                    },
                )
                return {
                    "request_id": request_id,
                    "status": "rejected",
                    "source_entity_id": request["source_entity_id"],
                    "target_entity_id": request["target_entity_id"],
                }

            source = _active_entity(connection, request["source_entity_id"])
            target = _active_entity(connection, request["target_entity_id"])
            if (
                source["entity_type"] != request["entity_type"]
                or target["entity_type"] != request["entity_type"]
            ):
                raise KnowledgeWorkbenchError("合并请求中的实体类型已不一致")
            relationship_history = connection.execute(
                """
                SELECT id FROM entity_relationships
                WHERE source_entity_id = ? OR target_entity_id = ?
                ORDER BY id
                LIMIT 1
                """,
                (
                    request["source_entity_id"],
                    request["source_entity_id"],
                ),
            ).fetchone()
            if relationship_history:
                raise KnowledgeWorkbenchError(
                    "源实体已有业务关系历史；为避免改写历史关系语义，不能自动合并："
                    + relationship_history["id"]
                )
            aliases = connection.execute(
                """
                SELECT id FROM entity_aliases
                WHERE entity_id = ? ORDER BY id
                """,
                (request["source_entity_id"],),
            ).fetchall()
            mentions = connection.execute(
                """
                SELECT evidence_id, alias_id, mention_text, created_by, created_at
                FROM evidence_entity_mentions
                WHERE entity_id = ?
                ORDER BY evidence_id, alias_id
                """,
                (request["source_entity_id"],),
            ).fetchall()
            connection.execute(
                "DELETE FROM evidence_entity_mentions WHERE entity_id = ?",
                (request["source_entity_id"],),
            )
            connection.execute(
                """
                UPDATE entity_aliases
                SET entity_id = ?,
                    is_merge_anchor = CASE
                        WHEN is_canonical = 1 OR is_merge_anchor = 1 THEN 1
                        ELSE 0
                    END,
                    is_canonical = 0
                WHERE entity_id = ?
                """,
                (request["target_entity_id"], request["source_entity_id"]),
            )
            connection.executemany(
                """
                INSERT INTO evidence_entity_mentions(
                    evidence_id, entity_id, alias_id, mention_text,
                    created_by, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        mention["evidence_id"],
                        request["target_entity_id"],
                        mention["alias_id"],
                        mention["mention_text"],
                        mention["created_by"],
                        mention["created_at"],
                    )
                    for mention in mentions
                ],
            )
            candidate_cursor = connection.execute(
                """
                UPDATE entity_candidates SET resolved_entity_id = ?
                WHERE status = 'accepted' AND resolved_entity_id = ?
                """,
                (request["target_entity_id"], request["source_entity_id"]),
            )
            connection.execute(
                """
                UPDATE canonical_entities
                SET status = 'archived', updated_at = ?
                WHERE id = ?
                """,
                (now, request["source_entity_id"]),
            )
            connection.execute(
                """
                UPDATE entity_merge_requests
                SET status = 'merged', reviewed_by = ?,
                    review_note_sha256 = ?, reviewed_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (actor, note_hash, now, now, request_id),
            )
            details = {
                "source_entity_id": request["source_entity_id"],
                "target_entity_id": request["target_entity_id"],
                "entity_type": request["entity_type"],
                "alias_count": len(aliases),
                "evidence_mention_count": len(mentions),
                "accepted_candidate_count": candidate_cursor.rowcount,
                "review_note_sha256": note_hash,
            }
            record_event(
                connection,
                "canonical_entities_merged",
                "canonical_entity",
                request["target_entity_id"],
                actor=actor,
                details={"merge_request_id": request_id, **details},
            )
            record_event(
                connection,
                "entity_merge_approved",
                "entity_merge_request",
                request_id,
                actor=actor,
                details=details,
            )
            return {
                "request_id": request_id,
                "status": "merged",
                "source_entity_id": request["source_entity_id"],
                "target_entity_id": request["target_entity_id"],
                "alias_count": len(aliases),
                "evidence_mention_count": len(mentions),
                "accepted_candidate_count": candidate_cursor.rowcount,
            }
    except sqlite3.IntegrityError as exc:
        raise KnowledgeWorkbenchError(
            "实体合并违反别名、证据关联或类型完整性约束，已整体回滚"
        ) from exc


def _active_entity(connection: sqlite3.Connection, entity_id: str):
    row = connection.execute(
        """
        SELECT id, entity_type FROM canonical_entities
        WHERE id = ? AND status = 'active'
        """,
        (entity_id,),
    ).fetchone()
    if not row:
        raise KnowledgeWorkbenchError("规范实体不存在或不是 active 状态")
    return row


def _required_actor(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise KnowledgeWorkbenchError("actor 不能为空")
    actor = value.strip()
    if len(actor) > 80:
        raise KnowledgeWorkbenchError("actor 不能超过 80 个字符")
    return actor


def _required_note(value: str, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise KnowledgeWorkbenchError(f"{label}不能为空")
    note = value.strip()
    if len(note) > 2000:
        raise KnowledgeWorkbenchError(f"{label}不能超过 2000 个字符")
    return note
