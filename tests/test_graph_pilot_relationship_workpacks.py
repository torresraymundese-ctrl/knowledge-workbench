import json
import tempfile
import unittest
from pathlib import Path

from knowledge_workbench.config import WorkspacePaths
from knowledge_workbench.database import Database
from knowledge_workbench.entities import (
    create_entity,
    link_evidence_entity,
)
from knowledge_workbench.entity_relationships import (
    create_entity_relationship,
    create_relation_type,
    list_entity_relationships,
)
from knowledge_workbench.errors import (
    InvalidTransitionError,
    KnowledgeWorkbenchError,
)
from knowledge_workbench.graph_pilot import build_graph_pilot_pack
from knowledge_workbench.graph_pilot_relationship_workpacks import (
    apply_graph_pilot_relationship_curation_work_pack,
    export_graph_pilot_relationship_curation_work_pack,
)
from knowledge_workbench.ingest import ingest_file
from knowledge_workbench.labeling import (
    approve_labeling_session,
    create_labeling_session,
    review_labeling_case,
    select_expected_evidence,
    submit_labeling_session,
)
from knowledge_workbench.models import Classification, EvidenceStatus
from knowledge_workbench.review import transition_evidence


class GraphPilotRelationshipWorkPackTests(unittest.TestCase):
    def test_relationship_ref_merges_support_and_pack_is_not_replayable(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = self._build_pilot(Path(temporary))
            paths, database, pack_path = fixture[:3]
            evidence_ids, company_id, platform_id = fixture[3:]
            output = paths.evaluations / "relationship-curation.md"
            export_graph_pilot_relationship_curation_work_pack(
                database,
                paths,
                pack_path,
                output,
                actor="relation-curator",
            )
            content = output.read_text(encoding="utf-8")
            relationship = {
                "relationship_ref": "responsible-platform",
                "relation_key": "responsible_for",
                "source_entity_id": company_id,
                "target_entity_id": platform_id,
                "note": "两份证据均逐字确认主体与对象",
            }
            decisions = {
                evidence_ids[0]: ([relationship], ""),
                evidence_ids[1]: ([relationship], ""),
                evidence_ids[2]: ([], "仅为共同出现，不表达业务关系"),
            }
            output.write_text(
                self._fill(content, decisions), encoding="utf-8"
            )
            result = apply_graph_pilot_relationship_curation_work_pack(
                database,
                paths,
                output,
                actor="relation-curator",
            )
            self.assertEqual(result["created_relationship_count"], 1)
            self.assertEqual(result["no_relationship_count"], 1)
            relationship_row = list_entity_relationships(database)[0]
            self.assertEqual(
                relationship_row["source_entity_id"], company_id
            )
            self.assertEqual(
                relationship_row["target_entity_id"], platform_id
            )
            self.assertEqual(
                set(relationship_row["supporting_evidence_ids"]),
                set(evidence_ids[:2]),
            )
            with database.connect() as connection:
                event = connection.execute(
                    """
                    SELECT details_json FROM audit_log
                    WHERE event_type =
                          'graph_pilot_relationship_curation_applied'
                    """
                ).fetchone()
            audit_text = event["details_json"]
            self.assertNotIn("两份证据均逐字确认", audit_text)
            self.assertNotIn("仅为共同出现", audit_text)
            self.assertNotIn("甲公司负责乙平台", audit_text)
            with self.assertRaisesRegex(
                InvalidTransitionError, "已经应用"
            ):
                apply_graph_pilot_relationship_curation_work_pack(
                    database,
                    paths,
                    output,
                    actor="relation-curator",
                )

    def test_tamper_mention_drift_and_existing_relation_are_atomic(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = self._build_pilot(Path(temporary))
            paths, database, pack_path = fixture[:3]
            evidence_ids, company_id, platform_id = fixture[3:]
            output = paths.evaluations / "relationship-curation.md"
            export_graph_pilot_relationship_curation_work_pack(
                database,
                paths,
                pack_path,
                output,
                actor="relation-curator",
            )
            content = output.read_text(encoding="utf-8")
            completed = self._fill(
                content,
                {
                    evidence_ids[0]: (
                        [
                            {
                                "relationship_ref": "a-support",
                                "relation_key": "supports",
                                "source_entity_id": company_id,
                                "target_entity_id": platform_id,
                                "note": "人工确认支持关系",
                            }
                        ],
                        "",
                    ),
                    evidence_ids[1]: (
                        [
                            {
                                "relationship_ref": "z-responsible",
                                "relation_key": "responsible_for",
                                "source_entity_id": company_id,
                                "target_entity_id": platform_id,
                                "note": "人工确认负责关系",
                            }
                        ],
                        "",
                    ),
                    evidence_ids[2]: ([], "没有可登记关系"),
                },
            )
            output.write_text(
                completed.replace("共同出现", "伪造描述", 1),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                KnowledgeWorkbenchError, "受保护内容"
            ):
                apply_graph_pilot_relationship_curation_work_pack(
                    database,
                    paths,
                    output,
                    actor="relation-curator",
                )
            create_entity_relationship(
                database,
                "responsible_for",
                company_id,
                platform_id,
                [evidence_ids[1]],
                actor="existing-curator",
                note="既有关系",
            )
            output.write_text(completed, encoding="utf-8")
            with self.assertRaisesRegex(
                KnowledgeWorkbenchError, "整包未写入"
            ):
                apply_graph_pilot_relationship_curation_work_pack(
                    database,
                    paths,
                    output,
                    actor="relation-curator",
                )
            relationships = list_entity_relationships(database)
            self.assertEqual(len(relationships), 1)
            self.assertEqual(
                relationships[0]["relation_key"], "responsible_for"
            )
            with database.connect() as connection:
                applied = connection.execute(
                    """
                    SELECT COUNT(*) FROM audit_log
                    WHERE event_type =
                          'graph_pilot_relationship_curation_applied'
                    """
                ).fetchone()[0]
            self.assertEqual(applied, 0)

        with tempfile.TemporaryDirectory() as temporary:
            fixture = self._build_pilot(Path(temporary))
            paths, database, pack_path = fixture[:3]
            evidence_ids, company_id, platform_id = fixture[3:]
            output = paths.evaluations / "relationship-drift.md"
            export_graph_pilot_relationship_curation_work_pack(
                database,
                paths,
                pack_path,
                output,
                actor="relation-curator",
            )
            content = self._fill(
                output.read_text(encoding="utf-8"),
                {
                    evidence_ids[0]: (
                        [
                            {
                                "relationship_ref": "responsible",
                                "relation_key": "responsible_for",
                                "source_entity_id": company_id,
                                "target_entity_id": platform_id,
                                "note": "人工确认",
                            }
                        ],
                        "",
                    ),
                    evidence_ids[1]: ([], "当前不登记"),
                    evidence_ids[2]: ([], "当前不登记"),
                },
            )
            extra_id = create_entity(
                database,
                "甲公司",
                "person",
                actor="external-curator",
            )
            link_evidence_entity(
                database,
                extra_id,
                evidence_ids[0],
                "甲公司",
                actor="external-curator",
            )
            output.write_text(content, encoding="utf-8")
            with self.assertRaisesRegex(
                KnowledgeWorkbenchError, "实体提及范围已变化"
            ):
                apply_graph_pilot_relationship_curation_work_pack(
                    database,
                    paths,
                    output,
                    actor="relation-curator",
                )
            self.assertEqual(len(list_entity_relationships(database)), 0)

    @staticmethod
    def _fill(
        content: str,
        decisions: dict[str, tuple[list[dict], str]],
    ) -> str:
        for evidence_id, (relationships, note) in decisions.items():
            start = content.index(f"## `{evidence_id}`")
            next_start = content.find("\n## `", start + 1)
            if next_start < 0:
                next_start = len(content)
            block = content[start:next_start]
            if relationships:
                block = block.replace(
                    "- [ ] 登记关系", "- [x] 登记关系", 1
                )
            else:
                block = block.replace(
                    "- [ ] 当前无关系", "- [x] 当前无关系", 1
                )
            block = block.replace(
                "- 关系裁决 JSON：[]",
                "- 关系裁决 JSON："
                + json.dumps(relationships, ensure_ascii=False),
                1,
            )
            block = block.replace(
                '- 裁决说明 JSON：""',
                "- 裁决说明 JSON："
                + json.dumps(note, ensure_ascii=False),
                1,
            )
            content = content[:start] + block + content[next_start:]
        return content

    @staticmethod
    def _build_pilot(
        root: Path,
    ) -> tuple[
        WorkspacePaths,
        Database,
        Path,
        list[str],
        str,
        str,
    ]:
        paths = WorkspacePaths(root / "workspace")
        sources = (
            ("建设.md", "甲公司负责乙平台建设。"),
            ("运维.md", "甲公司负责乙平台运维。"),
            ("会议.md", "甲公司与乙平台在会议材料中共同出现。"),
        )
        evidence_by_case = {}
        cases = []
        for index, (name, text) in enumerate(sources, start=1):
            source = root / name
            source.write_text(text, encoding="utf-8")
            result = ingest_file(
                source, paths, Classification.INTERNAL
            )
            database = Database(paths.database)
            with database.connect() as connection:
                evidence_id = connection.execute(
                    """
                    SELECT id FROM evidence
                    WHERE processing_run_id = ?
                    """,
                    (result.processing_run_id,),
                ).fetchone()[0]
            case_id = f"relationship-pilot-{index}"
            evidence_by_case[case_id] = evidence_id
            cases.append(
                {
                    "case_id": case_id,
                    "source_path": name,
                    "classification": "internal",
                    "expected_evidence": [
                        {"text": "【待人工填写】", "required": True}
                    ],
                    "forbidden_substrings": [],
                    "max_duplicate_rate": 0,
                }
            )
        template = root / "template.json"
        template.write_text(
            json.dumps(
                {
                    "schema_version": "1.0",
                    "name": "关系工作包测试",
                    "cases": cases,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        database = Database(paths.database)
        session_id = create_labeling_session(
            database,
            template,
            actor="gold-annotator",
            minimum_required_per_case=1,
        )
        for case_id, evidence_id in evidence_by_case.items():
            select_expected_evidence(
                database,
                session_id,
                case_id,
                evidence_id,
                actor="gold-annotator",
            )
        submit_labeling_session(
            database, session_id, actor="gold-annotator"
        )
        for case_id in evidence_by_case:
            review_labeling_case(
                database,
                session_id,
                case_id,
                "approved",
                actor="gold-reviewer",
            )
        approve_labeling_session(
            database, session_id, actor="gold-reviewer"
        )
        pack_path = paths.evaluations / "graph-pilot.json"
        pack = build_graph_pilot_pack(
            database,
            paths,
            session_id,
            pack_path,
            actor="pilot-builder",
        )
        evidence_ids = [
            item["evidence_id"] for item in pack["candidates"]
        ]
        for evidence_id in evidence_ids:
            transition_evidence(
                database,
                evidence_id,
                EvidenceStatus.REVIEWING,
                actor="evidence-curator",
            )
            transition_evidence(
                database,
                evidence_id,
                EvidenceStatus.VERIFIED,
                actor="evidence-reviewer",
            )
        company_id = create_entity(
            database,
            "甲公司",
            "organization",
            actor="entity-curator",
        )
        platform_id = create_entity(
            database,
            "乙平台",
            "product",
            actor="entity-curator",
        )
        for evidence_id in evidence_ids:
            link_evidence_entity(
                database,
                company_id,
                evidence_id,
                "甲公司",
                actor="entity-curator",
            )
            link_evidence_entity(
                database,
                platform_id,
                evidence_id,
                "乙平台",
                actor="entity-curator",
            )
        create_relation_type(
            database,
            "responsible_for",
            "负责",
            inverse_label="由其负责",
            actor="relation-admin",
        )
        create_relation_type(
            database,
            "supports",
            "支持",
            inverse_label="获得支持",
            actor="relation-admin",
        )
        return (
            paths,
            database,
            pack_path,
            evidence_ids,
            company_id,
            platform_id,
        )


if __name__ == "__main__":
    unittest.main()
