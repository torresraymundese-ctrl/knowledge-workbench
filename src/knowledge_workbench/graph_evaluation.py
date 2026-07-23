from __future__ import annotations

import json
from pathlib import Path

from .database import Database
from .entity_relationships import (
    list_entity_relationships,
    query_business_relationship_paths,
)
from .errors import KnowledgeWorkbenchError
from .schema_validation import validate_graph_evaluation_dataset
from .utils import sha256_text, utc_now


def evaluate_graph_dataset(database: Database, dataset_path: Path) -> dict:
    dataset_path = dataset_path.expanduser().resolve()
    try:
        dataset_text = dataset_path.read_text(encoding="utf-8")
        dataset = json.loads(dataset_text)
    except FileNotFoundError as exc:
        raise KnowledgeWorkbenchError(
            f"图谱评测数据集不存在：{dataset_path}"
        ) from exc
    except json.JSONDecodeError as exc:
        raise KnowledgeWorkbenchError(
            f"图谱评测数据集不是有效 JSON：{exc}"
        ) from exc
    validate_graph_evaluation_dataset(dataset)
    _validate_current_references(database, dataset)

    relation_results = [
        _evaluate_relation_case(database, case)
        for case in dataset["relation_cases"]
    ]
    path_results = [
        _evaluate_path_case(database, case)
        for case in dataset["path_cases"]
    ]
    all_results = [*relation_results, *path_results]
    restricted_leak_count = sum(
        result["restricted_support_leak_count"]
        for result in all_results
    )
    relation_confusion = _confusion(relation_results, "expected_present")
    path_confusion = _confusion(path_results, "expected_reachable")
    expected_relation_evidence = sum(
        result["expected_supporting_evidence_count"]
        for result in relation_results
    )
    found_relation_evidence = sum(
        result["found_expected_supporting_evidence_count"]
        for result in relation_results
    )
    expected_path_evidence = sum(
        result["expected_supporting_evidence_count"]
        for result in path_results
    )
    found_path_evidence = sum(
        result["found_expected_supporting_evidence_count"]
        for result in path_results
    )
    positive_relation_results = [
        result for result in relation_results if result["expected_present"]
    ]
    direction_matches = sum(
        result["direction_matched"] for result in positive_relation_results
    )
    positive_path_results = [
        result for result in path_results if result["expected_reachable"]
    ]
    route_matches = sum(
        result["route_matched"] for result in positive_path_results
    )
    passed_count = sum(result["passed"] for result in all_results)
    return {
        "schema_version": "1.0",
        "dataset_name": dataset["name"],
        "dataset_path": str(dataset_path),
        "dataset_sha256": sha256_text(dataset_text),
        "evaluated_at": utc_now(),
        "provenance": {
            **dataset["provenance"],
            "two_person_review": True,
        },
        "safety": {
            "current_references_validated": True,
            "restricted_support_leak_count": restricted_leak_count,
            "passed": restricted_leak_count == 0,
        },
        "aggregate": {
            "case_count": len(all_results),
            "passed_cases": passed_count,
            "pass_rate": _rate(passed_count, len(all_results)),
            "relation_case_count": len(relation_results),
            "relation_pass_rate": _rate(
                sum(result["passed"] for result in relation_results),
                len(relation_results),
            ),
            "relationship_precision": _precision(relation_confusion),
            "relationship_recall": _recall(relation_confusion),
            "relationship_direction_accuracy": _rate(
                direction_matches, len(positive_relation_results)
            ),
            "relationship_evidence_coverage": _rate(
                found_relation_evidence, expected_relation_evidence
            ),
            "path_case_count": len(path_results),
            "path_pass_rate": _rate(
                sum(result["passed"] for result in path_results),
                len(path_results),
            ),
            "path_precision": _precision(path_confusion),
            "path_recall": _recall(path_confusion),
            "path_route_accuracy": _rate(
                route_matches, len(positive_path_results)
            ),
            "path_evidence_coverage": _rate(
                found_path_evidence, expected_path_evidence
            ),
            "truncated_path_case_count": sum(
                result["query_truncated"] for result in path_results
            ),
        },
        "relation_cases": relation_results,
        "path_cases": path_results,
    }


def _evaluate_relation_case(database: Database, case: dict) -> dict:
    with database.connect() as connection:
        candidate_count = connection.execute(
            """
            SELECT COUNT(*) FROM entity_relationships
            WHERE status = 'active' AND relation_key = ?
              AND (source_entity_id = ? OR target_entity_id = ?)
            """,
            (
                case["relation_key"],
                case["source_entity_id"],
                case["source_entity_id"],
            ),
        ).fetchone()[0]
    if candidate_count > 500:
        raise KnowledgeWorkbenchError(
            f"图谱关系用例 {case['case_id']} 的候选关系超过 500 条，"
            "无法进行未截断正式评测"
        )
    relationships = list_entity_relationships(
        database,
        status="active",
        relation_key=case["relation_key"],
        entity_id=case["source_entity_id"],
        limit=500,
    )
    candidates = []
    for relationship in relationships:
        if (
            not relationship["projectable_supporting_evidence_ids"]
            or relationship["relation_type_status"] != "active"
            or relationship["source_status"] != "active"
            or relationship["target_status"] != "active"
        ):
            continue
        direction = _relationship_direction(relationship, case)
        if direction is not None:
            candidates.append((direction, relationship))

    predicted_directions = sorted(
        {direction for direction, _ in candidates}
    )
    matching = [
        relationship
        for direction, relationship in candidates
        if direction == case["expected_direction"]
    ]
    found_evidence = {
        evidence_id
        for relationship in matching
        for evidence_id in relationship[
            "projectable_supporting_evidence_ids"
        ]
    }
    expected_evidence = set(case["expected_supporting_evidence_ids"])
    missing_evidence = sorted(expected_evidence - found_evidence)
    predicted_present = bool(candidates)
    direction_matched = (
        case["expected_direction"] in predicted_directions
        if case["expected_present"]
        else not predicted_present
    )
    passed = (
        predicted_present
        and direction_matched
        and not missing_evidence
        if case["expected_present"]
        else not predicted_present
    )
    restricted_leaks = sum(
        "restricted" in relationship["projectable_classifications"]
        for _, relationship in candidates
    )
    return {
        "case_id": case["case_id"],
        "passed": passed,
        "expected_present": case["expected_present"],
        "expected_direction": case["expected_direction"],
        "predicted_present": predicted_present,
        "predicted_directions": predicted_directions,
        "direction_matched": direction_matched,
        "expected_supporting_evidence_count": len(expected_evidence),
        "found_expected_supporting_evidence_count": (
            len(expected_evidence) - len(missing_evidence)
        ),
        "missing_expected_supporting_evidence_ids": missing_evidence,
        "restricted_support_leak_count": restricted_leaks,
    }


def _evaluate_path_case(database: Database, case: dict) -> dict:
    projection = query_business_relationship_paths(
        database,
        case["source_entity_id"],
        target_entity_id=case["target_entity_id"],
        max_depth=case["max_depth"],
        include_inverse=case["include_inverse"],
        limit=100,
    )
    expected_entities = case["expected_entity_ids"]
    expected_relations = case["expected_relation_keys"]
    expected_directions = case["expected_traversal_directions"]
    matching_path = next(
        (
            path
            for path in projection["paths"]
            if path["entity_ids"] == expected_entities
            and [
                hop["relation_key"] for hop in path["hops"]
            ]
            == expected_relations
            and [
                hop["traversal_direction"] for hop in path["hops"]
            ]
            == expected_directions
        ),
        None,
    )
    expected_evidence = set(case["expected_supporting_evidence_ids"])
    found_evidence = (
        set(matching_path["supporting_evidence_ids"])
        if matching_path is not None
        else set()
    )
    missing_evidence = sorted(expected_evidence - found_evidence)
    predicted_reachable = bool(projection["paths"])
    route_matched = matching_path is not None
    query_truncated = projection["summary"]["truncated"]
    passed = not query_truncated and (
        predicted_reachable and route_matched and not missing_evidence
        if case["expected_reachable"]
        else not predicted_reachable
    )
    restricted_leaks = sum(
        "restricted" in hop["classifications"]
        for path in projection["paths"]
        for hop in path["hops"]
    )
    return {
        "case_id": case["case_id"],
        "passed": passed,
        "expected_reachable": case["expected_reachable"],
        "predicted_reachable": predicted_reachable,
        "route_matched": route_matched,
        "returned_path_count": len(projection["paths"]),
        "query_truncated": query_truncated,
        "expected_supporting_evidence_count": len(expected_evidence),
        "found_expected_supporting_evidence_count": (
            len(expected_evidence) - len(missing_evidence)
        ),
        "missing_expected_supporting_evidence_ids": missing_evidence,
        "restricted_support_leak_count": restricted_leaks,
    }


def _relationship_direction(relationship: dict, case: dict) -> str | None:
    source = case["source_entity_id"]
    target = case["target_entity_id"]
    relationship_source = relationship["source_entity_id"]
    relationship_target = relationship["target_entity_id"]
    if not relationship["directed"]:
        if {relationship_source, relationship_target} == {source, target}:
            return "undirected"
        return None
    if relationship_source == source and relationship_target == target:
        return "forward"
    if relationship_source == target and relationship_target == source:
        return "reverse"
    return None


def _validate_current_references(database: Database, dataset: dict) -> None:
    entity_ids = {
        entity_id
        for case in [
            *dataset["relation_cases"],
            *dataset["path_cases"],
        ]
        for entity_id in (
            case["source_entity_id"],
            case["target_entity_id"],
        )
    }
    entity_ids.update(
        entity_id
        for case in dataset["path_cases"]
        for entity_id in case["expected_entity_ids"]
    )
    evidence_ids = {
        evidence_id
        for case in [
            *dataset["relation_cases"],
            *dataset["path_cases"],
        ]
        for evidence_id in case["expected_supporting_evidence_ids"]
    }
    with database.connect() as connection:
        for entity_id in sorted(entity_ids):
            entity = connection.execute(
                """
                SELECT id FROM canonical_entities
                WHERE id = ? AND status = 'active'
                """,
                (entity_id,),
            ).fetchone()
            if not entity:
                raise KnowledgeWorkbenchError(
                    f"图谱评测数据集引用的实体不存在或已归档：{entity_id}"
                )
        for evidence_id in sorted(evidence_ids):
            evidence = connection.execute(
                """
                SELECT e.status, d.classification,
                       CASE WHEN pr.is_current = 1
                                  AND d.current_version_id = dv.id
                            THEN 1 ELSE 0 END AS source_is_current
                FROM evidence e
                JOIN processing_runs pr ON pr.id = e.processing_run_id
                JOIN document_versions dv ON dv.id = e.document_version_id
                JOIN documents d ON d.id = dv.document_id
                WHERE e.id = ?
                """,
                (evidence_id,),
            ).fetchone()
            if (
                not evidence
                or evidence["status"] != "verified"
                or not evidence["source_is_current"]
                or evidence["classification"] == "restricted"
            ):
                raise KnowledgeWorkbenchError(
                    "图谱评测数据集引用的证据不存在、已过期、"
                    f"未验证或为 restricted：{evidence_id}"
                )


def _confusion(results: list[dict], expected_key: str) -> dict:
    predicted_key = (
        "predicted_present"
        if expected_key == "expected_present"
        else "predicted_reachable"
    )
    counts = {
        "true_positive": 0,
        "false_positive": 0,
        "true_negative": 0,
        "false_negative": 0,
    }
    for result in results:
        expected = result[expected_key]
        predicted = result[predicted_key]
        if expected and predicted:
            counts["true_positive"] += 1
        elif expected:
            counts["false_negative"] += 1
        elif predicted:
            counts["false_positive"] += 1
        else:
            counts["true_negative"] += 1
    return counts


def _precision(confusion: dict) -> float:
    predicted_positive = (
        confusion["true_positive"] + confusion["false_positive"]
    )
    return _rate(confusion["true_positive"], predicted_positive)


def _recall(confusion: dict) -> float:
    actual_positive = (
        confusion["true_positive"] + confusion["false_negative"]
    )
    return _rate(confusion["true_positive"], actual_positive)


def _rate(numerator: int, denominator: int) -> float:
    return round(1.0 if denominator == 0 else numerator / denominator, 6)
