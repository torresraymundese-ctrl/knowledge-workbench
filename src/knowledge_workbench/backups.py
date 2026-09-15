from __future__ import annotations

import json
import os
import shutil
import sqlite3
import stat
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from uuid import uuid4

from .config import WorkspacePaths
from .errors import KnowledgeWorkbenchError
from .utils import sha256_file, utc_now


BACKUP_FORMAT = "knowledge-workbench-backup-v1"
MANIFEST_NAME = "manifest.json"
LATEST_NAME = "latest.json"
_SQLITE_TRANSIENT_SUFFIXES = ("-wal", "-shm", "-journal")


@dataclass(frozen=True, slots=True)
class BackupVerification:
    snapshot_id: str
    snapshot_path: Path
    file_count: int
    total_size_bytes: int
    database_integrity: str
    foreign_key_issues: int


def create_backup(
    paths: WorkspacePaths,
    target: Path,
    *,
    actor: str,
) -> dict[str, object]:
    source_root = paths.root.resolve()
    target_root = target.expanduser().resolve()
    _require_actor(actor)
    if not paths.database.is_file():
        raise KnowledgeWorkbenchError(f"工作区数据库不存在：{paths.database}")
    if _is_within(target_root, source_root):
        raise KnowledgeWorkbenchError("备份目标不能位于运行工作区内部")

    snapshot_id = (
        f"snapshot-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-"
        f"{uuid4().hex[:12]}"
    )
    staging_parent = target_root / ".staging"
    staging_snapshot = staging_parent / snapshot_id
    payload_workspace = staging_snapshot / "payload" / "workspace"
    final_snapshot = target_root / "snapshots" / snapshot_id
    if final_snapshot.exists() or staging_snapshot.exists():
        raise KnowledgeWorkbenchError(f"快照标识发生冲突：{snapshot_id}")

    source_files, source_directories = _inventory_workspace(source_root, paths.database)
    try:
        payload_workspace.mkdir(parents=True, exist_ok=False)
        for relative_directory in source_directories:
            (payload_workspace / relative_directory).mkdir(parents=True, exist_ok=True)

        entries: list[dict[str, object]] = []
        for relative_path, before_size, before_mtime_ns in source_files:
            source = source_root / relative_path
            destination = payload_workspace / relative_path
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            after = source.stat()
            if (
                after.st_size != before_size
                or after.st_mtime_ns != before_mtime_ns
                or source.is_symlink()
            ):
                raise KnowledgeWorkbenchError(
                    f"备份期间文件发生变化，请停止写入后重试：{relative_path}"
                )
            entries.append(_manifest_entry(destination, payload_workspace))

        database_destination = payload_workspace / paths.database.name
        _online_database_backup(paths.database, database_destination)
        entries.append(_manifest_entry(database_destination, payload_workspace))

        current_files, current_directories = _inventory_workspace(
            source_root, paths.database
        )
        if source_files != current_files or source_directories != current_directories:
            raise KnowledgeWorkbenchError("备份期间工作区文件清单发生变化，请重试")

        entries.sort(key=lambda item: str(item["path"]))
        database_check = _check_database(database_destination)
        if database_check["integrity"] != "ok":
            raise KnowledgeWorkbenchError(
                f"数据库完整性检查失败：{database_check['integrity']}"
            )
        if database_check["foreign_key_issues"]:
            raise KnowledgeWorkbenchError(
                "数据库外键检查失败："
                f"{database_check['foreign_key_issues']} 项"
            )

        manifest = {
            "format": BACKUP_FORMAT,
            "snapshot_id": snapshot_id,
            "created_at": utc_now(),
            "actor": actor.strip(),
            "workspace_name": source_root.name,
            "database": {
                "path": paths.database.name,
                "integrity": database_check["integrity"],
                "foreign_key_issues": database_check["foreign_key_issues"],
                "schema_version": database_check["schema_version"],
            },
            "directories": [
                relative.as_posix() for relative in source_directories
            ],
            "files": entries,
            "summary": {
                "file_count": len(entries),
                "total_size_bytes": sum(int(item["size_bytes"]) for item in entries),
            },
        }
        _write_json_atomic(staging_snapshot / MANIFEST_NAME, manifest)

        final_snapshot.parent.mkdir(parents=True, exist_ok=True)
        staging_snapshot.replace(final_snapshot)
        _write_json_atomic(
            target_root / LATEST_NAME,
            {
                "format": BACKUP_FORMAT,
                "snapshot_id": snapshot_id,
                "created_at": manifest["created_at"],
            },
        )
        try:
            staging_parent.rmdir()
        except OSError:
            pass
    except Exception:
        if staging_snapshot.exists():
            _remove_tree(staging_snapshot)
        raise

    verification = verify_backup(target_root, snapshot_id=snapshot_id)
    return {
        "snapshot_id": verification.snapshot_id,
        "snapshot_path": str(verification.snapshot_path),
        "created_at": manifest["created_at"],
        "file_count": verification.file_count,
        "total_size_bytes": verification.total_size_bytes,
        "database_integrity": verification.database_integrity,
        "foreign_key_issues": verification.foreign_key_issues,
    }


def list_backups(target: Path) -> list[dict[str, object]]:
    target_root = target.expanduser().resolve()
    snapshots_root = target_root / "snapshots"
    if not snapshots_root.exists():
        return []
    if not snapshots_root.is_dir() or snapshots_root.is_symlink():
        raise KnowledgeWorkbenchError(f"快照目录无效：{snapshots_root}")

    backups: list[dict[str, object]] = []
    for snapshot_path in snapshots_root.iterdir():
        if not snapshot_path.is_dir() or snapshot_path.is_symlink():
            continue
        manifest_path = snapshot_path / MANIFEST_NAME
        try:
            manifest = _read_manifest(manifest_path)
            summary = manifest["summary"]
            backups.append(
                {
                    "snapshot_id": manifest["snapshot_id"],
                    "created_at": manifest["created_at"],
                    "actor": manifest["actor"],
                    "file_count": summary["file_count"],
                    "total_size_bytes": summary["total_size_bytes"],
                    "snapshot_path": str(snapshot_path.resolve()),
                }
            )
        except (KnowledgeWorkbenchError, KeyError, TypeError):
            backups.append(
                {
                    "snapshot_id": snapshot_path.name,
                    "invalid": True,
                    "snapshot_path": str(snapshot_path.resolve()),
                }
            )
    return sorted(
        backups,
        key=lambda item: (str(item.get("created_at", "")), str(item["snapshot_id"])),
        reverse=True,
    )


def verify_backup(
    target: Path,
    *,
    snapshot_id: str | None = None,
) -> BackupVerification:
    target_root = target.expanduser().resolve()
    resolved_id = snapshot_id or _read_latest_snapshot_id(target_root)
    snapshot_path = _snapshot_path(target_root, resolved_id)
    manifest = _read_manifest(snapshot_path / MANIFEST_NAME)
    if manifest.get("snapshot_id") != resolved_id:
        raise KnowledgeWorkbenchError("快照目录名与清单标识不一致")

    files = manifest.get("files")
    summary = manifest.get("summary")
    if not isinstance(files, list) or not isinstance(summary, dict):
        raise KnowledgeWorkbenchError("备份清单缺少文件或汇总信息")

    payload_workspace = snapshot_path / "payload" / "workspace"
    expected_paths: set[str] = set()
    total_size = 0
    for entry in files:
        if not isinstance(entry, dict):
            raise KnowledgeWorkbenchError("备份清单包含无效文件项")
        relative = _safe_manifest_path(entry.get("path"))
        relative_text = relative.as_posix()
        if relative_text in expected_paths:
            raise KnowledgeWorkbenchError(f"备份清单包含重复路径：{relative_text}")
        expected_paths.add(relative_text)
        source = payload_workspace.joinpath(*relative.parts)
        if source.is_symlink() or not source.is_file():
            raise KnowledgeWorkbenchError(f"备份文件缺失或类型无效：{relative_text}")
        size = source.stat().st_size
        if size != entry.get("size_bytes"):
            raise KnowledgeWorkbenchError(f"备份文件大小不匹配：{relative_text}")
        if sha256_file(source) != entry.get("sha256"):
            raise KnowledgeWorkbenchError(f"备份文件 SHA-256 不匹配：{relative_text}")
        total_size += size

    actual_paths = {
        path.relative_to(payload_workspace).as_posix()
        for path in payload_workspace.rglob("*")
        if path.is_file()
    }
    if actual_paths != expected_paths:
        raise KnowledgeWorkbenchError("备份载荷与清单文件集合不一致")
    if summary.get("file_count") != len(files):
        raise KnowledgeWorkbenchError("备份清单文件数量汇总不一致")
    if summary.get("total_size_bytes") != total_size:
        raise KnowledgeWorkbenchError("备份清单文件大小汇总不一致")

    database_info = manifest.get("database")
    if not isinstance(database_info, dict):
        raise KnowledgeWorkbenchError("备份清单缺少数据库信息")
    database_relative = _safe_manifest_path(database_info.get("path"))
    database_path = payload_workspace.joinpath(*database_relative.parts)
    check = _check_database(database_path)
    if check["integrity"] != "ok":
        raise KnowledgeWorkbenchError(f"快照数据库完整性检查失败：{check['integrity']}")
    if check["foreign_key_issues"]:
        raise KnowledgeWorkbenchError(
            f"快照数据库外键检查失败：{check['foreign_key_issues']} 项"
        )

    return BackupVerification(
        snapshot_id=resolved_id,
        snapshot_path=snapshot_path,
        file_count=len(files),
        total_size_bytes=total_size,
        database_integrity=str(check["integrity"]),
        foreign_key_issues=int(check["foreign_key_issues"]),
    )


def restore_backup(
    target: Path,
    destination: Path,
    *,
    snapshot_id: str | None = None,
    apply: bool = False,
    confirmation: str | None = None,
    active_workspace: Path | None = None,
) -> dict[str, object]:
    target_root = target.expanduser().resolve()
    verification = verify_backup(target_root, snapshot_id=snapshot_id)
    destination_root = destination.expanduser().resolve()
    if active_workspace and destination_root == active_workspace.resolve():
        raise KnowledgeWorkbenchError(
            "禁止直接覆盖当前运行工作区；请恢复到新的空路径，验证后再切换"
        )
    if _is_within(destination_root, target_root):
        raise KnowledgeWorkbenchError("恢复目标不能位于备份仓库内部")

    result = {
        "mode": "apply" if apply else "dry-run",
        "snapshot_id": verification.snapshot_id,
        "source": str(verification.snapshot_path),
        "destination": str(destination_root),
        "file_count": verification.file_count,
        "total_size_bytes": verification.total_size_bytes,
        "verified": True,
    }
    if not apply:
        return result
    if confirmation != verification.snapshot_id:
        raise KnowledgeWorkbenchError(
            "恢复确认不匹配；--confirm 必须完整填写快照 ID"
        )
    if destination_root.exists():
        raise KnowledgeWorkbenchError("恢复目标必须是尚不存在的新目录")

    staging = destination_root.parent / (
        f".{destination_root.name}.restore-{uuid4().hex[:12]}"
    )
    if staging.exists():
        raise KnowledgeWorkbenchError(f"恢复暂存目录已存在：{staging}")
    payload_workspace = verification.snapshot_path / "payload" / "workspace"
    try:
        shutil.copytree(payload_workspace, staging, copy_function=shutil.copy2)
        manifest = _read_manifest(verification.snapshot_path / MANIFEST_NAME)
        _verify_restored_payload(staging, manifest)
        staging.replace(destination_root)
    except Exception:
        if staging.exists():
            _remove_tree(staging)
        raise
    return result


def _inventory_workspace(
    source_root: Path,
    database_path: Path,
) -> tuple[list[tuple[Path, int, int]], list[Path]]:
    files: list[tuple[Path, int, int]] = []
    directories: list[Path] = []
    for path in sorted(source_root.rglob("*")):
        if path.is_symlink():
            raise KnowledgeWorkbenchError(f"工作区不允许通过备份跟随符号链接：{path}")
        relative = path.relative_to(source_root)
        if path.is_dir():
            directories.append(relative)
            continue
        if not path.is_file():
            raise KnowledgeWorkbenchError(f"工作区包含不支持的文件类型：{path}")
        if path == database_path or any(
            path.name == f"{database_path.name}{suffix}"
            for suffix in _SQLITE_TRANSIENT_SUFFIXES
        ):
            continue
        details = path.stat()
        files.append((relative, details.st_size, details.st_mtime_ns))
    return files, directories


def _online_database_backup(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    source_uri = f"{source.resolve().as_uri()}?mode=ro"
    try:
        with closing(sqlite3.connect(source_uri, uri=True)) as source_connection:
            with closing(sqlite3.connect(destination)) as destination_connection:
                source_connection.backup(destination_connection)
                destination_connection.commit()
    except sqlite3.Error as exc:
        raise KnowledgeWorkbenchError(f"SQLite 在线备份失败：{exc}") from exc


def _check_database(path: Path) -> dict[str, object]:
    if not path.is_file():
        raise KnowledgeWorkbenchError(f"快照数据库不存在：{path}")
    uri = f"{path.resolve().as_uri()}?mode=ro&immutable=1"
    try:
        with closing(sqlite3.connect(uri, uri=True)) as connection:
            integrity_rows = connection.execute("PRAGMA integrity_check").fetchall()
            integrity = (
                "ok"
                if integrity_rows == [("ok",)]
                else "; ".join(str(row[0]) for row in integrity_rows[:10])
            )
            foreign_key_issues = len(
                connection.execute("PRAGMA foreign_key_check").fetchall()
            )
            schema_version = connection.execute(
                "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
            ).fetchone()[0]
    except sqlite3.Error as exc:
        raise KnowledgeWorkbenchError(f"无法校验快照数据库：{exc}") from exc
    return {
        "integrity": integrity,
        "foreign_key_issues": foreign_key_issues,
        "schema_version": schema_version,
    }


def _manifest_entry(path: Path, payload_workspace: Path) -> dict[str, object]:
    details = path.stat()
    return {
        "path": path.relative_to(payload_workspace).as_posix(),
        "size_bytes": details.st_size,
        "sha256": sha256_file(path),
        "read_only": not bool(details.st_mode & stat.S_IWRITE),
    }


def _read_latest_snapshot_id(target_root: Path) -> str:
    latest_path = target_root / LATEST_NAME
    try:
        latest = json.loads(latest_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise KnowledgeWorkbenchError("尚无 latest.json，请先创建备份") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise KnowledgeWorkbenchError(f"无法读取最近快照指针：{exc}") from exc
    if latest.get("format") != BACKUP_FORMAT:
        raise KnowledgeWorkbenchError("最近快照指针格式不受支持")
    return _validate_snapshot_id(latest.get("snapshot_id"))


def _snapshot_path(target_root: Path, snapshot_id: str) -> Path:
    safe_id = _validate_snapshot_id(snapshot_id)
    snapshot_path = (target_root / "snapshots" / safe_id).resolve()
    snapshots_root = (target_root / "snapshots").resolve()
    if snapshot_path.parent != snapshots_root:
        raise KnowledgeWorkbenchError("快照路径越界")
    if not snapshot_path.is_dir() or snapshot_path.is_symlink():
        raise KnowledgeWorkbenchError(f"快照不存在：{safe_id}")
    return snapshot_path


def _validate_snapshot_id(value: object) -> str:
    if not isinstance(value, str) or not value.startswith("snapshot-"):
        raise KnowledgeWorkbenchError("快照 ID 无效")
    if not value.replace("-", "").isalnum() or len(value) > 80:
        raise KnowledgeWorkbenchError("快照 ID 无效")
    return value


def _read_manifest(path: Path) -> dict[str, object]:
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise KnowledgeWorkbenchError(f"备份清单不存在：{path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise KnowledgeWorkbenchError(f"无法读取备份清单：{exc}") from exc
    if not isinstance(manifest, dict) or manifest.get("format") != BACKUP_FORMAT:
        raise KnowledgeWorkbenchError("备份清单格式不受支持")
    return manifest


def _safe_manifest_path(value: object) -> PurePosixPath:
    if not isinstance(value, str) or not value:
        raise KnowledgeWorkbenchError("备份清单包含无效路径")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or "." in path.parts:
        raise KnowledgeWorkbenchError(f"备份清单路径越界：{value}")
    if any(":" in part or "\\" in part for part in path.parts):
        raise KnowledgeWorkbenchError(f"备份清单路径无效：{value}")
    return path


def _verify_restored_payload(destination: Path, manifest: dict[str, object]) -> None:
    files = manifest.get("files")
    if not isinstance(files, list):
        raise KnowledgeWorkbenchError("备份清单缺少文件项")
    expected: set[str] = set()
    for entry in files:
        if not isinstance(entry, dict):
            raise KnowledgeWorkbenchError("备份清单包含无效文件项")
        relative = _safe_manifest_path(entry.get("path"))
        expected.add(relative.as_posix())
        restored = destination.joinpath(*relative.parts)
        if (
            not restored.is_file()
            or restored.stat().st_size != entry.get("size_bytes")
            or sha256_file(restored) != entry.get("sha256")
        ):
            raise KnowledgeWorkbenchError(
                f"恢复后的文件校验失败：{relative.as_posix()}"
            )
        if entry.get("read_only"):
            restored.chmod(restored.stat().st_mode & ~stat.S_IWRITE)
    actual = {
        path.relative_to(destination).as_posix()
        for path in destination.rglob("*")
        if path.is_file()
    }
    if actual != expected:
        raise KnowledgeWorkbenchError("恢复后的文件集合与备份清单不一致")
    database_info = manifest.get("database")
    if not isinstance(database_info, dict):
        raise KnowledgeWorkbenchError("备份清单缺少数据库信息")
    database_relative = _safe_manifest_path(database_info.get("path"))
    check = _check_database(destination.joinpath(*database_relative.parts))
    if check["integrity"] != "ok" or check["foreign_key_issues"]:
        raise KnowledgeWorkbenchError("恢复后的数据库完整性检查失败")


def _write_json_atomic(path: Path, value: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _require_actor(actor: str) -> None:
    if not actor or not actor.strip():
        raise KnowledgeWorkbenchError("备份操作者不能为空")


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _remove_tree(path: Path) -> None:
    for item in path.rglob("*"):
        if item.is_file() and not item.is_symlink():
            try:
                item.chmod(item.stat().st_mode | stat.S_IWRITE)
            except OSError:
                pass
    shutil.rmtree(path)
