from __future__ import annotations

import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

from .audit import record_event
from .config import WorkspacePaths
from .database import Database
from .errors import KnowledgeWorkbenchError
from .governance import infer_document_purpose, infer_knowledge_domain
from .models import Classification
from .nas_admission import (
    CREDENTIAL_MATERIAL_RISK,
    detect_content_credential_risk,
    detect_nas_risk_flags,
)
from .parsers import parse_document, supported_extensions
from .review import finalize_technical_validation
from .utils import new_id, sha256_file, sha256_text, utc_now


MAP_STATUSES = ("readable", "metadata_only", "blocked", "unreadable")
CORPUS_VIEWS = frozenset(
    {"all", "readable", "duplicates", "versions", "attention"}
)
SCOPE_STATUSES = ("unreviewed", "in_scope", "out_of_scope")
AUTHORITY_STATUSES = (
    "unknown",
    "reference",
    "authoritative",
    "superseded",
)
MAX_CONTENT_PREVIEW_ITEMS = 8
MAX_CONTENT_PREVIEW_ITEM_CHARS = 360
MAX_CONTENT_PREVIEW_TOTAL_CHARS = 2400

_ARCHIVE_PARTS = frozenset(
    {"archive", "archives", "archived", "backup", "backups", "归档", "备份"}
)
_TEMPORARY_SUFFIXES = frozenset(
    {".bak", ".crdownload", ".old", ".part", ".swp", ".tmp"}
)
_VERSION_NOISE = re.compile(
    r"(?ix)"
    r"(?:[\s._-]*"
    r"(?:v(?:er(?:sion)?)?\s*\d+(?:[._-]\d+)*"
    r"|20\d{2}[-_.年]?\d{0,2}[-_.月]?\d{0,2}日?"
    r"|最终版|终稿|定稿|修订版|修正版|最新版|旧版|新版"
    r"|副本|复制|copy"
    r"|第?\d+版)"
    r")+$"
)
_DATE_PATTERN = re.compile(
    r"(?<!\d)(?:19|20)\d{2}"
    r"(?:年\d{1,2}月(?:\d{1,2}日)?|[-./]\d{1,2}(?:[-./]\d{1,2})?)?"
)
_VERSION_PATTERN = re.compile(
    r"(?i)(?:\bv(?:er(?:sion)?)?\s*\d+(?:[._-]\d+)*\b|第?\d+版|最终版|终稿|修订版|定稿)"
)
_DOCUMENT_TYPES = {
    ".pdf": "PDF 文档",
    ".docx": "Word 文档",
    ".doc": "旧版 Word 文档",
    ".xlsx": "Excel 表格",
    ".pptx": "PowerPoint 演示文稿",
    ".txt": "文本文件",
    ".md": "Markdown 文档",
    ".markdown": "Markdown 文档",
    ".csv": "CSV 表格",
    ".sql": "SQL 数据库脚本",
}
_GENERIC_VERSION_STEMS = frozenset(
    {
        "agents",
        "changelog",
        "claude",
        "contributing",
        "index",
        "license",
        "readme",
    }
)
_READABLE_FILE_TITLES = {
    "00-init-database.sql": "数据库初始化脚本",
    "01-system-base.sql": "系统基础数据表",
    "02-course-route-auth.sql": "课程、线路与授权数据表",
    "03-trade-main.sql": "交易主流程数据表",
    "04-roster-consent.sql": "名单与监护人同意数据表",
    "05-settlement.sql": "缴费、分账与结算数据表",
    "06-safety-supervision.sql": "安全与监督数据表",
    "07-location-audit.sql": "定位与审计数据表",
    "88-readonly-triggers.sql": "数据只读保护规则",
    "89-audit-log-cdb.sql": "审计日志数据表",
    "product.md": "产品定位与用户场景",
    "claude.md": "AI 开发协作说明",
    "文旅部.pdf": "文化和旅游部办公厅关于促进旅行社研学旅游业务健康发展的通知",
    "文旅部研学旅游规范.pdf": "研学旅游合同（示范文本 GF-2026-2619）",
}


def scan_corpus_source(
    database: Database,
    source_root: Path,
    *,
    actor: str,
    project_name: str | None = None,
    allow_legacy_word_conversion: bool = False,
) -> dict[str, Any]:
    """Create a local, read-only corpus map without importing knowledge."""

    actor = _required_text(actor, "actor")
    project_name = (
        project_name.strip() if project_name and project_name.strip() else None
    )
    root = source_root.expanduser().resolve()
    if not root.is_dir():
        raise KnowledgeWorkbenchError(f"资料目录不存在或不是目录：{root}")

    previous_decisions = _previous_decisions(database, root)
    observations: list[dict[str, Any]] = []
    folder_paths: set[str] = set()
    unreadable_count = 0
    total_size = 0
    supported = frozenset(supported_extensions())
    scan_started_at = utc_now()

    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix().casefold()):
        try:
            if not path.is_file() or path.is_symlink():
                continue
            relative = path.relative_to(root)
            relative_path = relative.as_posix()
            folder_path = relative.parent.as_posix()
            if folder_path == ".":
                folder_path = ""
            folder_paths.add(folder_path)
            before = path.stat()
            digest = sha256_file(path)
            after = path.stat()
            total_size += after.st_size
        except OSError:
            unreadable_count += 1
            continue

        risk_flags = _risk_flags(relative)
        if (
            CREDENTIAL_MATERIAL_RISK not in risk_flags
            and detect_content_credential_risk(path)
        ):
            risk_flags.append(CREDENTIAL_MATERIAL_RISK)
        extension = path.suffix.lower()
        card = {
            "map_status": "metadata_only",
            "document_type": _DOCUMENT_TYPES.get(
                extension, f"{extension[1:].upper()} 文件" if extension else "无扩展名文件"
            ),
            "display_title": _readable_title(path),
            "plain_summary": "系统已登记这个文件，但当前格式尚未进入正文预读范围。",
            "outline": [],
            "content_preview": [],
            "key_signals": {
                "content_units": 0,
                "character_count": 0,
                "dates": [],
                "version_markers": [],
            },
            "parser_name": None,
            "parser_version": None,
        }

        if before.st_size != after.st_size or before.st_mtime_ns != after.st_mtime_ns:
            card["map_status"] = "unreadable"
            card["plain_summary"] = "扫描过程中这个文件发生了变化，系统没有读取正文，请稍后重新扫描。"
            risk_flags.append("changed_during_scan")
        elif CREDENTIAL_MATERIAL_RISK in risk_flags:
            card["map_status"] = "blocked"
            card["display_title"] = "疑似账号、密码或密钥资料"
            card["plain_summary"] = "为避免泄露，系统只登记了安全元数据，没有读取正文。该文件不能进入知识库。"
        elif "temporary_file" in risk_flags:
            card["plain_summary"] = "这是临时或下载中的文件。系统已登记位置，但没有读取正文。"
        elif "archive_or_backup" in risk_flags:
            card["plain_summary"] = "这是归档或备份目录中的文件。系统已登记位置，但没有读取正文。"
        elif extension == ".doc" and not allow_legacy_word_conversion:
            card["plain_summary"] = (
                "这是旧版 Word 文件。系统已登记位置；只有明确允许本机 Word 转换后才会预读正文。"
            )
        elif extension in supported:
            try:
                parsed = parse_document(
                    path,
                    allow_legacy_word_conversion=allow_legacy_word_conversion,
                )
            except Exception as exc:
                card["map_status"] = "unreadable"
                card["plain_summary"] = (
                    f"系统尝试读取这份{card['document_type']}，但没有得到可用正文。"
                    f"原因：{_plain_error(exc)}"
                )
            else:
                card.update(_build_plain_card(path, parsed))

        decision = previous_decisions.get((relative_path, digest))
        credential_risk = CREDENTIAL_MATERIAL_RISK in risk_flags
        observations.append(
            {
                "id": new_id("corpusfile"),
                "relative_path": relative_path,
                "folder_path": folder_path,
                "file_name": path.name,
                "extension": extension,
                "sha256": digest,
                "size_bytes": after.st_size,
                "modified_at_ns": after.st_mtime_ns,
                "risk_flags": sorted(set(risk_flags)),
                "scope_status": (
                    "out_of_scope"
                    if credential_risk
                    else decision["scope_status"] if decision else "unreviewed"
                ),
                "authority_status": (
                    "unknown"
                    if credential_risk
                    else decision["authority_status"] if decision else "unknown"
                ),
                "decided_by": (
                    "system:credential-scan"
                    if credential_risk
                    else decision["decided_by"] if decision else None
                ),
                "decided_at": (
                    scan_started_at
                    if credential_risk
                    else decision["decided_at"] if decision else None
                ),
                "decision_reason": (
                    "正文高置信凭据风险，已从知识问答隔离"
                    if credential_risk
                    else decision["decision_reason"] if decision else None
                ),
                **card,
            }
        )

    duplicate_groups, version_groups, relations = _candidate_relationships(
        observations
    )
    scan_id = new_id("corpus")
    now = utc_now()
    counts = {
        status: sum(1 for item in observations if item["map_status"] == status)
        for status in MAP_STATUSES
    }

    quarantined_document_ids: set[str] = set()
    with database.transaction() as connection:
        connection.execute(
            """
            UPDATE corpus_scans SET is_current = 0
            WHERE source_root = ? AND is_current = 1
            """,
            (str(root),),
        )
        connection.execute(
            """
            INSERT INTO corpus_scans(
                id, source_root, root_label, project_name, status, is_current,
                file_count, folder_count, total_size_bytes,
                readable_card_count, metadata_only_count, blocked_count,
                unreadable_count, duplicate_group_count, version_group_count,
                actor, created_at, completed_at
            ) VALUES (?, ?, ?, ?, 'completed', 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                scan_id,
                str(root),
                root.name or str(root),
                project_name,
                len(observations),
                len(folder_paths),
                total_size,
                counts["readable"],
                counts["metadata_only"],
                counts["blocked"],
                counts["unreadable"] + unreadable_count,
                len(duplicate_groups),
                len(version_groups),
                actor,
                now,
                now,
            ),
        )
        for item in observations:
            connection.execute(
                """
                INSERT INTO corpus_files(
                    id, scan_id, relative_path, folder_path, file_name,
                    extension, sha256, size_bytes, modified_at_ns, map_status,
                    document_type, display_title, plain_summary, outline_json,
                    content_preview_json, key_signals_json, risk_flags_json, parser_name,
                    parser_version, duplicate_group, version_group,
                    scope_status, authority_status, decided_by, decided_at,
                    decision_reason, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                          ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item["id"],
                    scan_id,
                    item["relative_path"],
                    item["folder_path"],
                    item["file_name"],
                    item["extension"],
                    item["sha256"],
                    item["size_bytes"],
                    item["modified_at_ns"],
                    item["map_status"],
                    item["document_type"],
                    item["display_title"],
                    item["plain_summary"],
                    json.dumps(item["outline"], ensure_ascii=False),
                    json.dumps(item["content_preview"], ensure_ascii=False),
                    json.dumps(item["key_signals"], ensure_ascii=False),
                    json.dumps(item["risk_flags"], ensure_ascii=False),
                    item["parser_name"],
                    item["parser_version"],
                    item.get("duplicate_group"),
                    item.get("version_group"),
                    item["scope_status"],
                    item["authority_status"],
                    item["decided_by"],
                    item["decided_at"],
                    item["decision_reason"],
                    now,
                ),
            )
            if CREDENTIAL_MATERIAL_RISK in item["risk_flags"]:
                quarantined_document_ids.update(
                    _quarantine_imported_documents(
                        connection,
                        sha256=item["sha256"],
                        actor="system:credential-scan",
                        now=now,
                    )
                )
        by_path = {item["relative_path"]: item["id"] for item in observations}
        for relation in relations:
            connection.execute(
                """
                INSERT INTO corpus_file_relations(
                    id, scan_id, source_file_id, target_file_id,
                    relation_type, plain_reason, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    new_id("corpusrel"),
                    scan_id,
                    by_path[relation["source_path"]],
                    by_path[relation["target_path"]],
                    relation["relation_type"],
                    relation["plain_reason"],
                    now,
                ),
            )
        record_event(
            connection,
            "corpus_map_created",
            "corpus_scan",
            scan_id,
            actor=actor,
            details={
                "file_count": len(observations),
                "folder_count": len(folder_paths),
                "readable_card_count": counts["readable"],
                "metadata_only_count": counts["metadata_only"],
                "blocked_count": counts["blocked"],
                "unreadable_count": counts["unreadable"] + unreadable_count,
                "duplicate_group_count": len(duplicate_groups),
                "version_group_count": len(version_groups),
                "project_name_sha256": (
                    sha256_text(project_name) if project_name else None
                ),
                "source_root_sha256": sha256_text(str(root)),
                "quarantined_document_count": len(quarantined_document_ids),
            },
        )

    return {
        "scan_id": scan_id,
        "root_label": root.name or str(root),
        "project_name": project_name,
        "file_count": len(observations),
        "folder_count": len(folder_paths),
        "total_size_bytes": total_size,
        "readable_card_count": counts["readable"],
        "metadata_only_count": counts["metadata_only"],
        "blocked_count": counts["blocked"],
        "unreadable_count": counts["unreadable"] + unreadable_count,
        "duplicate_group_count": len(duplicate_groups),
        "version_group_count": len(version_groups),
        "quarantined_document_count": len(quarantined_document_ids),
    }


def latest_corpus_map(
    database: Database,
    *,
    limit: int = 100,
    offset: int = 0,
    folder: str | None = None,
    scope_status: str | None = None,
    query: str | None = None,
    view: str = "all",
) -> dict[str, Any]:
    if limit <= 0 or limit > 500:
        raise KnowledgeWorkbenchError("limit 必须在 1 到 500 之间")
    if offset < 0:
        raise KnowledgeWorkbenchError("offset 不能小于 0")
    if scope_status and scope_status not in SCOPE_STATUSES:
        raise KnowledgeWorkbenchError(f"不支持的资料范围状态：{scope_status}")
    if view not in CORPUS_VIEWS:
        raise KnowledgeWorkbenchError(f"不支持的资料地图视图：{view}")
    query = (query or "").strip()
    if len(query) > 120:
        raise KnowledgeWorkbenchError("搜索文字不能超过 120 个字符")
    folder = (folder or "").strip().strip("/")

    with database.connect() as connection:
        scan = connection.execute(
            """
            SELECT * FROM corpus_scans
            WHERE is_current = 1
            ORDER BY completed_at DESC, id DESC
            LIMIT 1
            """
        ).fetchone()
        if scan is None:
            return {
                "scan": None,
                "folders": [],
                "total": 0,
                "limit": limit,
                "offset": offset,
                "view": view,
                "items": [],
            }
        where = ["cf.scan_id = ?"]
        parameters: list[Any] = [scan["id"]]
        if folder:
            where.append("(cf.folder_path = ? OR cf.folder_path LIKE ?)")
            parameters.extend((folder, f"{folder}/%"))
        if scope_status:
            where.append("cf.scope_status = ?")
            parameters.append(scope_status)
        if query:
            where.append(
                """
                (instr(lower(cf.display_title), lower(?)) > 0
                 OR instr(lower(cf.plain_summary), lower(?)) > 0
                 OR (
                    instr(cf.risk_flags_json, 'credential_material') = 0
                    AND instr(lower(cf.relative_path), lower(?)) > 0
                 ))
                """
            )
            parameters.extend((query, query, query))
        if view == "readable":
            where.append("cf.map_status = 'readable'")
        elif view == "duplicates":
            where.append("cf.duplicate_group IS NOT NULL")
        elif view == "versions":
            where.append("cf.version_group IS NOT NULL")
        elif view == "attention":
            where.append("cf.map_status IN ('blocked', 'unreadable')")
        predicate = " AND ".join(where)
        total = connection.execute(
            f"SELECT COUNT(*) FROM corpus_files cf WHERE {predicate}",
            parameters,
        ).fetchone()[0]
        rows = connection.execute(
            f"""
            SELECT cf.*,
                   CASE WHEN cf.scope_status = 'in_scope' THEN
                       (SELECT dv.document_id
                        FROM document_versions dv
                        WHERE dv.sha256 = cf.sha256
                        LIMIT 1)
                   END AS imported_document_id,
                   (SELECT COUNT(*) FROM corpus_file_relations cfr
                    WHERE cfr.scan_id = cf.scan_id
                      AND (cfr.source_file_id = cf.id OR cfr.target_file_id = cf.id))
                       AS relation_count
            FROM corpus_files cf
            WHERE {predicate}
            ORDER BY cf.folder_path, cf.relative_path
            LIMIT ? OFFSET ?
            """,
            (*parameters, limit, offset),
        ).fetchall()
        folder_rows = connection.execute(
            """
            SELECT folder_path, COUNT(*) AS file_count,
                   SUM(CASE WHEN scope_status = 'unreviewed' THEN 1 ELSE 0 END)
                       AS unreviewed_count
            FROM corpus_files
            WHERE scan_id = ?
            GROUP BY folder_path
            ORDER BY folder_path
            """,
            (scan["id"],),
        ).fetchall()
    return {
        "scan": _scan_projection(scan),
        "folders": [
            {
                "folder": row["folder_path"],
                "display_name": row["folder_path"] or "根目录",
                "file_count": row["file_count"],
                "unreviewed_count": row["unreviewed_count"],
            }
            for row in folder_rows
        ],
        "total": total,
        "limit": limit,
        "offset": offset,
        "view": view,
        "has_previous": offset > 0,
        "has_next": offset + len(rows) < total,
        "items": [_file_projection(row) for row in rows],
    }


def corpus_file_card(database: Database, file_id: str) -> dict[str, Any]:
    with database.connect() as connection:
        row = connection.execute(
            """
            SELECT cf.*,
                   CASE WHEN cf.scope_status = 'in_scope' THEN
                       (SELECT dv.document_id
                        FROM document_versions dv
                        WHERE dv.sha256 = cf.sha256
                        LIMIT 1)
                   END AS imported_document_id,
                   CASE WHEN cf.scope_status = 'in_scope' THEN
                       (SELECT d.classification
                        FROM document_versions dv
                        JOIN documents d ON d.id = dv.document_id
                        WHERE dv.sha256 = cf.sha256
                        LIMIT 1)
                   END AS imported_classification
            FROM corpus_files cf
            JOIN corpus_scans cs ON cs.id = cf.scan_id
            WHERE cf.id = ? AND cs.is_current = 1
            """,
            (file_id,),
        ).fetchone()
        if row is None:
            raise KnowledgeWorkbenchError("资料说明卡不存在或已经不是最新扫描结果")
        relations = connection.execute(
            """
            SELECT cfr.relation_type, cfr.plain_reason,
                   other.id AS other_file_id,
                   other.display_title AS other_title,
                   other.relative_path AS other_relative_path,
                   other.risk_flags_json AS other_risk_flags_json
            FROM corpus_file_relations cfr
            JOIN corpus_files other
              ON other.id = CASE
                  WHEN cfr.source_file_id = ? THEN cfr.target_file_id
                  ELSE cfr.source_file_id
              END
            WHERE cfr.scan_id = ?
              AND (cfr.source_file_id = ? OR cfr.target_file_id = ?)
            ORDER BY cfr.relation_type, other.relative_path
            """,
            (file_id, row["scan_id"], file_id, file_id),
        ).fetchall()
    result = _file_projection(row, detail=True)
    result["relations"] = [
        {
            "relation_type": relation["relation_type"],
            "plain_reason": relation["plain_reason"],
            "other_file_id": relation["other_file_id"],
            "other_title": (
                "受保护的风险文件"
                if CREDENTIAL_MATERIAL_RISK
                in json.loads(relation["other_risk_flags_json"])
                else relation["other_title"]
            ),
        }
        for relation in relations
    ]
    return result


def decide_corpus_file(
    database: Database,
    file_id: str,
    *,
    scope_status: str,
    authority_status: str,
    actor: str,
    reason: str,
) -> dict[str, Any]:
    actor = _required_text(actor, "actor")
    reason = _required_text(reason, "reason")
    if scope_status not in {"in_scope", "out_of_scope"}:
        raise KnowledgeWorkbenchError("资料范围决定必须是 in_scope 或 out_of_scope")
    if authority_status not in AUTHORITY_STATUSES:
        raise KnowledgeWorkbenchError(f"不支持的权威性状态：{authority_status}")
    now = utc_now()
    with database.transaction() as connection:
        row = connection.execute(
            """
            SELECT cf.*
            FROM corpus_files cf
            JOIN corpus_scans cs ON cs.id = cf.scan_id
            WHERE cf.id = ? AND cs.is_current = 1
            """,
            (file_id,),
        ).fetchone()
        if row is None:
            raise KnowledgeWorkbenchError("资料说明卡不存在或已经不是最新扫描结果")
        risk_flags = json.loads(row["risk_flags_json"])
        if (
            scope_status == "in_scope"
            and CREDENTIAL_MATERIAL_RISK in risk_flags
        ):
            raise KnowledgeWorkbenchError("疑似账号、密码或密钥资料不能纳入知识范围")
        if (
            CREDENTIAL_MATERIAL_RISK in risk_flags
            and authority_status != "unknown"
        ):
            raise KnowledgeWorkbenchError("凭据风险资料只能保持未知权威性并隔离")
        knowledge_domain = infer_knowledge_domain(
            str(row["relative_path"]),
            file_name=str(row["file_name"]),
        )
        document_purpose = infer_document_purpose(str(row["relative_path"]))
        imported = connection.execute(
            """
            SELECT dv.document_id, dg.purpose, dg.scope_status,
                   dg.authority_status, dg.knowledge_domain
            FROM document_versions dv
            JOIN document_governance dg ON dg.document_id = dv.document_id
            WHERE dv.sha256 = ?
            LIMIT 1
            """,
            (row["sha256"],),
        ).fetchone()
        connection.execute(
            """
            UPDATE corpus_files
            SET scope_status = ?, authority_status = ?, decided_by = ?,
                decided_at = ?, decision_reason = ?
            WHERE id = ?
            """,
            (
                scope_status,
                authority_status,
                actor,
                now,
                reason,
                file_id,
            ),
        )
        effective = connection.execute(
            """
            SELECT *
            FROM corpus_files
            WHERE scan_id = ? AND sha256 = ? AND scope_status = 'in_scope'
            ORDER BY
                CASE authority_status
                    WHEN 'authoritative' THEN 0
                    WHEN 'reference' THEN 1
                    ELSE 2
                END,
                decided_at DESC,
                relative_path
            LIMIT 1
            """,
            (row["scan_id"], row["sha256"]),
        ).fetchone()
        effective_scope = "in_scope" if effective is not None else scope_status
        effective_authority = (
            effective["authority_status"]
            if effective is not None
            else authority_status
        )
        effective_actor = (
            effective["decided_by"] if effective is not None else actor
        )
        effective_reviewed_at = (
            effective["decided_at"] if effective is not None else now
        )
        effective_reason = (
            effective["decision_reason"] if effective is not None else reason
        )
        effective_domain = (
            infer_knowledge_domain(
                str(effective["relative_path"]),
                file_name=str(effective["file_name"]),
            )
            if effective is not None
            else knowledge_domain
        )
        effective_purpose = (
            infer_document_purpose(str(effective["relative_path"]))
            if effective is not None
            else document_purpose
        )
        if imported is not None:
            connection.execute(
                """
                UPDATE document_governance
                SET purpose = ?, scope_status = ?, authority_status = ?,
                    reviewed_by = ?, reviewed_at = ?, decision_reason = ?,
                    knowledge_domain = ?, updated_at = ?
                WHERE document_id = ?
                """,
                (
                    effective_purpose,
                    effective_scope,
                    effective_authority,
                    effective_actor,
                    effective_reviewed_at,
                    effective_reason,
                    effective_domain,
                    now,
                    imported["document_id"],
                ),
            )
            if (
                imported["purpose"] != effective_purpose
                or imported["scope_status"] != effective_scope
                or imported["authority_status"] != effective_authority
                or imported["knowledge_domain"] != effective_domain
            ):
                _mark_document_wiki_revalidation(
                    connection,
                    document_id=imported["document_id"],
                    actor=actor,
                    now=now,
                    reason="document_governance_changed",
                    cause_id=file_id,
                )
        record_event(
            connection,
            "corpus_file_scope_decided",
            "corpus_file",
            file_id,
            actor=actor,
            details={
                "scope_status": scope_status,
                "authority_status": authority_status,
                "reason_sha256": sha256_text(reason),
                "sha256": row["sha256"],
                "document_id": (
                    imported["document_id"] if imported is not None else None
                ),
                "document_purpose": document_purpose,
                "knowledge_domain": knowledge_domain,
                "effective_governance_scope": effective_scope,
                "effective_governance_purpose": effective_purpose,
            },
        )
    return corpus_file_card(database, file_id)


def import_corpus_file(
    database: Database,
    paths: WorkspacePaths,
    file_id: str,
    *,
    classification: Classification | str,
    actor: str,
) -> dict[str, Any]:
    """Explicitly import one approved map item into the existing SHA-256 pipeline."""

    actor = _required_text(actor, "actor")
    try:
        selected_classification = (
            classification
            if isinstance(classification, Classification)
            else Classification(str(classification))
        )
    except ValueError as exc:
        raise KnowledgeWorkbenchError("请选择有效的资料密级") from exc

    with database.connect() as connection:
        row = connection.execute(
            """
            SELECT cf.*, cs.source_root
            FROM corpus_files cf
            JOIN corpus_scans cs ON cs.id = cf.scan_id
            WHERE cf.id = ? AND cs.is_current = 1
            """,
            (file_id,),
        ).fetchone()
    if row is None:
        raise KnowledgeWorkbenchError("资料说明卡不存在或已经不是最新扫描结果")
    risk_flags = json.loads(row["risk_flags_json"])
    if CREDENTIAL_MATERIAL_RISK in risk_flags:
        raise KnowledgeWorkbenchError("疑似账号、密码或密钥资料禁止导入知识库")
    if row["scope_status"] != "in_scope":
        raise KnowledgeWorkbenchError("只有明确纳入业务范围的资料才能导入")
    if row["authority_status"] not in {"reference", "authoritative"}:
        raise KnowledgeWorkbenchError("导入前必须确认这是参考资料还是现行权威资料")

    root = Path(str(row["source_root"])).resolve()
    source = (root / str(row["relative_path"])).resolve()
    if source == root or root not in source.parents:
        raise KnowledgeWorkbenchError("资料路径超出本次扫描目录")
    if not source.is_file() or source.is_symlink():
        raise KnowledgeWorkbenchError("资料已经移动、删除或变成不允许的链接")
    try:
        current_stat = source.stat()
        current_sha256 = sha256_file(source)
    except OSError as exc:
        raise KnowledgeWorkbenchError("资料当前无法读取，请重新生成资料地图") from exc
    if (
        current_sha256 != row["sha256"]
        or current_stat.st_size != row["size_bytes"]
        or current_stat.st_mtime_ns != row["modified_at_ns"]
    ):
        raise KnowledgeWorkbenchError("资料内容或文件状态已变化，请重新生成资料地图并确认")
    if detect_content_credential_risk(source):
        raise KnowledgeWorkbenchError("文件正文存在高置信凭据风险，禁止导入知识库")

    from .ingest import ingest_file

    result = ingest_file(
        source,
        paths,
        selected_classification,
        actor=actor,
        expected_sha256=str(row["sha256"]),
    )
    now = utc_now()
    knowledge_domain = infer_knowledge_domain(
        str(row["relative_path"]),
        file_name=str(row["file_name"]),
    )
    document_purpose = infer_document_purpose(str(row["relative_path"]))
    with database.transaction() as connection:
        connection.execute(
            """
            UPDATE document_governance
            SET purpose = ?,
                scope_status = 'in_scope',
                authority_status = ?,
                reviewed_by = ?,
                reviewed_at = ?,
                decision_reason = ?,
                knowledge_domain = ?,
                updated_at = ?
            WHERE document_id = ?
            """,
            (
                document_purpose,
                row["authority_status"],
                row["decided_by"] or actor,
                row["decided_at"] or now,
                row["decision_reason"],
                knowledge_domain,
                now,
                result.document_id,
            ),
        )
        technical_validation = finalize_technical_validation(
            connection,
            result.processing_run_id,
        )
        wiki_state = connection.execute(
            """
            SELECT wp.status AS page_status, wp.needs_revalidation,
                   wr.status AS revision_status
            FROM wiki_pages wp
            LEFT JOIN wiki_revisions wr ON wr.id = ?
            WHERE wp.id = ?
            """,
            (result.revision_id, result.page_id),
        ).fetchone()
        if not wiki_state:
            raise KnowledgeWorkbenchError("导入已中止：没有生成可复核的 Wiki 草稿")
        record_event(
            connection,
            "corpus_file_imported",
            "corpus_file",
            file_id,
            actor=actor,
            details={
                "document_id": result.document_id,
                "document_version_id": result.version_id,
                "classification": selected_classification.value,
                "document_purpose": document_purpose,
                "authority_status": row["authority_status"],
                "knowledge_domain": knowledge_domain,
                "duplicate": result.duplicate,
                "sha256": row["sha256"],
                "technically_validated_evidence_count": (
                    technical_validation["total"]
                ),
                "technical_status_promoted_count": (
                    technical_validation["promoted"]
                ),
                "preserved_exception_count": (
                    technical_validation["preserved_exception"]
                ),
                "wiki_page_status": wiki_state["page_status"],
                "wiki_revision_status": wiki_state["revision_status"],
            },
        )
    if (
        wiki_state["page_status"] == "verified"
        and not wiki_state["needs_revalidation"]
    ):
        next_action = "ready"
    elif wiki_state["revision_status"] == "reviewing":
        next_action = "finish_wiki_review"
    else:
        next_action = "review_wiki"
    return {
        "file_id": file_id,
        "document_id": result.document_id,
        "document_version_id": result.version_id,
        "processing_run_id": result.processing_run_id,
        "classification": selected_classification.value,
        "authority_status": row["authority_status"],
        "duplicate": result.duplicate,
        "evidence_count": result.evidence_count,
        "technically_validated_evidence_count": technical_validation["total"],
        "technical_status_promoted_count": technical_validation["promoted"],
        "preserved_exception_count": technical_validation["preserved_exception"],
        "wiki_status": wiki_state["page_status"],
        "wiki_revision_status": wiki_state["revision_status"],
        "next_action": next_action,
    }


def _build_plain_card(path: Path, parsed) -> dict[str, Any]:
    texts = [_clean_text(unit.text) for unit in parsed.units if unit.text.strip()]
    character_count = sum(len(text) for text in texts)
    sample_parts: list[str] = []
    sample_length = 0
    for text in texts:
        clipped = _clip(text, 180)
        if sample_length + len(clipped) > 420:
            break
        sample_parts.append(clipped)
        sample_length += len(clipped)
        if len(sample_parts) >= 3:
            break
    opening = "；".join(sample_parts)
    document_type = _DOCUMENT_TYPES.get(
        path.suffix.lower(),
        f"{path.suffix[1:].upper()} 文件" if path.suffix else "文件",
    )
    outline = _outline(parsed.units)
    combined_sample = "\n".join(texts[:30])[:20_000]
    dates = list(dict.fromkeys(_DATE_PATTERN.findall(combined_sample)))[:8]
    version_markers = list(
        dict.fromkeys(match.group(0) for match in _VERSION_PATTERN.finditer(
            f"{path.stem}\n{combined_sample}"
        ))
    )[:8]
    summary = (
        f"这是一份{document_type}，系统读取了{len(texts)}个内容单元，"
        f"约{character_count}个字符。"
    )
    if opening:
        summary += f"开头主要内容：{opening}"
    return {
        "map_status": "readable",
        "document_type": document_type,
        "display_title": _readable_title(path),
        "plain_summary": summary,
        "outline": outline,
        "content_preview": _content_preview(parsed.units),
        "key_signals": {
            "content_units": len(texts),
            "character_count": character_count,
            "dates": dates,
            "version_markers": version_markers,
        },
        "parser_name": parsed.parser_name,
        "parser_version": parsed.parser_version,
    }


def _preview_label(locator: dict[str, Any], ordinal: int) -> str:
    heading_path = locator.get("heading_path")
    if isinstance(heading_path, list):
        headings = [
            _clean_text(str(value))
            for value in heading_path
            if _clean_text(str(value))
        ]
        if headings:
            return " / ".join(headings[-2:])
    if isinstance(locator.get("page"), int):
        return f"第 {locator['page']} 页"
    if locator.get("sheet"):
        cell_range = locator.get("cell_range")
        suffix = f" · {cell_range}" if cell_range else ""
        return f"工作表“{locator['sheet']}”{suffix}"
    if isinstance(locator.get("slide"), int):
        return f"第 {locator['slide']} 张幻灯片"
    return f"正文片段 {ordinal}"


def _content_preview(units) -> list[dict[str, str]]:
    prepared: list[dict[str, str | bool]] = []
    for ordinal, unit in enumerate(units, start=1):
        text = _clip(_clean_text(unit.text), MAX_CONTENT_PREVIEW_ITEM_CHARS)
        if not text:
            continue
        label = _preview_label(unit.locator, ordinal)
        prepared.append(
            {
                "label": label,
                "text": text,
                "structured": not label.startswith("正文片段 "),
            }
        )
    structured_items: list[dict[str, str | bool]] = []
    seen_labels: set[str] = set()
    for item in prepared:
        label = str(item["label"])
        if not item["structured"] or label in seen_labels:
            continue
        structured_items.append(item)
        seen_labels.add(label)
    if structured_items:
        selected = structured_items[:MAX_CONTENT_PREVIEW_ITEMS]
    elif prepared:
        positions = sorted({0, len(prepared) // 2, len(prepared) - 1})
        selected = [prepared[index] for index in positions]
    else:
        selected = []
    result: list[dict[str, str]] = []
    total = 0
    for item in selected:
        remaining = MAX_CONTENT_PREVIEW_TOTAL_CHARS - total
        if remaining <= 0:
            break
        text = str(item["text"])[:remaining]
        result.append({"label": str(item["label"]), "text": text})
        total += len(text)
    return result


def _outline(units) -> list[str]:
    items: list[str] = []
    seen: set[str] = set()
    sheets: list[str] = []
    pages: list[int] = []
    slides: list[int] = []
    for unit in units:
        locator = unit.locator
        for heading in locator.get("heading_path", []):
            value = _clean_text(str(heading))
            if value and value not in seen:
                items.append(value)
                seen.add(value)
                if len(items) >= 12:
                    return items
        sheet = locator.get("sheet")
        if sheet and str(sheet) not in sheets:
            sheets.append(str(sheet))
        if isinstance(locator.get("page"), int):
            pages.append(locator["page"])
        if isinstance(locator.get("slide"), int):
            slides.append(locator["slide"])
    if items:
        return items[:12]
    if sheets:
        return [f"工作表：{value}" for value in sheets[:12]]
    if pages:
        return [f"共读取第 {min(pages)} 至 {max(pages)} 页"]
    if slides:
        return [f"共读取第 {min(slides)} 至 {max(slides)} 张幻灯片"]
    return []


def _candidate_relationships(
    observations: list[dict[str, Any]],
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, list[dict[str, Any]]], list[dict[str, str]]]:
    by_sha: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_version_key: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in observations:
        by_sha[item["sha256"]].append(item)
        key = _version_key(Path(item["file_name"]).stem)
        if key:
            by_version_key[key].append(item)

    duplicate_groups = {
        digest: items for digest, items in by_sha.items() if len(items) > 1
    }
    version_groups = {
        key: items
        for key, items in by_version_key.items()
        if len(items) > 1 and len({item["sha256"] for item in items}) > 1
    }
    relations: list[dict[str, str]] = []
    for digest, items in duplicate_groups.items():
        group_id = f"dup-{digest[:12]}"
        ordered = sorted(items, key=lambda value: value["relative_path"])
        anchor = ordered[0]
        for item in ordered:
            item["duplicate_group"] = group_id
        for item in ordered[1:]:
            relations.append(
                {
                    "source_path": anchor["relative_path"],
                    "target_path": item["relative_path"],
                    "relation_type": "exact_duplicate",
                    "plain_reason": "两个文件的内容哈希完全相同，属于内容重复文件。",
                }
            )
    for key, items in version_groups.items():
        group_id = f"version-{sha256_text(key)[:12]}"
        ordered = sorted(
            items,
            key=lambda value: (value["modified_at_ns"], value["relative_path"]),
        )
        anchor = ordered[-1]
        for item in ordered:
            item["version_group"] = group_id
        for item in ordered[:-1]:
            relations.append(
                {
                    "source_path": item["relative_path"],
                    "target_path": anchor["relative_path"],
                    "relation_type": "version_candidate",
                    "plain_reason": (
                        "文件名去除日期、版本号和“最终版”等字样后相近，"
                        "系统判断它们可能属于同一资料的不同版本，需要人工确认。"
                    ),
                }
            )
    return duplicate_groups, version_groups, relations


def _previous_decisions(
    database: Database, root: Path
) -> dict[tuple[str, str], dict[str, Any]]:
    if not database.path.exists():
        return {}
    try:
        with database.connect() as connection:
            rows = connection.execute(
                """
                SELECT cf.relative_path, cf.sha256, cf.scope_status,
                       cf.authority_status, cf.decided_by, cf.decided_at,
                       cf.decision_reason
                FROM corpus_files cf
                JOIN corpus_scans cs ON cs.id = cf.scan_id
                WHERE cs.source_root = ? AND cs.is_current = 1
                  AND cf.scope_status != 'unreviewed'
                """,
                (str(root),),
            ).fetchall()
    except Exception:
        return {}
    return {
        (row["relative_path"], row["sha256"]): dict(row)
        for row in rows
    }


def _scan_projection(row) -> dict[str, Any]:
    return {
        "scan_id": row["id"],
        "root_label": row["root_label"],
        "project_name": row["project_name"],
        "status": row["status"],
        "file_count": row["file_count"],
        "folder_count": row["folder_count"],
        "total_size_bytes": row["total_size_bytes"],
        "readable_card_count": row["readable_card_count"],
        "metadata_only_count": row["metadata_only_count"],
        "blocked_count": row["blocked_count"],
        "unreadable_count": row["unreadable_count"],
        "duplicate_group_count": row["duplicate_group_count"],
        "version_group_count": row["version_group_count"],
        "completed_at": row["completed_at"],
    }


def _file_projection(row, *, detail: bool = False) -> dict[str, Any]:
    risk_flags = json.loads(row["risk_flags_json"])
    credential_risk = CREDENTIAL_MATERIAL_RISK in risk_flags
    restricted = (
        row["imported_classification"] == Classification.RESTRICTED.value
        if "imported_classification" in row.keys()
        else False
    )
    result = {
        "file_id": row["id"],
        "display_title": (
            "疑似账号、密码或密钥资料" if credential_risk else row["display_title"]
        ),
        "folder": (
            "受保护目录" if credential_risk else row["folder_path"] or "根目录"
        ),
        "document_type": row["document_type"],
        "map_status": row["map_status"],
        "plain_summary": row["plain_summary"],
        "outline": json.loads(row["outline_json"]),
        "key_signals": json.loads(row["key_signals_json"]),
        "risk_flags": risk_flags,
        "size_bytes": row["size_bytes"],
        "duplicate_group": row["duplicate_group"],
        "version_group": row["version_group"],
        "scope_status": row["scope_status"],
        "authority_status": row["authority_status"],
        "decided_by": row["decided_by"],
        "decided_at": row["decided_at"],
        "decision_reason": row["decision_reason"],
        "imported": bool(
            row["imported_document_id"]
            if "imported_document_id" in row.keys()
            else False
        ),
        "imported_document_id": (
            row["imported_document_id"]
            if "imported_document_id" in row.keys()
            else None
        ),
        "relation_count": (
            row["relation_count"] if "relation_count" in row.keys() else None
        ),
    }
    if detail:
        result.update(
            {
                "parser": (
                    f"{row['parser_name']} {row['parser_version']}"
                    if row["parser_name"]
                    else None
                ),
                "relative_path": (
                    None if credential_risk else row["relative_path"]
                ),
                "content_preview": (
                    []
                    if credential_risk or restricted
                    else [
                        {
                            "label": str(item.get("label", ""))[:200],
                            "text": str(item.get("text", ""))[
                                :MAX_CONTENT_PREVIEW_ITEM_CHARS
                            ],
                        }
                        for item in json.loads(row["content_preview_json"])[
                            :MAX_CONTENT_PREVIEW_ITEMS
                        ]
                        if isinstance(item, dict) and str(item.get("text", "")).strip()
                    ]
                ),
            }
        )
    return result


def _quarantine_imported_documents(
    connection,
    *,
    sha256: str,
    actor: str,
    now: str,
) -> tuple[str, ...]:
    rows = connection.execute(
        """
        SELECT d.id, d.classification, dg.scope_status, dg.authority_status
        FROM document_versions dv
        JOIN documents d ON d.id = dv.document_id
        JOIN document_governance dg ON dg.document_id = d.id
        WHERE dv.sha256 = ?
        """,
        (sha256,),
    ).fetchall()
    changed_ids: list[str] = []
    for row in rows:
        already_quarantined = (
            row["classification"] == Classification.RESTRICTED.value
            and row["scope_status"] == "out_of_scope"
            and row["authority_status"] == "unknown"
        )
        connection.execute(
            """
            UPDATE documents
            SET classification = 'restricted', updated_at = ?
            WHERE id = ?
            """,
            (now, row["id"]),
        )
        connection.execute(
            """
            UPDATE document_governance
            SET scope_status = 'out_of_scope',
                authority_status = 'unknown',
                reviewed_by = ?,
                reviewed_at = ?,
                decision_reason = '正文高置信凭据风险，已从知识问答隔离',
                updated_at = ?
            WHERE document_id = ?
            """,
            (actor, now, now, row["id"]),
        )
        if already_quarantined:
            continue
        _mark_document_wiki_revalidation(
            connection,
            document_id=row["id"],
            actor=actor,
            now=now,
            reason="credential_material_quarantined",
            cause_id=row["id"],
        )
        record_event(
            connection,
            "credential_document_quarantined",
            "document",
            row["id"],
            actor=actor,
            details={
                "sha256": sha256,
                "risk_code": CREDENTIAL_MATERIAL_RISK,
            },
        )
        changed_ids.append(row["id"])
    return tuple(changed_ids)


def _mark_document_wiki_revalidation(
    connection,
    *,
    document_id: str,
    actor: str,
    now: str,
    reason: str,
    cause_id: str,
) -> None:
    pages = connection.execute(
        """
        SELECT DISTINCT wp.id
        FROM wiki_pages wp
        LEFT JOIN revision_evidence re
          ON re.revision_id = wp.current_verified_revision_id
        LEFT JOIN evidence e ON e.id = re.evidence_id
        LEFT JOIN document_versions dv ON dv.id = e.document_version_id
        WHERE wp.current_verified_revision_id IS NOT NULL
          AND wp.needs_revalidation = 0
          AND (
              wp.source_document_id = ?
              OR dv.document_id = ?
          )
        ORDER BY wp.id
        """,
        (document_id, document_id),
    ).fetchall()
    for page in pages:
        connection.execute(
            """
            UPDATE wiki_pages
            SET needs_revalidation = 1, updated_at = ?
            WHERE id = ?
            """,
            (now, page["id"]),
        )
        record_event(
            connection,
            "wiki_revalidation_required",
            "wiki_page",
            page["id"],
            actor=actor,
            details={
                "reason": reason,
                "cause_id": cause_id,
                "changed_document_id": document_id,
            },
        )


def _risk_flags(relative: Path) -> list[str]:
    flags = list(detect_nas_risk_flags(relative))
    lowered_parts = {part.casefold() for part in relative.parts[:-1]}
    if lowered_parts & _ARCHIVE_PARTS:
        flags.append("archive_or_backup")
    if relative.suffix.lower() in _TEMPORARY_SUFFIXES or relative.name.startswith(
        ("~$", ".~lock.")
    ):
        flags.append("temporary_file")
    return sorted(set(flags))


def _version_key(stem: str) -> str:
    value = _VERSION_NOISE.sub("", stem).strip(" ._-").casefold()
    if value in _GENERIC_VERSION_STEMS:
        return ""
    return re.sub(r"[\s._-]+", "", value)


def _readable_title(path: Path) -> str:
    exact = _READABLE_FILE_TITLES.get(path.name.casefold())
    if exact:
        return exact
    if path.name.casefold() == "readme.md":
        parent = path.parent.name.casefold()
        if parent == "bpmn":
            return "BPMN 业务流程图说明"
        if parent == "tests":
            return "测试资料说明"
        if parent == "archive":
            return "归档项目说明"
        return "项目总说明"
    stem = path.stem
    try:
        repaired = stem.encode("gbk").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        repaired = stem
    title = re.sub(r"[_-]+", " ", repaired).strip()
    return title or path.name


def _clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _clip(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[: limit - 1].rstrip() + "…"


def _plain_error(exc: Exception) -> str:
    message = _clean_text(str(exc))
    if not message:
        return "文件不可读取或没有可提取文字"
    return _clip(message, 180)


def _required_text(value: str, name: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise KnowledgeWorkbenchError(f"{name} 不能为空")
    return normalized
