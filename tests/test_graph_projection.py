import tempfile
import unittest
from pathlib import Path

from knowledge_workbench.config import WorkspacePaths
from knowledge_workbench.database import Database
from knowledge_workbench.entities import create_entity, link_evidence_entity
from knowledge_workbench.errors import KnowledgeWorkbenchError
from knowledge_workbench.graph_projection import project_entity_graph
from knowledge_workbench.ingest import ingest_file
from knowledge_workbench.models import Classification, EvidenceStatus
from knowledge_workbench.review import transition_evidence


class GraphProjectionTests(unittest.TestCase):
    def test_projection_uses_current_verified_nonrestricted_atomic_cooccurrence(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = WorkspacePaths(root / "workspace")
            source = root / "source.md"
            source.write_text(
                "甲公司负责甲项目。\n\n甲项目位于甲地点。",
                encoding="utf-8",
            )
            result = ingest_file(source, paths, Classification.INTERNAL)
            database = Database(paths.database)
            evidence = _evidence_by_excerpt(database, result.processing_run_id)
            company = create_entity(
                database, "甲公司", "organization", actor="curator-01"
            )
            project = create_entity(
                database, "甲项目", "project", actor="curator-01"
            )
            location = create_entity(
                database, "甲地点", "location", actor="curator-01"
            )
            _link(
                database,
                evidence["甲公司负责甲项目。"],
                ((company, "甲公司"), (project, "甲项目")),
            )
            _link(
                database,
                evidence["甲项目位于甲地点。"],
                ((project, "甲项目"), (location, "甲地点")),
            )

            empty = project_entity_graph(database)
            self.assertEqual(empty["summary"]["eligible_node_count"], 0)
            review_graph = project_entity_graph(database, include_unverified=True)
            self.assertEqual(review_graph["summary"]["eligible_node_count"], 3)
            self.assertEqual(review_graph["summary"]["eligible_edge_count"], 2)
            self.assertEqual(
                {edge["relationship"] for edge in review_graph["edges"]},
                {"co_occurs_in_atomic_evidence"},
            )

            for evidence_id in evidence.values():
                transition_evidence(
                    database, evidence_id, EvidenceStatus.REVIEWING, actor="reviewer-01"
                )
                transition_evidence(
                    database, evidence_id, EvidenceStatus.VERIFIED, actor="reviewer-01"
                )
            verified = project_entity_graph(database)
            self.assertEqual(verified["summary"]["eligible_node_count"], 3)
            self.assertEqual(verified["summary"]["eligible_edge_count"], 2)
            self.assertEqual(
                {tuple(edge["classifications"]) for edge in verified["edges"]},
                {("internal",)},
            )
            focal = project_entity_graph(database, entity_id=company)
            self.assertEqual(focal["summary"]["eligible_edge_count"], 1)
            self.assertEqual(
                {node["id"] for node in focal["nodes"]}, {company, project}
            )
            self.assertEqual(
                focal["edges"][0]["supporting_evidence_ids"],
                [evidence["甲公司负责甲项目。"]],
            )

    def test_restricted_and_historical_support_never_enters_projection(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = WorkspacePaths(root / "workspace")
            internal_source = root / "internal.md"
            restricted_source = root / "restricted.md"
            internal_source.write_text("甲公司负责甲项目。", encoding="utf-8")
            restricted_source.write_text("乙公司负责乙项目。", encoding="utf-8")
            internal = ingest_file(
                internal_source, paths, Classification.INTERNAL
            )
            restricted = ingest_file(
                restricted_source, paths, Classification.RESTRICTED
            )
            database = Database(paths.database)
            internal_evidence = next(
                iter(_evidence_by_excerpt(database, internal.processing_run_id).values())
            )
            restricted_evidence = next(
                iter(_evidence_by_excerpt(database, restricted.processing_run_id).values())
            )
            internal_entities = (
                (create_entity(database, "甲公司", "organization", actor="curator"), "甲公司"),
                (create_entity(database, "甲项目", "project", actor="curator"), "甲项目"),
            )
            restricted_entities = (
                (create_entity(database, "乙公司", "organization", actor="curator"), "乙公司"),
                (create_entity(database, "乙项目", "project", actor="curator"), "乙项目"),
            )
            _link(database, internal_evidence, internal_entities)
            _link(database, restricted_evidence, restricted_entities)
            for evidence_id in (internal_evidence, restricted_evidence):
                transition_evidence(
                    database, evidence_id, EvidenceStatus.REVIEWING, actor="reviewer"
                )
                transition_evidence(
                    database, evidence_id, EvidenceStatus.VERIFIED, actor="reviewer"
                )

            graph = project_entity_graph(database)
            self.assertEqual(
                {node["canonical_name"] for node in graph["nodes"]},
                {"甲公司", "甲项目"},
            )
            self.assertEqual(graph["summary"]["restricted_support_excluded"], 2)
            with self.assertRaisesRegex(KnowledgeWorkbenchError, "密级边界"):
                project_entity_graph(database, entity_id=restricted_entities[0][0])

            internal_source.write_text("甲公司已经负责甲项目。", encoding="utf-8")
            ingest_file(internal_source, paths, Classification.INTERNAL)
            after_version_change = project_entity_graph(database)
            self.assertEqual(after_version_change["summary"]["eligible_node_count"], 0)
            self.assertEqual(after_version_change["summary"]["eligible_edge_count"], 0)

    def test_limit_is_bounded(self):
        with tempfile.TemporaryDirectory() as temporary:
            database = Database(Path(temporary) / "knowledge.sqlite3")
            database.initialize("t1")
            with self.assertRaisesRegex(KnowledgeWorkbenchError, "1 到 500"):
                project_entity_graph(database, limit=0)


def _evidence_by_excerpt(database: Database, processing_run_id: str) -> dict[str, str]:
    with database.connect() as connection:
        rows = connection.execute(
            "SELECT id, excerpt FROM evidence WHERE processing_run_id = ?",
            (processing_run_id,),
        ).fetchall()
    return {row["excerpt"]: row["id"] for row in rows}


def _link(database: Database, evidence_id: str, entities: tuple[tuple[str, str], ...]) -> None:
    for entity_id, mention in entities:
        link_evidence_entity(
            database,
            entity_id,
            evidence_id,
            mention,
            actor="curator-01",
        )


if __name__ == "__main__":
    unittest.main()
