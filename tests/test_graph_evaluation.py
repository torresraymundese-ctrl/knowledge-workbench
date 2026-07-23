import json
import tempfile
import unittest
from pathlib import Path

from knowledge_workbench.config import WorkspacePaths
from knowledge_workbench.entities import create_entity, link_evidence_entity
from knowledge_workbench.entity_relationships import (
    create_entity_relationship,
    create_relation_type,
)
from knowledge_workbench.errors import KnowledgeWorkbenchError
from knowledge_workbench.graph_evaluation import evaluate_graph_dataset
from knowledge_workbench.ingest import ingest_file, initialize_workspace
from knowledge_workbench.models import Classification, EvidenceStatus
from knowledge_workbench.review import transition_evidence


class GraphEvaluationTests(unittest.TestCase):
    def test_evaluates_reviewed_relations_paths_and_current_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = WorkspacePaths(root / "workspace")
            database = initialize_workspace(paths)
            entities = {
                name: create_entity(
                    database, name, "organization", actor="curator-01"
                )
                for name in ("甲方", "乙方", "丙方")
            }

            def add_evidence(
                filename: str, text: str, mentions: tuple[str, str]
            ) -> tuple[Path, str]:
                source = root / filename
                source.write_text(text, encoding="utf-8")
                ingestion = ingest_file(
                    source, paths, Classification.INTERNAL
                )
                with database.connect() as connection:
                    evidence_id = connection.execute(
                        "SELECT id FROM evidence WHERE processing_run_id = ?",
                        (ingestion.processing_run_id,),
                    ).fetchone()[0]
                for mention in mentions:
                    link_evidence_entity(
                        database,
                        entities[mention],
                        evidence_id,
                        mention,
                        actor="curator-01",
                    )
                transition_evidence(
                    database,
                    evidence_id,
                    EvidenceStatus.REVIEWING,
                    actor="reviewer-01",
                )
                transition_evidence(
                    database,
                    evidence_id,
                    EvidenceStatus.VERIFIED,
                    actor="reviewer-01",
                )
                return source, evidence_id

            source_ab, evidence_ab = add_evidence(
                "ab.md", "甲方管理乙方。", ("甲方", "乙方")
            )
            _, evidence_bc = add_evidence(
                "bc.md", "乙方与丙方协作。", ("乙方", "丙方")
            )
            create_relation_type(
                database,
                "manages",
                "管理",
                inverse_label="被管理",
                actor="curator-01",
            )
            create_relation_type(
                database,
                "collaborates_with",
                "协作",
                directed=False,
                actor="curator-01",
            )
            create_entity_relationship(
                database,
                "manages",
                entities["甲方"],
                entities["乙方"],
                [evidence_ab],
                actor="curator-01",
                note="回源确认",
            )
            create_entity_relationship(
                database,
                "collaborates_with",
                entities["乙方"],
                entities["丙方"],
                [evidence_bc],
                actor="curator-01",
                note="回源确认",
            )

            dataset = paths.evaluations / "graph-gold.json"
            dataset.write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "name": "graph-pilot",
                        "provenance": {
                            "annotator": "annotator-01",
                            "reviewer": "reviewer-02",
                            "reviewed_at": "2026-07-23T10:00:00+08:00",
                            "decision": "approved",
                        },
                        "relation_cases": [
                            {
                                "case_id": "relation-forward",
                                "source_entity_id": entities["甲方"],
                                "target_entity_id": entities["乙方"],
                                "relation_key": "manages",
                                "expected_present": True,
                                "expected_direction": "forward",
                                "expected_supporting_evidence_ids": [
                                    evidence_ab
                                ],
                            },
                            {
                                "case_id": "relation-undirected",
                                "source_entity_id": entities["乙方"],
                                "target_entity_id": entities["丙方"],
                                "relation_key": "collaborates_with",
                                "expected_present": True,
                                "expected_direction": "undirected",
                                "expected_supporting_evidence_ids": [
                                    evidence_bc
                                ],
                            },
                            {
                                "case_id": "relation-negative",
                                "source_entity_id": entities["甲方"],
                                "target_entity_id": entities["丙方"],
                                "relation_key": "manages",
                                "expected_present": False,
                                "expected_direction": None,
                                "expected_supporting_evidence_ids": [],
                            },
                        ],
                        "path_cases": [
                            {
                                "case_id": "path-forward",
                                "source_entity_id": entities["甲方"],
                                "target_entity_id": entities["丙方"],
                                "max_depth": 2,
                                "include_inverse": False,
                                "expected_reachable": True,
                                "expected_entity_ids": [
                                    entities["甲方"],
                                    entities["乙方"],
                                    entities["丙方"],
                                ],
                                "expected_relation_keys": [
                                    "manages",
                                    "collaborates_with",
                                ],
                                "expected_traversal_directions": [
                                    "forward",
                                    "undirected",
                                ],
                                "expected_supporting_evidence_ids": [
                                    evidence_ab,
                                    evidence_bc,
                                ],
                            },
                            {
                                "case_id": "path-negative",
                                "source_entity_id": entities["丙方"],
                                "target_entity_id": entities["甲方"],
                                "max_depth": 2,
                                "include_inverse": False,
                                "expected_reachable": False,
                                "expected_entity_ids": [],
                                "expected_relation_keys": [],
                                "expected_traversal_directions": [],
                                "expected_supporting_evidence_ids": [],
                            },
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            report = evaluate_graph_dataset(database, dataset)

            self.assertEqual(report["aggregate"]["pass_rate"], 1.0)
            self.assertEqual(
                report["aggregate"]["relationship_direction_accuracy"], 1.0
            )
            self.assertEqual(
                report["aggregate"]["relationship_evidence_coverage"], 1.0
            )
            self.assertEqual(report["aggregate"]["path_recall"], 1.0)
            self.assertEqual(
                report["aggregate"]["path_route_accuracy"], 1.0
            )
            self.assertEqual(
                report["aggregate"]["path_evidence_coverage"], 1.0
            )
            self.assertEqual(
                report["safety"]["restricted_support_leak_count"], 0
            )
            serialized = json.dumps(report, ensure_ascii=False)
            self.assertNotIn("甲方管理乙方", serialized)
            self.assertNotIn("乙方与丙方协作", serialized)

            source_ab.write_text("甲方管理范围已经变更。", encoding="utf-8")
            ingest_file(source_ab, paths, Classification.INTERNAL)
            with self.assertRaisesRegex(
                KnowledgeWorkbenchError, "已过期"
            ):
                evaluate_graph_dataset(database, dataset)

    def test_rejects_self_review_and_duplicate_case_ids(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = WorkspacePaths(root / "workspace")
            database = initialize_workspace(paths)
            dataset = paths.evaluations / "invalid.json"
            case = {
                "case_id": "duplicate",
                "source_entity_id": "entity_a",
                "target_entity_id": "entity_b",
                "relation_key": "related_to",
                "expected_present": False,
                "expected_direction": None,
                "expected_supporting_evidence_ids": [],
            }
            dataset.write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "name": "invalid",
                        "provenance": {
                            "annotator": "same-person",
                            "reviewer": "same-person",
                            "reviewed_at": "2026-07-23T10:00:00+08:00",
                            "decision": "approved",
                        },
                        "relation_cases": [case],
                        "path_cases": [],
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                KnowledgeWorkbenchError, "必须不同"
            ):
                evaluate_graph_dataset(database, dataset)

            payload = json.loads(dataset.read_text(encoding="utf-8"))
            payload["provenance"]["reviewer"] = "reviewer-02"
            payload["relation_cases"].append(case)
            dataset.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(
                KnowledgeWorkbenchError, "重复 case_id"
            ):
                evaluate_graph_dataset(database, dataset)


if __name__ == "__main__":
    unittest.main()
