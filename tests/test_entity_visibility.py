import tempfile
import unittest
from pathlib import Path

from knowledge_workbench.config import WorkspacePaths
from knowledge_workbench.database import Database
from knowledge_workbench.entities import create_entity, link_evidence_entity
from knowledge_workbench.entity_visibility import (
    get_entity_visibility,
    list_entity_visibility,
)
from knowledge_workbench.errors import KnowledgeWorkbenchError
from knowledge_workbench.ingest import ingest_file
from knowledge_workbench.models import Classification


class EntityVisibilityTests(unittest.TestCase):
    def test_visibility_uses_highest_classification_across_all_evidence_history(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = WorkspacePaths(root / "workspace")
            evidence_ids = {}
            source_paths = {}
            for classification, text in (
                (Classification.PUBLIC, "统一平台和公开系统。"),
                (Classification.CONFIDENTIAL, "机密系统。"),
                (Classification.RESTRICTED, "统一平台属于受限范围。"),
            ):
                source = root / f"{classification.value}.md"
                source.write_text(text, encoding="utf-8")
                source_paths[classification.value] = source
                result = ingest_file(source, paths, classification)
                database = Database(paths.database)
                with database.connect() as connection:
                    evidence_ids[classification.value] = connection.execute(
                        "SELECT id FROM evidence WHERE processing_run_id = ?",
                        (result.processing_run_id,),
                    ).fetchone()[0]
            database = Database(paths.database)
            mixed = create_entity(
                database, "统一平台", "project", actor="curator-01"
            )
            public = create_entity(
                database, "公开系统", "project", actor="curator-01"
            )
            confidential = create_entity(
                database, "机密系统", "project", actor="curator-01"
            )
            unclassified = create_entity(
                database, "待分类系统", "project", actor="curator-01"
            )
            link_evidence_entity(
                database,
                mixed,
                evidence_ids["public"],
                "统一平台",
                actor="curator-01",
            )
            link_evidence_entity(
                database,
                mixed,
                evidence_ids["restricted"],
                "统一平台",
                actor="curator-01",
            )
            link_evidence_entity(
                database,
                public,
                evidence_ids["public"],
                "公开系统",
                actor="curator-01",
            )
            link_evidence_entity(
                database,
                confidential,
                evidence_ids["confidential"],
                "机密系统",
                actor="curator-01",
            )
            source_paths["restricted"].write_text(
                "受限范围已更新，不再重复实体名称。", encoding="utf-8"
            )
            ingest_file(
                source_paths["restricted"], paths, Classification.RESTRICTED
            )

            mixed_visibility = get_entity_visibility(database, mixed)
            self.assertEqual(mixed_visibility["visibility"], "restricted")
            self.assertFalse(mixed_visibility["web_visible"])
            self.assertEqual(
                mixed_visibility["evidence_by_classification"],
                {
                    "public": 1,
                    "internal": 0,
                    "confidential": 0,
                    "restricted": 1,
                },
            )
            self.assertEqual(
                get_entity_visibility(database, public)["visibility"], "public"
            )
            self.assertTrue(
                get_entity_visibility(database, confidential)["web_visible"]
            )
            self.assertEqual(
                get_entity_visibility(database, unclassified)["visibility"],
                "unclassified",
            )
            self.assertFalse(
                get_entity_visibility(database, unclassified)["web_visible"]
            )
            visible_ids = {
                row["entity_id"]
                for row in list_entity_visibility(
                    database, web_visible_only=True
                )
            }
            self.assertEqual(visible_ids, {public, confidential})

    def test_visibility_validates_filters_and_limits(self):
        with tempfile.TemporaryDirectory() as temporary:
            paths = WorkspacePaths(Path(temporary) / "workspace")
            database = Database(paths.database)
            database.initialize("t1")
            create_entity(
                database, "甲项目", "project", actor="curator-01"
            )
            self.assertEqual(
                len(list_entity_visibility(database, entity_type="project")), 1
            )
            self.assertEqual(
                list_entity_visibility(database, entity_type="organization"), []
            )
            with self.assertRaisesRegex(KnowledgeWorkbenchError, "实体类型"):
                list_entity_visibility(database, entity_type="unknown")
            with self.assertRaisesRegex(KnowledgeWorkbenchError, "1 到 500"):
                list_entity_visibility(database, limit=0)
            with self.assertRaisesRegex(KnowledgeWorkbenchError, "不存在"):
                get_entity_visibility(database, "entity_missing")


if __name__ == "__main__":
    unittest.main()
