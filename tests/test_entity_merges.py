import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from knowledge_workbench.config import WorkspacePaths
from knowledge_workbench.database import Database
from knowledge_workbench.entities import (
    add_entity_alias,
    create_entity,
    get_entity,
    link_evidence_entity,
    remove_entity_alias,
)
from knowledge_workbench.entity_candidates import (
    accept_entity_candidate,
    import_entity_candidates,
    list_entity_candidates,
)
from knowledge_workbench.entity_merges import (
    list_entity_merge_requests,
    propose_entity_merge,
    review_entity_merge,
)
from knowledge_workbench.errors import KnowledgeWorkbenchError
from knowledge_workbench.ingest import ingest_file
from knowledge_workbench.linting import lint_workspace
from knowledge_workbench.models import Classification


class EntityMergeTests(unittest.TestCase):
    def test_two_person_merge_moves_aliases_mentions_and_candidate_resolution(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = WorkspacePaths(root / "workspace")
            source_path = root / "source.md"
            source_path.write_text(
                "甲公司曾称旧甲，现统一使用甲集团。", encoding="utf-8"
            )
            ingestion = ingest_file(source_path, paths, Classification.INTERNAL)
            database = Database(paths.database)
            with database.connect() as connection:
                evidence_id = connection.execute(
                    "SELECT id FROM evidence WHERE processing_run_id = ?",
                    (ingestion.processing_run_id,),
                ).fetchone()[0]
            source_entity_id = create_entity(
                database, "甲公司", "organization", actor="curator-01"
            )
            add_entity_alias(
                database, source_entity_id, "旧甲", actor="curator-01"
            )
            target_entity_id = create_entity(
                database, "甲集团", "organization", actor="curator-01"
            )
            link_evidence_entity(
                database,
                source_entity_id,
                evidence_id,
                "甲公司",
                actor="curator-01",
            )
            link_evidence_entity(
                database,
                source_entity_id,
                evidence_id,
                "旧甲",
                actor="curator-01",
            )
            link_evidence_entity(
                database,
                target_entity_id,
                evidence_id,
                "甲集团",
                actor="curator-01",
            )

            analysis_path = _model_entity_analysis(
                paths,
                ingestion.processing_run_id,
                [{"name": "甲公司", "type": "organization"}],
            )
            import_entity_candidates(database, analysis_path, actor="curator-01")
            candidate_id = list_entity_candidates(database)[0]["id"]
            accept_entity_candidate(
                database,
                candidate_id,
                source_entity_id,
                actor="reviewer-01",
                note="确认逐字提及",
            )

            request_id = propose_entity_merge(
                database,
                source_entity_id,
                target_entity_id,
                actor="curator-01",
                note="经人工核对属于同一组织",
            )
            with mock.patch(
                "knowledge_workbench.entity_merges.record_event",
                side_effect=RuntimeError("audit failed"),
            ):
                with self.assertRaisesRegex(RuntimeError, "audit failed"):
                    review_entity_merge(
                        database,
                        request_id,
                        "approve",
                        actor="reviewer-02",
                        note="模拟审计失败",
                    )
            with database.connect() as connection:
                rolled_back = connection.execute(
                    """
                    SELECT emr.status AS request_status,
                           source.status AS source_status,
                           (SELECT COUNT(*) FROM entity_aliases
                            WHERE entity_id = source.id) AS source_alias_count,
                           (SELECT resolved_entity_id FROM entity_candidates
                            WHERE id = ?) AS candidate_entity_id
                    FROM entity_merge_requests emr
                    JOIN canonical_entities source
                      ON source.id = emr.source_entity_id
                    WHERE emr.id = ?
                    """,
                    (candidate_id, request_id),
                ).fetchone()
            self.assertEqual(rolled_back["request_status"], "reviewing")
            self.assertEqual(rolled_back["source_status"], "active")
            self.assertEqual(rolled_back["source_alias_count"], 2)
            self.assertEqual(
                rolled_back["candidate_entity_id"], source_entity_id
            )
            with self.assertRaisesRegex(KnowledgeWorkbenchError, "不同于提议人"):
                review_entity_merge(
                    database,
                    request_id,
                    "approve",
                    actor="curator-01",
                    note="自行批准",
                )
            result = review_entity_merge(
                database,
                request_id,
                "approve",
                actor="reviewer-02",
                note="已回源确认别名和提及",
            )
            self.assertEqual(result["status"], "merged")
            self.assertEqual(result["alias_count"], 2)
            self.assertEqual(result["evidence_mention_count"], 2)
            self.assertEqual(result["accepted_candidate_count"], 1)

            source_detail = get_entity(database, source_entity_id)
            target_detail = get_entity(database, target_entity_id)
            self.assertEqual(source_detail["status"], "archived")
            self.assertEqual(
                source_detail["merged_into_entity_id"], target_entity_id
            )
            self.assertEqual(source_detail["aliases"], [])
            self.assertEqual(
                {item["alias"] for item in target_detail["aliases"]},
                {"甲公司", "旧甲", "甲集团"},
            )
            self.assertEqual(
                sum(item["is_canonical"] for item in target_detail["aliases"]),
                1,
            )
            anchors = {
                item["alias"]: item["is_merge_anchor"]
                for item in target_detail["aliases"]
            }
            self.assertEqual(
                anchors, {"甲公司": 1, "旧甲": 0, "甲集团": 0}
            )
            self.assertEqual(target_detail["current_evidence_count"], 3)
            with database.connect() as connection:
                candidate = connection.execute(
                    "SELECT resolved_entity_id FROM entity_candidates WHERE id = ?",
                    (candidate_id,),
                ).fetchone()
                request = connection.execute(
                    "SELECT * FROM entity_merge_requests WHERE id = ?",
                    (request_id,),
                ).fetchone()
                audit = connection.execute(
                    """
                    SELECT details_json FROM audit_log
                    WHERE event_type IN (
                        'entity_merge_proposed', 'entity_merge_approved',
                        'canonical_entities_merged'
                    )
                    ORDER BY id
                    """
                ).fetchall()
            self.assertEqual(candidate["resolved_entity_id"], target_entity_id)
            self.assertEqual(request["status"], "merged")
            self.assertEqual(request["proposed_by"], "curator-01")
            self.assertEqual(request["reviewed_by"], "reviewer-02")
            audit_text = "\n".join(row["details_json"] for row in audit)
            self.assertNotIn("经人工核对", audit_text)
            self.assertNotIn("已回源确认", audit_text)
            self.assertNotIn("甲公司", audit_text)
            self.assertIn("review_note_sha256", audit_text)
            self.assertEqual(
                list_entity_merge_requests(database, status="merged")[0]["id"],
                request_id,
            )
            with self.assertRaisesRegex(KnowledgeWorkbenchError, "已完成复核"):
                review_entity_merge(
                    database,
                    request_id,
                    "reject",
                    actor="reviewer-03",
                    note="重复复核",
                )
            self.assertTrue(lint_workspace(database, paths)["passed"])
            with database.transaction() as connection:
                connection.execute(
                    """
                    UPDATE entity_merge_requests SET proposed_by = reviewed_by
                    WHERE id = ?
                    """,
                    (request_id,),
                )
            tampered = lint_workspace(database, paths)
            self.assertIn(
                "entity_merge_same_reviewer",
                {issue["code"] for issue in tampered["issues"]},
            )

    def test_proposal_guards_and_rejection_preserve_both_entities(self):
        with tempfile.TemporaryDirectory() as temporary:
            paths = WorkspacePaths(Path(temporary) / "workspace")
            database = Database(paths.database)
            database.initialize("t1")
            source = create_entity(
                database, "甲项目", "project", actor="curator-01"
            )
            target = create_entity(
                database, "甲工程", "project", actor="curator-01"
            )
            organization = create_entity(
                database, "甲公司", "organization", actor="curator-01"
            )
            with self.assertRaisesRegex(KnowledgeWorkbenchError, "不能相同"):
                propose_entity_merge(
                    database,
                    source,
                    source,
                    actor="curator-01",
                    note="同一个 ID",
                )
            with self.assertRaisesRegex(KnowledgeWorkbenchError, "相同类型"):
                propose_entity_merge(
                    database,
                    source,
                    organization,
                    actor="curator-01",
                    note="类型不同",
                )
            with self.assertRaisesRegex(KnowledgeWorkbenchError, "提议说明不能为空"):
                propose_entity_merge(
                    database,
                    source,
                    target,
                    actor="curator-01",
                    note=" ",
                )
            request_id = propose_entity_merge(
                database,
                source,
                target,
                actor="curator-01",
                note="名称近似，提交人工复核",
            )
            with self.assertRaisesRegex(KnowledgeWorkbenchError, "已有待复核"):
                propose_entity_merge(
                    database,
                    target,
                    source,
                    actor="curator-02",
                    note="重复反向请求",
                )
            with self.assertRaisesRegex(KnowledgeWorkbenchError, "复核意见不能为空"):
                review_entity_merge(
                    database,
                    request_id,
                    "reject",
                    actor="reviewer-01",
                    note="",
                )
            rejected = review_entity_merge(
                database,
                request_id,
                "reject",
                actor="reviewer-01",
                note="适用范围不同，不能合并",
            )
            self.assertEqual(rejected["status"], "rejected")
            self.assertEqual(get_entity(database, source)["status"], "active")
            self.assertEqual(get_entity(database, target)["status"], "active")
            self.assertEqual(
                list_entity_merge_requests(database, status="rejected")[0]["id"],
                request_id,
            )
            approved_request = propose_entity_merge(
                database,
                source,
                target,
                actor="curator-02",
                note="补充材料后重新提交",
            )
            review_entity_merge(
                database,
                approved_request,
                "approve",
                actor="reviewer-02",
                note="补充材料证明为同一项目",
            )
            with self.assertRaisesRegex(KnowledgeWorkbenchError, "规范名称别名不能移除"):
                remove_entity_alias(
                    database, target, "甲项目", actor="curator-03"
                )


def _model_entity_analysis(
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
    output = paths.analysis / f"{processing_run_id}.merge.model-analysis.json"
    output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return output


if __name__ == "__main__":
    unittest.main()
