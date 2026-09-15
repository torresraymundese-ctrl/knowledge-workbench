import json
import tempfile
import unittest
from pathlib import Path

from knowledge_workbench.config import WorkspacePaths
from knowledge_workbench.errors import KnowledgeWorkbenchError
from knowledge_workbench.ingest import initialize_workspace
from knowledge_workbench.models import Classification
from knowledge_workbench.nas_admission import (
    decide_nas_discovery,
    detect_content_credential_risk,
    import_nas_discovery,
    list_nas_discoveries,
    scan_nas_source,
)


class NasAdmissionTests(unittest.TestCase):
    def test_content_scanner_is_bounded_and_avoids_schema_column_false_positive(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            direct = root / "settings.sql"
            direct.write_text(
                "service_token = 'fake-test-token-value'",
                encoding="utf-8",
            )
            schema = root / "schema.sql"
            schema.write_text(
                "CREATE TABLE users (password VARCHAR(255), secret_token TEXT);",
                encoding="utf-8",
            )
            beyond_limit = root / "large.txt"
            beyond_limit.write_bytes(
                b"x" * (512 * 1024) + b"\npassword = 'outside-scan-window'"
            )
            private_key = root / "identity.pem"
            private_key.write_text(
                "-----BEGIN PRIVATE KEY-----\nnot-a-real-key\n"
                "-----END PRIVATE KEY-----",
                encoding="utf-8",
            )
            dotenv = root / ".env"
            dotenv.write_text(
                "PASSWORD=fake-dotenv-test-value",
                encoding="utf-8",
            )

            self.assertIs(detect_content_credential_risk(direct), True)
            self.assertIs(detect_content_credential_risk(schema), False)
            self.assertIs(detect_content_credential_risk(beyond_limit), False)
            self.assertIs(detect_content_credential_risk(private_key), True)
            self.assertIs(detect_content_credential_risk(dotenv), True)

    def test_innocuous_file_name_with_direct_secret_assignment_is_not_admissible(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            nas_root = root / "nas"
            nas_root.mkdir()
            (nas_root / "service-settings.sql").write_text(
                "password = 'fake-test-only-value'",
                encoding="utf-8",
            )
            paths = WorkspacePaths(root / "workspace")
            database = initialize_workspace(paths)

            scan = scan_nas_source(database, nas_root, actor="scanner")
            self.assertEqual(scan["credential_risk_files"], 1)
            discovery = list_nas_discoveries(database)[0]
            with self.assertRaisesRegex(KnowledgeWorkbenchError, "凭据"):
                decide_nas_discovery(
                    database,
                    discovery["id"],
                    decision="admit",
                    actor="approver",
                    reason="错误准入尝试",
                    classification=Classification.INTERNAL,
                )
            self.assertNotIn(
                "fake-test-only-value",
                discovery["risk_flags_json"],
            )

    def test_credential_list_can_only_be_ignored(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            nas_root = root / "nas"
            nas_root.mkdir()
            (nas_root / "各公司账号密码.md").write_text(
                "脱敏测试占位内容。",
                encoding="utf-8",
            )
            paths = WorkspacePaths(root / "workspace")
            database = initialize_workspace(paths)

            scan = scan_nas_source(
                database,
                nas_root,
                actor="scanner",
            )
            self.assertEqual(scan["credential_risk_files"], 1)
            discovery = list_nas_discoveries(database)[0]
            self.assertEqual(
                json.loads(discovery["risk_flags_json"]),
                ["credential_material"],
            )

            for classification in (
                Classification.INTERNAL,
                Classification.RESTRICTED,
            ):
                with self.assertRaisesRegex(
                    KnowledgeWorkbenchError,
                    "凭据清单",
                ):
                    decide_nas_discovery(
                        database,
                        discovery["id"],
                        decision="admit",
                        actor="approver",
                        reason="测试错误准入",
                        classification=classification,
                    )

            ignored = decide_nas_discovery(
                database,
                discovery["id"],
                decision="ignore",
                actor="approver",
                reason="凭据资料必须进入专用保管系统",
            )
            self.assertEqual(ignored["status"], "ignored")

    def test_scan_decide_import_and_changed_file_increment(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            nas_root = root / "nas"
            nas_root.mkdir()
            source = nas_root / "policy.md"
            duplicate = nas_root / "policy-copy.md"
            source.write_text("第一版制度要求。", encoding="utf-8")
            duplicate.write_text("第一版制度要求。", encoding="utf-8")
            archive = nas_root / "archive"
            archive.mkdir()
            (archive / "old.md").write_text("归档制度。", encoding="utf-8")
            (nas_root / "~$draft.docx").write_bytes(b"temporary")

            paths = WorkspacePaths(root / "workspace")
            database = initialize_workspace(paths)
            scan = scan_nas_source(
                database,
                nas_root,
                actor="scanner-01",
                project="policy",
            )

            self.assertEqual(scan["new_discoveries"], 1)
            self.assertEqual(scan["duplicate_files"], 1)
            self.assertEqual(scan["filtered_files"], 2)
            discoveries = list_nas_discoveries(database)
            self.assertEqual(len(discoveries), 1)
            self.assertEqual(discoveries[0]["status"], "discovered")
            self.assertEqual(discoveries[0]["project"], "policy")
            self.assertFalse(any(path.is_file() for path in paths.raw.rglob("*")))
            with database.connect() as connection:
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM documents").fetchone()[0],
                    0,
                )

            discovery_id = discoveries[0]["id"]
            with self.assertRaisesRegex(KnowledgeWorkbenchError, "只有 admitted"):
                import_nas_discovery(
                    database,
                    paths,
                    discovery_id,
                    actor="importer-01",
                )

            admitted = decide_nas_discovery(
                database,
                discovery_id,
                decision="admit",
                actor="approver-01",
                reason="属于现行制度范围",
                classification=Classification.INTERNAL,
            )
            self.assertEqual(admitted["status"], "admitted")
            result = import_nas_discovery(
                database,
                paths,
                discovery_id,
                actor="importer-01",
            )
            self.assertFalse(result.duplicate)
            imported = list_nas_discoveries(
                database,
                status="imported",
            )
            self.assertEqual(imported[0]["document_version_id"], result.version_id)

            source.write_text("第二版制度要求。", encoding="utf-8")
            changed_scan = scan_nas_source(
                database,
                nas_root,
                actor="scanner-01",
                project="policy",
            )
            self.assertEqual(changed_scan["new_discoveries"], 1)
            pending = list_nas_discoveries(database, status="discovered")
            self.assertEqual(len(pending), 1)
            self.assertNotEqual(pending[0]["sha256"], result.sha256)

            with database.connect() as connection:
                events = {
                    row[0]
                    for row in connection.execute(
                        "SELECT event_type FROM audit_log"
                    ).fetchall()
                }
            self.assertTrue(
                {
                    "nas_scan_completed",
                    "nas_file_discovered",
                    "nas_file_admitted",
                    "nas_file_imported",
                }.issubset(events)
            )

    def test_changed_admitted_file_requires_rescan_and_reapproval(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            nas_root = root / "nas"
            nas_root.mkdir()
            source = nas_root / "requirements.txt"
            source.write_text("原始需求。", encoding="utf-8")
            paths = WorkspacePaths(root / "workspace")
            database = initialize_workspace(paths)
            scan_nas_source(database, nas_root, actor="scanner")
            discovery = list_nas_discoveries(database)[0]
            decide_nas_discovery(
                database,
                discovery["id"],
                decision="admit",
                actor="approver",
                reason="批准纳入",
                classification=Classification.CONFIDENTIAL,
            )

            source.write_text("未经重新批准的新需求。", encoding="utf-8")
            with self.assertRaisesRegex(KnowledgeWorkbenchError, "重新扫描并重新准入"):
                import_nas_discovery(
                    database,
                    paths,
                    discovery["id"],
                    actor="importer",
                )

            admitted = list_nas_discoveries(database, status="admitted")
            self.assertEqual(len(admitted), 1)
            with database.connect() as connection:
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM documents").fetchone()[0],
                    0,
                )

    def test_ignore_is_terminal_and_does_not_import(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            nas_root = root / "nas"
            nas_root.mkdir()
            (nas_root / "notes.md").write_text("临时会议记录。", encoding="utf-8")
            paths = WorkspacePaths(root / "workspace")
            database = initialize_workspace(paths)
            scan_nas_source(database, nas_root, actor="scanner")
            discovery = list_nas_discoveries(database)[0]

            ignored = decide_nas_discovery(
                database,
                discovery["id"],
                decision="ignore",
                actor="approver",
                reason="不属于知识库范围",
            )
            self.assertEqual(ignored["status"], "ignored")
            with self.assertRaisesRegex(KnowledgeWorkbenchError, "只有 admitted"):
                import_nas_discovery(
                    database,
                    paths,
                    discovery["id"],
                    actor="importer",
                )


if __name__ == "__main__":
    unittest.main()
