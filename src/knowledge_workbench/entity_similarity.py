from __future__ import annotations

from collections import defaultdict
from difflib import SequenceMatcher
from itertools import combinations

from .database import Database
from .entities import ENTITY_TYPES
from .errors import KnowledgeWorkbenchError
from .utils import sha256_text


MAX_BLOCK_ENTITY_COUNT = 50
MAX_BLOCKED_PAIRS = 50_000


def project_similar_entity_candidates(
    database: Database,
    *,
    entity_type: str | None = None,
    minimum_similarity: float = 0.65,
    limit: int = 100,
) -> dict:
    entity_type = _optional_entity_type(entity_type)
    if not 0 <= minimum_similarity <= 1:
        raise KnowledgeWorkbenchError("minimum_similarity 必须在 0 到 1 之间")
    if limit <= 0 or limit > 500:
        raise KnowledgeWorkbenchError("实体相似候选 limit 必须在 1 到 500 之间")
    with database.connect() as connection:
        parameters: list[object] = []
        type_filter = ""
        if entity_type:
            type_filter = "AND ce.entity_type = ?"
            parameters.append(entity_type)
        rows = connection.execute(
            f"""
            SELECT ce.id, ce.canonical_name, ce.entity_type,
                   ea.alias, ea.normalized_alias,
                   (SELECT COUNT(DISTINCT eem.evidence_id)
                    FROM evidence_entity_mentions eem
                    JOIN evidence e ON e.id = eem.evidence_id
                    JOIN processing_runs pr
                      ON pr.id = e.processing_run_id AND pr.is_current = 1
                    JOIN document_versions dv ON dv.id = e.document_version_id
                    JOIN documents d
                      ON d.id = dv.document_id AND d.current_version_id = dv.id
                    WHERE eem.entity_id = ce.id) AS current_evidence_count
            FROM canonical_entities ce
            JOIN entity_aliases ea ON ea.entity_id = ce.id
            WHERE ce.status = 'active' {type_filter}
            ORDER BY ce.entity_type, ce.id, ea.is_canonical DESC,
                     ea.normalized_alias, ea.id
            """,
            parameters,
        ).fetchall()
        request_rows = connection.execute(
            """
            SELECT id, source_entity_id, target_entity_id, status,
                   created_at, reviewed_at
            FROM entity_merge_requests
            ORDER BY created_at, id
            """
        ).fetchall()

    entities: dict[str, dict] = {}
    for row in rows:
        entity = entities.setdefault(
            row["id"],
            {
                "entity_id": row["id"],
                "canonical_name": row["canonical_name"],
                "entity_type": row["entity_type"],
                "current_evidence_count": row["current_evidence_count"],
                "aliases": [],
            },
        )
        entity["aliases"].append(
            {
                "alias": row["alias"],
                "normalized": _comparison_value(row["normalized_alias"]),
            }
        )

    blocks: dict[str, set[str]] = defaultdict(set)
    for entity in entities.values():
        for alias in entity["aliases"]:
            for shingle in _shingles(alias["normalized"]):
                blocks[f"{entity['entity_type']}:{shingle}"].add(
                    entity["entity_id"]
                )

    blocked_pairs: set[tuple[str, str]] = set()
    skipped_frequent_blocks = 0
    pair_cap_reached = False
    for block_key in sorted(blocks):
        entity_ids = sorted(blocks[block_key])
        if len(entity_ids) < 2:
            continue
        if len(entity_ids) > MAX_BLOCK_ENTITY_COUNT:
            skipped_frequent_blocks += 1
            continue
        for left_id, right_id in combinations(entity_ids, 2):
            blocked_pairs.add((left_id, right_id))
            if len(blocked_pairs) >= MAX_BLOCKED_PAIRS:
                pair_cap_reached = True
                break
        if pair_cap_reached:
            break

    requests_by_pair: dict[tuple[str, str], dict] = {}
    for row in request_rows:
        pair = tuple(sorted((row["source_entity_id"], row["target_entity_id"])))
        requests_by_pair[pair] = {
            "request_id": row["id"],
            "status": row["status"],
            "source_entity_id": row["source_entity_id"],
            "target_entity_id": row["target_entity_id"],
            "reviewed_at": row["reviewed_at"],
        }

    candidates = []
    for left_id, right_id in sorted(blocked_pairs):
        left = entities[left_id]
        right = entities[right_id]
        if left["entity_type"] != right["entity_type"]:
            continue
        best = _best_alias_match(left["aliases"], right["aliases"])
        if best["similarity"] < minimum_similarity:
            continue
        candidate_id = "entitypair_" + sha256_text(
            f"{left_id}:{right_id}"
        )[:20]
        candidates.append(
            {
                "candidate_id": candidate_id,
                "entity_type": left["entity_type"],
                "similarity": round(best["similarity"], 6),
                "left": _entity_projection(left, best["left_alias"]),
                "right": _entity_projection(right, best["right_alias"]),
                "merge_request": requests_by_pair.get((left_id, right_id)),
            }
        )
    candidates.sort(key=lambda item: (-item["similarity"], item["candidate_id"]))
    selected = candidates[:limit]
    return {
        "kind": "similar-entity-merge-candidate-projection",
        "entity_type": entity_type,
        "minimum_similarity": minimum_similarity,
        "statistics": {
            "active_entity_count": len(entities),
            "alias_count": len(rows),
            "block_count": len(blocks),
            "skipped_frequent_block_count": skipped_frequent_blocks,
            "blocked_pair_count": len(blocked_pairs),
            "pair_cap": MAX_BLOCKED_PAIRS,
            "pair_cap_reached": pair_cap_reached,
            "total_candidate_count": len(candidates),
            "returned_candidate_count": len(selected),
            "truncated": pair_cap_reached or len(candidates) > len(selected),
        },
        "items": selected,
    }


def _best_alias_match(left_aliases: list[dict], right_aliases: list[dict]) -> dict:
    best = {"similarity": -1.0, "left_alias": "", "right_alias": ""}
    for left in left_aliases:
        for right in right_aliases:
            similarity = SequenceMatcher(
                None, left["normalized"], right["normalized"]
            ).ratio()
            key = (-similarity, left["alias"], right["alias"])
            best_key = (
                -best["similarity"],
                best["left_alias"],
                best["right_alias"],
            )
            if key < best_key:
                best = {
                    "similarity": similarity,
                    "left_alias": left["alias"],
                    "right_alias": right["alias"],
                }
    return best


def _entity_projection(entity: dict, matched_alias: str) -> dict:
    return {
        "entity_id": entity["entity_id"],
        "canonical_name": entity["canonical_name"],
        "matched_alias": matched_alias,
        "current_evidence_count": entity["current_evidence_count"],
    }


def _comparison_value(value: str) -> str:
    return "".join(value.split())


def _shingles(value: str) -> set[str]:
    if len(value) < 3:
        return {value} if value else set()
    return {value[index : index + 3] for index in range(len(value) - 2)}


def _optional_entity_type(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip().casefold()
    if normalized not in ENTITY_TYPES:
        raise KnowledgeWorkbenchError(
            "实体类型必须是：" + ", ".join(ENTITY_TYPES)
        )
    return normalized
