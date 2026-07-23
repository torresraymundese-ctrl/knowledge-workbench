import json
import tempfile
import unittest
from pathlib import Path

from knowledge_workbench.config import WorkspacePaths
from knowledge_workbench.database import Database
from knowledge_workbench.entities import (
    add_entity_alias,
    create_entity,
    get_entity,
    link_evidence_entity,
    list_entities,
    normalize_entity_name,
    remove_entity_alias,
    unlink_evidence_entity,
)
from knowledge_workbench.errors import KnowledgeWorkbenchError
from knowledge_workbench.ingest import ingest_file
from knowledge_workbench.linting import lint_workspace
from knowledge_workbench.models import Classification


class EntityNormalizationTests(unittest.TestCase):
    def test_normalization_uses_nfkc_casefold_and_collapsed_whitespace(self):
        self.assertEqual(normalize_entity_name("  ＡＣＭＥ   Corp  "), "acme corp")

    def test_manual_aliases_are_unique_per_type_and_audited_without_raw_names(self):
        with tempfile.TemporaryDirectory() as temporary:
            paths = WorkspacePaths(Path(temporary) / "workspace")
            database = Database(paths.database)
            database.initialize("t1")
            first = create_entity(
                database, "产权交易平台", "project", actor="curator-01"
            )
            alias_id = add_entity_alias(
                database, first, "交易平台升级", actor="curator-01"
            )
            self.assertEqual(
                add_entity_alias(
                    database, first, "交易平台升级", actor="curator-01"
                ),
                alias_id,
            )
            second = create_entity(
                database, "另一个项目", "project", actor="curator-01"
            )
            with self.assertRaisesRegex(KnowledgeWorkbenchError, "另一个规范实体"):
                add_entity_alias(
                    database, second, "交易平台升级", actor="curator-01"
                )
            organization = create_entity(
                database, "交易平台主管部门", "organization", actor="curator-01"
            )
            self.assertTrue(
                add_entity_alias(
                    database,
                    organization,
                    "交易平台升级",
                    actor="curator-01",
                ).startswith("alias_")
            )

            detail = get_entity(database, first)
            self.assertEqual(
                [item["alias"] for item in detail["aliases"]],
                ["产权交易平台", "交易平台升级"],
            )
            with database.connect() as connection:
                audit = connection.execute(
                    """
                    SELECT details_json FROM audit_log
                    WHERE event_type = 'entity_alias_added' AND entity_id = ?
                    """,
                    (first,),
                ).fetchone()
            self.assertNotIn("交易平台升级", audit["details_json"])
            self.assertIn("alias_sha256", json.loads(audit["details_json"]))

    def test_evidence_link_requires_registered_verbatim_alias_and_is_reversible(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = WorkspacePaths(root / "workspace")
            source = root / "source.md"
            source.write_text("交易平台升级必须保留完整审计记录。", encoding="utf-8")
            result = ingest_file(source, paths, Classification.INTERNAL)
            database = Database(paths.database)
            with database.connect() as connection:
                evidence_id = connection.execute(
                    """
                    SELECT id FROM evidence
                    WHERE processing_run_id = ? ORDER BY run_ordinal LIMIT 1
                    """,
                    (result.processing_run_id,),
                ).fetchone()[0]
            entity_id = create_entity(
                database, "产权交易平台", "project", actor="curator-01"
            )
            add_entity_alias(
                database, entity_id, "交易平台升级", actor="curator-01"
            )

            with self.assertRaisesRegex(KnowledgeWorkbenchError, "尚未登记"):
                link_evidence_entity(
                    database,
                    entity_id,
                    evidence_id,
                    "交易系统",
                    actor="curator-01",
                )
            with self.assertRaisesRegex(KnowledgeWorkbenchError, "逐字出现在"):
                link_evidence_entity(
                    database,
                    entity_id,
                    evidence_id,
                    "产权交易平台",
                    actor="curator-01",
                )

            self.assertTrue(
                link_evidence_entity(
                    database,
                    entity_id,
                    evidence_id,
                    "交易平台升级",
                    actor="curator-01",
                )
            )
            self.assertFalse(
                link_evidence_entity(
                    database,
                    entity_id,
                    evidence_id,
                    "交易平台升级",
                    actor="curator-01",
                )
            )
            self.assertEqual(list_entities(database)[0]["current_evidence_count"], 1)
            with self.assertRaisesRegex(KnowledgeWorkbenchError, "仍被证据关联"):
                remove_entity_alias(
                    database, entity_id, "交易平台升级", actor="curator-01"
                )

            self.assertEqual(
                unlink_evidence_entity(
                    database, entity_id, evidence_id, actor="curator-01"
                ),
                1,
            )
            remove_entity_alias(
                database, entity_id, "交易平台升级", actor="curator-01"
            )
            self.assertEqual(get_entity(database, entity_id)["current_evidence_count"], 0)

    def test_historical_evidence_cannot_receive_new_entity_links(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = WorkspacePaths(root / "workspace")
            source = root / "source.md"
            source.write_text("甲项目启动。", encoding="utf-8")
            first = ingest_file(source, paths, Classification.INTERNAL)
            database = Database(paths.database)
            with database.connect() as connection:
                old_evidence_id = connection.execute(
                    "SELECT id FROM evidence WHERE processing_run_id = ?",
                    (first.processing_run_id,),
                ).fetchone()[0]
            source.write_text("甲项目已经启动。", encoding="utf-8")
            ingest_file(source, paths, Classification.INTERNAL)
            entity_id = create_entity(
                database, "甲项目", "project", actor="curator-01"
            )
            with self.assertRaisesRegex(KnowledgeWorkbenchError, "不是当前"):
                link_evidence_entity(
                    database,
                    entity_id,
                    old_evidence_id,
                    "甲项目",
                    actor="curator-01",
                )

    def test_lint_detects_entity_mention_that_no_longer_matches_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = WorkspacePaths(root / "workspace")
            source = root / "source.md"
            source.write_text("甲项目启动。", encoding="utf-8")
            result = ingest_file(source, paths, Classification.INTERNAL)
            database = Database(paths.database)
            entity_id = create_entity(
                database, "甲项目", "project", actor="curator-01"
            )
            with database.transaction() as connection:
                evidence_id = connection.execute(
                    "SELECT id FROM evidence WHERE processing_run_id = ?",
                    (result.processing_run_id,),
                ).fetchone()[0]
                alias_id = connection.execute(
                    "SELECT id FROM entity_aliases WHERE entity_id = ?",
                    (entity_id,),
                ).fetchone()[0]
                connection.execute(
                    """
                    INSERT INTO evidence_entity_mentions(
                        evidence_id, entity_id, alias_id, mention_text,
                        created_by, created_at
                    ) VALUES (?, ?, ?, '伪造提及', 'tamper', 't1')
                    """,
                    (evidence_id, entity_id, alias_id),
                )

            report = lint_workspace(database, paths)
            self.assertFalse(report["passed"])
            self.assertIn(
                "evidence_entity_mention_invalid",
                {issue["code"] for issue in report["issues"]},
            )


if __name__ == "__main__":
    unittest.main()
