import tempfile
import unittest
from pathlib import Path

from knowledge_workbench.config import WorkspacePaths
from knowledge_workbench.database import Database
from knowledge_workbench.entities import (
    create_entity,
    link_evidence_entity,
    unlink_evidence_entity,
)
from knowledge_workbench.entity_merges import (
    propose_entity_merge,
    review_entity_merge,
)
from knowledge_workbench.entity_relationships import (
    create_entity_relationship,
    create_relation_type,
    list_entity_relationships,
    list_relation_types,
    project_business_relationship_graph,
    retract_entity_relationship,
)
from knowledge_workbench.errors import KnowledgeWorkbenchError
from knowledge_workbench.ingest import ingest_file
from knowledge_workbench.linting import lint_workspace
from knowledge_workbench.models import Classification, EvidenceStatus
from knowledge_workbench.review import transition_evidence


class EntityRelationshipTests(unittest.TestCase):
    def test_manual_relationship_requires_verified_dual_mentions_and_projects(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = WorkspacePaths(root / "workspace")
            source = root / "relationship.md"
            source.write_text("甲公司负责甲项目。", encoding="utf-8")
            ingestion = ingest_file(source, paths, Classification.INTERNAL)
            database = Database(paths.database)
            with database.connect() as connection:
                evidence_id = connection.execute(
                    "SELECT id FROM evidence WHERE processing_run_id = ?",
                    (ingestion.processing_run_id,),
                ).fetchone()[0]
            organization = create_entity(
                database, "甲公司", "organization", actor="curator-01"
            )
            project = create_entity(
                database, "甲项目", "project", actor="curator-01"
            )
            link_evidence_entity(
                database,
                organization,
                evidence_id,
                "甲公司",
                actor="curator-01",
            )
            create_relation_type(
                database,
                "responsible_for",
                "负责",
                inverse_label="由其负责",
                directed=True,
                actor="curator-01",
            )
            with self.assertRaisesRegex(
                KnowledgeWorkbenchError, "verified"
            ):
                create_entity_relationship(
                    database,
                    "responsible_for",
                    organization,
                    project,
                    [evidence_id],
                    actor="curator-01",
                    note="证据尚未审核",
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
            with self.assertRaisesRegex(
                KnowledgeWorkbenchError, "同时关联"
            ):
                create_entity_relationship(
                    database,
                    "responsible_for",
                    organization,
                    project,
                    [evidence_id],
                    actor="curator-01",
                    note="目标实体尚未关联",
                )
            link_evidence_entity(
                database,
                project,
                evidence_id,
                "甲项目",
                actor="curator-01",
            )
            relationship_id = create_entity_relationship(
                database,
                "responsible_for",
                organization,
                project,
                [evidence_id],
                actor="curator-01",
                note="已回源确认明确负责关系",
            )
            relationship = list_entity_relationships(database)[0]
            self.assertEqual(relationship["relationship_id"], relationship_id)
            self.assertEqual(relationship["label"], "负责")
            self.assertTrue(relationship["directed"])
            self.assertEqual(
                relationship["current_verified_supporting_evidence_ids"],
                [evidence_id],
            )
            self.assertEqual(
                relationship["projectable_supporting_evidence_ids"],
                [evidence_id],
            )
            projection = project_business_relationship_graph(database)
            self.assertEqual(projection["summary"]["eligible_edge_count"], 1)
            self.assertEqual(
                projection["edges"][0]["relation_key"], "responsible_for"
            )
            self.assertEqual(
                projection["edges"][0]["supporting_evidence_ids"],
                [evidence_id],
            )
            restricted_source = root / "restricted.md"
            restricted_source.write_text(
                "甲公司为甲项目提供受限支持。", encoding="utf-8"
            )
            restricted_ingestion = ingest_file(
                restricted_source, paths, Classification.RESTRICTED
            )
            with database.connect() as connection:
                restricted_evidence_id = connection.execute(
                    "SELECT id FROM evidence WHERE processing_run_id = ?",
                    (restricted_ingestion.processing_run_id,),
                ).fetchone()[0]
            for entity_id, mention in (
                (organization, "甲公司"),
                (project, "甲项目"),
            ):
                link_evidence_entity(
                    database,
                    entity_id,
                    restricted_evidence_id,
                    mention,
                    actor="curator-01",
                )
            transition_evidence(
                database,
                restricted_evidence_id,
                EvidenceStatus.REVIEWING,
                actor="reviewer-01",
            )
            transition_evidence(
                database,
                restricted_evidence_id,
                EvidenceStatus.VERIFIED,
                actor="reviewer-01",
            )
            create_relation_type(
                database,
                "restricted_support",
                "受限支持",
                directed=True,
                actor="curator-01",
            )
            create_entity_relationship(
                database,
                "restricted_support",
                organization,
                project,
                [restricted_evidence_id],
                actor="curator-01",
                note="仅受限资料支持",
            )
            self.assertEqual(len(list_entity_relationships(database)), 2)
            projection = project_business_relationship_graph(database)
            self.assertEqual(
                {edge["relation_key"] for edge in projection["edges"]},
                {"responsible_for"},
            )
            with database.connect() as connection:
                audits = connection.execute(
                    """
                    SELECT details_json FROM audit_log
                    WHERE event_type IN (
                        'entity_relation_type_created',
                        'entity_relationship_created'
                    )
                    ORDER BY id
                    """
                ).fetchall()
            audit_text = "\n".join(row["details_json"] for row in audits)
            self.assertNotIn("已回源确认", audit_text)
            self.assertNotIn('"负责"', audit_text)
            self.assertIn("creation_note_sha256", audit_text)
            self.assertTrue(lint_workspace(database, paths)["passed"])

    def test_undirected_normalization_retraction_staleness_and_merge_guard(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = WorkspacePaths(root / "workspace")
            source = root / "partners.md"
            source.write_text("乙公司与甲公司合作。", encoding="utf-8")
            ingestion = ingest_file(source, paths, Classification.INTERNAL)
            database = Database(paths.database)
            with database.connect() as connection:
                evidence_id = connection.execute(
                    "SELECT id FROM evidence WHERE processing_run_id = ?",
                    (ingestion.processing_run_id,),
                ).fetchone()[0]
            entity_a = create_entity(
                database, "甲公司", "organization", actor="curator-01"
            )
            entity_b = create_entity(
                database, "乙公司", "organization", actor="curator-01"
            )
            for entity_id, mention in (
                (entity_a, "甲公司"),
                (entity_b, "乙公司"),
            ):
                link_evidence_entity(
                    database,
                    entity_id,
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
            create_relation_type(
                database,
                "cooperates_with",
                "合作",
                directed=False,
                actor="curator-01",
            )
            self.assertFalse(list_relation_types(database)[0]["directed"])
            relationship_id = create_entity_relationship(
                database,
                "cooperates_with",
                entity_b,
                entity_a,
                [evidence_id, evidence_id],
                actor="curator-01",
                note="双方合作",
            )
            relationship = list_entity_relationships(database)[0]
            self.assertEqual(
                (
                    relationship["source_entity_id"],
                    relationship["target_entity_id"],
                ),
                tuple(sorted((entity_a, entity_b))),
            )
            with self.assertRaisesRegex(
                KnowledgeWorkbenchError, "active 业务关系已存在"
            ):
                create_entity_relationship(
                    database,
                    "cooperates_with",
                    entity_a,
                    entity_b,
                    [evidence_id],
                    actor="curator-02",
                    note="反向重复",
                )

            merge_request = propose_entity_merge(
                database,
                relationship["source_entity_id"],
                relationship["target_entity_id"],
                actor="curator-01",
                note="尝试合并",
            )
            with self.assertRaisesRegex(
                KnowledgeWorkbenchError, "业务关系历史"
            ):
                review_entity_merge(
                    database,
                    merge_request,
                    "approve",
                    actor="reviewer-02",
                    note="不应静默迁移语义关系",
                )
            with self.assertRaisesRegex(
                KnowledgeWorkbenchError, "关系历史引用"
            ):
                unlink_evidence_entity(
                    database,
                    relationship["source_entity_id"],
                    evidence_id,
                    actor="curator-02",
                )

            retract_entity_relationship(
                database,
                relationship_id,
                actor="curator-02",
                note="关系适用期结束",
            )
            with self.assertRaisesRegex(
                KnowledgeWorkbenchError, "业务关系历史"
            ):
                review_entity_merge(
                    database,
                    merge_request,
                    "approve",
                    actor="reviewer-02",
                    note="撤销后仍不能改写历史",
                )
            with self.assertRaisesRegex(
                KnowledgeWorkbenchError, "已撤销"
            ):
                retract_entity_relationship(
                    database,
                    relationship_id,
                    actor="curator-03",
                    note="重复撤销",
                )
            self.assertEqual(
                list_entity_relationships(database, status="retracted")[0][
                    "supporting_evidence_ids"
                ],
                [evidence_id],
            )
            replacement_id = create_entity_relationship(
                database,
                "cooperates_with",
                entity_a,
                entity_b,
                [evidence_id],
                actor="curator-03",
                note="新适用期重新登记",
            )
            self.assertNotEqual(replacement_id, relationship_id)
            source.write_text("合作情况已更新。", encoding="utf-8")
            ingest_file(source, paths, Classification.INTERNAL)
            stale = list_entity_relationships(database)[0]
            self.assertEqual(
                stale["current_verified_supporting_evidence_count"], 0
            )
            self.assertEqual(
                project_business_relationship_graph(database)["edges"], []
            )
            lint = lint_workspace(database, paths)
            self.assertTrue(lint["passed"])
            self.assertIn(
                "entity_relationship_needs_revalidation",
                {issue["code"] for issue in lint["issues"]},
            )
            with database.connect() as connection:
                raw = connection.execute(
                    """
                    SELECT creation_note_sha256, retraction_note_sha256
                    FROM entity_relationships WHERE id = ?
                    """,
                    (relationship_id,),
                ).fetchone()
            self.assertEqual(len(raw["creation_note_sha256"]), 64)
            self.assertEqual(len(raw["retraction_note_sha256"]), 64)


if __name__ == "__main__":
    unittest.main()
