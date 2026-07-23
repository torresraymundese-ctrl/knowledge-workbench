import tempfile
import unittest
from pathlib import Path

from knowledge_workbench.config import WorkspacePaths
from knowledge_workbench.database import Database
from knowledge_workbench.entities import add_entity_alias, create_entity
from knowledge_workbench.entity_merges import (
    propose_entity_merge,
    review_entity_merge,
)
from knowledge_workbench.entity_similarity import (
    project_similar_entity_candidates,
)
from knowledge_workbench.errors import KnowledgeWorkbenchError


class EntitySimilarityTests(unittest.TestCase):
    def test_projection_is_deterministic_read_only_and_shows_review_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            paths = WorkspacePaths(Path(temporary) / "workspace")
            database = Database(paths.database)
            database.initialize("t1")
            platform = create_entity(
                database, "产权交易平台", "project", actor="curator-01"
            )
            add_entity_alias(
                database, platform, "交易平台升级", actor="curator-01"
            )
            system = create_entity(
                database, "产权交易系统", "project", actor="curator-01"
            )
            service = create_entity(
                database, "产权交易服务", "project", actor="curator-01"
            )
            create_entity(
                database, "完全无关事项", "project", actor="curator-01"
            )
            create_entity(
                database, "产权交易平台公司", "organization", actor="curator-01"
            )
            request_id = propose_entity_merge(
                database,
                platform,
                system,
                actor="curator-01",
                note="提交人工核对",
            )
            with database.connect() as connection:
                audit_before = connection.execute(
                    "SELECT COUNT(*) FROM audit_log"
                ).fetchone()[0]

            first = project_similar_entity_candidates(
                database, entity_type="project", minimum_similarity=0.6, limit=20
            )
            second = project_similar_entity_candidates(
                database, entity_type="project", minimum_similarity=0.6, limit=20
            )
            self.assertEqual(first, second)
            self.assertEqual(first["kind"], "similar-entity-merge-candidate-projection")
            self.assertEqual(first["statistics"]["active_entity_count"], 4)
            self.assertGreaterEqual(first["statistics"]["total_candidate_count"], 3)
            pair = next(
                item
                for item in first["items"]
                if {
                    item["left"]["entity_id"],
                    item["right"]["entity_id"],
                }
                == {platform, system}
            )
            self.assertGreaterEqual(pair["similarity"], 0.6)
            self.assertEqual(pair["entity_type"], "project")
            self.assertEqual(pair["merge_request"]["request_id"], request_id)
            self.assertEqual(pair["merge_request"]["status"], "reviewing")
            self.assertNotIn(
                "产权交易平台公司",
                {
                    side["canonical_name"]
                    for item in first["items"]
                    for side in (item["left"], item["right"])
                },
            )
            limited = project_similar_entity_candidates(
                database, entity_type="project", minimum_similarity=0.6, limit=1
            )
            self.assertEqual(len(limited["items"]), 1)
            self.assertTrue(limited["statistics"]["truncated"])
            with database.connect() as connection:
                audit_after = connection.execute(
                    "SELECT COUNT(*) FROM audit_log"
                ).fetchone()[0]
            self.assertEqual(audit_after, audit_before)

            review_entity_merge(
                database,
                request_id,
                "approve",
                actor="reviewer-01",
                note="确认同一项目",
            )
            after_merge = project_similar_entity_candidates(
                database, entity_type="project", minimum_similarity=0.0, limit=50
            )
            projected_ids = {
                side["entity_id"]
                for item in after_merge["items"]
                for side in (item["left"], item["right"])
            }
            self.assertNotIn(platform, projected_ids)
            self.assertIn(system, projected_ids)
            self.assertIn(service, projected_ids)

    def test_projection_bounds_inputs_and_skips_high_frequency_blocks(self):
        with tempfile.TemporaryDirectory() as temporary:
            paths = WorkspacePaths(Path(temporary) / "workspace")
            database = Database(paths.database)
            database.initialize("t1")
            for index in range(80):
                create_entity(
                    database,
                    f"公共项目编号{index:03d}专用名称",
                    "project",
                    actor="curator-01",
                )
            projection = project_similar_entity_candidates(
                database, minimum_similarity=0.0, limit=500
            )
            statistics = projection["statistics"]
            self.assertEqual(statistics["active_entity_count"], 80)
            self.assertGreater(statistics["skipped_frequent_block_count"], 0)
            self.assertLess(statistics["blocked_pair_count"], 80 * 79 // 2)
            with self.assertRaisesRegex(KnowledgeWorkbenchError, "0 到 1"):
                project_similar_entity_candidates(
                    database, minimum_similarity=1.1
                )
            with self.assertRaisesRegex(KnowledgeWorkbenchError, "1 到 500"):
                project_similar_entity_candidates(database, limit=0)
            with self.assertRaisesRegex(KnowledgeWorkbenchError, "实体类型"):
                project_similar_entity_candidates(database, entity_type="unknown")


if __name__ == "__main__":
    unittest.main()
