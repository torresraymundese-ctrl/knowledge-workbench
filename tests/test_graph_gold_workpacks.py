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
    retract_entity_relationship,
)
from knowledge_workbench.errors import (
    InvalidTransitionError,
    KnowledgeWorkbenchError,
)
from knowledge_workbench.graph_gold_workpacks import (
    apply_graph_gold_annotation_work_pack,
    apply_graph_gold_review_work_pack,
    export_graph_gold_annotation_work_pack,
    export_graph_gold_review_work_pack,
    inspect_graph_gold_work_pack,
)
from knowledge_workbench.graph_pilot import build_graph_pilot_pack
from knowledge_workbench.ingest import ingest_file
from knowledge_workbench.labeling import (
    approve_labeling_session,
    create_labeling_session,
    review_labeling_case,
    select_expected_evidence,
    submit_labeling_session,
)
from knowledge_workbench.models import Classification, EvidenceStatus
from knowledge_workbench.quality_closure import (
    build_quality_closure_status,
)
from knowledge_workbench.review import transition_evidence
from knowledge_workbench.review_assurance import (
    SOLO_ATTESTATION_PHRASE,
)
from knowledge_workbench.utils import sha256_text


class GraphGoldWorkPackTests(unittest.TestCase):
    def test_solo_review_finalizes_with_explicit_non_independent_provenance(
        self,
    ):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = self._build_graph(Path(temporary))
            (
                paths,
                database,
                pilot_path,
                evidence_id,
                company_id,
                platform_id,
            ) = fixture
            annotation_path = paths.evaluations / "solo-annotation.md"
            export_graph_gold_annotation_work_pack(
                database,
                paths,
                pilot_path,
                annotation_path,
                actor="solo-owner",
            )
            annotation_path.write_text(
                self._fill_annotation(
                    annotation_path.read_text(encoding="utf-8"),
                    self._annotation(
                        evidence_id, company_id, platform_id
                    ),
                ),
                encoding="utf-8",
            )
            candidate_path = paths.evaluations / "solo-candidate.json"
            apply_graph_gold_annotation_work_pack(
                database,
                paths,
                annotation_path,
                candidate_path,
                actor="solo-owner",
            )
            with self.assertRaisesRegex(
                InvalidTransitionError, "必须不同"
            ):
                export_graph_gold_review_work_pack(
                    database,
                    paths,
                    candidate_path,
                    paths.evaluations / "false-independent.md",
                    actor="solo-owner",
                )
            review_path = paths.evaluations / "solo-review.md"
            export_graph_gold_review_work_pack(
                database,
                paths,
                candidate_path,
                review_path,
                actor="solo-owner",
                review_mode="solo_attested",
            )
            review_content = review_path.read_text(encoding="utf-8")
            self.assertIn("非独立复核", review_content)
            solo_blank_status = inspect_graph_gold_work_pack(
                database, paths, review_path
            )
            self.assertEqual(
                solo_blank_status["review_mode"], "solo_attested"
            )
            self.assertFalse(
                solo_blank_status["independent_review"]
            )
            self.assertTrue(
                solo_blank_status["solo_attestation_required"]
            )
            self.assertTrue(
                solo_blank_status["review_actor_policy_valid"]
            )
            review_path.write_text(
                self._fill_review(
                    review_content,
                    approve_ids={
                        "relation-positive",
                        "relation-negative",
                        "path-positive",
                    },
                    reject_notes={},
                ),
                encoding="utf-8",
            )
            solo_ready_status = inspect_graph_gold_work_pack(
                database, paths, review_path
            )
            self.assertTrue(solo_ready_status["apply_ready"])
            output = paths.evaluations / "solo-gold.json"
            with self.assertRaisesRegex(
                KnowledgeWorkbenchError, "确认声明"
            ):
                apply_graph_gold_review_work_pack(
                    database,
                    paths,
                    review_path,
                    output,
                    actor="solo-owner",
                )
            with self.assertRaisesRegex(
                KnowledgeWorkbenchError, "确认短语"
            ):
                apply_graph_gold_review_work_pack(
                    database,
                    paths,
                    review_path,
                    output,
                    actor="solo-owner",
                    solo_attestation="我确认",
                )
            result = apply_graph_gold_review_work_pack(
                database,
                paths,
                review_path,
                output,
                actor="solo-owner",
                solo_attestation=SOLO_ATTESTATION_PHRASE,
            )
            self.assertTrue(result["approved"])
            self.assertEqual(result["review_mode"], "solo_attested")
            self.assertFalse(result["independent_review"])
            dataset = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(
                dataset["provenance"]["review_mode"],
                "solo_attested",
            )
            self.assertFalse(
                dataset["provenance"]["independent_review"]
            )
            quality = build_quality_closure_status(
                database, paths, target_gold_documents=10
            )
            graph = quality["metrics"]["graph"]
            self.assertEqual(
                graph["solo_attested_passing_graph_gold_dataset_count"],
                1,
            )
            self.assertEqual(
                graph["independent_passing_graph_gold_dataset_count"],
                0,
            )
            self.assertEqual(
                graph[
                    "human_attested_passing_graph_gold_dataset_count"
                ],
                1,
            )
            with database.connect() as connection:
                rows = connection.execute(
                    """
                    SELECT details_json FROM audit_log
                    WHERE event_type IN (
                      'graph_gold_review_applied',
                      'graph_gold_dataset_finalized'
                    )
                    ORDER BY id
                    """
                ).fetchall()
            self.assertEqual(len(rows), 2)
            for row in rows:
                details = json.loads(row["details_json"])
                self.assertEqual(
                    details["review_mode"], "solo_attested"
                )
                self.assertFalse(details["independent_review"])
                self.assertEqual(
                    details["solo_attestation_sha256"],
                    sha256_text(SOLO_ATTESTATION_PHRASE),
                )
                self.assertNotIn(
                    SOLO_ATTESTATION_PHRASE, row["details_json"]
                )

    def test_two_person_workflow_finalizes_and_evaluates_dataset(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = self._build_graph(Path(temporary))
            (
                paths,
                database,
                pilot_path,
                evidence_id,
                company_id,
                platform_id,
            ) = fixture
            annotation_path = paths.evaluations / "gold-annotation.md"
            export_graph_gold_annotation_work_pack(
                database,
                paths,
                pilot_path,
                annotation_path,
                actor="graph-annotator",
            )
            annotation = self._annotation(
                evidence_id, company_id, platform_id
            )
            original = annotation_path.read_text(encoding="utf-8")
            with database.connect() as connection:
                audit_count_before_status = connection.execute(
                    "SELECT COUNT(*) FROM audit_log"
                ).fetchone()[0]
            blank_status = inspect_graph_gold_work_pack(
                database, paths, annotation_path
            )
            self.assertEqual(
                blank_status["work_pack_type"], "annotation"
            )
            self.assertTrue(blank_status["integrity_valid"])
            self.assertTrue(blank_status["source_snapshot_valid"])
            self.assertEqual(blank_status["case_count"], 0)
            self.assertFalse(blank_status["annotation_valid"])
            self.assertIn(
                "dataset_name_missing", blank_status["issue_codes"]
            )
            self.assertIn("case_missing", blank_status["issue_codes"])
            self.assertFalse(blank_status["apply_ready"])
            with database.connect() as connection:
                audit_count_after_status = connection.execute(
                    "SELECT COUNT(*) FROM audit_log"
                ).fetchone()[0]
            self.assertEqual(
                audit_count_before_status, audit_count_after_status
            )
            annotation_path.write_text(
                self._fill_annotation(original, annotation),
                encoding="utf-8",
            )
            ready_annotation = inspect_graph_gold_work_pack(
                database, paths, annotation_path
            )
            self.assertTrue(ready_annotation["annotation_valid"])
            self.assertEqual(ready_annotation["relation_case_count"], 2)
            self.assertEqual(ready_annotation["path_case_count"], 1)
            self.assertEqual(ready_annotation["issue_codes"], [])
            self.assertTrue(ready_annotation["apply_ready"])
            candidate_path = paths.evaluations / "gold-candidate.json"
            saved = apply_graph_gold_annotation_work_pack(
                database,
                paths,
                annotation_path,
                candidate_path,
                actor="graph-annotator",
            )
            self.assertEqual(saved["relation_case_count"], 2)
            self.assertEqual(saved["path_case_count"], 1)
            applied_annotation = inspect_graph_gold_work_pack(
                database, paths, annotation_path
            )
            self.assertTrue(applied_annotation["already_applied"])
            self.assertFalse(applied_annotation["apply_ready"])
            with self.assertRaisesRegex(
                InvalidTransitionError, "已经应用"
            ):
                apply_graph_gold_annotation_work_pack(
                    database,
                    paths,
                    annotation_path,
                    paths.evaluations / "candidate-replay.json",
                    actor="graph-annotator",
                )
            with self.assertRaisesRegex(
                InvalidTransitionError, "必须不同"
            ):
                export_graph_gold_review_work_pack(
                    database,
                    paths,
                    candidate_path,
                    paths.evaluations / "self-review.md",
                    actor="graph-annotator",
                )
            copied = paths.evaluations / "candidate-copy.json"
            copied.write_text(
                candidate_path.read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                KnowledgeWorkbenchError, "保存审计"
            ):
                export_graph_gold_review_work_pack(
                    database,
                    paths,
                    copied,
                    paths.evaluations / "copied-review.md",
                    actor="graph-reviewer",
                )

            review_path = paths.evaluations / "gold-review.md"
            export_graph_gold_review_work_pack(
                database,
                paths,
                candidate_path,
                review_path,
                actor="graph-reviewer",
            )
            review_content = review_path.read_text(encoding="utf-8")
            blank_review = inspect_graph_gold_work_pack(
                database, paths, review_path
            )
            self.assertEqual(blank_review["work_pack_type"], "review")
            self.assertTrue(blank_review["integrity_valid"])
            self.assertEqual(blank_review["case_count"], 3)
            self.assertEqual(
                blank_review["decision_counts"]["undecided"], 3
            )
            self.assertFalse(blank_review["decisions_complete"])
            self.assertFalse(blank_review["apply_ready"])
            review_path.write_text(
                self._fill_review(
                    review_content,
                    approve_ids={
                        "relation-positive",
                        "relation-negative",
                        "path-positive",
                    },
                    reject_notes={},
                ),
                encoding="utf-8",
            )
            ready_review = inspect_graph_gold_work_pack(
                database, paths, review_path
            )
            self.assertEqual(ready_review["review_mode"], "independent")
            self.assertTrue(ready_review["independent_review"])
            self.assertEqual(
                ready_review["decision_counts"]["approved"], 3
            )
            self.assertTrue(ready_review["decisions_complete"])
            self.assertEqual(ready_review["issue_codes"], [])
            self.assertTrue(ready_review["apply_ready"])
            dataset_path = paths.evaluations / "graph-gold-v1.json"
            result = apply_graph_gold_review_work_pack(
                database,
                paths,
                review_path,
                dataset_path,
                actor="graph-reviewer",
            )
            self.assertTrue(result["approved"])
            self.assertEqual(result["evaluation"]["case_count"], 3)
            self.assertEqual(result["evaluation"]["pass_rate"], 1.0)
            applied_review = inspect_graph_gold_work_pack(
                database, paths, review_path
            )
            self.assertTrue(applied_review["already_applied"])
            self.assertFalse(applied_review["apply_ready"])
            dataset = json.loads(dataset_path.read_text(encoding="utf-8"))
            self.assertEqual(
                dataset["provenance"]["annotator"], "graph-annotator"
            )
            self.assertEqual(
                dataset["provenance"]["reviewer"], "graph-reviewer"
            )
            quality = build_quality_closure_status(
                database, paths, target_gold_documents=10
            )
            self.assertEqual(
                quality["metrics"]["graph"][
                    "passing_graph_gold_dataset_count"
                ],
                1,
            )
            self.assertEqual(
                quality["metrics"]["graph"][
                    "unaudited_graph_gold_dataset_count"
                ],
                0,
            )
            with self.assertRaisesRegex(
                InvalidTransitionError, "已经应用"
            ):
                apply_graph_gold_review_work_pack(
                    database,
                    paths,
                    review_path,
                    paths.evaluations / "replay.json",
                    actor="graph-reviewer",
                )
            with database.connect() as connection:
                audits = connection.execute(
                    """
                    SELECT event_type, details_json FROM audit_log
                    WHERE event_type IN (
                      'graph_gold_candidate_saved',
                      'graph_gold_review_applied',
                      'graph_gold_dataset_finalized'
                    )
                    ORDER BY id
                    """
                ).fetchall()
            self.assertEqual(len(audits), 3)
            audit_text = json.dumps(
                [json.loads(row["details_json"]) for row in audits],
                ensure_ascii=False,
            )
            self.assertNotIn("甲公司负责乙平台建设", audit_text)
            self.assertNotIn("人工确认关系与方向", audit_text)

    def test_protected_tamper_and_rejection_do_not_create_dataset(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = self._build_graph(Path(temporary))
            (
                paths,
                database,
                pilot_path,
                evidence_id,
                company_id,
                platform_id,
            ) = fixture
            annotation_path = paths.evaluations / "gold-annotation.md"
            export_graph_gold_annotation_work_pack(
                database,
                paths,
                pilot_path,
                annotation_path,
                actor="graph-annotator",
            )
            original = annotation_path.read_text(encoding="utf-8")
            annotation = self._annotation(
                evidence_id, company_id, platform_id
            )
            completed = self._fill_annotation(original, annotation)
            annotation_path.write_text(
                completed.replace("甲公司", "伪造公司", 1),
                encoding="utf-8",
            )
            tampered_status = inspect_graph_gold_work_pack(
                database, paths, annotation_path
            )
            self.assertFalse(tampered_status["integrity_valid"])
            self.assertIn(
                "protected_content_or_export_audit_invalid",
                tampered_status["issue_codes"],
            )
            with self.assertRaisesRegex(
                KnowledgeWorkbenchError, "受保护内容"
            ):
                apply_graph_gold_annotation_work_pack(
                    database,
                    paths,
                    annotation_path,
                    paths.evaluations / "tampered-candidate.json",
                    actor="graph-annotator",
                )
            annotation_path.write_text(completed, encoding="utf-8")
            candidate_path = paths.evaluations / "candidate.json"
            apply_graph_gold_annotation_work_pack(
                database,
                paths,
                annotation_path,
                candidate_path,
                actor="graph-annotator",
            )
            review_path = paths.evaluations / "review.md"
            export_graph_gold_review_work_pack(
                database,
                paths,
                candidate_path,
                review_path,
                actor="graph-reviewer",
            )
            review_path.write_text(
                self._fill_review(
                    review_path.read_text(encoding="utf-8"),
                    approve_ids={
                        "relation-positive",
                        "path-positive",
                    },
                    reject_notes={
                        "relation-negative": "负例适用范围需要重新核对"
                    },
                ),
                encoding="utf-8",
            )
            dataset_path = paths.evaluations / "rejected.json"
            result = apply_graph_gold_review_work_pack(
                database,
                paths,
                review_path,
                dataset_path,
                actor="graph-reviewer",
            )
            self.assertFalse(result["approved"])
            self.assertFalse(result["dataset_written"])
            self.assertFalse(dataset_path.exists())
            with database.connect() as connection:
                row = connection.execute(
                    """
                    SELECT details_json FROM audit_log
                    WHERE event_type = 'graph_gold_review_applied'
                    """
                ).fetchone()
                finalized = connection.execute(
                    """
                    SELECT COUNT(*) FROM audit_log
                    WHERE event_type = 'graph_gold_dataset_finalized'
                    """
                ).fetchone()[0]
            self.assertEqual(finalized, 0)
            self.assertNotIn(
                "负例适用范围需要重新核对", row["details_json"]
            )
            self.assertIn("note_sha256_by_case", row["details_json"])

            candidate = json.loads(
                candidate_path.read_text(encoding="utf-8")
            )
            hand_written = paths.evaluations / "hand-written-gold.json"
            hand_written.write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "name": candidate["name"],
                        "provenance": {
                            "annotator": "claimed-annotator",
                            "reviewer": "claimed-reviewer",
                            "reviewed_at": candidate["annotated_at"],
                            "decision": "approved",
                        },
                        "relation_cases": candidate["relation_cases"],
                        "path_cases": candidate["path_cases"],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            quality = build_quality_closure_status(
                database, paths, target_gold_documents=10
            )
            self.assertEqual(
                quality["metrics"]["graph"][
                    "passing_graph_gold_dataset_count"
                ],
                0,
            )
            self.assertEqual(
                quality["metrics"]["graph"][
                    "unaudited_graph_gold_dataset_count"
                ],
                1,
            )

    def test_graph_snapshot_drift_blocks_independent_review(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = self._build_graph(Path(temporary))
            (
                paths,
                database,
                pilot_path,
                evidence_id,
                company_id,
                platform_id,
            ) = fixture
            annotation_path = paths.evaluations / "annotation.md"
            export_graph_gold_annotation_work_pack(
                database,
                paths,
                pilot_path,
                annotation_path,
                actor="graph-annotator",
            )
            annotation_path.write_text(
                self._fill_annotation(
                    annotation_path.read_text(encoding="utf-8"),
                    self._annotation(
                        evidence_id, company_id, platform_id
                    ),
                ),
                encoding="utf-8",
            )
            candidate_path = paths.evaluations / "candidate.json"
            apply_graph_gold_annotation_work_pack(
                database,
                paths,
                annotation_path,
                candidate_path,
                actor="graph-annotator",
            )
            review_path = paths.evaluations / "review-before-drift.md"
            export_graph_gold_review_work_pack(
                database,
                paths,
                candidate_path,
                review_path,
                actor="graph-reviewer",
            )
            relationship_id = list_entity_relationships(database)[0][
                "relationship_id"
            ]
            retract_entity_relationship(
                database,
                relationship_id,
                actor="external-curator",
                note="外部并发撤销",
            )
            drifted_status = inspect_graph_gold_work_pack(
                database, paths, review_path
            )
            self.assertFalse(
                drifted_status["source_snapshot_valid"]
            )
            self.assertFalse(drifted_status["integrity_valid"])
            self.assertFalse(drifted_status["apply_ready"])
            self.assertIn(
                "source_snapshot_invalid",
                drifted_status["issue_codes"],
            )
            with self.assertRaisesRegex(
                KnowledgeWorkbenchError, "来源或业务图快照已变化"
            ):
                export_graph_gold_review_work_pack(
                    database,
                    paths,
                    candidate_path,
                    paths.evaluations / "review-after-drift.md",
                    actor="graph-reviewer",
                )

    @staticmethod
    def _annotation(
        evidence_id: str, company_id: str, platform_id: str
    ) -> dict:
        return {
            "name": "真实图谱黄金试点",
            "relation_cases": [
                {
                    "case_id": "relation-positive",
                    "source_entity_id": company_id,
                    "target_entity_id": platform_id,
                    "relation_key": "responsible_for",
                    "expected_present": True,
                    "expected_direction": "forward",
                    "expected_supporting_evidence_ids": [evidence_id],
                },
                {
                    "case_id": "relation-negative",
                    "source_entity_id": company_id,
                    "target_entity_id": platform_id,
                    "relation_key": "supports",
                    "expected_present": False,
                    "expected_direction": None,
                    "expected_supporting_evidence_ids": [],
                },
            ],
            "path_cases": [
                {
                    "case_id": "path-positive",
                    "source_entity_id": company_id,
                    "target_entity_id": platform_id,
                    "max_depth": 1,
                    "include_inverse": False,
                    "expected_reachable": True,
                    "expected_entity_ids": [company_id, platform_id],
                    "expected_relation_keys": ["responsible_for"],
                    "expected_traversal_directions": ["forward"],
                    "expected_supporting_evidence_ids": [evidence_id],
                }
            ],
        }

    @staticmethod
    def _fill_annotation(content: str, annotation: dict) -> str:
        prefix = "- 图谱黄金标注 JSON："
        lines = content.splitlines()
        matches = [
            index
            for index, line in enumerate(lines)
            if line.startswith(prefix)
        ]
        if len(matches) != 1:
            raise AssertionError("annotation field missing")
        lines[matches[0]] = prefix + json.dumps(
            annotation,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return "\n".join(lines) + "\n"

    @staticmethod
    def _fill_review(
        content: str,
        *,
        approve_ids: set[str],
        reject_notes: dict[str, str],
    ) -> str:
        for case_id in approve_ids | set(reject_notes):
            marker = "- Case ID JSON：" + json.dumps(
                case_id, ensure_ascii=False
            )
            marker_index = content.index(marker)
            start = content.rfind("\n## Case ", 0, marker_index) + 1
            next_start = content.find("\n## Case ", marker_index)
            if next_start < 0:
                next_start = len(content)
            block = content[start:next_start]
            if case_id in approve_ids:
                block = block.replace("- [ ] 批准", "- [x] 批准", 1)
                note = "独立复核通过"
            else:
                block = block.replace("- [ ] 驳回", "- [x] 驳回", 1)
                note = reject_notes[case_id]
            block = block.replace(
                '- 复核意见 JSON：""',
                "- 复核意见 JSON："
                + json.dumps(note, ensure_ascii=False),
                1,
            )
            content = content[:start] + block + content[next_start:]
        return content

    @staticmethod
    def _build_graph(
        root: Path,
    ) -> tuple[WorkspacePaths, Database, Path, str, str, str]:
        source = root / "建设.md"
        source.write_text(
            "甲公司负责乙平台建设。", encoding="utf-8"
        )
        paths = WorkspacePaths(root / "workspace")
        result = ingest_file(source, paths, Classification.INTERNAL)
        database = Database(paths.database)
        with database.connect() as connection:
            evidence_id = connection.execute(
                """
                SELECT id FROM evidence WHERE processing_run_id = ?
                """,
                (result.processing_run_id,),
            ).fetchone()[0]
        template = root / "template.json"
        template.write_text(
            json.dumps(
                {
                    "schema_version": "1.0",
                    "name": "图谱黄金工作包测试",
                    "cases": [
                        {
                            "case_id": "graph-gold-source",
                            "source_path": source.name,
                            "classification": "internal",
                            "expected_evidence": [
                                {
                                    "text": "【待人工填写】",
                                    "required": True,
                                }
                            ],
                            "forbidden_substrings": [],
                            "max_duplicate_rate": 0,
                        }
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        session_id = create_labeling_session(
            database,
            template,
            actor="gold-source-annotator",
            minimum_required_per_case=1,
        )
        select_expected_evidence(
            database,
            session_id,
            "graph-gold-source",
            evidence_id,
            actor="gold-source-annotator",
        )
        submit_labeling_session(
            database, session_id, actor="gold-source-annotator"
        )
        review_labeling_case(
            database,
            session_id,
            "graph-gold-source",
            "approved",
            actor="gold-source-reviewer",
        )
        approve_labeling_session(
            database, session_id, actor="gold-source-reviewer"
        )
        pilot_path = paths.evaluations / "graph-pilot.json"
        build_graph_pilot_pack(
            database,
            paths,
            session_id,
            pilot_path,
            actor="pilot-builder",
        )
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
        create_entity_relationship(
            database,
            "responsible_for",
            company_id,
            platform_id,
            [evidence_id],
            actor="relation-curator",
            note="人工确认关系与方向",
        )
        return (
            paths,
            database,
            pilot_path,
            evidence_id,
            company_id,
            platform_id,
        )


if __name__ == "__main__":
    unittest.main()
