import json
import tempfile
import unittest
from pathlib import Path

from knowledge_workbench.config import WorkspacePaths
from knowledge_workbench.database import Database
from knowledge_workbench.entities import (
    add_entity_alias,
    create_entity,
)
from knowledge_workbench.errors import (
    InvalidTransitionError,
    KnowledgeWorkbenchError,
)
from knowledge_workbench.graph_pilot import build_graph_pilot_pack
from knowledge_workbench.graph_pilot_entity_workpacks import (
    apply_graph_pilot_entity_curation_work_pack,
    export_graph_pilot_entity_curation_work_pack,
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


class GraphPilotEntityWorkPackTests(unittest.TestCase):
    def test_entity_curation_creates_links_and_records_explicit_none(self):
        with tempfile.TemporaryDirectory() as temporary:
            paths, database, pack_path, pack, evidence_ids = (
                self._build_verified_pilot(Path(temporary))
            )
            first_id = next(
                item["evidence_id"]
                for item in pack["candidates"]
                if "甲项目" in item["excerpt"]
            )
            second_id = next(
                item["evidence_id"]
                for item in pack["candidates"]
                if "乙项目" in item["excerpt"]
            )
            output = paths.evaluations / "entity-curation.md"
            export_graph_pilot_entity_curation_work_pack(
                database,
                paths,
                pack_path,
                output,
                actor="entity-curator",
            )
            original = output.read_text(encoding="utf-8")
            decisions = {
                first_id: (
                    [
                        {
                            "mention": "甲项目",
                            "canonical_name": "甲项目",
                            "entity_type": "project",
                        }
                    ],
                    "",
                ),
                second_id: ([], "当前证据不建立实体"),
            }
            output.write_text(
                self._fill(original, decisions), encoding="utf-8"
            )
            result = apply_graph_pilot_entity_curation_work_pack(
                database,
                paths,
                output,
                actor="entity-curator",
            )
            self.assertEqual(result["created_entity_count"], 1)
            self.assertEqual(result["linked_evidence_entity_count"], 1)
            self.assertEqual(result["no_entity_count"], 1)
            with database.connect() as connection:
                entities = connection.execute(
                    "SELECT id, canonical_name FROM canonical_entities"
                ).fetchall()
                mentions = connection.execute(
                    """
                    SELECT evidence_id, mention_text
                    FROM evidence_entity_mentions
                    """
                ).fetchall()
                event = connection.execute(
                    """
                    SELECT details_json FROM audit_log
                    WHERE event_type = 'graph_pilot_entity_curation_applied'
                      AND entity_id = ?
                    """,
                    (pack["pack_id"],),
                ).fetchone()
            self.assertEqual(
                [(row["canonical_name"]) for row in entities], ["甲项目"]
            )
            self.assertEqual(
                [(row["evidence_id"], row["mention_text"]) for row in mentions],
                [(first_id, "甲项目")],
            )
            audit_text = event["details_json"]
            self.assertNotIn("甲项目", audit_text)
            self.assertNotIn("当前证据不建立实体", audit_text)
            self.assertIn("note_sha256_by_evidence", audit_text)
            with self.assertRaisesRegex(
                InvalidTransitionError, "已经应用"
            ):
                apply_graph_pilot_entity_curation_work_pack(
                    database,
                    paths,
                    output,
                    actor="entity-curator",
                )

    def test_protected_content_and_alias_conflict_keep_batch_atomic(self):
        with tempfile.TemporaryDirectory() as temporary:
            paths, database, pack_path, pack, evidence_ids = (
                self._build_verified_pilot(Path(temporary))
            )
            first_id = next(
                item["evidence_id"]
                for item in pack["candidates"]
                if "甲项目" in item["excerpt"]
            )
            second_id = next(
                item["evidence_id"]
                for item in pack["candidates"]
                if "乙项目" in item["excerpt"]
            )
            output = paths.evaluations / "entity-curation.md"
            export_graph_pilot_entity_curation_work_pack(
                database,
                paths,
                pack_path,
                output,
                actor="entity-curator",
            )
            original = output.read_text(encoding="utf-8")
            completed = self._fill(
                original,
                {
                    first_id: (
                        [
                            {
                                "mention": "甲项目",
                                "canonical_name": "甲项目",
                                "entity_type": "project",
                            }
                        ],
                        "",
                    ),
                    second_id: (
                        [
                            {
                                "mention": "实施单位",
                                "canonical_name": "实施主体",
                                "entity_type": "organization",
                            }
                        ],
                        "",
                    ),
                },
            )
            output.write_text(
                completed.replace("乙项目", "伪造项目", 1),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                KnowledgeWorkbenchError, "受保护内容"
            ):
                apply_graph_pilot_entity_curation_work_pack(
                    database,
                    paths,
                    output,
                    actor="entity-curator",
                )
            blocker_id = create_entity(
                database,
                "现有实施机构",
                "organization",
                actor="existing-curator",
            )
            add_entity_alias(
                database,
                blocker_id,
                "实施单位",
                actor="existing-curator",
            )
            output.write_text(completed, encoding="utf-8")
            with self.assertRaisesRegex(
                KnowledgeWorkbenchError, "整包未写入"
            ):
                apply_graph_pilot_entity_curation_work_pack(
                    database,
                    paths,
                    output,
                    actor="entity-curator",
                )
            with database.connect() as connection:
                entity_count = connection.execute(
                    "SELECT COUNT(*) FROM canonical_entities"
                ).fetchone()[0]
                mention_count = connection.execute(
                    "SELECT COUNT(*) FROM evidence_entity_mentions"
                ).fetchone()[0]
                applied_count = connection.execute(
                    """
                    SELECT COUNT(*) FROM audit_log
                    WHERE event_type = 'graph_pilot_entity_curation_applied'
                    """
                ).fetchone()[0]
            self.assertEqual(entity_count, 1)
            self.assertEqual(mention_count, 0)
            self.assertEqual(applied_count, 0)

    @staticmethod
    def _fill(
        content: str,
        decisions: dict[str, tuple[list[dict], str]],
    ) -> str:
        for evidence_id, (entities, note) in decisions.items():
            start = content.index(f"## `{evidence_id}`")
            next_start = content.find("\n## `", start + 1)
            if next_start < 0:
                next_start = len(content)
            block = content[start:next_start]
            if entities:
                block = block.replace(
                    "- [ ] 登记实体", "- [x] 登记实体", 1
                )
            else:
                block = block.replace(
                    "- [ ] 当前无实体", "- [x] 当前无实体", 1
                )
            block = block.replace(
                "- 实体裁决 JSON：[]",
                "- 实体裁决 JSON："
                + json.dumps(entities, ensure_ascii=False),
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
    def _build_verified_pilot(
        root: Path,
    ) -> tuple[WorkspacePaths, Database, Path, dict, list[str]]:
        paths = WorkspacePaths(root / "workspace")
        sources = (
            ("甲项目.md", "甲项目由建设单位负责验收。"),
            ("乙项目.md", "乙项目由实施单位负责交付。"),
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
            case_id = f"entity-pilot-{index}"
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
                    "name": "实体工作包测试",
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
        return paths, database, pack_path, pack, evidence_ids


if __name__ == "__main__":
    unittest.main()
