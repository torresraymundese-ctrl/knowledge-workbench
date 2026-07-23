from __future__ import annotations

import sqlite3
import unicodedata

from .audit import record_event
from .database import Database
from .errors import KnowledgeWorkbenchError
from .utils import new_id, sha256_text, utc_now


ENTITY_TYPES = (
    "person",
    "organization",
    "project",
    "product",
    "location",
    "concept",
    "other",
)


def normalize_entity_name(value: str) -> str:
    cleaned = _required_text(value, "实体名称或别名")
    return " ".join(unicodedata.normalize("NFKC", cleaned).casefold().split())


def create_entity(
    database: Database,
    canonical_name: str,
    entity_type: str,
    *,
    actor: str,
) -> str:
    try:
        with database.transaction() as connection:
            entity_id = _create_entity_in_transaction(
                connection,
                canonical_name,
                entity_type,
                actor=actor,
            )
    except sqlite3.IntegrityError as exc:
        raise KnowledgeWorkbenchError("同类型的规范实体名称或别名已存在") from exc
    return entity_id


def add_entity_alias(
    database: Database,
    entity_id: str,
    alias: str,
    *,
    actor: str,
) -> str:
    with database.transaction() as connection:
        return _add_entity_alias_in_transaction(
            connection,
            entity_id,
            alias,
            actor=actor,
        )


def _create_entity_in_transaction(
    connection: sqlite3.Connection,
    canonical_name: str,
    entity_type: str,
    *,
    actor: str,
) -> str:
    canonical_name = _required_text(canonical_name, "规范实体名称")
    entity_type = _entity_type(entity_type)
    actor = _required_text(actor, "actor")
    normalized_name = normalize_entity_name(canonical_name)
    entity_id = new_id("entity")
    alias_id = new_id("alias")
    now = utc_now()
    connection.execute(
        """
        INSERT INTO canonical_entities(
            id, canonical_name, normalized_name, entity_type,
            status, created_by, created_at, updated_at
        ) VALUES (?, ?, ?, ?, 'active', ?, ?, ?)
        """,
        (
            entity_id,
            canonical_name,
            normalized_name,
            entity_type,
            actor,
            now,
            now,
        ),
    )
    connection.execute(
        """
        INSERT INTO entity_aliases(
            id, entity_id, entity_type, alias, normalized_alias,
            is_canonical, created_by, created_at
        ) VALUES (?, ?, ?, ?, ?, 1, ?, ?)
        """,
        (
            alias_id,
            entity_id,
            entity_type,
            canonical_name,
            normalized_name,
            actor,
            now,
        ),
    )
    record_event(
        connection,
        "canonical_entity_created",
        "canonical_entity",
        entity_id,
        actor=actor,
        details={
            "entity_type": entity_type,
            "canonical_name_sha256": sha256_text(canonical_name),
        },
    )
    return entity_id


def _add_entity_alias_in_transaction(
    connection: sqlite3.Connection,
    entity_id: str,
    alias: str,
    *,
    actor: str,
) -> str:
    alias = _required_text(alias, "实体别名")
    actor = _required_text(actor, "actor")
    normalized_alias = normalize_entity_name(alias)
    entity = _active_entity(connection, entity_id)
    existing = connection.execute(
        """
        SELECT id, entity_id FROM entity_aliases
        WHERE entity_type = ? AND normalized_alias = ?
        """,
        (entity["entity_type"], normalized_alias),
    ).fetchone()
    if existing:
        if existing["entity_id"] == entity_id:
            return existing["id"]
        raise KnowledgeWorkbenchError("该别名已指向同类型的另一个规范实体")
    alias_id = new_id("alias")
    connection.execute(
        """
        INSERT INTO entity_aliases(
            id, entity_id, entity_type, alias, normalized_alias,
            is_canonical, created_by, created_at
        ) VALUES (?, ?, ?, ?, ?, 0, ?, ?)
        """,
        (
            alias_id,
            entity_id,
            entity["entity_type"],
            alias,
            normalized_alias,
            actor,
            utc_now(),
        ),
    )
    record_event(
        connection,
        "entity_alias_added",
        "canonical_entity",
        entity_id,
        actor=actor,
        details={
            "alias_id": alias_id,
            "alias_sha256": sha256_text(alias),
        },
    )
    return alias_id


def remove_entity_alias(
    database: Database,
    entity_id: str,
    alias: str,
    *,
    actor: str,
) -> None:
    alias = _required_text(alias, "实体别名")
    actor = _required_text(actor, "actor")
    normalized_alias = normalize_entity_name(alias)
    try:
        with database.transaction() as connection:
            entity = _active_entity(connection, entity_id)
            row = connection.execute(
                """
                SELECT id, is_canonical, is_merge_anchor FROM entity_aliases
                WHERE entity_id = ? AND entity_type = ? AND normalized_alias = ?
                """,
                (entity_id, entity["entity_type"], normalized_alias),
            ).fetchone()
            if not row:
                raise KnowledgeWorkbenchError("该实体没有此别名")
            if row["is_canonical"]:
                raise KnowledgeWorkbenchError("规范实体名称不能作为普通别名移除")
            if row["is_merge_anchor"]:
                raise KnowledgeWorkbenchError("已合并源实体的规范名称别名不能移除")
            connection.execute("DELETE FROM entity_aliases WHERE id = ?", (row["id"],))
            record_event(
                connection,
                "entity_alias_removed",
                "canonical_entity",
                entity_id,
                actor=actor,
                details={
                    "alias_id": row["id"],
                    "alias_sha256": sha256_text(alias),
                },
            )
    except sqlite3.IntegrityError as exc:
        raise KnowledgeWorkbenchError("该别名仍被证据关联使用，请先解除关联") from exc


def link_evidence_entity(
    database: Database,
    entity_id: str,
    evidence_id: str,
    mention: str,
    *,
    actor: str,
) -> bool:
    with database.transaction() as connection:
        return _link_evidence_entity_in_transaction(
            connection,
            entity_id,
            evidence_id,
            mention,
            actor=actor,
        )


def _link_evidence_entity_in_transaction(
    connection: sqlite3.Connection,
    entity_id: str,
    evidence_id: str,
    mention: str,
    *,
    actor: str,
) -> bool:
    mention = _required_text(mention, "证据中的实体提及")
    actor = _required_text(actor, "actor")
    normalized_mention = normalize_entity_name(mention)
    entity = _active_entity(connection, entity_id)
    alias = connection.execute(
        """
        SELECT id FROM entity_aliases
        WHERE entity_id = ? AND entity_type = ? AND normalized_alias = ?
        """,
        (entity_id, entity["entity_type"], normalized_mention),
    ).fetchone()
    if not alias:
        raise KnowledgeWorkbenchError("该提及尚未登记为此实体的别名")
    evidence = connection.execute(
        """
        SELECT e.excerpt, d.classification
        FROM evidence e
        JOIN processing_runs pr
          ON pr.id = e.processing_run_id AND pr.is_current = 1
        JOIN document_versions dv ON dv.id = e.document_version_id
        JOIN documents d
          ON d.id = dv.document_id AND d.current_version_id = dv.id
        WHERE e.id = ?
        """,
        (evidence_id,),
    ).fetchone()
    if not evidence:
        raise KnowledgeWorkbenchError("证据不存在或不是当前文件版本和当前处理运行")
    if mention not in evidence["excerpt"]:
        raise KnowledgeWorkbenchError("实体提及必须逐字出现在证据原文中")
    cursor = connection.execute(
        """
        INSERT OR IGNORE INTO evidence_entity_mentions(
            evidence_id, entity_id, alias_id, mention_text, created_by, created_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (evidence_id, entity_id, alias["id"], mention, actor, utc_now()),
    )
    if cursor.rowcount == 0:
        return False
    record_event(
        connection,
        "evidence_entity_linked",
        "canonical_entity",
        entity_id,
        actor=actor,
        details={
            "evidence_id": evidence_id,
            "alias_id": alias["id"],
            "mention_sha256": sha256_text(mention),
            "classification": evidence["classification"],
        },
    )
    return True


def unlink_evidence_entity(
    database: Database,
    entity_id: str,
    evidence_id: str,
    *,
    actor: str,
) -> int:
    actor = _required_text(actor, "actor")
    with database.transaction() as connection:
        _active_entity(connection, entity_id)
        rows = connection.execute(
            """
            SELECT alias_id, mention_text FROM evidence_entity_mentions
            WHERE evidence_id = ? AND entity_id = ?
            """,
            (evidence_id, entity_id),
        ).fetchall()
        if not rows:
            raise KnowledgeWorkbenchError("该证据没有关联此规范实体")
        relationship = connection.execute(
            """
            SELECT er.id
            FROM entity_relationship_evidence ere
            JOIN entity_relationships er ON er.id = ere.relationship_id
            WHERE ere.evidence_id = ?
              AND (
                  er.source_entity_id = ?
                  OR er.target_entity_id = ?
              )
            ORDER BY er.id
            LIMIT 1
            """,
            (evidence_id, entity_id, entity_id),
        ).fetchone()
        if relationship:
            raise KnowledgeWorkbenchError(
                "该实体提及已被业务关系历史引用，不能解除："
                + relationship["id"]
            )
        connection.execute(
            """
            DELETE FROM evidence_entity_mentions
            WHERE evidence_id = ? AND entity_id = ?
            """,
            (evidence_id, entity_id),
        )
        record_event(
            connection,
            "evidence_entity_unlinked",
            "canonical_entity",
            entity_id,
            actor=actor,
            details={
                "evidence_id": evidence_id,
                "removed_count": len(rows),
                "mention_sha256": sorted(sha256_text(row["mention_text"]) for row in rows),
            },
        )
    return len(rows)


def list_entities(
    database: Database,
    *,
    entity_type: str | None = None,
    limit: int = 50,
) -> list[dict]:
    if limit <= 0 or limit > 500:
        raise KnowledgeWorkbenchError("实体列表 limit 必须在 1 到 500 之间")
    parameters: list[object] = []
    where = ""
    if entity_type is not None:
        where = "WHERE ce.entity_type = ?"
        parameters.append(_entity_type(entity_type))
    parameters.append(limit)
    with database.connect() as connection:
        rows = connection.execute(
            f"""
            SELECT ce.id, ce.canonical_name, ce.entity_type, ce.status,
                   (SELECT COUNT(*) FROM entity_aliases ea
                    WHERE ea.entity_id = ce.id) AS alias_count,
                   (SELECT COUNT(*) FROM evidence_entity_mentions eem
                    JOIN evidence e ON e.id = eem.evidence_id
                    JOIN processing_runs pr
                      ON pr.id = e.processing_run_id AND pr.is_current = 1
                    JOIN document_versions dv ON dv.id = e.document_version_id
                    JOIN documents d
                      ON d.id = dv.document_id AND d.current_version_id = dv.id
                    WHERE eem.entity_id = ce.id) AS current_evidence_count,
                   (SELECT emr.target_entity_id
                     FROM entity_merge_requests emr
                     WHERE emr.source_entity_id = ce.id
                       AND emr.status = 'merged'
                     ORDER BY emr.reviewed_at DESC, emr.id DESC LIMIT 1)
                       AS merged_into_entity_id
            FROM canonical_entities ce
            {where}
            ORDER BY ce.updated_at DESC, ce.canonical_name
            LIMIT ?
            """,
            parameters,
        ).fetchall()
    return [dict(row) for row in rows]


def get_entity(database: Database, entity_id: str) -> dict:
    with database.connect() as connection:
        entity = connection.execute(
            "SELECT * FROM canonical_entities WHERE id = ?", (entity_id,)
        ).fetchone()
        if not entity:
            raise KnowledgeWorkbenchError("规范实体不存在")
        aliases = connection.execute(
            """
            SELECT id, alias, is_canonical, is_merge_anchor, created_by, created_at
            FROM entity_aliases WHERE entity_id = ?
            ORDER BY is_canonical DESC, normalized_alias
            """,
            (entity_id,),
        ).fetchall()
        current_evidence_count = connection.execute(
            """
            SELECT COUNT(*) FROM evidence_entity_mentions eem
            JOIN evidence e ON e.id = eem.evidence_id
            JOIN processing_runs pr
              ON pr.id = e.processing_run_id AND pr.is_current = 1
            JOIN document_versions dv ON dv.id = e.document_version_id
            JOIN documents d
              ON d.id = dv.document_id AND d.current_version_id = dv.id
            WHERE eem.entity_id = ?
            """,
            (entity_id,),
        ).fetchone()[0]
        merged_into_entity_id = connection.execute(
            """
            SELECT target_entity_id FROM entity_merge_requests
            WHERE source_entity_id = ? AND status = 'merged'
            ORDER BY reviewed_at DESC, id DESC LIMIT 1
            """,
            (entity_id,),
        ).fetchone()
    payload = dict(entity)
    payload["aliases"] = [dict(row) for row in aliases]
    payload["current_evidence_count"] = current_evidence_count
    payload["merged_into_entity_id"] = (
        merged_into_entity_id[0] if merged_into_entity_id else None
    )
    return payload


def _active_entity(connection: sqlite3.Connection, entity_id: str):
    row = connection.execute(
        "SELECT id, entity_type FROM canonical_entities WHERE id = ? AND status = 'active'",
        (entity_id,),
    ).fetchone()
    if not row:
        raise KnowledgeWorkbenchError("规范实体不存在或不是 active 状态")
    return row


def _entity_type(value: str) -> str:
    normalized = _required_text(value, "实体类型").casefold()
    if normalized not in ENTITY_TYPES:
        raise KnowledgeWorkbenchError(
            "实体类型必须是：" + ", ".join(ENTITY_TYPES)
        )
    return normalized


def _required_text(value: str, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise KnowledgeWorkbenchError(f"{label}不能为空")
    return value.strip()
