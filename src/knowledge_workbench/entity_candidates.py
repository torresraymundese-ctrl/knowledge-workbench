from __future__ import annotations

import json
from pathlib import Path

from .audit import record_event
from .database import Database
from .entities import normalize_entity_name
from .errors import KnowledgeWorkbenchError
from .schema_validation import validate_analysis
from .utils import new_id, sha256_file, sha256_text, utc_now


MAX_ANALYSIS_BYTES = 50 * 1024 * 1024
CANDIDATE_STATUSES = ("pending", "accepted", "rejected")


def import_entity_candidates(
    database: Database,
    analysis_path: Path,
    *,
    actor: str,
) -> dict:
    actor = _required_text(actor, "actor")
    path = analysis_path.expanduser().resolve()
    if not path.is_file():
        raise KnowledgeWorkbenchError(f"分析 JSON 不存在：{path}")
    if path.stat().st_size > MAX_ANALYSIS_BYTES:
        raise KnowledgeWorkbenchError("分析 JSON 超过 50 MiB 导入上限")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise KnowledgeWorkbenchError("分析文件不是有效的 UTF-8 JSON") from exc
    if not isinstance(payload, dict):
        raise KnowledgeWorkbenchError("分析 JSON 顶层必须是对象")
    validate_analysis(payload)
    provenance = payload["provenance"]
    if provenance["mode"] != "model_assisted":
        raise KnowledgeWorkbenchError("只允许导入 model_assisted 阶段一实体候选")

    source = payload["source"]
    analysis_sha256 = sha256_file(path)
    raw_candidates = _candidate_rows(payload)
    imported = 0
    existing = 0
    with database.transaction() as connection:
        current = connection.execute(
            """
            SELECT dv.id AS document_version_id, dv.sha256,
                   d.classification, pr.id AS processing_run_id
            FROM document_versions dv
            JOIN documents d
              ON d.id = dv.document_id AND d.current_version_id = dv.id
            JOIN processing_runs pr
              ON pr.document_version_id = dv.id AND pr.is_current = 1
            WHERE dv.id = ?
            """,
            (source["document_version_id"],),
        ).fetchone()
        if not current:
            raise KnowledgeWorkbenchError("分析来源不是当前文件版本和当前处理运行")
        if current["sha256"] != source["sha256"]:
            raise KnowledgeWorkbenchError("分析来源 SHA-256 与数据库文件版本不一致")
        if current["classification"] != source["classification"]:
            raise KnowledgeWorkbenchError("分析来源密级与数据库不一致")

        evidence_rows = connection.execute(
            """
            SELECT id, excerpt FROM evidence
            WHERE processing_run_id = ?
            ORDER BY run_ordinal
            """,
            (current["processing_run_id"],),
        ).fetchall()
        evidence_by_excerpt: dict[str, str] = {}
        ambiguous: set[str] = set()
        for row in evidence_rows:
            if row["excerpt"] in evidence_by_excerpt:
                ambiguous.add(row["excerpt"])
            evidence_by_excerpt[row["excerpt"]] = row["id"]

        mapped: list[dict] = []
        for candidate in raw_candidates:
            excerpt = candidate["excerpt"]
            if excerpt in ambiguous:
                raise KnowledgeWorkbenchError("分析证据正文在当前处理运行中映射不唯一")
            evidence_id = evidence_by_excerpt.get(excerpt)
            if not evidence_id:
                raise KnowledgeWorkbenchError(
                    f"分析证据 {candidate['source_candidate_id']} 无法逐字映射到当前证据"
                )
            mapped.append({**candidate, "evidence_id": evidence_id})

        now = utc_now()
        for candidate in mapped:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO entity_candidates(
                    id, analysis_sha256, document_version_id,
                    processing_run_id, evidence_id, source_candidate_id,
                    suggested_name, normalized_name, suggested_type,
                    verbatim_match, provider, model, prompt_version,
                    status, imported_by, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                          'pending', ?, ?)
                """,
                (
                    new_id("entitycand"),
                    analysis_sha256,
                    current["document_version_id"],
                    current["processing_run_id"],
                    candidate["evidence_id"],
                    candidate["source_candidate_id"],
                    candidate["suggested_name"],
                    candidate["normalized_name"],
                    candidate["suggested_type"],
                    candidate["verbatim_match"],
                    provenance["provider"],
                    provenance["model"],
                    provenance["prompt_version"],
                    actor,
                    now,
                ),
            )
            if cursor.rowcount:
                imported += 1
            else:
                existing += 1
        record_event(
            connection,
            "entity_candidates_imported",
            "analysis",
            analysis_sha256,
            actor=actor,
            details={
                "document_version_id": current["document_version_id"],
                "processing_run_id": current["processing_run_id"],
                "classification": current["classification"],
                "candidate_count": len(mapped),
                "imported_count": imported,
                "existing_count": existing,
                "prompt_version": provenance["prompt_version"],
            },
        )
    return {
        "analysis_sha256": analysis_sha256,
        "candidate_count": len(raw_candidates),
        "imported_count": imported,
        "existing_count": existing,
    }


def list_entity_candidates(
    database: Database,
    *,
    status: str = "pending",
    limit: int = 50,
) -> list[dict]:
    if status not in CANDIDATE_STATUSES:
        raise KnowledgeWorkbenchError("实体候选状态无效")
    if limit <= 0 or limit > 500:
        raise KnowledgeWorkbenchError("实体候选 limit 必须在 1 到 500 之间")
    with database.connect() as connection:
        rows = connection.execute(
            """
            SELECT ec.id, ec.status, ec.suggested_name, ec.suggested_type,
                   ec.verbatim_match, ec.evidence_id, ec.source_candidate_id,
                   ec.resolved_entity_id, ec.created_at, ec.reviewed_at,
                   CASE WHEN d.current_version_id = ec.document_version_id
                                  AND pr.is_current = 1
                                  AND pr.document_version_id = ec.document_version_id
                                  AND e.processing_run_id = ec.processing_run_id
                                  AND e.document_version_id = ec.document_version_id
                        THEN 1 ELSE 0 END AS source_is_current
            FROM entity_candidates ec
            JOIN evidence e ON e.id = ec.evidence_id
            JOIN processing_runs pr ON pr.id = ec.processing_run_id
            JOIN document_versions dv ON dv.id = ec.document_version_id
            JOIN documents d ON d.id = dv.document_id
            WHERE ec.status = ?
            ORDER BY ec.created_at, ec.id
            LIMIT ?
            """,
            (status, limit),
        ).fetchall()
    return [dict(row) for row in rows]


def accept_entity_candidate(
    database: Database,
    candidate_id: str,
    entity_id: str,
    *,
    actor: str,
    note: str | None = None,
) -> dict:
    actor = _required_text(actor, "actor")
    note_hash = sha256_text(note.strip()) if note and note.strip() else None
    with database.transaction() as connection:
        candidate = _pending_candidate(connection, candidate_id)
        if not candidate["source_is_current"]:
            raise KnowledgeWorkbenchError("候选来源已不是当前文件版本和当前处理运行")
        if not candidate["verbatim_match"] or candidate["suggested_name"] not in candidate["excerpt"]:
            raise KnowledgeWorkbenchError("非逐字实体候选不能接受，请驳回或人工重新登记")
        entity = connection.execute(
            """
            SELECT id, entity_type FROM canonical_entities
            WHERE id = ? AND status = 'active'
            """,
            (entity_id,),
        ).fetchone()
        if not entity:
            raise KnowledgeWorkbenchError("规范实体不存在或不是 active 状态")

        alias = connection.execute(
            """
            SELECT id, entity_id FROM entity_aliases
            WHERE entity_type = ? AND normalized_alias = ?
            """,
            (entity["entity_type"], candidate["normalized_name"]),
        ).fetchone()
        alias_created = False
        if alias and alias["entity_id"] != entity_id:
            raise KnowledgeWorkbenchError("候选名称已指向同类型的另一个规范实体")
        if not alias:
            alias_id = new_id("alias")
            connection.execute(
                """
                INSERT INTO entity_aliases(
                    id, entity_id, entity_type, alias, normalized_alias,
                    is_canonical, created_by, created_at
                ) VALUES (?, ?, ?, ?, ?, 0, ?, ?)
                """,
                (
                    alias_id,
                    entity_id,
                    entity["entity_type"],
                    candidate["suggested_name"],
                    candidate["normalized_name"],
                    actor,
                    utc_now(),
                ),
            )
            record_event(
                connection,
                "entity_alias_added",
                "canonical_entity",
                entity_id,
                actor=actor,
                details={
                    "alias_id": alias_id,
                    "alias_sha256": sha256_text(candidate["suggested_name"]),
                    "source": "accepted_entity_candidate",
                    "candidate_id": candidate_id,
                },
            )
            alias_created = True
        else:
            alias_id = alias["id"]

        link_cursor = connection.execute(
            """
            INSERT OR IGNORE INTO evidence_entity_mentions(
                evidence_id, entity_id, alias_id, mention_text,
                created_by, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                candidate["evidence_id"],
                entity_id,
                alias_id,
                candidate["suggested_name"],
                actor,
                utc_now(),
            ),
        )
        if link_cursor.rowcount:
            record_event(
                connection,
                "evidence_entity_linked",
                "canonical_entity",
                entity_id,
                actor=actor,
                details={
                    "evidence_id": candidate["evidence_id"],
                    "alias_id": alias_id,
                    "mention_sha256": sha256_text(candidate["suggested_name"]),
                    "classification": candidate["classification"],
                    "source": "accepted_entity_candidate",
                    "candidate_id": candidate_id,
                },
            )
        now = utc_now()
        connection.execute(
            """
            UPDATE entity_candidates
            SET status = 'accepted', resolved_entity_id = ?, reviewed_by = ?,
                review_note_sha256 = ?, reviewed_at = ?
            WHERE id = ?
            """,
            (entity_id, actor, note_hash, now, candidate_id),
        )
        record_event(
            connection,
            "entity_candidate_accepted",
            "entity_candidate",
            candidate_id,
            actor=actor,
            details={
                "resolved_entity_id": entity_id,
                "evidence_id": candidate["evidence_id"],
                "suggested_name_sha256": sha256_text(candidate["suggested_name"]),
                "alias_created": alias_created,
                "evidence_link_created": bool(link_cursor.rowcount),
                "review_note_sha256": note_hash,
            },
        )
    return {
        "candidate_id": candidate_id,
        "entity_id": entity_id,
        "alias_created": alias_created,
        "evidence_link_created": bool(link_cursor.rowcount),
    }


def reject_entity_candidate(
    database: Database,
    candidate_id: str,
    *,
    actor: str,
    note: str,
) -> None:
    actor = _required_text(actor, "actor")
    note = _required_text(note, "驳回原因")
    note_hash = sha256_text(note)
    with database.transaction() as connection:
        candidate = _pending_candidate(connection, candidate_id)
        connection.execute(
            """
            UPDATE entity_candidates
            SET status = 'rejected', reviewed_by = ?,
                review_note_sha256 = ?, reviewed_at = ?
            WHERE id = ?
            """,
            (actor, note_hash, utc_now(), candidate_id),
        )
        record_event(
            connection,
            "entity_candidate_rejected",
            "entity_candidate",
            candidate_id,
            actor=actor,
            details={
                "evidence_id": candidate["evidence_id"],
                "suggested_name_sha256": sha256_text(candidate["suggested_name"]),
                "review_note_sha256": note_hash,
            },
        )


def _candidate_rows(payload: dict) -> list[dict]:
    rows: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    for evidence in payload["evidence"]:
        for entity in evidence["entities"]:
            name = entity["name"].strip()
            suggested_type = entity["type"].strip()
            normalized_name = normalize_entity_name(name)
            key = (evidence["candidate_id"], normalized_name, suggested_type.casefold())
            if key in seen:
                continue
            seen.add(key)
            rows.append(
                {
                    "source_candidate_id": evidence["candidate_id"],
                    "excerpt": evidence["excerpt"],
                    "suggested_name": name,
                    "normalized_name": normalized_name,
                    "suggested_type": suggested_type,
                    "verbatim_match": int(name in evidence["excerpt"]),
                }
            )
    return rows


def _pending_candidate(connection, candidate_id: str):
    row = connection.execute(
        """
        SELECT ec.*, e.excerpt, d.classification,
               CASE WHEN e.processing_run_id = ec.processing_run_id
                              AND e.document_version_id = ec.document_version_id
                              AND pr.document_version_id = ec.document_version_id
                    THEN 1 ELSE 0 END AS source_identity_valid,
               CASE WHEN d.current_version_id = ec.document_version_id
                              AND pr.is_current = 1
                    THEN 1 ELSE 0 END AS source_is_current
        FROM entity_candidates ec
        JOIN evidence e ON e.id = ec.evidence_id
        JOIN processing_runs pr ON pr.id = ec.processing_run_id
        JOIN document_versions dv ON dv.id = ec.document_version_id
        JOIN documents d ON d.id = dv.document_id
        WHERE ec.id = ? AND ec.status = 'pending'
        """,
        (candidate_id,),
    ).fetchone()
    if not row:
        raise KnowledgeWorkbenchError("实体候选不存在或已完成裁决")
    if not row["source_identity_valid"]:
        raise KnowledgeWorkbenchError("实体候选来源身份与证据不一致")
    return row


def _required_text(value: str, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise KnowledgeWorkbenchError(f"{label}不能为空")
    return value.strip()
