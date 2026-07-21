from __future__ import annotations

import json
import re
from pathlib import Path

from .audit import record_event
from .config import WorkspacePaths
from .database import Database
from .errors import InvalidTransitionError, KnowledgeWorkbenchError
from .utils import new_id, sha256_text, utc_now
from .wiki import write_text_atomic


LINK_BLOCK_PATTERN = re.compile(
    r"\n?<!-- knowledge-links:start -->.*?<!-- knowledge-links:end -->\n?",
    flags=re.DOTALL,
)


def add_wiki_link(
    database: Database,
    paths: WorkspacePaths,
    *,
    source_revision_id: str,
    target_page_id: str,
    relationship: str,
    evidence_ids: list[str],
    actor: str,
) -> str:
    relationship = relationship.strip()
    if not relationship:
        raise KnowledgeWorkbenchError("relationship 不能为空")
    if not evidence_ids:
        raise KnowledgeWorkbenchError("Wiki 链接至少需要一条来源证据")
    now = utc_now()
    link_id = new_id("link")
    original_content: str | None = None
    markdown_path: Path | None = None
    try:
        with database.transaction() as connection:
            source = connection.execute(
                """
                SELECT wr.*, wp.id AS source_page_id
                FROM wiki_revisions wr
                JOIN wiki_pages wp ON wp.id = wr.page_id
                WHERE wr.id = ?
                """,
                (source_revision_id,),
            ).fetchone()
            if not source:
                raise KnowledgeWorkbenchError(f"Wiki 修订不存在：{source_revision_id}")
            if source["status"] != "draft":
                raise InvalidTransitionError("只能修改 draft 修订的知识链接")
            target = connection.execute(
                "SELECT id, slug, title FROM wiki_pages WHERE id = ?",
                (target_page_id,),
            ).fetchone()
            if not target:
                raise KnowledgeWorkbenchError(f"目标 Wiki 页面不存在：{target_page_id}")
            if source["source_page_id"] == target_page_id:
                raise KnowledgeWorkbenchError("不允许创建页面自链接")
            placeholders = ",".join("?" for _ in evidence_ids)
            evidence_rows = connection.execute(
                f"""
                SELECT evidence_id FROM revision_evidence
                WHERE revision_id = ? AND evidence_id IN ({placeholders})
                """,
                (source_revision_id, *evidence_ids),
            ).fetchall()
            valid_ids = {row["evidence_id"] for row in evidence_rows}
            unknown = set(evidence_ids) - valid_ids
            if unknown:
                raise KnowledgeWorkbenchError(
                    "Wiki 链接引用了不属于来源修订的证据：" + ", ".join(sorted(unknown))
                )
            existing = connection.execute(
                """
                SELECT id FROM wiki_links
                WHERE source_revision_id = ? AND target_page_id = ? AND relationship = ?
                """,
                (source_revision_id, target_page_id, relationship),
            ).fetchone()
            if existing:
                raise KnowledgeWorkbenchError(f"相同知识链接已存在：{existing['id']}")
            connection.execute(
                """
                INSERT INTO wiki_links(
                    id, source_revision_id, target_page_id, relationship,
                    evidence_ids_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    link_id,
                    source_revision_id,
                    target_page_id,
                    relationship,
                    json.dumps(sorted(set(evidence_ids)), ensure_ascii=False),
                    now,
                    now,
                ),
            )
            markdown_path = paths.root / source["markdown_path"]
            if not markdown_path.is_file():
                raise KnowledgeWorkbenchError(f"Wiki 修订文件不存在：{markdown_path}")
            original_content = markdown_path.read_text(encoding="utf-8")
            updated_content = _render_links_block(connection, source_revision_id, original_content)
            write_text_atomic(markdown_path, updated_content)
            connection.execute(
                "UPDATE wiki_revisions SET content_sha256 = ?, updated_at = ? WHERE id = ?",
                (sha256_text(updated_content), now, source_revision_id),
            )
            record_event(
                connection,
                "wiki_link_added",
                "wiki_link",
                link_id,
                actor=actor,
                details={
                    "source_revision_id": source_revision_id,
                    "target_page_id": target_page_id,
                    "relationship": relationship,
                    "evidence_ids": sorted(set(evidence_ids)),
                },
            )
    except Exception:
        if markdown_path and original_content is not None:
            write_text_atomic(markdown_path, original_content)
        raise
    return link_id


def remove_wiki_link(
    database: Database,
    paths: WorkspacePaths,
    link_id: str,
    *,
    actor: str,
) -> None:
    now = utc_now()
    original_content: str | None = None
    markdown_path: Path | None = None
    try:
        with database.transaction() as connection:
            link = connection.execute(
                """
                SELECT wl.*, wr.status, wr.markdown_path
                FROM wiki_links wl
                JOIN wiki_revisions wr ON wr.id = wl.source_revision_id
                WHERE wl.id = ?
                """,
                (link_id,),
            ).fetchone()
            if not link:
                raise KnowledgeWorkbenchError(f"Wiki 链接不存在：{link_id}")
            if link["status"] != "draft":
                raise InvalidTransitionError("只能修改 draft 修订的知识链接")
            markdown_path = paths.root / link["markdown_path"]
            original_content = markdown_path.read_text(encoding="utf-8")
            connection.execute("DELETE FROM wiki_links WHERE id = ?", (link_id,))
            updated_content = _render_links_block(
                connection, link["source_revision_id"], original_content
            )
            write_text_atomic(markdown_path, updated_content)
            connection.execute(
                "UPDATE wiki_revisions SET content_sha256 = ?, updated_at = ? WHERE id = ?",
                (sha256_text(updated_content), now, link["source_revision_id"]),
            )
            record_event(
                connection,
                "wiki_link_removed",
                "wiki_link",
                link_id,
                actor=actor,
                details={"source_revision_id": link["source_revision_id"]},
            )
    except Exception:
        if markdown_path and original_content is not None:
            write_text_atomic(markdown_path, original_content)
        raise


def _render_links_block(connection, revision_id: str, content: str) -> str:
    rows = connection.execute(
        """
        SELECT wl.relationship, wl.evidence_ids_json, wp.slug, wp.title
        FROM wiki_links wl
        JOIN wiki_pages wp ON wp.id = wl.target_page_id
        WHERE wl.source_revision_id = ?
        ORDER BY wp.title, wl.relationship
        """,
        (revision_id,),
    ).fetchall()
    content = LINK_BLOCK_PATTERN.sub("\n", content).rstrip() + "\n"
    if not rows:
        return content
    lines = ["", "<!-- knowledge-links:start -->", "## 相关知识", ""]
    for row in rows:
        evidence = ", ".join(
            f"`{evidence_id}`" for evidence_id in json.loads(row["evidence_ids_json"])
        )
        lines.append(
            f"- [[{row['slug']}|{row['title']}]] — {row['relationship']}（证据：{evidence}）"
        )
    lines.extend(["<!-- knowledge-links:end -->", ""])
    return content + "\n".join(lines)
