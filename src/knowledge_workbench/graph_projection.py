from __future__ import annotations

from collections import defaultdict
from itertools import combinations

from .database import Database
from .errors import KnowledgeWorkbenchError


GRAPH_PROJECTION_VERSION = "entity-evidence-cooccurrence-v1"
ACTIVE_EVIDENCE_STATUSES = ("draft", "reviewing", "verified", "conflicted")
CLASSIFICATION_ORDER = ("public", "internal", "confidential", "restricted")


def project_entity_graph(
    database: Database,
    *,
    entity_id: str | None = None,
    include_unverified: bool = False,
    limit: int = 100,
) -> dict:
    if limit <= 0 or limit > 500:
        raise KnowledgeWorkbenchError("图谱投影 limit 必须在 1 到 500 之间")
    statuses = ACTIVE_EVIDENCE_STATUSES if include_unverified else ("verified",)
    placeholders = ", ".join("?" for _ in statuses)
    with database.connect() as connection:
        rows = connection.execute(
            f"""
            SELECT DISTINCT ce.id AS entity_id, ce.canonical_name,
                   ce.entity_type, eem.evidence_id, e.status AS evidence_status,
                   d.classification
            FROM evidence_entity_mentions eem
            JOIN canonical_entities ce
              ON ce.id = eem.entity_id AND ce.status = 'active'
            JOIN evidence e ON e.id = eem.evidence_id
            JOIN processing_runs pr
              ON pr.id = e.processing_run_id AND pr.is_current = 1
            JOIN document_versions dv ON dv.id = e.document_version_id
            JOIN documents d
              ON d.id = dv.document_id AND d.current_version_id = dv.id
            WHERE e.status IN ({placeholders})
              AND d.classification != 'restricted'
            ORDER BY eem.evidence_id, ce.id
            """,
            statuses,
        ).fetchall()
        restricted_support_excluded = connection.execute(
            f"""
            SELECT COUNT(*) FROM (
                SELECT DISTINCT eem.entity_id, eem.evidence_id
                FROM evidence_entity_mentions eem
                JOIN canonical_entities ce
                  ON ce.id = eem.entity_id AND ce.status = 'active'
                JOIN evidence e ON e.id = eem.evidence_id
                JOIN processing_runs pr
                  ON pr.id = e.processing_run_id AND pr.is_current = 1
                JOIN document_versions dv ON dv.id = e.document_version_id
                JOIN documents d
                  ON d.id = dv.document_id AND d.current_version_id = dv.id
                WHERE e.status IN ({placeholders})
                  AND d.classification = 'restricted'
            )
            """,
            statuses,
        ).fetchone()[0]

    node_support: dict[str, dict] = {}
    evidence_entities: dict[str, list[str]] = defaultdict(list)
    evidence_metadata: dict[str, tuple[str, str]] = {}
    for row in rows:
        node = node_support.setdefault(
            row["entity_id"],
            {
                "id": row["entity_id"],
                "canonical_name": row["canonical_name"],
                "entity_type": row["entity_type"],
                "evidence_ids": set(),
                "status_counts": defaultdict(int),
                "classifications": set(),
            },
        )
        node["evidence_ids"].add(row["evidence_id"])
        node["status_counts"][row["evidence_status"]] += 1
        node["classifications"].add(row["classification"])
        evidence_entities[row["evidence_id"]].append(row["entity_id"])
        evidence_metadata[row["evidence_id"]] = (
            row["evidence_status"],
            row["classification"],
        )

    if entity_id is not None and entity_id not in node_support:
        raise KnowledgeWorkbenchError(
            "实体没有符合当前状态与密级边界的可投影证据支持"
        )

    edge_support: dict[tuple[str, str], dict] = {}
    for evidence_id, raw_entity_ids in evidence_entities.items():
        entity_ids = sorted(set(raw_entity_ids))
        evidence_status, classification = evidence_metadata[evidence_id]
        for left, right in combinations(entity_ids, 2):
            edge = edge_support.setdefault(
                (left, right),
                {
                    "source_entity_id": left,
                    "target_entity_id": right,
                    "relationship": "co_occurs_in_atomic_evidence",
                    "evidence_ids": set(),
                    "status_counts": defaultdict(int),
                    "classifications": set(),
                },
            )
            edge["evidence_ids"].add(evidence_id)
            edge["status_counts"][evidence_status] += 1
            edge["classifications"].add(classification)

    all_edges = list(edge_support.values())
    if entity_id is not None:
        all_edges = [
            edge
            for edge in all_edges
            if entity_id in (edge["source_entity_id"], edge["target_entity_id"])
        ]
        eligible_node_ids = {entity_id}
        for edge in all_edges:
            eligible_node_ids.add(edge["source_entity_id"])
            eligible_node_ids.add(edge["target_entity_id"])
    else:
        eligible_node_ids = set(node_support)
    all_edges.sort(
        key=lambda edge: (
            -len(edge["evidence_ids"]),
            edge["source_entity_id"],
            edge["target_entity_id"],
        )
    )
    returned_edges = all_edges[:limit]

    if entity_id is not None:
        returned_node_ids = {entity_id}
        for edge in returned_edges:
            returned_node_ids.add(edge["source_entity_id"])
            returned_node_ids.add(edge["target_entity_id"])
    else:
        returned_node_ids = {
            entity
            for edge in returned_edges
            for entity in (edge["source_entity_id"], edge["target_entity_id"])
        }
        for candidate_id in sorted(node_support):
            if len(returned_node_ids) >= limit:
                break
            returned_node_ids.add(candidate_id)

    nodes = [_serialize_node(node_support[item]) for item in sorted(returned_node_ids)]
    edges = [_serialize_edge(edge) for edge in returned_edges]
    return {
        "schema_version": "1.0",
        "projection_version": GRAPH_PROJECTION_VERSION,
        "filters": {
            "entity_id": entity_id,
            "include_unverified": include_unverified,
            "evidence_statuses": list(statuses),
            "restricted_excluded": True,
            "current_sources_only": True,
        },
        "summary": {
            "eligible_node_count": len(eligible_node_ids),
            "eligible_edge_count": len(all_edges),
            "returned_node_count": len(nodes),
            "returned_edge_count": len(edges),
            "restricted_support_excluded": restricted_support_excluded,
            "truncated": (
                len(all_edges) > len(returned_edges)
                or len(eligible_node_ids) > len(returned_node_ids)
            ),
        },
        "nodes": nodes,
        "edges": edges,
    }


def _serialize_node(node: dict) -> dict:
    evidence_ids = sorted(node["evidence_ids"])
    return {
        "id": node["id"],
        "canonical_name": node["canonical_name"],
        "entity_type": node["entity_type"],
        "supporting_evidence_count": len(evidence_ids),
        "supporting_evidence_ids": evidence_ids,
        "evidence_status_counts": dict(sorted(node["status_counts"].items())),
        "classifications": _sort_classifications(node["classifications"]),
    }


def _serialize_edge(edge: dict) -> dict:
    evidence_ids = sorted(edge["evidence_ids"])
    return {
        "source_entity_id": edge["source_entity_id"],
        "target_entity_id": edge["target_entity_id"],
        "relationship": edge["relationship"],
        "supporting_evidence_count": len(evidence_ids),
        "supporting_evidence_ids": evidence_ids,
        "evidence_status_counts": dict(sorted(edge["status_counts"].items())),
        "classifications": _sort_classifications(edge["classifications"]),
    }


def _sort_classifications(values: set[str]) -> list[str]:
    order = {value: index for index, value in enumerate(CLASSIFICATION_ORDER)}
    return sorted(values, key=lambda value: (order.get(value, len(order)), value))
