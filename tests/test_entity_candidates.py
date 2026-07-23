import json
import tempfile
import unittest
from pathlib import Path

from knowledge_workbench.config import WorkspacePaths
from knowledge_workbench.database import Database
from knowledge_workbench.entities import create_entity, get_entity
from knowledge_workbench.entity_candidates import (
    accept_entity_candidate,
    import_entity_candidates,
    list_entity_candidates,
    reject_entity_candidate,
)
from knowledge_workbench.errors import KnowledgeWorkbenchError
from knowledge_workbench.ingest import ingest_file
from knowledge_workbench.linting import lint_workspace
from knowledge_workbench.models import Classification


class EntityCandidateTests(unittest.TestCase):
    def test_import_is_idempotent_and_human_acceptance_creates_alias_and_link(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = WorkspacePaths(root / "workspace")
            source = root / "source.md"
            source.write_text("甲公司负责甲项目。", encoding="utf-8")
            result = ingest_file(source, paths, Classification.INTERNAL)
            analysis_path = _model_analysis(
                paths,
                result.processing_run_id,
                [
                    {"name": "甲公司", "type": "organization"},
                    {"name": "乙公司", "type": "organization"},
                ],
            )
            database = Database(paths.database)

            summary = import_entity_candidates(
                database, analysis_path, actor="curator-01"
            )
            self.assertEqual(summary["candidate_count"], 2)
            self.assertEqual(summary["imported_count"], 2)
            self.assertEqual(summary["existing_count"], 0)
            repeated = import_entity_candidates(
                database, analysis_path, actor="curator-01"
            )
            self.assertEqual(repeated["imported_count"], 0)
            self.assertEqual(repeated["existing_count"], 2)

            candidates = list_entity_candidates(database)
            exact = next(row for row in candidates if row["suggested_name"] == "甲公司")
            non_verbatim = next(
                row for row in candidates if row["suggested_name"] == "乙公司"
            )
            self.assertEqual(exact["verbatim_match"], 1)
            self.assertEqual(non_verbatim["verbatim_match"], 0)

            entity_id = create_entity(
                database, "甲集团", "organization", actor="curator-01"
            )
            accepted = accept_entity_candidate(
                database,
                exact["id"],
                entity_id,
                actor="reviewer-01",
                note="确认是同一组织",
            )
            self.assertTrue(accepted["alias_created"])
            self.assertTrue(accepted["evidence_link_created"])
            self.assertEqual(
                [item["alias"] for item in get_entity(database, entity_id)["aliases"]],
                ["甲集团", "甲公司"],
            )

            with self.assertRaisesRegex(KnowledgeWorkbenchError, "非逐字"):
                accept_entity_candidate(
                    database,
                    non_verbatim["id"],
                    entity_id,
                    actor="reviewer-01",
                )
            reject_entity_candidate(
                database,
                non_verbatim["id"],
                actor="reviewer-01",
                note="原文没有该名称",
            )
            with self.assertRaisesRegex(KnowledgeWorkbenchError, "已完成裁决"):
                reject_entity_candidate(
                    database,
                    non_verbatim["id"],
                    actor="reviewer-01",
                    note="重复驳回",
                )

            with database.connect() as connection:
                accepted_row = connection.execute(
                    "SELECT * FROM entity_candidates WHERE id = ?", (exact["id"],)
                ).fetchone()
                rejected_row = connection.execute(
                    "SELECT * FROM entity_candidates WHERE id = ?",
                    (non_verbatim["id"],),
                ).fetchone()
                audit_rows = connection.execute(
                    """
                    SELECT details_json FROM audit_log
                    WHERE event_type IN (
                        'entity_candidates_imported',
                        'entity_candidate_accepted',
                        'entity_candidate_rejected'
                    )
                    """
                ).fetchall()
            self.assertEqual(accepted_row["status"], "accepted")
            self.assertEqual(accepted_row["resolved_entity_id"], entity_id)
            self.assertEqual(rejected_row["status"], "rejected")
            self.assertIsNotNone(rejected_row["review_note_sha256"])
            audit_text = "\n".join(row["details_json"] for row in audit_rows)
            self.assertNotIn("甲公司", audit_text)
            self.assertNotIn("原文没有该名称", audit_text)

            with database.transaction() as connection:
                connection.execute(
                    "DELETE FROM evidence_entity_mentions WHERE entity_id = ?",
                    (entity_id,),
                )
            lint_report = lint_workspace(database, paths)
            self.assertIn(
                "accepted_entity_candidate_link_missing",
                {issue["code"] for issue in lint_report["issues"]},
            )

    def test_import_rejects_non_model_or_mismatched_source_metadata(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = WorkspacePaths(root / "workspace")
            source = root / "source.md"
            source.write_text("甲项目启动。", encoding="utf-8")
            result = ingest_file(source, paths, Classification.INTERNAL)
            database = Database(paths.database)
            faithful_path = paths.analysis / f"{result.processing_run_id}.analysis.json"
            with self.assertRaisesRegex(KnowledgeWorkbenchError, "model_assisted"):
                import_entity_candidates(
                    database, faithful_path, actor="curator-01"
                )

            analysis_path = _model_analysis(
                paths,
                result.processing_run_id,
                [{"name": "甲项目", "type": "project"}],
            )
            payload = json.loads(analysis_path.read_text(encoding="utf-8"))
            payload["source"]["classification"] = "public"
            analysis_path.write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8"
            )
            with self.assertRaisesRegex(KnowledgeWorkbenchError, "密级"):
                import_entity_candidates(
                    database, analysis_path, actor="curator-01"
                )

    def test_acceptance_is_blocked_when_candidate_source_becomes_historical(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = WorkspacePaths(root / "workspace")
            source = root / "source.md"
            source.write_text("甲项目启动。", encoding="utf-8")
            result = ingest_file(source, paths, Classification.INTERNAL)
            analysis_path = _model_analysis(
                paths,
                result.processing_run_id,
                [{"name": "甲项目", "type": "project"}],
            )
            database = Database(paths.database)
            import_entity_candidates(database, analysis_path, actor="curator-01")
            candidate = list_entity_candidates(database)[0]
            entity_id = create_entity(
                database, "甲项目", "project", actor="curator-01"
            )

            source.write_text("甲项目已经启动。", encoding="utf-8")
            ingest_file(source, paths, Classification.INTERNAL)
            stale = list_entity_candidates(database)[0]
            self.assertEqual(stale["source_is_current"], 0)
            with self.assertRaisesRegex(KnowledgeWorkbenchError, "来源已不是当前"):
                accept_entity_candidate(
                    database,
                    candidate["id"],
                    entity_id,
                    actor="reviewer-01",
                )


def _model_analysis(
    paths: WorkspacePaths,
    processing_run_id: str,
    entities: list[dict],
) -> Path:
    source_path = paths.analysis / f"{processing_run_id}.analysis.json"
    payload = json.loads(source_path.read_text(encoding="utf-8"))
    payload["provenance"] = {
        "mode": "model_assisted",
        "provider": "DeepSeekChatModel",
        "model": "deepseek-chat",
        "prompt_version": "analysis-v3-source-anchored",
    }
    payload["evidence"][0]["entities"] = entities
    output = paths.analysis / f"{processing_run_id}.model-analysis.json"
    output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return output


if __name__ == "__main__":
    unittest.main()
