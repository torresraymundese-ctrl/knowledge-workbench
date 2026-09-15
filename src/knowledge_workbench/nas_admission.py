from __future__ import annotations

import fnmatch
import json
import re
from pathlib import Path
from typing import Any, Iterable

from .audit import record_event
from .config import WorkspacePaths
from .database import Database
from .errors import KnowledgeWorkbenchError
from .ingest import ingest_file
from .models import Classification, IngestResult
from .parsers import supported_extensions
from .utils import new_id, sha256_file, utc_now


NAS_STATUSES = ("discovered", "admitted", "ignored", "imported")
DEFAULT_ARCHIVE_DIRECTORIES = frozenset(
    {"archive", "archives", "archived", "backup", "backups", "归档", "备份"}
)
TEMPORARY_SUFFIXES = frozenset(
    {".bak", ".crdownload", ".old", ".part", ".swp", ".tmp"}
)
CREDENTIAL_MATERIAL_RISK = "credential_material"
CONTENT_SCAN_LIMIT_BYTES = 512 * 1024
CONTENT_SCAN_EXTENSIONS = frozenset(
    {
        ".cfg",
        ".conf",
        ".csv",
        ".env",
        ".ini",
        ".json",
        ".key",
        ".markdown",
        ".md",
        ".pem",
        ".properties",
        ".sql",
        ".toml",
        ".txt",
        ".yaml",
        ".yml",
    }
)
_PRIVATE_KEY_PATTERN = re.compile(
    r"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----",
    re.IGNORECASE,
)
_DIRECT_CREDENTIAL_ASSIGNMENT_PATTERN = re.compile(
    r"""(?imx)
    ^[ \t]*
    (?:[,{][ \t]*)?
    (?:(?:export|set|const|let|var)[ \t]+)?
    ["']?
    (?:[A-Z0-9_.-]+[_-])?
    (?:
        password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key
        |access[_-]?token|client[_-]?secret|密码|口令|密钥|令牌
    )
    ["']?
    [ \t]*(?:=|:|：)[ \t]*
    (?:
        "(?![ \t]*(?:null|none)?[ \t]*")[^"\r\n]{1,}"
        |'(?![ \t]*(?:null|none)?[ \t]*')[^'\r\n]{1,}'
        |(?!\$\{|\{\{|<[^>\r\n]+>|null\b|none\b|changeme\b|your[_-])
         [^\s#;,]{1,}
    )
    """
)
_ACCOUNT_LABEL_PATTERN = re.compile(
    r"(?i)(?:账号|帐号|账户|用户名|用户名称|登录名|account|username|user[ _-]?name)"
)
_PASSWORD_LABEL_PATTERN = re.compile(
    r"(?i)(?:密码|口令|password|passwd|pwd)"
)
_EXAMPLE_CONTEXT_PATTERN = re.compile(
    r"(?i)(?:默认|初始|示例|样例|演示|测试|default|initial|example|sample|demo|test)"
)
_CREDENTIAL_CONTEXT_SPAN = 400


def scan_nas_source(
    database: Database,
    source_root: Path,
    *,
    actor: str,
    project: str | None = None,
    extensions: Iterable[str] | None = None,
    include_globs: Iterable[str] = (),
    exclude_globs: Iterable[str] = (),
    modified_since_ns: int | None = None,
    include_archives: bool = False,
) -> dict[str, Any]:
    """Read and register eligible NAS files without copying or parsing them."""
    actor = _required_text(actor, "actor")
    project = project.strip() if project and project.strip() else None
    root = source_root.expanduser().resolve()
    if not root.is_dir():
        raise KnowledgeWorkbenchError(f"NAS 扫描根目录不存在或不是目录：{root}")

    allowed_extensions = _normalize_extensions(extensions)
    includes = tuple(value for value in include_globs if value)
    excludes = tuple(value for value in exclude_globs if value)
    counters = {
        "visited_files": 0,
        "eligible_files": 0,
        "new_discoveries": 0,
        "known_observations": 0,
        "duplicate_files": 0,
        "filtered_files": 0,
        "unstable_files": 0,
        "unreadable_files": 0,
        "credential_risk_files": 0,
    }
    observations: list[dict[str, Any]] = []

    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix().casefold()):
        try:
            if not path.is_file() or path.is_symlink():
                continue
            counters["visited_files"] += 1
            relative = path.relative_to(root)
            relative_posix = relative.as_posix()
            if not _is_eligible(
                relative,
                allowed_extensions=allowed_extensions,
                include_globs=includes,
                exclude_globs=excludes,
                include_archives=include_archives,
            ):
                counters["filtered_files"] += 1
                continue
            before = path.stat()
            if (
                modified_since_ns is not None
                and before.st_mtime_ns < modified_since_ns
            ):
                counters["filtered_files"] += 1
                continue
            counters["eligible_files"] += 1
            digest = sha256_file(path)
            after = path.stat()
            if (
                before.st_size != after.st_size
                or before.st_mtime_ns != after.st_mtime_ns
            ):
                counters["unstable_files"] += 1
                continue
            risk_flags = list(detect_nas_risk_flags(relative))
            if (
                CREDENTIAL_MATERIAL_RISK not in risk_flags
                and detect_content_credential_risk(path)
            ):
                risk_flags.append(CREDENTIAL_MATERIAL_RISK)
            observations.append(
                {
                    "relative_path": relative_posix,
                    "file_name": path.name,
                    "extension": path.suffix.lower(),
                    "sha256": digest,
                    "size_bytes": after.st_size,
                    "modified_at_ns": after.st_mtime_ns,
                    "risk_flags": tuple(sorted(set(risk_flags))),
                }
            )
            if CREDENTIAL_MATERIAL_RISK in observations[-1]["risk_flags"]:
                counters["credential_risk_files"] += 1
        except OSError:
            counters["unreadable_files"] += 1

    now = utc_now()
    scan_id = new_id("nasscan")
    with database.transaction() as connection:
        for observation in observations:
            existing = connection.execute(
                """
                SELECT id FROM nas_discoveries
                WHERE source_root = ? AND relative_path = ? AND sha256 = ?
                """,
                (
                    str(root),
                    observation["relative_path"],
                    observation["sha256"],
                ),
            ).fetchone()
            if existing:
                connection.execute(
                    """
                    UPDATE nas_discoveries
                    SET size_bytes = ?, modified_at_ns = ?, last_seen_at = ?,
                        project = COALESCE(project, ?), risk_flags_json = ?
                    WHERE id = ?
                    """,
                    (
                        observation["size_bytes"],
                        observation["modified_at_ns"],
                        now,
                        project,
                        json.dumps(observation["risk_flags"]),
                        existing["id"],
                    ),
                )
                counters["known_observations"] += 1
                continue

            imported_duplicate = connection.execute(
                "SELECT 1 FROM document_versions WHERE sha256 = ? LIMIT 1",
                (observation["sha256"],),
            ).fetchone()
            nas_duplicate = connection.execute(
                "SELECT 1 FROM nas_discoveries WHERE sha256 = ? LIMIT 1",
                (observation["sha256"],),
            ).fetchone()
            if imported_duplicate or nas_duplicate:
                counters["duplicate_files"] += 1
                continue

            discovery_id = new_id("nas")
            connection.execute(
                """
                INSERT INTO nas_discoveries(
                    id, source_root, relative_path, file_name, extension,
                    sha256, size_bytes, modified_at_ns, project, status,
                    discovered_at, last_seen_at, risk_flags_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'discovered', ?, ?, ?)
                """,
                (
                    discovery_id,
                    str(root),
                    observation["relative_path"],
                    observation["file_name"],
                    observation["extension"],
                    observation["sha256"],
                    observation["size_bytes"],
                    observation["modified_at_ns"],
                    project,
                    now,
                    now,
                    json.dumps(observation["risk_flags"]),
                ),
            )
            record_event(
                connection,
                "nas_file_discovered",
                "nas_discovery",
                discovery_id,
                actor=actor,
                details={
                    "extension": observation["extension"],
                    "project": project,
                    "sha256": observation["sha256"],
                    "size_bytes": observation["size_bytes"],
                    "risk_flags": observation["risk_flags"],
                },
            )
            counters["new_discoveries"] += 1

        record_event(
            connection,
            "nas_scan_completed",
            "nas_scan",
            scan_id,
            actor=actor,
            details={
                **counters,
                "project": project,
                "source_root": str(root),
            },
        )

    return {
        "scan_id": scan_id,
        "source_root": str(root),
        "project": project,
        **counters,
    }


def list_nas_discoveries(
    database: Database,
    *,
    status: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    if status is not None and status not in NAS_STATUSES:
        raise KnowledgeWorkbenchError(f"不支持的 NAS 准入状态：{status}")
    if limit <= 0:
        raise KnowledgeWorkbenchError("limit 必须大于 0")
    query = "SELECT * FROM nas_discoveries"
    parameters: list[object] = []
    if status is not None:
        query += " WHERE status = ?"
        parameters.append(status)
    query += " ORDER BY discovered_at DESC, id LIMIT ?"
    parameters.append(limit)
    with database.connect() as connection:
        return [dict(row) for row in connection.execute(query, parameters).fetchall()]


def decide_nas_discovery(
    database: Database,
    discovery_id: str,
    *,
    decision: str,
    actor: str,
    reason: str,
    classification: Classification | None = None,
) -> dict[str, Any]:
    actor = _required_text(actor, "actor")
    reason = _required_text(reason, "reason")
    if decision not in {"admit", "ignore"}:
        raise KnowledgeWorkbenchError("decision 必须是 admit 或 ignore")
    if decision == "admit" and classification is None:
        raise KnowledgeWorkbenchError("批准吸收时必须指定 classification")
    if decision == "ignore" and classification is not None:
        raise KnowledgeWorkbenchError("明确忽略时不能指定 classification")

    now = utc_now()
    target_status = "admitted" if decision == "admit" else "ignored"
    with database.transaction() as connection:
        row = connection.execute(
            "SELECT * FROM nas_discoveries WHERE id = ?",
            (discovery_id,),
        ).fetchone()
        if row is None:
            raise KnowledgeWorkbenchError(f"NAS 发现项不存在：{discovery_id}")
        if row["status"] != "discovered":
            raise KnowledgeWorkbenchError(
                f"NAS 发现项当前状态为 {row['status']}，不能重复做准入决定"
            )
        if decision == "admit":
            _assert_admission_allowed(row)
        connection.execute(
            """
            UPDATE nas_discoveries
            SET status = ?, classification = ?, decided_at = ?,
                decided_by = ?, decision_reason = ?
            WHERE id = ?
            """,
            (
                target_status,
                classification.value if classification else None,
                now,
                actor,
                reason,
                discovery_id,
            ),
        )
        record_event(
            connection,
            f"nas_file_{target_status}",
            "nas_discovery",
            discovery_id,
            actor=actor,
            details={
                "classification": (
                    classification.value if classification else None
                ),
                "reason": reason,
            },
        )
        updated = connection.execute(
            "SELECT * FROM nas_discoveries WHERE id = ?",
            (discovery_id,),
        ).fetchone()
    return dict(updated)


def import_nas_discovery(
    database: Database,
    paths: WorkspacePaths,
    discovery_id: str,
    *,
    actor: str,
    allow_legacy_word_conversion: bool = False,
) -> IngestResult:
    actor = _required_text(actor, "actor")
    with database.connect() as connection:
        row = connection.execute(
            "SELECT * FROM nas_discoveries WHERE id = ?",
            (discovery_id,),
        ).fetchone()
    if row is None:
        raise KnowledgeWorkbenchError(f"NAS 发现项不存在：{discovery_id}")
    if row["status"] != "admitted":
        raise KnowledgeWorkbenchError(
            f"NAS 发现项当前状态为 {row['status']}，只有 admitted 才能导入"
        )
    _assert_admission_allowed(row)

    root = Path(row["source_root"]).resolve()
    source = (root / row["relative_path"]).resolve()
    if not source.is_relative_to(root) or not source.is_file():
        raise KnowledgeWorkbenchError("NAS 来源文件已缺失或路径越界，请重新扫描")
    current_digest = sha256_file(source)
    if current_digest != row["sha256"]:
        raise KnowledgeWorkbenchError("NAS 来源文件内容已变化，请重新扫描并重新准入")

    result = ingest_file(
        source,
        paths,
        Classification(row["classification"]),
        actor=actor,
        allow_legacy_word_conversion=allow_legacy_word_conversion,
        expected_sha256=row["sha256"],
    )
    now = utc_now()
    with database.transaction() as connection:
        changed = connection.execute(
            """
            UPDATE nas_discoveries
            SET status = 'imported', imported_at = ?, imported_by = ?,
                document_version_id = ?
            WHERE id = ? AND status = 'admitted'
            """,
            (now, actor, result.version_id, discovery_id),
        ).rowcount
        if changed != 1:
            raise KnowledgeWorkbenchError("NAS 准入状态已变化，未能登记导入结果")
        record_event(
            connection,
            "nas_file_imported",
            "nas_discovery",
            discovery_id,
            actor=actor,
            details={
                "document_version_id": result.version_id,
                "duplicate": result.duplicate,
                "sha256": result.sha256,
            },
        )
    return result


def _normalize_extensions(extensions: Iterable[str] | None) -> frozenset[str]:
    values = supported_extensions() if extensions is None else tuple(extensions)
    normalized = {
        value.lower() if value.startswith(".") else f".{value.lower()}"
        for value in values
        if value.strip()
    }
    supported = set(supported_extensions())
    unsupported = normalized - supported
    if unsupported:
        raise KnowledgeWorkbenchError(
            "NAS 扫描包含暂不支持的扩展名：" + ", ".join(sorted(unsupported))
        )
    return frozenset(normalized)


def detect_nas_risk_flags(relative_path: Path) -> tuple[str, ...]:
    """Detect high-confidence admission risks from path metadata only."""
    stem = relative_path.stem.casefold()
    compact = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", stem)
    chinese_markers = (
        "账号密码",
        "帐号密码",
        "账户密码",
        "用户密码",
        "账号口令",
        "账户口令",
    )
    english_pair = (
        ("account" in compact or "user" in compact)
        and "password" in compact
    )
    english_list = (
        ("password" in compact or "credential" in compact)
        and "list" in compact
    )
    exact_english = compact in {"credentials", "passwords", "secrets"}
    if (
        any(marker in compact for marker in chinese_markers)
        or english_pair
        or english_list
        or exact_english
    ):
        return (CREDENTIAL_MATERIAL_RISK,)
    return ()


def detect_content_credential_risk(path: Path) -> bool:
    """Return only a high-confidence risk decision, never matched credential text."""

    name = path.name.casefold()
    if (
        path.suffix.casefold() not in CONTENT_SCAN_EXTENSIONS
        and name != ".env"
        and not name.startswith(".env.")
    ):
        return False
    try:
        with path.open("rb") as stream:
            payload = stream.read(CONTENT_SCAN_LIMIT_BYTES)
    except OSError:
        return False
    if not payload:
        return False
    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = payload.decode("gb18030", errors="ignore")
    if not text:
        return False
    if _PRIVATE_KEY_PATTERN.search(text):
        return True
    if _DIRECT_CREDENTIAL_ASSIGNMENT_PATTERN.search(text):
        return True
    return _has_account_password_example_context(text)


def _has_account_password_example_context(text: str) -> bool:
    account_positions = [
        match.start() for match in _ACCOUNT_LABEL_PATTERN.finditer(text)
    ]
    password_positions = [
        match.start() for match in _PASSWORD_LABEL_PATTERN.finditer(text)
    ]
    if not account_positions or not password_positions:
        return False
    for context in _EXAMPLE_CONTEXT_PATTERN.finditer(text):
        context_position = context.start()
        nearby_accounts = (
            position
            for position in account_positions
            if abs(position - context_position) <= _CREDENTIAL_CONTEXT_SPAN
        )
        nearby_passwords = [
            position
            for position in password_positions
            if abs(position - context_position) <= _CREDENTIAL_CONTEXT_SPAN
        ]
        for account_position in nearby_accounts:
            if any(
                max(account_position, password_position, context_position)
                - min(account_position, password_position, context_position)
                <= _CREDENTIAL_CONTEXT_SPAN
                for password_position in nearby_passwords
            ):
                return True
    return False


def _assert_admission_allowed(row) -> None:
    flags = set(json.loads(row["risk_flags_json"]))
    if CREDENTIAL_MATERIAL_RISK in flags:
        raise KnowledgeWorkbenchError(
            "该文件名高度疑似账号或凭据清单，知识库禁止吸收；"
            "请执行 ignore，并使用专用凭据管理系统"
        )


def _is_eligible(
    relative: Path,
    *,
    allowed_extensions: frozenset[str],
    include_globs: tuple[str, ...],
    exclude_globs: tuple[str, ...],
    include_archives: bool,
) -> bool:
    name = relative.name
    relative_posix = relative.as_posix()
    if name.startswith((".", "~$")) or name.endswith("~"):
        return False
    if relative.suffix.lower() in TEMPORARY_SUFFIXES:
        return False
    if relative.suffix.lower() not in allowed_extensions:
        return False
    if not include_archives and any(
        part.casefold() in DEFAULT_ARCHIVE_DIRECTORIES
        for part in relative.parts[:-1]
    ):
        return False
    if include_globs and not any(
        fnmatch.fnmatch(relative_posix, pattern) for pattern in include_globs
    ):
        return False
    return not any(
        fnmatch.fnmatch(relative_posix, pattern) for pattern in exclude_globs
    )


def _required_text(value: str, field: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise KnowledgeWorkbenchError(f"{field} 不能为空")
    return normalized
