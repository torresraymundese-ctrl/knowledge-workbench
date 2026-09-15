import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from knowledge_workbench.backups import (
    create_backup,
    list_backups,
    restore_backup,
    verify_backup,
)
from knowledge_workbench.config import WorkspacePaths
from knowledge_workbench.errors import KnowledgeWorkbenchError
from knowledge_workbench.ingest import initialize_workspace


class BackupTests(unittest.TestCase):
    def test_create_list_and_verify_complete_workspace_snapshot(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = WorkspacePaths(root / "workspace")
            database = initialize_workspace(paths)
            raw = paths.raw / "source.txt"
            raw.write_text("只读原始资料", encoding="utf-8")
            raw.chmod(0o444)
            (paths.logs / "worker.log").write_text("completed", encoding="utf-8")
            with database.transaction() as connection:
                connection.execute(
                    """
                    INSERT INTO audit_log(
                        event_type, entity_type, entity_id, actor,
                        details_json, created_at
                    ) VALUES ('test', 'workspace', 'one', 'tester', '{}',
                              '2026-07-27T00:00:00+00:00')
                    """
                )

            target = root / "synology-drive" / "数据库工作台"
            result = create_backup(paths, target, actor="backup-operator")

            self.assertEqual(result["database_integrity"], "ok")
            self.assertGreater(result["file_count"], 2)
            self.assertEqual(len(list_backups(target)), 1)
            verification = verify_backup(target)
            self.assertEqual(verification.snapshot_id, result["snapshot_id"])
            self.assertEqual(verification.file_count, result["file_count"])

            manifest = json.loads(
                (
                    Path(result["snapshot_path"]) / "manifest.json"
                ).read_text(encoding="utf-8")
            )
            paths_in_manifest = {item["path"] for item in manifest["files"]}
            self.assertIn("knowledge.sqlite3", paths_in_manifest)
            self.assertIn("raw/source.txt", paths_in_manifest)
            self.assertIn("logs/worker.log", paths_in_manifest)
            self.assertNotIn("knowledge.sqlite3-wal", paths_in_manifest)

            snapshot_database = (
                Path(result["snapshot_path"])
                / "payload"
                / "workspace"
                / "knowledge.sqlite3"
            )
            with closing(sqlite3.connect(snapshot_database)) as connection:
                count = connection.execute(
                    "SELECT COUNT(*) FROM audit_log WHERE event_type = 'test'"
                ).fetchone()[0]
            self.assertEqual(count, 1)

    def test_verify_rejects_tampered_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = WorkspacePaths(root / "workspace")
            initialize_workspace(paths)
            (paths.wiki / "note.md").write_text("original", encoding="utf-8")
            target = root / "backup"
            result = create_backup(paths, target, actor="operator")
            copied = (
                Path(result["snapshot_path"])
                / "payload"
                / "workspace"
                / "wiki"
                / "note.md"
            )
            copied.write_text("tampered", encoding="utf-8")

            with self.assertRaisesRegex(
                KnowledgeWorkbenchError, "SHA-256 不匹配"
            ):
                verify_backup(target, snapshot_id=result["snapshot_id"])

    def test_restore_is_dry_run_by_default_and_applies_only_to_new_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = WorkspacePaths(root / "workspace")
            initialize_workspace(paths)
            (paths.evidence / "evidence.jsonl").write_text(
                '{"excerpt":"证据"}\n', encoding="utf-8"
            )
            target = root / "backup"
            backup = create_backup(paths, target, actor="operator")
            destination = root / "restored-workspace"

            dry_run = restore_backup(
                target,
                destination,
                active_workspace=paths.root,
            )
            self.assertEqual(dry_run["mode"], "dry-run")
            self.assertFalse(destination.exists())

            with self.assertRaisesRegex(KnowledgeWorkbenchError, "确认不匹配"):
                restore_backup(
                    target,
                    destination,
                    apply=True,
                    confirmation="wrong",
                    active_workspace=paths.root,
                )

            applied = restore_backup(
                target,
                destination,
                apply=True,
                confirmation=backup["snapshot_id"],
                active_workspace=paths.root,
            )
            self.assertEqual(applied["mode"], "apply")
            self.assertEqual(
                (destination / "evidence" / "evidence.jsonl").read_text(
                    encoding="utf-8"
                ),
                '{"excerpt":"证据"}\n',
            )
            with closing(
                sqlite3.connect(destination / "knowledge.sqlite3")
            ) as connection:
                self.assertEqual(
                    connection.execute("PRAGMA integrity_check").fetchone()[0],
                    "ok",
                )

            with self.assertRaisesRegex(KnowledgeWorkbenchError, "尚不存在"):
                restore_backup(
                    target,
                    destination,
                    apply=True,
                    confirmation=backup["snapshot_id"],
                    active_workspace=paths.root,
                )

    def test_rejects_backup_inside_workspace_and_active_restore_target(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = WorkspacePaths(root / "workspace")
            initialize_workspace(paths)

            with self.assertRaisesRegex(KnowledgeWorkbenchError, "运行工作区内部"):
                create_backup(paths, paths.root / "backups", actor="operator")

            target = root / "backup"
            backup = create_backup(paths, target, actor="operator")
            with self.assertRaisesRegex(KnowledgeWorkbenchError, "当前运行工作区"):
                restore_backup(
                    target,
                    paths.root,
                    snapshot_id=backup["snapshot_id"],
                    apply=True,
                    confirmation=backup["snapshot_id"],
                    active_workspace=paths.root,
                )


if __name__ == "__main__":
    unittest.main()
