from __future__ import annotations

from .database import Database
from .entities import ENTITY_TYPES
from .errors import KnowledgeWorkbenchError


ENTITY_VISIBILITIES = (
    "public",
    "internal",
    "confidential",
    "restricted",
    "unclassified",
)
WEB_VISIBLE_ENTITY_CLASSIFICATIONS = frozenset(
    {"public", "internal", "confidential"}
)


def list_entity_visibility(
    database: Database,
    *,
    entity_type: str | None = None,
    web_visible_only: bool = False,
    limit: int = 100,
) -> list[dict]:
    entity_type = _optional_entity_type(entity_type)
    if limit <= 0 or limit > 500:
        raise KnowledgeWorkbenchError("实体可见密级 limit 必须在 1 到 500 之间")
    rows = _visibility_rows(
        database,
        status="active",
        entity_type=entity_type,
    )
    projected = [_visibility_projection(row) for row in rows]
    if web_visible_only:
        projected = [row for row in projected if row["web_visible"]]
    return projected[:limit]


def get_entity_visibility(database: Database, entity_id: str) -> dict:
    rows = _visibility_rows(database, entity_id=entity_id)
    if not rows:
        raise KnowledgeWorkbenchError("规范实体不存在")
    return _visibility_projection(rows[0])


def _visibility_rows(
    database: Database,
    *,
    entity_id: str | None = None,
    status: str | None = None,
    entity_type: str | None = None,
):
    where: list[str] = []
    parameters: list[object] = []
    if entity_id is not None:
        where.append("ce.id = ?")
        parameters.append(entity_id)
    if status is not None:
        where.append("ce.status = ?")
        parameters.append(status)
    if entity_type is not None:
        where.append("ce.entity_type = ?")
        parameters.append(entity_type)
    predicate = " AND ".join(where) if where else "1 = 1"
    with database.connect() as connection:
        return connection.execute(
            f"""
            SELECT ce.id, ce.canonical_name, ce.entity_type, ce.status,
                   COUNT(DISTINCT e.id) AS evidence_count,
                   COUNT(DISTINCT CASE WHEN d.classification = 'public'
                                  THEN e.id END) AS public_count,
                   COUNT(DISTINCT CASE WHEN d.classification = 'internal'
                                  THEN e.id END) AS internal_count,
                   COUNT(DISTINCT CASE WHEN d.classification = 'confidential'
                                  THEN e.id END) AS confidential_count,
                   COUNT(DISTINCT CASE WHEN d.classification = 'restricted'
                                  THEN e.id END) AS restricted_count
            FROM canonical_entities ce
            LEFT JOIN evidence_entity_mentions eem ON eem.entity_id = ce.id
            LEFT JOIN evidence e ON e.id = eem.evidence_id
            LEFT JOIN document_versions dv ON dv.id = e.document_version_id
            LEFT JOIN documents d ON d.id = dv.document_id
            WHERE {predicate}
            GROUP BY ce.id
            ORDER BY ce.entity_type, ce.canonical_name, ce.id
            """,
            parameters,
        ).fetchall()


def _visibility_projection(row) -> dict:
    counts = {
        "public": row["public_count"],
        "internal": row["internal_count"],
        "confidential": row["confidential_count"],
        "restricted": row["restricted_count"],
    }
    if counts["restricted"]:
        visibility = "restricted"
    elif counts["confidential"]:
        visibility = "confidential"
    elif counts["internal"]:
        visibility = "internal"
    elif counts["public"]:
        visibility = "public"
    else:
        visibility = "unclassified"
    return {
        "entity_id": row["id"],
        "canonical_name": row["canonical_name"],
        "entity_type": row["entity_type"],
        "status": row["status"],
        "visibility": visibility,
        "web_visible": (
            row["status"] == "active"
            and visibility in WEB_VISIBLE_ENTITY_CLASSIFICATIONS
        ),
        "evidence_count": row["evidence_count"],
        "evidence_by_classification": counts,
    }


def _optional_entity_type(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip().casefold()
    if normalized not in ENTITY_TYPES:
        raise KnowledgeWorkbenchError(
            "实体类型必须是：" + ", ".join(ENTITY_TYPES)
        )
    return normalized
