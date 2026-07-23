from __future__ import annotations

import re
import sqlite3
from collections import defaultdict, deque
from typing import Iterable

from .audit import record_event
from .database import Database
from .errors import KnowledgeWorkbenchError
from .utils import new_id, sha256_text, utc_now


RELATIONSHIP_STATUSES = ("active", "retracted")
RELATION_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
CLASSIFICATION_ORDER = ("public", "internal", "confidential", "restricted")
PATH_MAX_DEPTH = 4
PATH_MAX_RESULTS = 100
PATH_MAX_EXPANSIONS = 20_000


def create_relation_type(
    database: Database,
    relation_key: str,
    label: str,
    *,
    actor: str,
    directed: bool = True,
    inverse_label: str | None = None,
) -> str:
    actor = _required_actor(actor)
    relation_key = _required_relation_key(relation_key)
    label = _required_text(label, "关系类型名称", maximum=120)
    inverse_label = _optional_text(
        inverse_label, "反向关系名称", maximum=120
    )
    now = utc_now()
    try:
        with database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO entity_relation_types(
                    relation_key, label, inverse_label, directed, status,
                    created_by, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'active', ?, ?, ?)
                """,
                (
                    relation_key,
                    label,
                    inverse_label,
                    int(bool(directed)),
                    actor,
                    now,
                    now,
                ),
            )
            record_event(
                connection,
                "entity_relation_type_created",
                "entity_relation_type",
                relation_key,
                actor=actor,
                details={
                    "relation_key": relation_key,
                    "label_sha256": sha256_text(label),
                    "inverse_label_sha256": (
                        sha256_text(inverse_label) if inverse_label else None
                    ),
                    "directed": bool(directed),
                },
            )
    except sqlite3.IntegrityError as exc:
        raise KnowledgeWorkbenchError(
            f"关系类型已存在或字段无效：{relation_key}"
        ) from exc
    return relation_key


def list_relation_types(
    database: Database,
    *,
    status: str = "active",
    limit: int = 100,
) -> list[dict]:
    if status not in {"active", "archived"}:
        raise KnowledgeWorkbenchError("关系类型状态必须是 active 或 archived")
    _validate_limit(limit)
    with database.connect() as connection:
        rows = connection.execute(
            """
            SELECT relation_key, label, inverse_label, directed, status,
                   created_by, created_at, updated_at
            FROM entity_relation_types
            WHERE status = ?
            ORDER BY relation_key
            LIMIT ?
            """,
            (status, limit),
        ).fetchall()
    return [
        {
            **dict(row),
            "directed": bool(row["directed"]),
        }
        for row in rows
    ]


def create_entity_relationship(
    database: Database,
    relation_key: str,
    source_entity_id: str,
    target_entity_id: str,
    evidence_ids: Iterable[str],
    *,
    actor: str,
    note: str,
) -> str:
    actor = _required_actor(actor)
    relation_key = _required_relation_key(relation_key)
    note_hash = sha256_text(
        _required_text(note, "业务关系登记说明", maximum=2000)
    )
    source_entity_id = source_entity_id.strip()
    target_entity_id = target_entity_id.strip()
    if not source_entity_id or not target_entity_id:
        raise KnowledgeWorkbenchError("业务关系源实体和目标实体不能为空")
    if source_entity_id == target_entity_id:
        raise KnowledgeWorkbenchError("业务关系源实体和目标实体不能相同")
    support_ids = sorted(
        {
            item.strip()
            for item in evidence_ids
            if isinstance(item, str) and item.strip()
        }
    )
    if not support_ids:
        raise KnowledgeWorkbenchError("业务关系必须至少引用一条证据")
    relationship_id = new_id("entityrel")
    now = utc_now()
    try:
        with database.transaction() as connection:
            relation_type = connection.execute(
                """
                SELECT relation_key, directed FROM entity_relation_types
                WHERE relation_key = ? AND status = 'active'
                """,
                (relation_key,),
            ).fetchone()
            if not relation_type:
                raise KnowledgeWorkbenchError("关系类型不存在或不是 active 状态")
            _active_entity(connection, source_entity_id)
            _active_entity(connection, target_entity_id)
            if not relation_type["directed"] and source_entity_id > target_entity_id:
                source_entity_id, target_entity_id = (
                    target_entity_id,
                    source_entity_id,
                )
            for evidence_id in support_ids:
                _validate_relationship_evidence(
                    connection,
                    evidence_id,
                    source_entity_id,
                    target_entity_id,
                )
            connection.execute(
                """
                INSERT INTO entity_relationships(
                    id, relation_key, source_entity_id, target_entity_id,
                    status, created_by, creation_note_sha256,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'active', ?, ?, ?, ?)
                """,
                (
                    relationship_id,
                    relation_key,
                    source_entity_id,
                    target_entity_id,
                    actor,
                    note_hash,
                    now,
                    now,
                ),
            )
            connection.executemany(
                """
                INSERT INTO entity_relationship_evidence(
                    relationship_id, evidence_id, added_by, added_at
                ) VALUES (?, ?, ?, ?)
                """,
                [
                    (relationship_id, evidence_id, actor, now)
                    for evidence_id in support_ids
                ],
            )
            record_event(
                connection,
                "entity_relationship_created",
                "entity_relationship",
                relationship_id,
                actor=actor,
                details={
                    "relation_key": relation_key,
                    "source_entity_id": source_entity_id,
                    "target_entity_id": target_entity_id,
                    "supporting_evidence_ids": support_ids,
                    "creation_note_sha256": note_hash,
                },
            )
    except sqlite3.IntegrityError as exc:
        raise KnowledgeWorkbenchError(
            "相同方向的 active 业务关系已存在，或关系完整性校验失败"
        ) from exc
    return relationship_id


def retract_entity_relationship(
    database: Database,
    relationship_id: str,
    *,
    actor: str,
    note: str,
) -> dict:
    actor = _required_actor(actor)
    note_hash = sha256_text(
        _required_text(note, "业务关系撤销说明", maximum=2000)
    )
    now = utc_now()
    with database.transaction() as connection:
        relationship = connection.execute(
            """
            SELECT id, relation_key, source_entity_id, target_entity_id
            FROM entity_relationships
            WHERE id = ? AND status = 'active'
            """,
            (relationship_id,),
        ).fetchone()
        if not relationship:
            raise KnowledgeWorkbenchError("业务关系不存在或已撤销")
        connection.execute(
            """
            UPDATE entity_relationships
            SET status = 'retracted', retracted_by = ?,
                retraction_note_sha256 = ?, retracted_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (actor, note_hash, now, now, relationship_id),
        )
        record_event(
            connection,
            "entity_relationship_retracted",
            "entity_relationship",
            relationship_id,
            actor=actor,
            details={
                "relation_key": relationship["relation_key"],
                "source_entity_id": relationship["source_entity_id"],
                "target_entity_id": relationship["target_entity_id"],
                "retraction_note_sha256": note_hash,
            },
        )
    return {
        "relationship_id": relationship_id,
        "status": "retracted",
        "source_entity_id": relationship["source_entity_id"],
        "target_entity_id": relationship["target_entity_id"],
    }


def list_entity_relationships(
    database: Database,
    *,
    status: str = "active",
    relation_key: str | None = None,
    entity_id: str | None = None,
    limit: int = 100,
) -> list[dict]:
    if status not in RELATIONSHIP_STATUSES:
        raise KnowledgeWorkbenchError(
            "业务关系状态必须是 active 或 retracted"
        )
    _validate_limit(limit)
    relation_key = (
        _required_relation_key(relation_key) if relation_key is not None else None
    )
    where = ["er.status = ?"]
    parameters: list[object] = [status]
    if relation_key is not None:
        where.append("er.relation_key = ?")
        parameters.append(relation_key)
    if entity_id is not None:
        where.append(
            "(er.source_entity_id = ? OR er.target_entity_id = ?)"
        )
        parameters.extend([entity_id, entity_id])
    predicate = " AND ".join(where)
    with database.connect() as connection:
        rows = connection.execute(
            f"""
            SELECT er.*, ert.label, ert.inverse_label, ert.directed,
                   ert.status AS relation_type_status,
                   source.canonical_name AS source_name,
                   source.entity_type AS source_entity_type,
                   source.status AS source_status,
                   target.canonical_name AS target_name,
                   target.entity_type AS target_entity_type,
                   target.status AS target_status
            FROM entity_relationships er
            JOIN entity_relation_types ert
              ON ert.relation_key = er.relation_key
            JOIN canonical_entities source
              ON source.id = er.source_entity_id
            JOIN canonical_entities target
              ON target.id = er.target_entity_id
            WHERE {predicate}
            ORDER BY er.created_at, er.id
            LIMIT ?
            """,
            [*parameters, limit],
        ).fetchall()
        relationship_ids = [row["id"] for row in rows]
        support_rows = []
        if relationship_ids:
            placeholders = ", ".join("?" for _ in relationship_ids)
            support_rows = connection.execute(
                f"""
                SELECT ere.relationship_id, ere.evidence_id, e.status,
                       d.classification,
                       CASE WHEN pr.is_current = 1
                                  AND d.current_version_id = dv.id
                            THEN 1 ELSE 0 END AS source_is_current
                FROM entity_relationship_evidence ere
                JOIN evidence e ON e.id = ere.evidence_id
                JOIN processing_runs pr ON pr.id = e.processing_run_id
                JOIN document_versions dv ON dv.id = e.document_version_id
                JOIN documents d ON d.id = dv.document_id
                WHERE ere.relationship_id IN ({placeholders})
                ORDER BY ere.relationship_id, ere.evidence_id
                """,
                relationship_ids,
            ).fetchall()
    supports: dict[str, list] = defaultdict(list)
    for support in support_rows:
        supports[support["relationship_id"]].append(support)
    return [
        _serialize_relationship(row, supports.get(row["id"], []))
        for row in rows
    ]


def project_business_relationship_graph(
    database: Database,
    *,
    entity_id: str | None = None,
    limit: int = 100,
) -> dict:
    _validate_limit(limit)
    relationships = list_entity_relationships(
        database,
        status="active",
        entity_id=entity_id,
        limit=500,
    )
    eligible = [
        item
        for item in relationships
        if item["projectable_supporting_evidence_ids"]
    ]
    if entity_id is not None and not eligible:
        raise KnowledgeWorkbenchError(
            "实体没有当前、已验证且非 restricted 的业务关系证据支持"
        )
    returned = eligible[:limit]
    nodes: dict[str, dict] = {}
    edges = []
    for relationship in returned:
        for prefix in ("source", "target"):
            node_id = relationship[f"{prefix}_entity_id"]
            nodes[node_id] = {
                "id": node_id,
                "canonical_name": relationship[f"{prefix}_name"],
                "entity_type": relationship[f"{prefix}_entity_type"],
            }
        edges.append(
            {
                "relationship_id": relationship["relationship_id"],
                "source_entity_id": relationship["source_entity_id"],
                "target_entity_id": relationship["target_entity_id"],
                "relation_key": relationship["relation_key"],
                "label": relationship["label"],
                "inverse_label": relationship["inverse_label"],
                "directed": relationship["directed"],
                "supporting_evidence_ids": relationship[
                    "projectable_supporting_evidence_ids"
                ],
                "supporting_evidence_count": len(
                    relationship["projectable_supporting_evidence_ids"]
                ),
            }
        )
    return {
        "schema_version": "1.0",
        "projection_version": "manual-business-relationships-v1",
        "filters": {
            "entity_id": entity_id,
            "evidence_status": "verified",
            "restricted_excluded": True,
            "current_sources_only": True,
        },
        "summary": {
            "eligible_node_count": len(
                {
                    entity
                    for item in eligible
                    for entity in (
                        item["source_entity_id"],
                        item["target_entity_id"],
                    )
                }
            ),
            "eligible_edge_count": len(eligible),
            "returned_node_count": len(nodes),
            "returned_edge_count": len(edges),
            "truncated": len(eligible) > len(returned),
        },
        "nodes": [nodes[item] for item in sorted(nodes)],
        "edges": edges,
    }


def query_business_relationship_paths(
    database: Database,
    source_entity_id: str,
    *,
    target_entity_id: str | None = None,
    max_depth: int = 3,
    include_inverse: bool = False,
    limit: int = 50,
) -> dict:
    source_entity_id = _required_text(
        source_entity_id, "起点实体 ID", maximum=120
    )
    target_entity_id = (
        _required_text(target_entity_id, "目标实体 ID", maximum=120)
        if target_entity_id is not None
        else None
    )
    if target_entity_id == source_entity_id:
        raise KnowledgeWorkbenchError("起点实体和目标实体不能相同")
    if max_depth <= 0 or max_depth > PATH_MAX_DEPTH:
        raise KnowledgeWorkbenchError(
            f"max-depth 必须在 1 到 {PATH_MAX_DEPTH} 之间"
        )
    if limit <= 0 or limit > PATH_MAX_RESULTS:
        raise KnowledgeWorkbenchError(
            f"limit 必须在 1 到 {PATH_MAX_RESULTS} 之间"
        )

    with database.connect() as connection:
        source = _active_entity_details(connection, source_entity_id)
        target = (
            _active_entity_details(connection, target_entity_id)
            if target_entity_id is not None
            else None
        )
        candidate_relationship_count = connection.execute(
            """
            SELECT COUNT(*) FROM entity_relationships
            WHERE status = 'active'
            """
        ).fetchone()[0]

    candidate_limit = 500
    candidate_edge_limit_reached = (
        candidate_relationship_count > candidate_limit
    )
    relationships = list_entity_relationships(
        database,
        status="active",
        limit=candidate_limit,
    )
    eligible = [
        relationship
        for relationship in relationships
        if relationship["projectable_supporting_evidence_ids"]
        and relationship["relation_type_status"] == "active"
        and relationship["source_status"] == "active"
        and relationship["target_status"] == "active"
    ]
    nodes = {
        source["id"]: source,
        **({target["id"]: target} if target is not None else {}),
    }
    adjacency: dict[str, list[dict]] = defaultdict(list)
    for relationship in eligible:
        for prefix in ("source", "target"):
            nodes[relationship[f"{prefix}_entity_id"]] = {
                "id": relationship[f"{prefix}_entity_id"],
                "canonical_name": relationship[f"{prefix}_name"],
                "entity_type": relationship[f"{prefix}_entity_type"],
            }
        adjacency[relationship["source_entity_id"]].append(
            _path_hop(
                relationship,
                relationship["source_entity_id"],
                relationship["target_entity_id"],
                "forward" if relationship["directed"] else "undirected",
            )
        )
        if not relationship["directed"] or include_inverse:
            adjacency[relationship["target_entity_id"]].append(
                _path_hop(
                    relationship,
                    relationship["target_entity_id"],
                    relationship["source_entity_id"],
                    "reverse" if relationship["directed"] else "undirected",
                )
            )
    for transitions in adjacency.values():
        transitions.sort(
            key=lambda hop: (
                hop["to_entity_id"],
                hop["relation_key"],
                hop["relationship_id"],
                hop["traversal_direction"],
            )
        )

    queue = deque([(source_entity_id, (source_entity_id,), tuple())])
    found: list[dict] = []
    expanded_transition_count = 0
    expansion_limit_reached = False
    path_limit_reached = False
    while queue:
        current_entity_id, entity_ids, hops = queue.popleft()
        if len(hops) >= max_depth:
            continue
        for hop in adjacency.get(current_entity_id, []):
            if expanded_transition_count >= PATH_MAX_EXPANSIONS:
                expansion_limit_reached = True
                queue.clear()
                break
            expanded_transition_count += 1
            next_entity_id = hop["to_entity_id"]
            if next_entity_id in entity_ids:
                continue
            next_entity_ids = (*entity_ids, next_entity_id)
            next_hops = (*hops, hop)
            matched = (
                target_entity_id is None
                or next_entity_id == target_entity_id
            )
            if matched:
                found.append(
                    _serialize_path(next_entity_ids, next_hops, nodes)
                )
                if len(found) > limit:
                    path_limit_reached = True
                    queue.clear()
                    break
            if (
                next_entity_id != target_entity_id
                and len(next_hops) < max_depth
            ):
                queue.append((next_entity_id, next_entity_ids, next_hops))
        if path_limit_reached or expansion_limit_reached:
            break

    returned = found[:limit]
    returned_node_ids = {
        entity_id
        for path in returned
        for entity_id in path["entity_ids"]
    }
    return {
        "schema_version": "1.0",
        "projection_version": "manual-business-relationship-paths-v1",
        "semantics": {
            "path_meaning": (
                "每一跳都是人工登记且仍有合格证据支撑的关系；"
                "多跳连通性不构成新的业务关系或事实结论"
            ),
            "simple_paths_only": True,
            "inverse_traversal_is_explicit": True,
        },
        "filters": {
            "source_entity_id": source_entity_id,
            "target_entity_id": target_entity_id,
            "max_depth": max_depth,
            "include_inverse": bool(include_inverse),
            "relationship_status": "active",
            "evidence_status": "verified",
            "restricted_excluded": True,
            "current_sources_only": True,
        },
        "summary": {
            "eligible_edge_count": len(eligible),
            "candidate_relationship_count": candidate_relationship_count,
            "candidate_edge_limit_reached": candidate_edge_limit_reached,
            "expanded_transition_count": expanded_transition_count,
            "returned_path_count": len(returned),
            "returned_node_count": len(returned_node_ids),
            "path_limit_reached": path_limit_reached,
            "expansion_limit_reached": expansion_limit_reached,
            "truncated": candidate_edge_limit_reached
            or path_limit_reached
            or expansion_limit_reached,
        },
        "nodes": [
            nodes[entity_id] for entity_id in sorted(returned_node_ids)
        ],
        "paths": returned,
    }


def _serialize_relationship(row, support_rows: list) -> dict:
    evidence_ids = [support["evidence_id"] for support in support_rows]
    current_verified = [
        support["evidence_id"]
        for support in support_rows
        if support["source_is_current"] and support["status"] == "verified"
    ]
    projectable = [
        support["evidence_id"]
        for support in support_rows
        if support["source_is_current"]
        and support["status"] == "verified"
        and support["classification"] != "restricted"
    ]
    projectable_classifications = {
        support["classification"]
        for support in support_rows
        if support["source_is_current"]
        and support["status"] == "verified"
        and support["classification"] != "restricted"
    }
    classifications = {
        support["classification"] for support in support_rows
    }
    return {
        "relationship_id": row["id"],
        "relation_key": row["relation_key"],
        "label": row["label"],
        "inverse_label": row["inverse_label"],
        "directed": bool(row["directed"]),
        "relation_type_status": row["relation_type_status"],
        "source_entity_id": row["source_entity_id"],
        "source_name": row["source_name"],
        "source_entity_type": row["source_entity_type"],
        "source_status": row["source_status"],
        "target_entity_id": row["target_entity_id"],
        "target_name": row["target_name"],
        "target_entity_type": row["target_entity_type"],
        "target_status": row["target_status"],
        "status": row["status"],
        "created_by": row["created_by"],
        "created_at": row["created_at"],
        "retracted_by": row["retracted_by"],
        "retracted_at": row["retracted_at"],
        "supporting_evidence_ids": evidence_ids,
        "supporting_evidence_count": len(evidence_ids),
        "current_verified_supporting_evidence_ids": current_verified,
        "current_verified_supporting_evidence_count": len(current_verified),
        "projectable_supporting_evidence_ids": projectable,
        "projectable_classifications": _sort_classifications(
            projectable_classifications
        ),
        "classifications": _sort_classifications(classifications),
    }


def _path_hop(
    relationship: dict,
    from_entity_id: str,
    to_entity_id: str,
    traversal_direction: str,
) -> dict:
    traversal_label = relationship["label"]
    if traversal_direction == "reverse":
        traversal_label = relationship["inverse_label"]
    evidence_ids = relationship["projectable_supporting_evidence_ids"]
    return {
        "relationship_id": relationship["relationship_id"],
        "relation_key": relationship["relation_key"],
        "label": relationship["label"],
        "inverse_label": relationship["inverse_label"],
        "directed": relationship["directed"],
        "relationship_source_entity_id": relationship["source_entity_id"],
        "relationship_target_entity_id": relationship["target_entity_id"],
        "from_entity_id": from_entity_id,
        "to_entity_id": to_entity_id,
        "traversal_direction": traversal_direction,
        "traversal_label": traversal_label,
        "supporting_evidence_ids": evidence_ids,
        "supporting_evidence_count": len(evidence_ids),
        "classifications": relationship["projectable_classifications"],
    }


def _serialize_path(
    entity_ids: tuple[str, ...],
    hops: tuple[dict, ...],
    nodes: dict[str, dict],
) -> dict:
    evidence_sets = [
        set(hop["supporting_evidence_ids"]) for hop in hops
    ]
    evidence_union = set().union(*evidence_sets)
    shared_evidence = set.intersection(*evidence_sets)
    classifications = {
        classification
        for hop in hops
        for classification in hop["classifications"]
    }
    return {
        "depth": len(hops),
        "entity_ids": list(entity_ids),
        "entities": [nodes[entity_id] for entity_id in entity_ids],
        "hops": list(hops),
        "supporting_evidence_ids": sorted(evidence_union),
        "shared_supporting_evidence_ids": sorted(shared_evidence),
        "classifications": _sort_classifications(classifications),
    }


def _validate_relationship_evidence(
    connection: sqlite3.Connection,
    evidence_id: str,
    source_entity_id: str,
    target_entity_id: str,
) -> None:
    row = connection.execute(
        """
        SELECT e.id, e.status,
               CASE WHEN pr.is_current = 1
                          AND d.current_version_id = dv.id
                    THEN 1 ELSE 0 END AS source_is_current,
               EXISTS (
                   SELECT 1 FROM evidence_entity_mentions source_mention
                   WHERE source_mention.evidence_id = e.id
                     AND source_mention.entity_id = ?
               ) AS source_mentioned,
               EXISTS (
                   SELECT 1 FROM evidence_entity_mentions target_mention
                   WHERE target_mention.evidence_id = e.id
                     AND target_mention.entity_id = ?
               ) AS target_mentioned
        FROM evidence e
        JOIN processing_runs pr ON pr.id = e.processing_run_id
        JOIN document_versions dv ON dv.id = e.document_version_id
        JOIN documents d ON d.id = dv.document_id
        WHERE e.id = ?
        """,
        (source_entity_id, target_entity_id, evidence_id),
    ).fetchone()
    if not row:
        raise KnowledgeWorkbenchError(f"业务关系证据不存在：{evidence_id}")
    if not row["source_is_current"]:
        raise KnowledgeWorkbenchError(
            f"业务关系只能引用当前文件版本与当前处理运行的证据：{evidence_id}"
        )
    if row["status"] != "verified":
        raise KnowledgeWorkbenchError(
            f"业务关系只能引用 verified 证据：{evidence_id}"
        )
    if not row["source_mentioned"] or not row["target_mentioned"]:
        raise KnowledgeWorkbenchError(
            f"业务关系证据必须同时关联源实体和目标实体：{evidence_id}"
        )


def _active_entity(connection: sqlite3.Connection, entity_id: str):
    row = connection.execute(
        """
        SELECT id FROM canonical_entities
        WHERE id = ? AND status = 'active'
        """,
        (entity_id,),
    ).fetchone()
    if not row:
        raise KnowledgeWorkbenchError("规范实体不存在或不是 active 状态")
    return row


def _active_entity_details(
    connection: sqlite3.Connection, entity_id: str
) -> dict:
    row = connection.execute(
        """
        SELECT id, canonical_name, entity_type
        FROM canonical_entities
        WHERE id = ? AND status = 'active'
        """,
        (entity_id,),
    ).fetchone()
    if not row:
        raise KnowledgeWorkbenchError("规范实体不存在或不是 active 状态")
    return dict(row)


def _required_relation_key(value: str) -> str:
    key = value.strip().casefold()
    if not RELATION_KEY_PATTERN.fullmatch(key):
        raise KnowledgeWorkbenchError(
            "关系类型键必须以小写字母开头，且只包含小写字母、数字和下划线，最长 63 个字符"
        )
    return key


def _required_actor(value: str) -> str:
    return _required_text(value, "actor", maximum=80)


def _required_text(value: str, label: str, *, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise KnowledgeWorkbenchError(f"{label}不能为空")
    text = value.strip()
    if len(text) > maximum:
        raise KnowledgeWorkbenchError(f"{label}不能超过 {maximum} 个字符")
    return text


def _optional_text(
    value: str | None, label: str, *, maximum: int
) -> str | None:
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    if len(text) > maximum:
        raise KnowledgeWorkbenchError(f"{label}不能超过 {maximum} 个字符")
    return text


def _validate_limit(limit: int) -> None:
    if limit <= 0 or limit > 500:
        raise KnowledgeWorkbenchError("limit 必须在 1 到 500 之间")


def _sort_classifications(values: set[str]) -> list[str]:
    order = {value: index for index, value in enumerate(CLASSIFICATION_ORDER)}
    return sorted(values, key=lambda value: (order.get(value, len(order)), value))
