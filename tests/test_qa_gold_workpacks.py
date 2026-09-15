import json
import re
import tempfile
import unittest
from pathlib import Path

from knowledge_workbench.config import WorkspacePaths
from knowledge_workbench.database import Database
from knowledge_workbench.ingest import ingest_file
from knowledge_workbench.models import Classification, EvidenceStatus
from knowledge_workbench.qa_gold_workpacks import (
    evaluate_qa_gold_dataset,
    export_qa_gold_work_pack,
    finalize_qa_gold_dataset,
    inspect_qa_gold_work_pack,
)
from knowledge_workbench.review import transition_evidence
from knowledge_workbench.review_assurance import SOLO_ATTESTATION_PHRASE


class QaGoldWorkPackTests(unittest.TestCase):
    def test_exports_checks_finalizes_and_evaluates_small_gold_set(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = WorkspacePaths(root / "workspace")
            database = Database(paths.database)
            self._ingest_verified(
                paths,
                database,
                root / "验收规则.md",
                "项目验收期限为30个工作日。",
            )
            self._ingest_verified(
                paths,
                database,
                root / "实施方案.md",
                "本项目团队人员不少于12人。",
            )
            self._ingest_verified(
                paths,
                database,
                root / "采购要求.md",
                "项目团队人员不少于8人。",
            )
            questions = paths.evaluations / "qa-questions.json"
            questions.write_text(
                json.dumps(
                    {
                        "name": "真实问答黄金集-v1",
                        "cases": [
                            {
                                "case_id": "qa-001",
                                "category": "事实问答",
                                "question": "项目验收期限是多少？",
                            },
                            {
                                "case_id": "qa-002",
                                "category": "数值歧义",
                                "question": "项目团队人员不少于多少人？",
                            },
                            {
                                "case_id": "qa-003",
                                "category": "无答案",
                                "question": "月球基地的氧气储量是多少？",
                            },
                        ],
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            work_pack = paths.evaluations / "qa-gold-work-pack.md"
            exported = export_qa_gold_work_pack(
                database,
                paths,
                questions,
                work_pack,
                actor="qa-curator",
            )
            self.assertEqual(exported["case_count"], 3)

            initial = inspect_qa_gold_work_pack(
                database, paths, work_pack
            )
            self.assertTrue(initial["integrity_valid"])
            self.assertFalse(initial["apply_ready"])
            content = work_pack.read_text(encoding="utf-8")
            decisions = {
                "qa-001": ("evidence", "[ x]"),
                "qa-002": ("ambiguous", "[x ]"),
                "qa-003": ("insufficient", "[x]"),
            }
            for case in initial["_protected"]["cases"]:
                content = self._complete_case(
                    content,
                    case["case_id"],
                    decisions[case["case_id"]][0],
                    [
                        citation["evidence_id"]
                        for citation in case["citations"]
                    ],
                    approval_box=decisions[case["case_id"]][1],
                )
            work_pack.write_text(content, encoding="utf-8", newline="\n")

            completed = inspect_qa_gold_work_pack(
                database, paths, work_pack
            )
            self.assertTrue(completed["apply_ready"])
            dataset = paths.evaluations / "qa-gold-v1.json"
            finalized = finalize_qa_gold_dataset(
                database,
                paths,
                work_pack,
                dataset,
                actor="qa-curator",
                review_mode="solo_attested",
                solo_attestation=SOLO_ATTESTATION_PHRASE,
            )
            self.assertEqual(
                finalized["evaluation"]["aggregate"]["pass_rate"], 1.0
            )
            self.assertEqual(
                finalized["evaluation"]["aggregate"][
                    "answer_type_accuracy"
                ],
                1.0,
            )
            self.assertEqual(
                finalized["evaluation"]["aggregate"][
                    "restricted_leak_count"
                ],
                0,
            )
            repeated = evaluate_qa_gold_dataset(database, paths, dataset)
            self.assertEqual(repeated["aggregate"]["citation_recall"], 1.0)
            self.assertTrue(
                inspect_qa_gold_work_pack(
                    database, paths, work_pack
                )["already_applied"]
            )

            with database.connect() as connection:
                rows = connection.execute(
                    """
                    SELECT details_json FROM audit_log
                    WHERE event_type IN (
                        'qa_gold_work_pack_exported',
                        'qa_gold_dataset_finalized'
                    )
                    ORDER BY id
                    """
                ).fetchall()
            audit = "\n".join(row["details_json"] for row in rows)
            self.assertNotIn("项目验收期限", audit)
            self.assertNotIn("30个工作日", audit)
            self.assertNotIn("月球基地", audit)

    def test_detects_protected_snapshot_tampering(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = WorkspacePaths(root / "workspace")
            database = Database(paths.database)
            self._ingest_verified(
                paths,
                database,
                root / "规则.md",
                "项目提交期限为5个工作日。",
            )
            questions = paths.evaluations / "qa-questions.json"
            questions.write_text(
                json.dumps(
                    {
                        "name": "防篡改问答集",
                        "cases": [
                            {
                                "case_id": f"qa-00{index}",
                                "question": question,
                            }
                            for index, question in enumerate(
                                [
                                    "项目提交期限是多少？",
                                    "月球基地在哪里？",
                                    "火星基地在哪里？",
                                ],
                                start=1,
                            )
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            work_pack = paths.evaluations / "qa-gold-work-pack.md"
            export_qa_gold_work_pack(
                database,
                paths,
                questions,
                work_pack,
                actor="qa-curator",
            )
            content = work_pack.read_text(encoding="utf-8")
            work_pack.write_text(
                content.replace(
                    '"question":"项目提交期限是多少？"',
                    '"question":"项目提交期限改成多久？"',
                    1,
                ),
                encoding="utf-8",
                newline="\n",
            )
            status = inspect_qa_gold_work_pack(
                database, paths, work_pack
            )
            self.assertFalse(status["integrity_valid"])
            self.assertIn(
                "protected_snapshot_invalid", status["issue_codes"]
            )

    @staticmethod
    def _complete_case(
        content: str,
        case_id: str,
        answer_type: str,
        evidence_ids: list[str],
        *,
        approval_box: str,
    ) -> str:
        pattern = re.compile(
            rf"(^## `{re.escape(case_id)}`\n)(.*?)(?=^## `|\Z)",
            re.MULTILINE | re.DOTALL,
        )
        match = pattern.search(content)
        if not match:
            raise AssertionError(f"case section missing: {case_id}")
        body = match.group(2)
        body = body.replace(
            f"  - [ ] `{answer_type}`",
            f"  - [x] `{answer_type}`",
            1,
        )
        for evidence_id in evidence_ids:
            body = body.replace(
                f"  - [ ] `{evidence_id}`",
                f"  - [x] `{evidence_id}`",
                1,
            )
        body = body.replace(
            "- [ ] 我已核对问题、答案类型和全部必要引用",
            f"- {approval_box} 我已核对问题、答案类型和全部必要引用",
            1,
        )
        return content[: match.start(2)] + body + content[match.end(2) :]

    @staticmethod
    def _ingest_verified(
        paths: WorkspacePaths,
        database: Database,
        source: Path,
        content: str,
    ) -> None:
        source.write_text(content, encoding="utf-8")
        result = ingest_file(source, paths, Classification.INTERNAL)
        with database.transaction() as connection:
            evidence_ids = [
                row["id"]
                for row in connection.execute(
                    """
                    SELECT id FROM evidence
                    WHERE processing_run_id = ?
                    ORDER BY run_ordinal
                    """,
                    (result.processing_run_id,),
                ).fetchall()
            ]
            connection.execute(
                """
                UPDATE document_governance
                SET purpose = 'production',
                    scope_status = 'in_scope',
                    authority_status = 'authoritative'
                WHERE document_id = ?
                """,
                (result.document_id,),
            )
        for evidence_id in evidence_ids:
            transition_evidence(
                database,
                evidence_id,
                EvidenceStatus.REVIEWING,
                actor="reviewer",
            )
            transition_evidence(
                database,
                evidence_id,
                EvidenceStatus.VERIFIED,
                actor="reviewer",
            )


if __name__ == "__main__":
    unittest.main()
