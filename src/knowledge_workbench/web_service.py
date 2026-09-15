from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Callable

from .audit import record_event
from .config import WorkspacePaths
from .conflict_candidates import (
    cross_document_candidate_page,
    list_cross_document_candidate_packs,
    submit_cross_document_candidate_annotations_by_id,
    update_cross_document_candidate_label,
    update_cross_document_candidate_review,
)
from .conflict_labeling_plan import (
    list_conflict_labeling_plans,
    resolve_conflict_labeling_batch,
)
from .conflicts import transition_conflict
from .corpus_map import (
    corpus_file_card,
    decide_corpus_file,
    import_corpus_file,
    latest_corpus_map,
    scan_corpus_source,
)
from .database import Database
from .entity_candidates import accept_entity_candidate, reject_entity_candidate
from .entity_merges import (
    list_entity_merge_requests,
    propose_entity_merge,
    review_entity_merge,
)
from .entity_relationships import (
    create_entity_relationship,
    list_entity_relationships,
    list_relation_types,
    retract_entity_relationship,
)
from .entity_visibility import get_entity_visibility, list_entity_visibility
from .errors import KnowledgeWorkbenchError
from .graph_pilot import graph_pilot_pack_page, list_graph_pilot_packs
from .models import Classification, ConflictStatus, EvidenceStatus
from .question_answering import answer_question as answer_knowledge_question
from .providers import AuditedModelGateway, DeepSeekChatModel
from .review import (
    publish_revision,
    reject_revision,
    request_revision_review,
    transition_evidence,
)
from .topic_wiki import knowledge_setup
from .utils import sha256_text


MAX_WIKI_PREVIEW_CHARACTERS = 50_000
_TOPIC_CLASSIFICATION_SQL = """
CASE MAX(
    CASE d.classification
        WHEN 'restricted' THEN 4
        WHEN 'confidential' THEN 3
        WHEN 'internal' THEN 2
        ELSE 1
    END
)
    WHEN 4 THEN 'restricted'
    WHEN 3 THEN 'confidential'
    WHEN 2 THEN 'internal'
    ELSE 'public'
END
"""


def _open_with_default_application(path: Path) -> None:
    if os.name != "nt":
        raise KnowledgeWorkbenchError(
            "当前系统不支持用本机应用打开资料"
        )
    try:
        os.startfile(path)  # type: ignore[attr-defined]
    except OSError as exc:
        raise KnowledgeWorkbenchError(
            "本机应用无法打开这份资料"
        ) from exc


class WorkbenchReadService:
    """Read-only projections for the local Web workbench.

    Every database query applies the same current-version/current-run boundary as
    the CLI. The Web layer receives projections rather than a writable connection.
    """

    def __init__(self, database: Database, paths: WorkspacePaths):
        self.database = database
        self.paths = paths

    def bootstrap(self) -> dict[str, Any]:
        return {
            "corpus_map": self.corpus_map(limit=60),
            "summary": self.summary(),
            "documents": self.documents(limit=12),
            "evaluations": self.evaluations(limit=6),
            "activity": self.activity(limit=8),
        }

    def corpus_map(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        folder: str | None = None,
        scope_status: str | None = None,
        query: str | None = None,
        view: str = "all",
    ) -> dict[str, Any]:
        return latest_corpus_map(
            self.database,
            limit=limit,
            offset=offset,
            folder=folder,
            scope_status=scope_status,
            query=query,
            view=view,
        )

    def corpus_file_card(self, file_id: str) -> dict[str, Any]:
        return corpus_file_card(self.database, file_id)

    def knowledge_setup(self) -> dict[str, Any]:
        return knowledge_setup(self.database)

    def conflict_candidate_packs(self) -> dict[str, Any]:
        return list_cross_document_candidate_packs(self.database, self.paths)

    def conflict_labeling_plans(
        self, *, source_pack_id: str | None = None
    ) -> dict[str, Any]:
        return list_conflict_labeling_plans(
            self.database,
            self.paths,
            source_pack_id=source_pack_id,
        )

    def graph_pilot_packs(self) -> dict[str, Any]:
        return list_graph_pilot_packs(self.database, self.paths)

    def graph_pilot_pack_page(
        self,
        pack_id: str,
        *,
        limit: int = 10,
        offset: int = 0,
        status: str | None = None,
        query: str | None = None,
    ) -> dict[str, Any]:
        return graph_pilot_pack_page(
            self.database,
            self.paths,
            pack_id,
            limit=limit,
            offset=offset,
            status=status,
            query=query,
        )

    def conflict_candidate_page(
        self,
        pack_id: str,
        *,
        limit: int = 10,
        offset: int = 0,
        state: str | None = None,
        query: str | None = None,
        plan_id: str | None = None,
        batch_id: str | None = None,
    ) -> dict[str, Any]:
        normalized_plan_id = (plan_id or "").strip() or None
        normalized_batch_id = (batch_id or "").strip() or None
        if (normalized_plan_id is None) != (normalized_batch_id is None):
            raise ValueError("plan_id 与 batch_id 必须同时提供")
        batch = None
        candidate_ids = None
        if normalized_plan_id is not None:
            batch = resolve_conflict_labeling_batch(
                self.database,
                self.paths,
                normalized_plan_id,
                normalized_batch_id or "",
            )
            if batch["source_pack_id"] != pack_id:
                raise KnowledgeWorkbenchError(
                    "冲突标注批次不属于请求的候选包"
                )
            candidate_ids = batch["candidate_ids"]
        page = cross_document_candidate_page(
            self.database,
            self.paths,
            pack_id,
            limit=limit,
            offset=offset,
            state=state,
            query=query,
            candidate_ids=candidate_ids,
        )
        page["labeling_batch"] = (
            {
                "plan_id": batch["plan_id"],
                "batch_id": batch["batch_id"],
                "status": batch["status"],
            }
            if batch is not None
            else None
        )
        return page

    def entity_candidate_page(
        self,
        *,
        limit: int = 10,
        offset: int = 0,
        status: str | None = "pending",
        query: str | None = None,
    ) -> dict[str, Any]:
        limit, offset = _pagination(limit, offset)
        status = _entity_candidate_status(status)
        query = _review_query(query)
        where = ["d.classification != 'restricted'"]
        parameters: list[Any] = []
        if status:
            where.append("ec.status = ?")
            parameters.append(status)
        if query:
            pattern = f"%{query}%"
            where.append(
                "(ec.id LIKE ? OR ec.evidence_id LIKE ? "
                "OR ec.source_candidate_id LIKE ? OR ec.suggested_name LIKE ? "
                "OR ec.suggested_type LIKE ? OR ce.canonical_name LIKE ?)"
            )
            parameters.extend([pattern] * 6)
        predicate = " AND ".join(where)
        with self.database.connect() as connection:
            total = connection.execute(
                f"""
                SELECT COUNT(*)
                FROM entity_candidates ec
                JOIN evidence e ON e.id = ec.evidence_id
                JOIN processing_runs pr ON pr.id = ec.processing_run_id
                JOIN document_versions dv ON dv.id = ec.document_version_id
                JOIN documents d ON d.id = dv.document_id
                LEFT JOIN canonical_entities ce ON ce.id = ec.resolved_entity_id
                WHERE {predicate}
                """,
                parameters,
            ).fetchone()[0]
            rows = connection.execute(
                f"""
                SELECT ec.id, ec.status, ec.suggested_name, ec.suggested_type,
                       ec.verbatim_match, ec.evidence_id,
                       ec.source_candidate_id, ec.provider, ec.model,
                       ec.prompt_version, ec.resolved_entity_id,
                       ec.reviewed_by, ec.created_at, ec.reviewed_at,
                       d.classification, ce.canonical_name,
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
                LEFT JOIN canonical_entities ce ON ce.id = ec.resolved_entity_id
                WHERE {predicate}
                ORDER BY ec.created_at, ec.id
                LIMIT ? OFFSET ?
                """,
                [*parameters, limit, offset],
            ).fetchall()
        visible_entities = list_entity_visibility(
            self.database, web_visible_only=True, limit=200
        )
        return {
            "status": status,
            "query": query,
            "total": total,
            "limit": limit,
            "offset": offset,
            "has_previous": offset > 0,
            "has_next": offset + len(rows) < total,
            "items": [
                {
                    "candidate_id": row["id"],
                    "status": row["status"],
                    "suggested_name": row["suggested_name"],
                    "suggested_type": row["suggested_type"],
                    "verbatim_match": bool(row["verbatim_match"]),
                    "evidence_id": row["evidence_id"],
                    "source_candidate_id": row["source_candidate_id"],
                    "provider": row["provider"],
                    "model": row["model"],
                    "prompt_version": row["prompt_version"],
                    "classification": row["classification"],
                    "source_is_current": bool(row["source_is_current"]),
                    "resolved_entity_id": row["resolved_entity_id"],
                    "canonical_name": row["canonical_name"],
                    "reviewed_by": row["reviewed_by"],
                    "created_at": row["created_at"],
                    "reviewed_at": row["reviewed_at"],
                }
                for row in rows
            ],
            "entity_options": [
                {
                    "entity_id": row["entity_id"],
                    "canonical_name": row["canonical_name"],
                    "entity_type": row["entity_type"],
                    "evidence_count": row["evidence_count"],
                    "visibility": row["visibility"],
                }
                for row in visible_entities
            ],
        }

    def entity_merge_page(
        self,
        *,
        limit: int = 10,
        offset: int = 0,
        query: str | None = None,
    ) -> dict[str, Any]:
        limit, offset = _pagination(limit, offset)
        query = _review_query(query)
        visible_entities = list_entity_visibility(
            self.database, web_visible_only=True, limit=500
        )
        visible_by_id = {
            item["entity_id"]: item for item in visible_entities
        }
        requests = list_entity_merge_requests(
            self.database, status="reviewing", limit=500
        )
        safe_requests = [
            request
            for request in requests
            if request["source_entity_id"] in visible_by_id
            and request["target_entity_id"] in visible_by_id
        ]
        if query:
            needle = query.casefold()
            safe_requests = [
                request
                for request in safe_requests
                if any(
                    needle in str(value).casefold()
                    for value in (
                        request["id"],
                        request["source_name"],
                        request["target_name"],
                        request["entity_type"],
                        request["proposed_by"],
                    )
                )
            ]
        total = len(safe_requests)
        page_rows = safe_requests[offset : offset + limit]
        return {
            "query": query,
            "total": total,
            "limit": limit,
            "offset": offset,
            "has_previous": offset > 0,
            "has_next": offset + len(page_rows) < total,
            "source_limit_reached": len(requests) == 500,
            "items": [
                {
                    **request,
                    "source_visibility": visible_by_id[
                        request["source_entity_id"]
                    ]["visibility"],
                    "source_evidence_count": visible_by_id[
                        request["source_entity_id"]
                    ]["evidence_count"],
                    "target_visibility": visible_by_id[
                        request["target_entity_id"]
                    ]["visibility"],
                    "target_evidence_count": visible_by_id[
                        request["target_entity_id"]
                    ]["evidence_count"],
                    "approval_confirmation": _entity_merge_confirmation_phrase(
                        request["id"]
                    ),
                }
                for request in page_rows
            ],
            "entity_options": [
                {
                    "entity_id": item["entity_id"],
                    "canonical_name": item["canonical_name"],
                    "entity_type": item["entity_type"],
                    "evidence_count": item["evidence_count"],
                    "visibility": item["visibility"],
                }
                for item in visible_entities
            ],
        }

    def entity_relationship_page(
        self,
        *,
        limit: int = 10,
        offset: int = 0,
        status: str = "active",
        query: str | None = None,
    ) -> dict[str, Any]:
        limit, offset = _pagination(limit, offset)
        status = _entity_relationship_status(status)
        query = _review_query(query)
        visible_entities = list_entity_visibility(
            self.database, web_visible_only=True, limit=500
        )
        visible_by_id = {
            item["entity_id"]: item for item in visible_entities
        }
        relationships = list_entity_relationships(
            self.database, status=status, limit=500
        )
        safe_relationships = [
            relationship
            for relationship in relationships
            if relationship["source_entity_id"] in visible_by_id
            and relationship["target_entity_id"] in visible_by_id
        ]
        if query:
            needle = query.casefold()
            safe_relationships = [
                relationship
                for relationship in safe_relationships
                if any(
                    needle in str(value).casefold()
                    for value in (
                        relationship["relationship_id"],
                        relationship["relation_key"],
                        relationship["label"],
                        relationship["source_name"],
                        relationship["target_name"],
                        relationship["created_by"],
                    )
                )
            ]
        total = len(safe_relationships)
        page_rows = safe_relationships[offset : offset + limit]
        return {
            "status": status,
            "query": query,
            "total": total,
            "limit": limit,
            "offset": offset,
            "has_previous": offset > 0,
            "has_next": offset + len(page_rows) < total,
            "source_limit_reached": len(relationships) == 500,
            "items": [
                {
                    **relationship,
                    "retraction_confirmation": (
                        _entity_relationship_retraction_phrase(
                            relationship["relationship_id"]
                        )
                        if relationship["status"] == "active"
                        else None
                    ),
                }
                for relationship in page_rows
            ],
            "relation_types": [
                {
                    "relation_key": item["relation_key"],
                    "label": item["label"],
                    "inverse_label": item["inverse_label"],
                    "directed": item["directed"],
                }
                for item in list_relation_types(
                    self.database, status="active", limit=200
                )
            ],
            "entity_options": [
                {
                    "entity_id": item["entity_id"],
                    "canonical_name": item["canonical_name"],
                    "entity_type": item["entity_type"],
                    "evidence_count": item["evidence_count"],
                    "visibility": item["visibility"],
                }
                for item in visible_entities
            ],
        }

    def entity_relationship_evidence_page(
        self,
        source_entity_id: str,
        target_entity_id: str,
        *,
        limit: int = 50,
        offset: int = 0,
        query: str | None = None,
    ) -> dict[str, Any]:
        limit, offset = _pagination(limit, offset)
        query = _review_query(query)
        source = _web_visible_entity(self.database, source_entity_id)
        target = _web_visible_entity(self.database, target_entity_id)
        if source_entity_id == target_entity_id:
            raise ValueError("业务关系源实体和目标实体不能相同")
        where = [
            "e.status = 'verified'",
            "pr.is_current = 1",
            "d.current_version_id = dv.id",
            "d.classification != 'restricted'",
            """
            EXISTS (
                SELECT 1 FROM evidence_entity_mentions source_mention
                WHERE source_mention.evidence_id = e.id
                  AND source_mention.entity_id = ?
            )
            """,
            """
            EXISTS (
                SELECT 1 FROM evidence_entity_mentions target_mention
                WHERE target_mention.evidence_id = e.id
                  AND target_mention.entity_id = ?
            )
            """,
        ]
        parameters: list[object] = [source_entity_id, target_entity_id]
        if query:
            pattern = f"%{_escape_like(query)}%"
            where.append(
                """
                (
                    e.id LIKE ? ESCAPE '\\'
                    OR d.original_name LIKE ? ESCAPE '\\'
                    OR d.classification LIKE ? ESCAPE '\\'
                )
                """
            )
            parameters.extend([pattern, pattern, pattern])
        predicate = " AND ".join(where)
        with self.database.connect() as connection:
            total = connection.execute(
                f"""
                SELECT COUNT(DISTINCT e.id)
                FROM evidence e
                JOIN processing_runs pr ON pr.id = e.processing_run_id
                JOIN document_versions dv ON dv.id = e.document_version_id
                JOIN documents d ON d.id = dv.document_id
                WHERE {predicate}
                """,
                parameters,
            ).fetchone()[0]
            rows = connection.execute(
                f"""
                SELECT DISTINCT e.id, d.original_name, d.classification,
                       e.updated_at
                FROM evidence e
                JOIN processing_runs pr ON pr.id = e.processing_run_id
                JOIN document_versions dv ON dv.id = e.document_version_id
                JOIN documents d ON d.id = dv.document_id
                WHERE {predicate}
                ORDER BY e.updated_at DESC, e.id
                LIMIT ? OFFSET ?
                """,
                [*parameters, limit, offset],
            ).fetchall()
        return {
            "source_entity": {
                "entity_id": source["entity_id"],
                "canonical_name": source["canonical_name"],
                "entity_type": source["entity_type"],
                "visibility": source["visibility"],
            },
            "target_entity": {
                "entity_id": target["entity_id"],
                "canonical_name": target["canonical_name"],
                "entity_type": target["entity_type"],
                "visibility": target["visibility"],
            },
            "query": query,
            "total": total,
            "limit": limit,
            "offset": offset,
            "has_previous": offset > 0,
            "has_next": offset + len(rows) < total,
            "items": [
                {
                    "evidence_id": row["id"],
                    "document_name": row["original_name"],
                    "classification": row["classification"],
                    "updated_at": row["updated_at"],
                }
                for row in rows
            ],
        }

    def summary(self) -> dict[str, Any]:
        with self.database.connect() as connection:
            document_count = connection.execute(
                """
                SELECT COUNT(*)
                FROM documents d
                JOIN document_governance dg ON dg.document_id = d.id
                WHERE d.current_version_id IS NOT NULL
                  AND dg.purpose != 'development_fixture'
                """
            ).fetchone()[0]
            evidence_rows = connection.execute(
                """
                SELECT e.status, COUNT(*) AS count
                FROM evidence e
                JOIN processing_runs pr
                  ON pr.id = e.processing_run_id AND pr.is_current = 1
                JOIN documents d ON d.current_version_id = pr.document_version_id
                JOIN document_governance dg ON dg.document_id = d.id
                WHERE dg.purpose != 'development_fixture'
                GROUP BY e.status
                """
            ).fetchall()
            page_rows = connection.execute(
                """
                SELECT wp.status, COUNT(*) AS count
                FROM wiki_pages wp
                LEFT JOIN document_governance dg
                  ON dg.document_id = wp.source_document_id
                WHERE wp.source_document_id IS NULL
                   OR dg.purpose != 'development_fixture'
                GROUP BY wp.status
                """
            ).fetchall()
            active_conflicts = connection.execute(
                "SELECT COUNT(*) FROM conflicts WHERE status IN ('pending', 'reviewing')"
            ).fetchone()[0]
            active_tasks = connection.execute(
                "SELECT COUNT(*) FROM tasks WHERE status IN ('pending', 'running', 'retrying')"
            ).fetchone()[0]
            needs_revalidation = connection.execute(
                """
                SELECT COUNT(*)
                FROM wiki_pages wp
                LEFT JOIN document_governance dg
                  ON dg.document_id = wp.source_document_id
                WHERE wp.needs_revalidation = 1
                  AND (
                    wp.source_document_id IS NULL
                    OR dg.purpose != 'development_fixture'
                  )
                """
            ).fetchone()[0]
            approved_labeling_sessions = connection.execute(
                "SELECT COUNT(*) FROM labeling_sessions WHERE status = 'approved'"
            ).fetchone()[0]
            governance_rows = connection.execute(
                """
                SELECT purpose, scope_status, COUNT(*) AS count
                FROM document_governance
                GROUP BY purpose, scope_status
                """
            ).fetchall()
            technically_passed = connection.execute(
                """
                SELECT COUNT(*)
                FROM evidence_technical_validation etv
                JOIN evidence e ON e.id = etv.evidence_id
                JOIN processing_runs pr
                  ON pr.id = e.processing_run_id AND pr.is_current = 1
                JOIN documents d ON d.current_version_id = pr.document_version_id
                JOIN document_governance dg ON dg.document_id = d.id
                WHERE etv.status = 'passed'
                  AND dg.purpose != 'development_fixture'
                """
            ).fetchone()[0]
        evidence_by_status = {row["status"]: row["count"] for row in evidence_rows}
        pages_by_status = {row["status"]: row["count"] for row in page_rows}
        governance = {
            f"{row['purpose']}:{row['scope_status']}": row["count"]
            for row in governance_rows
        }
        return {
            "document_count": document_count,
            "current_evidence_count": sum(evidence_by_status.values()),
            "evidence_by_status": evidence_by_status,
            "wiki_page_count": sum(pages_by_status.values()),
            "wiki_pages_by_status": pages_by_status,
            "active_conflict_count": active_conflicts,
            "active_task_count": active_tasks,
            "needs_revalidation_count": needs_revalidation,
            "approved_labeling_session_count": approved_labeling_sessions,
            "technically_validated_evidence_count": technically_passed,
            "development_fixture_count": sum(
                row["count"]
                for row in governance_rows
                if row["purpose"] == "development_fixture"
            ),
            "candidate_document_count": sum(
                row["count"]
                for row in governance_rows
                if row["purpose"] == "candidate"
            ),
            "production_document_count": sum(
                row["count"]
                for row in governance_rows
                if (
                    row["purpose"] == "production"
                    and row["scope_status"] == "in_scope"
                )
            ),
            "business_scope_counts": governance,
            "access_mode": "local-controlled-write",
        }

    def documents(self, *, limit: int = 20, offset: int = 0) -> dict[str, Any]:
        limit, offset = _pagination(limit, offset)
        with self.database.connect() as connection:
            total = connection.execute(
                """
                SELECT COUNT(*)
                FROM documents d
                JOIN document_governance dg ON dg.document_id = d.id
                WHERE d.current_version_id IS NOT NULL
                  AND dg.purpose != 'development_fixture'
                """
            ).fetchone()[0]
            rows = connection.execute(
                """
                SELECT d.id, d.original_name, d.classification, d.updated_at,
                       dv.id AS version_id, dv.size_bytes, dv.media_type,
                       pr.id AS processing_run_id, pr.parser_name, pr.parser_version,
                       dg.purpose, dg.scope_status, dg.authority_status,
                       dg.knowledge_domain,
                       COUNT(e.id) AS evidence_count,
                       SUM(CASE WHEN etv.status = 'passed' THEN 1 ELSE 0 END)
                           AS technically_validated_evidence_count,
                       SUM(CASE WHEN e.status = 'reviewing' THEN 1 ELSE 0 END)
                           AS reviewing_evidence_count,
                       SUM(CASE WHEN e.status = 'verified' THEN 1 ELSE 0 END)
                           AS verified_evidence_count,
                       wp.status AS wiki_status,
                       COALESCE(wp.needs_revalidation, 0) AS needs_revalidation
                FROM documents d
                JOIN document_versions dv ON dv.id = d.current_version_id
                JOIN processing_runs pr
                  ON pr.document_version_id = dv.id AND pr.is_current = 1
                JOIN document_governance dg ON dg.document_id = d.id
                LEFT JOIN evidence e ON e.processing_run_id = pr.id
                LEFT JOIN evidence_technical_validation etv
                  ON etv.evidence_id = e.id
                LEFT JOIN wiki_pages wp ON wp.source_document_id = d.id
                WHERE dg.purpose != 'development_fixture'
                GROUP BY d.id, dv.id, pr.id, wp.id
                ORDER BY d.updated_at DESC, d.id
                LIMIT ? OFFSET ?
                """,
                (limit, offset),
            ).fetchall()
        return {
            "total": total,
            "limit": limit,
            "offset": offset,
            "items": [self._document_projection(row) for row in rows],
        }

    def review_queue(self, *, limit: int = 20) -> dict[str, Any]:
        limit, _ = _pagination(limit, 0)
        with self.database.connect() as connection:
            evidence_total = connection.execute(
                """
                SELECT COUNT(*)
                FROM evidence e
                JOIN processing_runs pr
                  ON pr.id = e.processing_run_id AND pr.is_current = 1
                JOIN documents d ON d.current_version_id = pr.document_version_id
                JOIN document_governance dg ON dg.document_id = d.id
                WHERE e.status IN ('draft', 'reviewing', 'conflicted')
                  AND dg.purpose != 'development_fixture'
                """
            ).fetchone()[0]
            conflict_total = connection.execute(
                """
                SELECT COUNT(*)
                FROM conflicts c
                JOIN documents d ON d.id = c.document_id
                JOIN document_governance dg ON dg.document_id = d.id
                WHERE c.status IN ('pending', 'reviewing')
                  AND dg.purpose != 'development_fixture'
                """
            ).fetchone()[0]
            revision_total = connection.execute(
                """
                SELECT COUNT(*)
                FROM wiki_revisions wr
                JOIN wiki_pages wp ON wp.id = wr.page_id
                WHERE wr.status IN ('draft', 'reviewing')
                  AND wp.source_document_id IS NULL
                  AND NOT EXISTS (
                      SELECT 1
                      FROM revision_evidence hidden_re
                      JOIN evidence hidden_e
                        ON hidden_e.id = hidden_re.evidence_id
                      JOIN document_versions hidden_dv
                        ON hidden_dv.id = hidden_e.document_version_id
                      JOIN document_governance hidden_dg
                        ON hidden_dg.document_id = hidden_dv.document_id
                      WHERE hidden_re.revision_id = wr.id
                        AND hidden_dg.purpose = 'development_fixture'
                  )
                """
            ).fetchone()[0]
            evidence = connection.execute(
                """
                SELECT e.id, e.status, e.run_ordinal, e.locator_json,
                       d.original_name, d.classification, e.updated_at
                FROM evidence e
                JOIN processing_runs pr
                  ON pr.id = e.processing_run_id AND pr.is_current = 1
                JOIN documents d ON d.current_version_id = pr.document_version_id
                JOIN document_governance dg ON dg.document_id = d.id
                WHERE e.status IN ('draft', 'reviewing', 'conflicted')
                  AND dg.purpose != 'development_fixture'
                ORDER BY CASE e.status
                             WHEN 'conflicted' THEN 0
                             WHEN 'reviewing' THEN 1
                             ELSE 2
                         END,
                         e.updated_at DESC, e.id
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            conflicts = connection.execute(
                """
                SELECT c.id, c.status, c.conflict_type, c.created_at,
                       d.original_name, d.classification
                FROM conflicts c
                JOIN documents d ON d.id = c.document_id
                JOIN document_governance dg ON dg.document_id = d.id
                WHERE c.status IN ('pending', 'reviewing')
                  AND dg.purpose != 'development_fixture'
                ORDER BY c.updated_at DESC, c.id
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            revisions = connection.execute(
                f"""
                SELECT wr.id, wr.status, wr.revision_number, wr.updated_at,
                       wp.title, {_TOPIC_CLASSIFICATION_SQL} AS classification
                FROM wiki_revisions wr
                JOIN wiki_pages wp ON wp.id = wr.page_id
                JOIN revision_evidence re ON re.revision_id = wr.id
                JOIN evidence e ON e.id = re.evidence_id
                JOIN document_versions dv ON dv.id = e.document_version_id
                JOIN documents d ON d.id = dv.document_id
                WHERE wr.status IN ('draft', 'reviewing')
                  AND wp.source_document_id IS NULL
                  AND NOT EXISTS (
                      SELECT 1
                      FROM revision_evidence hidden_re
                      JOIN evidence hidden_e
                        ON hidden_e.id = hidden_re.evidence_id
                      JOIN document_versions hidden_dv
                        ON hidden_dv.id = hidden_e.document_version_id
                      JOIN document_governance hidden_dg
                        ON hidden_dg.document_id = hidden_dv.document_id
                      WHERE hidden_re.revision_id = wr.id
                        AND hidden_dg.purpose = 'development_fixture'
                  )
                GROUP BY wr.id, wp.title
                ORDER BY wr.updated_at DESC, wr.id
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return {
            "totals": {
                "evidence": evidence_total,
                "conflicts": conflict_total,
                "wiki_revisions": revision_total,
            },
            "evidence": [self._evidence_review_projection(row) for row in evidence],
            "conflicts": [self._conflict_projection(row) for row in conflicts],
            "wiki_revisions": [self._revision_projection(row) for row in revisions],
        }

    def review_queue_page(
        self,
        *,
        kind: str,
        limit: int = 10,
        offset: int = 0,
        status: str | None = None,
        classification: str | None = None,
        query: str | None = None,
    ) -> dict[str, Any]:
        """Return one filtered review queue page without exposing restricted metadata."""

        limit, offset = _pagination(limit, offset)
        kind = _review_kind(kind)
        status = _review_status(status, kind=kind)
        classification = _classification_filter(classification)
        query = _review_query(query)
        if kind == "evidence":
            total, rows = self._evidence_review_page(
                limit=limit,
                offset=offset,
                status=status,
                classification=classification,
                query=query,
            )
            items = [self._evidence_review_projection(row) for row in rows]
        elif kind == "conflicts":
            total, rows = self._conflict_review_page(
                limit=limit,
                offset=offset,
                status=status,
                classification=classification,
                query=query,
            )
            items = [self._conflict_projection(row) for row in rows]
        else:
            total, rows = self._revision_review_page(
                limit=limit,
                offset=offset,
                status=status,
                classification=classification,
                query=query,
            )
            items = [self._revision_projection(row) for row in rows]
        return {
            "kind": kind,
            "total": total,
            "limit": limit,
            "offset": offset,
            "has_previous": offset > 0,
            "has_next": offset + len(items) < total,
            "items": items,
            "filters": {
                "status": status,
                "classification": classification,
                "query": query,
            },
        }

    def rejected_revision_history(
        self,
        *,
        limit: int = 10,
        offset: int = 0,
        classification: str | None = None,
        query: str | None = None,
    ) -> dict[str, Any]:
        """Return immutable rejected Wiki revision history without page content."""

        limit, offset = _pagination(limit, offset)
        classification = _classification_filter(classification)
        query = _review_query(query)
        where = ["wr.status = 'rejected'"]
        parameters: list[Any] = []
        if classification:
            where.append("sources.classification = ?")
            parameters.append(classification)
        if query:
            where.append(
                """
                (instr(lower(wr.id), lower(?)) > 0
                 OR (sources.classification <> 'restricted'
                     AND instr(lower(wp.title), lower(?)) > 0))
                """
            )
            parameters.extend((query, query))
        predicate = " AND ".join(where)
        cte = f"""
            WITH revision_sources AS (
                SELECT wr_source.id AS revision_id,
                       CASE
                           WHEN COUNT(d.id) = 0
                                AND source_document.id IS NULL
                           THEN 'restricted'
                           WHEN MAX(
                               CASE
                                   WHEN d.classification = 'restricted'
                                        OR source_document.classification = 'restricted'
                                   THEN 4
                                   WHEN d.classification = 'confidential'
                                        OR source_document.classification = 'confidential'
                                   THEN 3
                                   WHEN d.classification = 'internal'
                                        OR source_document.classification = 'internal'
                                   THEN 2
                                   ELSE 1
                               END
                           ) = 4 THEN 'restricted'
                           WHEN MAX(
                               CASE
                                   WHEN d.classification = 'confidential'
                                        OR source_document.classification = 'confidential'
                                   THEN 3
                                   WHEN d.classification = 'internal'
                                        OR source_document.classification = 'internal'
                                   THEN 2
                                   ELSE 1
                               END
                           ) = 3 THEN 'confidential'
                           WHEN MAX(
                               CASE
                                   WHEN d.classification = 'internal'
                                        OR source_document.classification = 'internal'
                                   THEN 2
                                   ELSE 1
                               END
                           ) = 2 THEN 'internal'
                           ELSE 'public'
                       END AS classification,
                       CASE
                           WHEN COUNT(e.id) > 0
                                AND MIN(
                                    CASE
                                        WHEN pr.is_current = 1
                                             AND d.current_version_id = dv.id
                                             AND pr.document_version_id =
                                                 e.document_version_id
                                             AND (
                                                 wp_source.source_document_id IS NULL
                                                 OR (
                                                     d.id =
                                                         wp_source.source_document_id
                                                     AND e.processing_run_id =
                                                         wr_source.processing_run_id
                                                 )
                                             )
                                             AND (
                                                 wp_source.source_document_id
                                                     IS NOT NULL
                                                 OR wr_source.processing_run_id
                                                     IS NULL
                                             )
                                        THEN 1 ELSE 0
                                    END
                                ) = 1
                           THEN 1 ELSE 0
                       END AS source_is_current
                FROM wiki_revisions wr_source
                JOIN wiki_pages wp_source
                  ON wp_source.id = wr_source.page_id
                LEFT JOIN documents source_document
                  ON source_document.id = wp_source.source_document_id
                LEFT JOIN revision_evidence re
                  ON re.revision_id = wr_source.id
                LEFT JOIN evidence e ON e.id = re.evidence_id
                LEFT JOIN document_versions dv
                  ON dv.id = e.document_version_id
                LEFT JOIN documents d ON d.id = dv.document_id
                LEFT JOIN processing_runs pr
                  ON pr.id = e.processing_run_id
                WHERE wr_source.status = 'rejected'
                GROUP BY wr_source.id
            )
        """
        source = f"""
            FROM wiki_revisions wr
            JOIN wiki_pages wp ON wp.id = wr.page_id
            JOIN revision_sources sources ON sources.revision_id = wr.id
            LEFT JOIN audit_log rejection ON rejection.id = (
                SELECT al.id
                FROM audit_log al
                WHERE al.event_type = 'wiki_revision_rejected'
                  AND al.entity_type = 'wiki_revision'
                  AND al.entity_id = wr.id
                ORDER BY al.id DESC
                LIMIT 1
            )
            WHERE {predicate}
        """
        with self.database.connect() as connection:
            total = connection.execute(
                f"{cte} SELECT COUNT(*) {source}", parameters
            ).fetchone()[0]
            rows = connection.execute(
                f"""
                {cte}
                SELECT wr.id, wr.revision_number, wr.updated_at,
                       wp.title, sources.classification,
                       sources.source_is_current,
                       rejection.actor AS rejected_by,
                       rejection.created_at AS rejected_at,
                       rejection.details_json AS rejection_details_json
                {source}
                ORDER BY COALESCE(rejection.created_at, wr.updated_at) DESC, wr.id
                LIMIT ? OFFSET ?
                """,
                (*parameters, limit, offset),
            ).fetchall()
        items = [self._rejected_revision_projection(row) for row in rows]
        return {
            "total": total,
            "limit": limit,
            "offset": offset,
            "has_previous": offset > 0,
            "has_next": offset + len(items) < total,
            "items": items,
            "filters": {
                "classification": classification,
                "query": query,
            },
        }

    def _evidence_review_page(
        self,
        *,
        limit: int,
        offset: int,
        status: str | None,
        classification: str | None,
        query: str | None,
    ):
        where = [
            "e.status IN ('draft', 'reviewing', 'conflicted')",
            "dg.purpose != 'development_fixture'",
        ]
        parameters: list[Any] = []
        if status:
            where.append("e.status = ?")
            parameters.append(status)
        if classification:
            where.append("d.classification = ?")
            parameters.append(classification)
        if query:
            where.append(
                """
                (instr(lower(e.id), lower(?)) > 0
                 OR instr(CAST(e.run_ordinal AS TEXT), ?) > 0
                 OR (d.classification <> 'restricted'
                     AND instr(lower(d.original_name), lower(?)) > 0))
                """
            )
            parameters.extend((query, query, query))
        predicate = " AND ".join(where)
        source = f"""
            FROM evidence e
            JOIN processing_runs pr
              ON pr.id = e.processing_run_id AND pr.is_current = 1
            JOIN documents d ON d.current_version_id = pr.document_version_id
            JOIN document_governance dg ON dg.document_id = d.id
            WHERE {predicate}
        """
        with self.database.connect() as connection:
            total = connection.execute(
                f"SELECT COUNT(*) {source}", parameters
            ).fetchone()[0]
            rows = connection.execute(
                f"""
                SELECT e.id, e.status, e.run_ordinal, e.locator_json,
                       d.original_name, d.classification, e.updated_at
                {source}
                ORDER BY CASE e.status
                             WHEN 'conflicted' THEN 0
                             WHEN 'reviewing' THEN 1
                             ELSE 2
                         END,
                         e.updated_at DESC, e.id
                LIMIT ? OFFSET ?
                """,
                (*parameters, limit, offset),
            ).fetchall()
        return total, rows

    def _conflict_review_page(
        self,
        *,
        limit: int,
        offset: int,
        status: str | None,
        classification: str | None,
        query: str | None,
    ):
        where = [
            "c.status IN ('pending', 'reviewing')",
            "dg.purpose != 'development_fixture'",
        ]
        parameters: list[Any] = []
        if status:
            where.append("c.status = ?")
            parameters.append(status)
        if classification:
            where.append("d.classification = ?")
            parameters.append(classification)
        if query:
            where.append(
                """
                (instr(lower(c.id), lower(?)) > 0
                 OR instr(lower(c.conflict_type), lower(?)) > 0
                 OR (d.classification <> 'restricted'
                     AND instr(lower(d.original_name), lower(?)) > 0))
                """
            )
            parameters.extend((query, query, query))
        predicate = " AND ".join(where)
        source = f"""
            FROM conflicts c
            JOIN documents d ON d.id = c.document_id
            JOIN document_governance dg ON dg.document_id = d.id
            WHERE {predicate}
        """
        with self.database.connect() as connection:
            total = connection.execute(
                f"SELECT COUNT(*) {source}", parameters
            ).fetchone()[0]
            rows = connection.execute(
                f"""
                SELECT c.id, c.status, c.conflict_type, c.created_at,
                       d.original_name, d.classification
                {source}
                ORDER BY c.updated_at DESC, c.id
                LIMIT ? OFFSET ?
                """,
                (*parameters, limit, offset),
            ).fetchall()
        return total, rows

    def _revision_review_page(
        self,
        *,
        limit: int,
        offset: int,
        status: str | None,
        classification: str | None,
        query: str | None,
    ):
        where = ["status IN ('draft', 'reviewing')"]
        parameters: list[Any] = []
        if status:
            where.append("status = ?")
            parameters.append(status)
        if classification:
            where.append("classification = ?")
            parameters.append(classification)
        if query:
            where.append(
                """
                (instr(lower(id), lower(?)) > 0
                 OR (classification <> 'restricted'
                     AND instr(lower(title), lower(?)) > 0))
                """
            )
            parameters.extend((query, query))
        predicate = " AND ".join(where)
        source = f"""
            WITH topic_queue AS (
                SELECT wr.id, wr.status, wr.revision_number, wr.updated_at,
                       wp.title,
                       {_TOPIC_CLASSIFICATION_SQL} AS classification
                FROM wiki_revisions wr
                JOIN wiki_pages wp ON wp.id = wr.page_id
                JOIN revision_evidence re ON re.revision_id = wr.id
                JOIN evidence e ON e.id = re.evidence_id
                JOIN document_versions dv ON dv.id = e.document_version_id
                JOIN documents d ON d.id = dv.document_id
                WHERE wp.source_document_id IS NULL
                  AND wr.status IN ('draft', 'reviewing')
                  AND NOT EXISTS (
                      SELECT 1
                      FROM revision_evidence hidden_re
                      JOIN evidence hidden_e
                        ON hidden_e.id = hidden_re.evidence_id
                      JOIN document_versions hidden_dv
                        ON hidden_dv.id = hidden_e.document_version_id
                      JOIN document_governance hidden_dg
                        ON hidden_dg.document_id = hidden_dv.document_id
                      WHERE hidden_re.revision_id = wr.id
                        AND hidden_dg.purpose = 'development_fixture'
                  )
                GROUP BY wr.id, wp.title
            )
        """
        with self.database.connect() as connection:
            total = connection.execute(
                f"{source} SELECT COUNT(*) FROM topic_queue WHERE {predicate}",
                parameters,
            ).fetchone()[0]
            rows = connection.execute(
                f"""
                {source}
                SELECT id, status, revision_number, updated_at,
                       title, classification
                FROM topic_queue
                WHERE {predicate}
                ORDER BY updated_at DESC, id
                LIMIT ? OFFSET ?
                """,
                (*parameters, limit, offset),
            ).fetchall()
        return total, rows

    def evidence_detail(self, evidence_id: str) -> dict[str, Any]:
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT e.id, e.status, e.run_ordinal, e.excerpt, e.locator_json,
                       e.updated_at, d.original_name, d.classification
                FROM evidence e
                JOIN processing_runs pr
                  ON pr.id = e.processing_run_id AND pr.is_current = 1
                JOIN documents d ON d.current_version_id = pr.document_version_id
                WHERE e.id = ?
                """,
                (evidence_id,),
            ).fetchone()
            location_rows = connection.execute(
                """
                SELECT locator_json FROM evidence_locations
                WHERE evidence_id = ? ORDER BY location_ordinal
                """,
                (evidence_id,),
            ).fetchall()
        if not row:
            raise KnowledgeWorkbenchError(f"当前原子证据不存在：{evidence_id}")
        if row["classification"] == "restricted":
            raise PermissionError("restricted 证据不能通过 Web 查看原文")
        locators = [_safe_locator(item["locator_json"]) for item in location_rows]
        if not locators:
            locators = [_safe_locator(row["locator_json"])]
        return {
            "evidence_id": row["id"],
            "status": row["status"],
            "ordinal": row["run_ordinal"],
            "excerpt": row["excerpt"],
            "locator": locators[0],
            "locators": locators,
            "location_count": len(locators),
            "document_name": row["original_name"],
            "classification": row["classification"],
            "updated_at": row["updated_at"],
        }

    def revision_detail(self, revision_id: str) -> dict[str, Any]:
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT wr.id, wr.status, wr.revision_number, wr.markdown_path,
                       wr.content_sha256, wr.generator, wr.created_at, wr.updated_at,
                       wp.id AS page_id, wp.title, wp.needs_revalidation,
                       wp.source_document_id, wr.processing_run_id,
                       source_document.classification
                           AS source_document_classification
                FROM wiki_revisions wr
                JOIN wiki_pages wp ON wp.id = wr.page_id
                LEFT JOIN documents source_document
                  ON source_document.id = wp.source_document_id
                WHERE wr.id = ?
                """,
                (revision_id,),
            ).fetchone()
            if not row:
                raise KnowledgeWorkbenchError(f"Wiki 修订不存在：{revision_id}")
            expected_evidence_count = connection.execute(
                """
                SELECT COUNT(*)
                FROM revision_evidence
                WHERE revision_id = ?
                """,
                (revision_id,),
            ).fetchone()[0]
            if row["source_document_id"] is not None:
                source_is_current = connection.execute(
                    """
                    SELECT 1
                    FROM processing_runs pr
                    JOIN document_versions dv ON dv.id = pr.document_version_id
                    JOIN documents d ON d.id = dv.document_id
                    WHERE pr.id = ? AND pr.is_current = 1
                      AND d.current_version_id = dv.id
                      AND d.id = ?
                    """,
                    (row["processing_run_id"], row["source_document_id"]),
                ).fetchone()
                if not source_is_current:
                    raise KnowledgeWorkbenchError(
                        f"当前 Wiki 修订不存在：{revision_id}"
                    )
            evidence_rows = connection.execute(
                """
                SELECT e.id, e.status, d.id AS document_id, d.original_name,
                       d.classification, etv.status AS validation_status,
                       pr.status AS run_status, pr.is_current AS run_is_current,
                       e.processing_run_id AS evidence_processing_run_id,
                       (pr.document_version_id = e.document_version_id)
                           AS run_version_matches,
                       (d.current_version_id = dv.id) AS version_is_current,
                       dg.purpose, dg.scope_status, dg.authority_status
                FROM revision_evidence re
                JOIN evidence e ON e.id = re.evidence_id
                LEFT JOIN evidence_technical_validation etv
                  ON etv.evidence_id = e.id
                JOIN document_versions dv ON dv.id = e.document_version_id
                JOIN documents d ON d.id = dv.document_id
                LEFT JOIN processing_runs pr ON pr.id = e.processing_run_id
                LEFT JOIN document_governance dg ON dg.document_id = d.id
                WHERE re.revision_id = ?
                ORDER BY d.original_name, e.run_ordinal, e.id
                """,
                (revision_id,),
            ).fetchall()
        classifications = [item["classification"] for item in evidence_rows]
        if row["source_document_classification"] is not None:
            classifications.append(row["source_document_classification"])
        classification = _most_restrictive_classification(classifications)
        if classification == "restricted":
            raise PermissionError("restricted Wiki 修订不能通过 Web 查看内容")
        source_names = list(
            dict.fromkeys(item["original_name"] for item in evidence_rows)
        )
        source_document_ids = {
            item["document_id"] for item in evidence_rows
        }
        source_blockers: list[str] = []
        if row["source_document_id"] is None:
            if len(evidence_rows) != expected_evidence_count:
                source_blockers.append("主题页来源记录不完整")
            if len(source_document_ids) < 2:
                source_blockers.append("主题页至少需要两份不同资料")
            invalid_source_count = sum(
                1
                for item in evidence_rows
                if item["status"] != "verified"
                or item["validation_status"] != "passed"
                or item["run_status"] != "completed"
                or not item["run_is_current"]
                or not item["run_version_matches"]
                or not item["version_is_current"]
                or item["purpose"] != "production"
                or item["scope_status"] != "in_scope"
                or item["authority_status"]
                not in {"reference", "authoritative"}
            )
            if invalid_source_count:
                source_blockers.append(
                    f"{invalid_source_count} 条来源依据当前不可用"
                )
        else:
            if len(evidence_rows) != expected_evidence_count or not evidence_rows:
                source_blockers.append("单资料页面来源记录不完整")
            invalid_source_count = sum(
                1
                for item in evidence_rows
                if item["document_id"] != row["source_document_id"]
                or item["evidence_processing_run_id"] != row["processing_run_id"]
                or item["status"] != "verified"
                or item["validation_status"] != "passed"
                or item["run_status"] != "completed"
                or not item["run_is_current"]
                or not item["run_version_matches"]
                or not item["version_is_current"]
                or item["purpose"] != "production"
                or item["scope_status"] != "in_scope"
                or item["authority_status"]
                not in {"reference", "authoritative"}
            )
            if invalid_source_count:
                source_blockers.append(
                    f"{invalid_source_count} 条来源依据未完成准入或已失效"
                )
        if source_blockers:
            content = ""
            content_integrity = "source_blocked"
        else:
            content = _read_revision_content(
                self.paths,
                row["markdown_path"],
                expected_sha256=row["content_sha256"],
            )
            content_integrity = "verified"
        page_kind = "topic" if row["source_document_id"] is None else "document"
        readable_content = _human_readable_revision_content(
            content,
            page_kind=page_kind,
        )
        preview = readable_content[:MAX_WIKI_PREVIEW_CHARACTERS]
        evidence_by_status: dict[str, int] = {}
        for evidence_row in evidence_rows:
            status = evidence_row["status"]
            evidence_by_status[status] = evidence_by_status.get(status, 0) + 1
        content_truncated = len(preview) < len(readable_content)
        publish_blockers = _revision_publish_blockers(
            status=row["status"],
            evidence_by_status=evidence_by_status,
            content_truncated=content_truncated,
        )
        publish_blockers.extend(source_blockers)
        return {
            "revision_id": row["id"],
            "page_id": row["page_id"],
            "page_title": row["title"],
            "page_kind": page_kind,
            "status": row["status"],
            "revision_number": row["revision_number"],
            "classification": classification,
            "generator": row["generator"],
            "generator_label": _human_generator_label(
                row["generator"],
                page_kind=page_kind,
            ),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "needs_revalidation": bool(row["needs_revalidation"]),
            "source_document_count": len(source_document_ids),
            "source_document_names": source_names,
            "evidence_count": sum(evidence_by_status.values()),
            "evidence_by_status": evidence_by_status,
            "content_preview": preview,
            "content_preview_format": "plain_text",
            "content_length": len(readable_content),
            "content_truncated": content_truncated,
            "content_integrity": content_integrity,
            "can_submit_review": (
                row["status"] == "draft" and not source_blockers
            ),
            "can_publish": not publish_blockers,
            "publish_blockers": publish_blockers,
            "publish_confirmation_phrase": _publish_confirmation_phrase(revision_id),
        }

    def evaluations(self, *, limit: int = 10) -> list[dict[str, Any]]:
        limit, _ = _pagination(limit, 0)
        reports: list[dict[str, Any]] = []
        if not self.paths.evaluations.is_dir():
            return reports
        for path in self.paths.evaluations.glob("*.json"):
            report = _read_evaluation_summary(path)
            if report is not None:
                reports.append(report)
        reports.sort(
            key=lambda item: (item.get("evaluated_at") or "", item["file_name"]),
            reverse=True,
        )
        return reports[:limit]

    def activity(self, *, limit: int = 20) -> list[dict[str, Any]]:
        limit, _ = _pagination(limit, 0)
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT event_type, entity_type, entity_id, actor, created_at
                FROM audit_log ORDER BY id DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def _document_projection(self, row) -> dict[str, Any]:
        restricted = row["classification"] == "restricted"
        return {
            "document_id": row["id"],
            "display_name": "[受限资料]" if restricted else row["original_name"],
            "classification": row["classification"],
            "version_id": row["version_id"],
            "size_bytes": row["size_bytes"],
            "media_type": row["media_type"],
            "processing_run_id": row["processing_run_id"],
            "parser": f"{row['parser_name']} {row['parser_version']}",
            "evidence_count": row["evidence_count"],
            "reviewing_evidence_count": row["reviewing_evidence_count"],
            "verified_evidence_count": row["verified_evidence_count"],
            "technically_validated_evidence_count": (
                row["technically_validated_evidence_count"]
            ),
            "purpose": row["purpose"],
            "scope_status": row["scope_status"],
            "authority_status": row["authority_status"],
            "knowledge_domain": row["knowledge_domain"],
            "wiki_status": row["wiki_status"],
            "needs_revalidation": bool(row["needs_revalidation"]),
            "updated_at": row["updated_at"],
        }

    def _evidence_review_projection(self, row) -> dict[str, Any]:
        restricted = row["classification"] == "restricted"
        locator = {} if restricted else _safe_locator(row["locator_json"])
        return {
            "evidence_id": row["id"],
            "status": row["status"],
            "ordinal": row["run_ordinal"],
            "document_name": "[受限资料]" if restricted else row["original_name"],
            "classification": row["classification"],
            "locator": locator,
            "updated_at": row["updated_at"],
        }

    def _conflict_projection(self, row) -> dict[str, Any]:
        restricted = row["classification"] == "restricted"
        return {
            "conflict_id": row["id"],
            "status": row["status"],
            "conflict_type": row["conflict_type"],
            "document_name": "[受限资料]" if restricted else row["original_name"],
            "classification": row["classification"],
            "created_at": row["created_at"],
        }

    def _revision_projection(self, row) -> dict[str, Any]:
        restricted = row["classification"] == "restricted"
        return {
            "revision_id": row["id"],
            "status": row["status"],
            "revision_number": row["revision_number"],
            "page_title": "[受限知识页]" if restricted else row["title"],
            "classification": row["classification"],
            "updated_at": row["updated_at"],
        }

    def _rejected_revision_projection(self, row) -> dict[str, Any]:
        restricted = row["classification"] == "restricted"
        return {
            "revision_id": row["id"],
            "status": "rejected",
            "revision_number": row["revision_number"],
            "page_title": "[受限知识页]" if restricted else row["title"],
            "classification": row["classification"],
            "source_is_current": bool(row["source_is_current"]),
            "rejected_by": row["rejected_by"],
            "rejected_at": row["rejected_at"] or row["updated_at"],
            "review_note": (
                None
                if restricted
                else _safe_rejection_note(row["rejection_details_json"])
            ),
        }


class WorkbenchActionService:
    """Narrow Web adapter that delegates all writes to existing state machines."""

    evidence_targets = frozenset(
        {
            EvidenceStatus.DRAFT,
            EvidenceStatus.REVIEWING,
            EvidenceStatus.VERIFIED,
            EvidenceStatus.CONFLICTED,
        }
    )
    conflict_targets = frozenset(
        {
            ConflictStatus.REVIEWING,
            ConflictStatus.RESOLVED,
            ConflictStatus.DISMISSED,
        }
    )

    def __init__(
        self,
        database: Database,
        paths: WorkspacePaths | None = None,
        *,
        deepseek_gateway_factory: Callable[[], AuditedModelGateway] | None = None,
        file_opener: Callable[[Path], None] | None = None,
    ):
        self.database = database
        self.paths = paths or WorkspacePaths(database.path.parent)
        self.file_opener = file_opener or _open_with_default_application
        self.deepseek_gateway_factory = (
            deepseek_gateway_factory
            or (lambda: AuditedModelGateway(
                self.database,
                DeepSeekChatModel.from_environment(),
            ))
        )

    @property
    def deepseek_configured(self) -> bool:
        return DeepSeekChatModel.is_configured()

    def open_document(self, document_id: str, *, actor: str) -> dict[str, Any]:
        actor = _required_actor(actor)
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT d.id, d.original_name, d.classification,
                       dv.id AS version_id, dv.stored_path
                FROM documents d
                JOIN document_versions dv ON dv.id = d.current_version_id
                WHERE d.id = ?
                """,
                (document_id,),
            ).fetchone()
        if row is None:
            raise KnowledgeWorkbenchError(
                "资料不存在或没有当前版本"
            )
        if row["classification"] == "restricted":
            raise PermissionError("受限资料不能从 Web 打开")

        raw_root = self.paths.raw.resolve()
        candidate = (self.paths.root / row["stored_path"]).resolve()
        try:
            candidate.relative_to(raw_root)
        except ValueError as exc:
            raise KnowledgeWorkbenchError(
                "资料不在受控只读资料目录中"
            ) from exc
        if not candidate.is_file():
            raise KnowledgeWorkbenchError("只读资料副本不存在")

        self.file_opener(candidate)
        with self.database.transaction() as connection:
            record_event(
                connection,
                "document_opened_locally",
                "document",
                row["id"],
                actor=actor,
                details={"version_id": row["version_id"]},
            )
        return {
            "document_id": row["id"],
            "display_name": row["original_name"],
            "opened": True,
        }

    def ask_knowledge_question(
        self,
        question: str,
        *,
        actor: str,
        history: list[dict[str, object]] | None = None,
        limit: int = 5,
        allow_deepseek_once: bool = False,
    ) -> dict[str, Any]:
        model_gateway = None
        unavailable_reason = None
        if allow_deepseek_once:
            try:
                model_gateway = self.deepseek_gateway_factory()
            except KnowledgeWorkbenchError:
                unavailable_reason = "not_configured"
        return answer_knowledge_question(
            self.database,
            question,
            actor=actor,
            history=history,
            limit=limit,
            paths=self.paths,
            model_gateway=model_gateway,
            cloud_model_requested=allow_deepseek_once,
            allow_internal_cloud_once=allow_deepseek_once,
            cloud_model_unavailable_reason=unavailable_reason,
        )

    def scan_corpus(
        self,
        root: str,
        *,
        actor: str,
        project_name: str | None = None,
        allow_legacy_word_conversion: bool = False,
    ) -> dict[str, Any]:
        actor = _required_actor(actor)
        root = root.strip()
        if not root:
            raise ValueError("请选择或填写要读取的资料目录")
        return scan_corpus_source(
            self.database,
            Path(root),
            actor=actor,
            project_name=project_name,
            allow_legacy_word_conversion=allow_legacy_word_conversion,
        )

    def decide_corpus_file(
        self,
        file_id: str,
        *,
        scope_status: str,
        authority_status: str,
        actor: str,
        reason: str,
    ) -> dict[str, Any]:
        return decide_corpus_file(
            self.database,
            file_id,
            scope_status=scope_status,
            authority_status=authority_status,
            actor=_required_actor(actor),
            reason=reason,
        )

    def import_corpus_file(
        self,
        file_id: str,
        *,
        classification: str,
        actor: str,
    ) -> dict[str, Any]:
        try:
            selected = Classification(classification)
        except ValueError as exc:
            raise KnowledgeWorkbenchError("请选择有效的资料密级") from exc
        return import_corpus_file(
            self.database,
            self.paths,
            file_id,
            classification=selected,
            actor=actor,
        )

    def transition_evidence(
        self, evidence_id: str, target: str, *, actor: str
    ) -> dict[str, Any]:
        actor = _required_actor(actor)
        try:
            target_status = EvidenceStatus(target)
        except ValueError as exc:
            raise ValueError(f"Web 不支持证据目标状态：{target}") from exc
        if target_status not in self.evidence_targets:
            raise ValueError(f"Web 不支持证据目标状态：{target}")
        classification = self._current_evidence_classification(evidence_id)
        if classification == "restricted":
            raise PermissionError("restricted 证据只能回到原始资料并通过 CLI 审核")
        transition_evidence(
            self.database,
            evidence_id,
            target_status,
            actor=actor,
        )
        return {
            "entity_type": "evidence",
            "entity_id": evidence_id,
            "status": target_status.value,
            "actor": actor,
        }

    def accept_entity_candidate_web(
        self,
        candidate_id: str,
        entity_id: str,
        *,
        actor: str,
        note: str | None = None,
    ) -> dict[str, Any]:
        actor = _required_actor(actor)
        entity_id = entity_id.strip()
        if not entity_id:
            raise ValueError("entity_id 不能为空")
        note = _optional_note(note)
        if self._entity_candidate_classification(candidate_id) == "restricted":
            raise PermissionError("restricted 实体候选只能通过 CLI 审核")
        target_visibility = get_entity_visibility(self.database, entity_id)
        if not target_visibility["web_visible"]:
            raise PermissionError(
                "该规范实体没有可用于 Web 的非受限证据密级，请通过 CLI 审核"
            )
        result = accept_entity_candidate(
            self.database,
            candidate_id,
            entity_id,
            actor=actor,
            note=note,
        )
        return {
            "entity_type": "entity_candidate",
            "entity_id": candidate_id,
            "candidate_id": candidate_id,
            "resolved_entity_id": entity_id,
            "status": "accepted",
            "actor": actor,
            "alias_created": result["alias_created"],
            "evidence_link_created": result["evidence_link_created"],
        }

    def reject_entity_candidate_web(
        self,
        candidate_id: str,
        *,
        actor: str,
        note: str,
    ) -> dict[str, Any]:
        actor = _required_actor(actor)
        note = _required_entity_candidate_note(note)
        if self._entity_candidate_classification(candidate_id) == "restricted":
            raise PermissionError("restricted 实体候选只能通过 CLI 审核")
        reject_entity_candidate(
            self.database,
            candidate_id,
            actor=actor,
            note=note,
        )
        return {
            "entity_type": "entity_candidate",
            "entity_id": candidate_id,
            "status": "rejected",
            "actor": actor,
        }

    def propose_entity_merge_web(
        self,
        source_entity_id: str,
        target_entity_id: str,
        *,
        actor: str,
        note: str,
    ) -> dict[str, Any]:
        actor = _required_actor(actor)
        source_entity_id = source_entity_id.strip()
        target_entity_id = target_entity_id.strip()
        if not source_entity_id or not target_entity_id:
            raise ValueError("源实体和目标实体不能为空")
        note = _required_entity_merge_note(note, "合并提议说明")
        self._require_web_visible_entity(source_entity_id)
        self._require_web_visible_entity(target_entity_id)
        request_id = propose_entity_merge(
            self.database,
            source_entity_id,
            target_entity_id,
            actor=actor,
            note=note,
        )
        return {
            "entity_type": "entity_merge_request",
            "entity_id": request_id,
            "request_id": request_id,
            "source_entity_id": source_entity_id,
            "target_entity_id": target_entity_id,
            "status": "reviewing",
            "actor": actor,
        }

    def review_entity_merge_web(
        self,
        request_id: str,
        decision: str,
        *,
        actor: str,
        note: str,
        confirmation: str,
    ) -> dict[str, Any]:
        actor = _required_actor(actor)
        decision = decision.strip().casefold()
        if decision not in {"approve", "reject"}:
            raise ValueError("实体合并复核决定必须是 approve 或 reject")
        note = _required_entity_merge_note(note, "合并复核意见")
        request = self._entity_merge_request(request_id)
        self._require_web_visible_entity(request["source_entity_id"])
        self._require_web_visible_entity(request["target_entity_id"])
        if decision == "approve":
            _required_entity_merge_confirmation(confirmation, request_id)
        result = review_entity_merge(
            self.database,
            request_id,
            decision,
            actor=actor,
            note=note,
        )
        return {
            "entity_type": "entity_merge_request",
            "entity_id": request_id,
            "actor": actor,
            **result,
        }

    def create_entity_relationship_web(
        self,
        relation_key: str,
        source_entity_id: str,
        target_entity_id: str,
        evidence_ids: list[str],
        *,
        actor: str,
        note: str,
        confirmation: str,
    ) -> dict[str, Any]:
        actor = _required_actor(actor)
        relation_key = relation_key.strip().casefold()
        source_entity_id = source_entity_id.strip()
        target_entity_id = target_entity_id.strip()
        if not relation_key or not source_entity_id or not target_entity_id:
            raise ValueError("关系类型、源实体和目标实体不能为空")
        if source_entity_id == target_entity_id:
            raise ValueError("业务关系源实体和目标实体不能相同")
        note = _required_entity_relationship_note(note, "业务关系登记说明")
        _required_entity_relationship_creation_confirmation(
            confirmation,
            relation_key,
            source_entity_id,
            target_entity_id,
        )
        self._require_web_visible_entity(source_entity_id)
        self._require_web_visible_entity(target_entity_id)
        support_ids = _web_evidence_ids(evidence_ids)
        self._require_web_safe_relationship_evidence(support_ids)
        relationship_id = create_entity_relationship(
            self.database,
            relation_key,
            source_entity_id,
            target_entity_id,
            support_ids,
            actor=actor,
            note=note,
        )
        return {
            "entity_type": "entity_relationship",
            "entity_id": relationship_id,
            "relationship_id": relationship_id,
            "relation_key": relation_key,
            "source_entity_id": source_entity_id,
            "target_entity_id": target_entity_id,
            "status": "active",
            "actor": actor,
        }

    def retract_entity_relationship_web(
        self,
        relationship_id: str,
        *,
        actor: str,
        note: str,
        confirmation: str,
    ) -> dict[str, Any]:
        actor = _required_actor(actor)
        note = _required_entity_relationship_note(note, "业务关系撤销说明")
        _required_entity_relationship_retraction_confirmation(
            confirmation, relationship_id
        )
        relationship = self._entity_relationship(relationship_id)
        self._require_web_visible_entity(relationship["source_entity_id"])
        self._require_web_visible_entity(relationship["target_entity_id"])
        result = retract_entity_relationship(
            self.database,
            relationship_id,
            actor=actor,
            note=note,
        )
        return {
            "entity_type": "entity_relationship",
            "entity_id": relationship_id,
            "actor": actor,
            **result,
        }

    def update_conflict_candidate_label(
        self,
        pack_id: str,
        candidate_id: str,
        *,
        expected_content_sha256: str,
        expected_conflict: bool,
        expected_type: str | None,
        note: str | None,
        actor: str,
    ) -> dict[str, Any]:
        return update_cross_document_candidate_label(
            self.database,
            self.paths,
            pack_id,
            candidate_id,
            expected_content_sha256=expected_content_sha256,
            expected_conflict=expected_conflict,
            expected_type=expected_type,
            note=note,
            actor=actor,
        )

    def submit_conflict_candidate_pack(
        self,
        pack_id: str,
        *,
        expected_content_sha256: str,
        actor: str,
    ) -> dict[str, Any]:
        return submit_cross_document_candidate_annotations_by_id(
            self.database,
            self.paths,
            pack_id,
            expected_content_sha256=expected_content_sha256,
            actor=actor,
        )

    def update_conflict_candidate_review(
        self,
        pack_id: str,
        candidate_id: str,
        *,
        expected_content_sha256: str,
        decision: str,
        note: str | None,
        actor: str,
    ) -> dict[str, Any]:
        return update_cross_document_candidate_review(
            self.database,
            self.paths,
            pack_id,
            candidate_id,
            expected_content_sha256=expected_content_sha256,
            decision=decision,
            note=note,
            actor=actor,
        )

    def transition_conflict(
        self,
        conflict_id: str,
        target: str,
        *,
        actor: str,
        note: str | None = None,
    ) -> dict[str, Any]:
        actor = _required_actor(actor)
        note = _optional_note(note)
        try:
            target_status = ConflictStatus(target)
        except ValueError as exc:
            raise ValueError(f"Web 不支持冲突目标状态：{target}") from exc
        if target_status not in self.conflict_targets:
            raise ValueError(f"Web 不支持冲突目标状态：{target}")
        classification = self._conflict_classification(conflict_id)
        if classification == "restricted":
            raise PermissionError("restricted 冲突只能回到原始资料并通过 CLI 处理")
        transition_conflict(
            self.database,
            conflict_id,
            target_status,
            actor=actor,
            note=note,
        )
        return {
            "entity_type": "conflict",
            "entity_id": conflict_id,
            "status": target_status.value,
            "actor": actor,
        }

    def submit_revision_review(
        self, revision_id: str, *, actor: str
    ) -> dict[str, Any]:
        actor = _required_actor(actor)
        row = self._current_revision_for_web(revision_id)
        if row["classification"] == "restricted":
            raise PermissionError("restricted Wiki 修订只能通过 CLI 提交复核")
        _read_revision_content(
            self.paths, row["markdown_path"], expected_sha256=row["content_sha256"]
        )
        request_revision_review(
            self.database,
            revision_id,
            actor=actor,
            paths=self.paths,
        )
        return {
            "entity_type": "wiki_revision",
            "entity_id": revision_id,
            "status": "reviewing",
            "actor": actor,
        }

    def reject_revision_review(
        self,
        revision_id: str,
        *,
        actor: str,
        note: str,
    ) -> dict[str, Any]:
        actor = _required_actor(actor)
        note = _required_review_note(note)
        row = self._current_revision_for_web(revision_id)
        if row["classification"] == "restricted":
            raise PermissionError("restricted Wiki 修订只能通过 CLI 复核")
        _read_revision_content(
            self.paths, row["markdown_path"], expected_sha256=row["content_sha256"]
        )
        reject_revision(
            self.database,
            revision_id,
            actor=actor,
            note=note,
        )
        return {
            "entity_type": "wiki_revision",
            "entity_id": revision_id,
            "status": "rejected",
            "actor": actor,
        }

    def publish_revision_web(
        self,
        revision_id: str,
        *,
        actor: str,
        confirmation: str,
    ) -> dict[str, Any]:
        actor = _required_actor(actor)
        _required_publish_confirmation(confirmation, revision_id)
        row = self._current_revision_for_web(revision_id)
        if row["classification"] == "restricted":
            raise PermissionError("restricted Wiki 修订只能通过 CLI 发布")
        content = _read_revision_content(
            self.paths, row["markdown_path"], expected_sha256=row["content_sha256"]
        )
        with self.database.connect() as connection:
            evidence_rows = connection.execute(
                """
                SELECT e.status, COUNT(*) AS count
                FROM revision_evidence re
                JOIN evidence e ON e.id = re.evidence_id
                WHERE re.revision_id = ?
                GROUP BY e.status
                """,
                (revision_id,),
            ).fetchall()
        evidence_by_status = {
            evidence_row["status"]: evidence_row["count"]
            for evidence_row in evidence_rows
        }
        blockers = _revision_publish_blockers(
            status=row["status"],
            evidence_by_status=evidence_by_status,
            content_truncated=len(content) > MAX_WIKI_PREVIEW_CHARACTERS,
        )
        if blockers:
            raise KnowledgeWorkbenchError(
                "Web 正式发布条件未满足：" + "；".join(blockers)
            )
        publish_revision(self.database, self.paths, revision_id, actor=actor)
        return {
            "entity_type": "wiki_revision",
            "entity_id": revision_id,
            "status": "verified",
            "actor": actor,
        }

    def _current_revision_for_web(self, revision_id: str):
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT wr.status, wr.markdown_path, wr.content_sha256,
                       wr.processing_run_id, wp.source_document_id
                FROM wiki_revisions wr
                JOIN wiki_pages wp ON wp.id = wr.page_id
                WHERE wr.id = ?
                """,
                (revision_id,),
            ).fetchone()
            if not row:
                raise KnowledgeWorkbenchError(
                    f"当前 Wiki 修订不存在：{revision_id}"
                )
            result = dict(row)
            if row["source_document_id"] is not None:
                source = connection.execute(
                    """
                    SELECT d.classification
                    FROM processing_runs pr
                    JOIN document_versions dv ON dv.id = pr.document_version_id
                    JOIN documents d ON d.id = dv.document_id
                    WHERE pr.id = ? AND pr.is_current = 1
                      AND d.current_version_id = dv.id
                      AND d.id = ?
                    """,
                    (row["processing_run_id"], row["source_document_id"]),
                ).fetchone()
                classifications = (
                    [source["classification"]] if source is not None else []
                )
            else:
                sources = connection.execute(
                    """
                    SELECT d.classification
                    FROM revision_evidence re
                    JOIN evidence e ON e.id = re.evidence_id
                    JOIN document_versions dv ON dv.id = e.document_version_id
                    JOIN documents d ON d.id = dv.document_id
                    WHERE re.revision_id = ?
                    """,
                    (revision_id,),
                ).fetchall()
                classifications = [item["classification"] for item in sources]
        if not classifications:
            raise KnowledgeWorkbenchError(f"当前 Wiki 修订不存在：{revision_id}")
        result["classification"] = _most_restrictive_classification(
            classifications
        )
        return result

    def _current_evidence_classification(self, evidence_id: str) -> str:
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT d.classification
                FROM evidence e
                JOIN processing_runs pr
                  ON pr.id = e.processing_run_id AND pr.is_current = 1
                JOIN documents d ON d.current_version_id = pr.document_version_id
                WHERE e.id = ?
                """,
                (evidence_id,),
            ).fetchone()
        if not row:
            raise KnowledgeWorkbenchError(f"当前原子证据不存在：{evidence_id}")
        return row["classification"]

    def _conflict_classification(self, conflict_id: str) -> str:
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT d.classification
                FROM conflicts c JOIN documents d ON d.id = c.document_id
                WHERE c.id = ?
                """,
                (conflict_id,),
            ).fetchone()
        if not row:
            raise KnowledgeWorkbenchError(f"冲突不存在：{conflict_id}")
        return row["classification"]

    def _entity_candidate_classification(self, candidate_id: str) -> str:
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT d.classification
                FROM entity_candidates ec
                JOIN document_versions dv ON dv.id = ec.document_version_id
                JOIN documents d ON d.id = dv.document_id
                WHERE ec.id = ?
                """,
                (candidate_id,),
            ).fetchone()
        if not row:
            raise KnowledgeWorkbenchError(f"实体候选不存在：{candidate_id}")
        return row["classification"]

    def _entity_merge_request(self, request_id: str):
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT id, status, source_entity_id, target_entity_id
                FROM entity_merge_requests
                WHERE id = ?
                """,
                (request_id,),
            ).fetchone()
        if not row:
            raise KnowledgeWorkbenchError(f"实体合并请求不存在：{request_id}")
        return row

    def _entity_relationship(self, relationship_id: str):
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT id, status, source_entity_id, target_entity_id
                FROM entity_relationships
                WHERE id = ?
                """,
                (relationship_id,),
            ).fetchone()
        if not row:
            raise KnowledgeWorkbenchError(
                f"业务关系不存在：{relationship_id}"
            )
        return row

    def _require_web_safe_relationship_evidence(
        self, evidence_ids: list[str]
    ) -> None:
        placeholders = ", ".join("?" for _ in evidence_ids)
        with self.database.connect() as connection:
            safe_count = connection.execute(
                f"""
                SELECT COUNT(DISTINCT e.id)
                FROM evidence e
                JOIN processing_runs pr
                  ON pr.id = e.processing_run_id AND pr.is_current = 1
                JOIN document_versions dv ON dv.id = e.document_version_id
                JOIN documents d
                  ON d.id = dv.document_id AND d.current_version_id = dv.id
                WHERE e.id IN ({placeholders})
                  AND e.status = 'verified'
                  AND d.classification != 'restricted'
                """,
                evidence_ids,
            ).fetchone()[0]
        if safe_count != len(evidence_ids):
            raise PermissionError(
                "Web 只能引用当前、verified 且非 restricted 的关系证据"
            )

    def _require_web_visible_entity(self, entity_id: str) -> dict:
        visibility = get_entity_visibility(self.database, entity_id)
        if not visibility["web_visible"]:
            raise PermissionError(
                "该规范实体没有可用于 Web 的非受限证据密级，请通过 CLI 处理"
            )
        return visibility


def _pagination(limit: int, offset: int) -> tuple[int, int]:
    if limit < 1 or limit > 200:
        raise ValueError("limit 必须在 1 到 200 之间")
    if offset < 0:
        raise ValueError("offset 不能小于 0")
    return limit, offset


def _review_kind(value: str) -> str:
    if value not in {"evidence", "conflicts", "wiki_revisions"}:
        raise ValueError("kind 必须是 evidence、conflicts 或 wiki_revisions")
    return value


def _review_status(value: str | None, *, kind: str) -> str | None:
    if value is None or not value.strip():
        return None
    status = value.strip()
    allowed = {
        "evidence": {"draft", "reviewing", "conflicted"},
        "conflicts": {"pending", "reviewing"},
        "wiki_revisions": {"draft", "reviewing"},
    }[kind]
    if status not in allowed:
        raise ValueError(f"{kind} 审核队列不支持状态：{status}")
    return status


def _classification_filter(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    classification = value.strip()
    if classification not in {"public", "internal", "confidential", "restricted"}:
        raise ValueError("classification 不受支持")
    return classification


def _review_query(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    query = value.strip()
    if len(query) > 120:
        raise ValueError("q 不能超过 120 个字符")
    return query


def _entity_candidate_status(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    status = value.strip()
    if status == "all":
        return None
    if status not in {"pending", "accepted", "rejected"}:
        raise ValueError(f"实体候选不支持状态：{status}")
    return status


def _entity_relationship_status(value: str) -> str:
    status = value.strip()
    if status not in {"active", "retracted"}:
        raise ValueError(f"业务关系不支持状态：{status}")
    return status


def _web_visible_entity(database: Database, entity_id: str) -> dict:
    entity_id = entity_id.strip()
    if not entity_id:
        raise ValueError("entity_id 不能为空")
    visibility = get_entity_visibility(database, entity_id)
    if not visibility["web_visible"]:
        raise PermissionError(
            "该规范实体没有可用于 Web 的非受限证据密级，请通过 CLI 处理"
        )
    return visibility


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


_MARKDOWN_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_MARKDOWN_HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*$")
_INTERNAL_OBJECT_ID = re.compile(
    r"^(?:doc|ev|page|rev|run|ver)_[A-Za-z0-9_-]+$",
    re.IGNORECASE,
)
_MARKDOWN_LINK = re.compile(r"!?\[([^\]]*)\]\([^)]+\)")
_MARKDOWN_CODE = re.compile(r"`([^`]*)`")
_DOCUMENT_LOCATOR = re.compile(
    r"^定位(?:（\d+\s*处）)?[：:]\s*`?(.+?)`?\s*$"
)


def _human_readable_revision_content(content: str, *, page_kind: str) -> str:
    """Convert stored Markdown into a plain-language Web review preview.

    The immutable Markdown file retains frontmatter and evidence markers for
    traceability. The Web preview deliberately omits those internal details so
    reviewers see the document meaning rather than database identifiers.
    """

    lines = content.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if lines and lines[0].lstrip("\ufeff").strip() == "---":
        for index in range(1, len(lines)):
            if lines[index].strip() == "---":
                lines = lines[index + 1 :]
                break
    body = _MARKDOWN_HTML_COMMENT.sub("", "\n".join(lines))
    output: list[str] = []
    extract_number = 0
    for raw_line in body.split("\n"):
        line = raw_line.strip()
        if not line:
            if output and output[-1]:
                output.append("")
            continue

        heading = _MARKDOWN_HEADING.match(line)
        if heading:
            label = _plain_markdown_text(heading.group(1))
            if _INTERNAL_OBJECT_ID.fullmatch(label):
                extract_number += 1
                label = f"原文摘录 {extract_number}"
            _append_plain_block(output, label)
            continue

        if page_kind == "document" and re.match(
            r"^-\s*(?:SHA-256|文件版本)\s*[：:]",
            line,
            re.IGNORECASE,
        ):
            continue

        locator = _DOCUMENT_LOCATOR.match(line)
        if page_kind == "document" and locator:
            _append_plain_block(
                output,
                _human_locator_summary(locator.group(1)),
            )
            continue

        while line.startswith(">"):
            line = line[1:].lstrip()
        checkbox = re.match(r"^-\s*\[[ xX]\]\s*(.*)$", line)
        if checkbox:
            line = f"待确认：{checkbox.group(1)}"
        elif line.startswith("- "):
            line = f"• {line[2:].strip()}"
        line = _plain_markdown_text(line)
        if line and not _INTERNAL_OBJECT_ID.fullmatch(line):
            output.append(line)

    while output and not output[-1]:
        output.pop()
    return "\n".join(output)


def _append_plain_block(output: list[str], value: str) -> None:
    if output and output[-1]:
        output.append("")
    if value:
        output.append(value)
        output.append("")


def _plain_markdown_text(value: str) -> str:
    value = _MARKDOWN_LINK.sub(r"\1", value)
    value = _MARKDOWN_CODE.sub(r"\1", value)
    value = value.replace("**", "").replace("__", "")
    return value.strip()


def _human_locator_summary(raw: str) -> str:
    try:
        parsed = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return "位置：请在原始资料中核对对应段落"
    locators = parsed if isinstance(parsed, list) else [parsed]
    labels = [
        label
        for item in locators
        if isinstance(item, dict)
        for label in [_human_locator_label(item)]
        if label
    ]
    if not labels:
        return "位置：请在原始资料中核对对应段落"
    return "位置：" + "；".join(labels)


def _human_locator_label(locator: dict[str, Any]) -> str:
    labels: list[str] = []
    page = locator.get("page")
    if isinstance(page, int):
        labels.append(f"第 {page} 页")
    slide = locator.get("slide")
    if isinstance(slide, int):
        labels.append(f"第 {slide} 张幻灯片")
    sheet = locator.get("sheet")
    if isinstance(sheet, str) and sheet.strip():
        labels.append(f"工作表“{sheet.strip()}”")
    cell_range = locator.get("cell_range")
    if isinstance(cell_range, str) and cell_range.strip():
        labels.append(f"单元格 {cell_range.strip()}")
    paragraph = locator.get("paragraph")
    if isinstance(paragraph, int):
        labels.append(f"第 {paragraph} 段")
    line_start = locator.get("line_start")
    line_end = locator.get("line_end")
    if isinstance(line_start, int):
        if isinstance(line_end, int) and line_end != line_start:
            labels.append(f"第 {line_start}—{line_end} 行")
        else:
            labels.append(f"第 {line_start} 行")
    heading_path = locator.get("heading_path")
    if isinstance(heading_path, list):
        headings = [
            str(item).strip()
            for item in heading_path
            if isinstance(item, str) and item.strip()
        ]
        if headings:
            labels.append("章节“" + " / ".join(headings) + "”")
    return "，".join(labels)


def _human_generator_label(generator: str, *, page_kind: str) -> str:
    if page_kind == "topic":
        return "系统按主题整理"
    if "faithful" in generator.lower():
        return "系统按原文整理"
    return "系统整理"


def _read_revision_content(
    paths: WorkspacePaths, relative_path: str, *, expected_sha256: str
) -> str:
    root = paths.root.resolve()
    candidate = (root / relative_path).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise KnowledgeWorkbenchError("Wiki 修订文件位置无效") from exc
    if not candidate.is_file():
        raise KnowledgeWorkbenchError("Wiki 修订文件不存在")
    try:
        content = candidate.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise KnowledgeWorkbenchError("Wiki 修订文件无法安全读取") from exc
    if sha256_text(content) != expected_sha256:
        raise KnowledgeWorkbenchError(
            "Wiki 修订文件已被外部修改，请先通过受控流程同步后再提交复核"
        )
    return content


def _required_actor(value: str) -> str:
    actor = value.strip()
    if not actor:
        raise ValueError("actor 不能为空")
    if len(actor) > 80:
        raise ValueError("actor 不能超过 80 个字符")
    return actor


def _required_review_note(value: str) -> str:
    note = value.strip()
    if not note:
        raise ValueError("驳回 Wiki 修订必须填写复核意见")
    if len(note) > 2000:
        raise ValueError("Wiki 修订复核意见不能超过 2000 个字符")
    return note


def _required_entity_candidate_note(value: str) -> str:
    note = value.strip()
    if not note:
        raise ValueError("驳回实体候选必须填写复核意见")
    if len(note) > 2000:
        raise ValueError("实体候选复核意见不能超过 2000 个字符")
    return note


def _required_entity_merge_note(value: str, label: str) -> str:
    note = value.strip()
    if not note:
        raise ValueError(f"{label}不能为空")
    if len(note) > 2000:
        raise ValueError(f"{label}不能超过 2000 个字符")
    return note


def _required_entity_relationship_note(value: str, label: str) -> str:
    note = value.strip()
    if not note:
        raise ValueError(f"{label}不能为空")
    if len(note) > 2000:
        raise ValueError(f"{label}不能超过 2000 个字符")
    return note


def _web_evidence_ids(value: list[str]) -> list[str]:
    if not isinstance(value, list):
        raise ValueError("evidence_ids 必须是数组")
    evidence_ids = sorted(
        {
            item.strip()
            for item in value
            if isinstance(item, str) and item.strip()
        }
    )
    if not evidence_ids:
        raise ValueError("业务关系必须至少选择一条证据")
    if len(evidence_ids) > 50:
        raise ValueError("Web 单次最多选择 50 条业务关系证据")
    if any(len(item) > 120 for item in evidence_ids):
        raise ValueError("evidence_id 长度无效")
    return evidence_ids


def _entity_relationship_creation_phrase(
    relation_key: str, source_entity_id: str, target_entity_id: str
) -> str:
    return f"登记 {relation_key} {source_entity_id} {target_entity_id}"


def _required_entity_relationship_creation_confirmation(
    value: str,
    relation_key: str,
    source_entity_id: str,
    target_entity_id: str,
) -> str:
    confirmation = value.strip()
    expected = _entity_relationship_creation_phrase(
        relation_key, source_entity_id, target_entity_id
    )
    if confirmation != expected:
        raise ValueError(f"登记业务关系前必须完整输入确认短语：{expected}")
    return confirmation


def _entity_relationship_retraction_phrase(relationship_id: str) -> str:
    return f"撤销 {relationship_id}"


def _required_entity_relationship_retraction_confirmation(
    value: str, relationship_id: str
) -> str:
    confirmation = value.strip()
    expected = _entity_relationship_retraction_phrase(relationship_id)
    if confirmation != expected:
        raise ValueError(f"撤销业务关系前必须完整输入确认短语：{expected}")
    return confirmation


def _entity_merge_confirmation_phrase(request_id: str) -> str:
    return f"合并 {request_id}"


def _required_entity_merge_confirmation(value: str, request_id: str) -> str:
    confirmation = value.strip()
    expected = _entity_merge_confirmation_phrase(request_id)
    if confirmation != expected:
        raise ValueError(f"批准实体合并前必须完整输入确认短语：{expected}")
    return confirmation


def _publish_confirmation_phrase(revision_id: str) -> str:
    return f"发布 {revision_id}"


def _required_publish_confirmation(value: str, revision_id: str) -> str:
    confirmation = value.strip()
    expected = _publish_confirmation_phrase(revision_id)
    if confirmation != expected:
        raise ValueError(f"正式发布前必须完整输入确认短语：{expected}")
    return confirmation


def _most_restrictive_classification(values: list[str]) -> str:
    order = {
        "public": 1,
        "internal": 2,
        "confidential": 3,
        "restricted": 4,
    }
    return max(values or ["internal"], key=lambda item: order.get(item, 4))


def _revision_publish_blockers(
    *,
    status: str,
    evidence_by_status: dict[str, int],
    content_truncated: bool,
) -> list[str]:
    blockers: list[str] = []
    if status != "reviewing":
        blockers.append("修订状态必须是 reviewing")
    evidence_count = sum(evidence_by_status.values())
    if evidence_count == 0:
        blockers.append("修订必须至少引用一条证据")
    unverified_count = evidence_count - evidence_by_status.get("verified", 0)
    if unverified_count:
        blockers.append(f"仍有 {unverified_count} 条引用证据未通过审核")
    if content_truncated:
        blockers.append("Web 预览未覆盖全文，请通过 CLI 核对并发布")
    return blockers


def _optional_note(value: str | None) -> str | None:
    if value is None:
        return None
    note = value.strip()
    if len(note) > 2000:
        raise ValueError("note 不能超过 2000 个字符")
    return note or None


def _safe_locator(raw: str) -> dict[str, Any]:
    try:
        locator = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {}
    allowed = {
        "page",
        "paragraph",
        "table",
        "row",
        "cell_range",
        "slide",
        "line_start",
        "line_end",
        "unit",
        "segment",
    }
    return {key: value for key, value in locator.items() if key in allowed}


def _safe_rejection_note(raw: str | None) -> str | None:
    if not raw:
        return None
    try:
        details = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(details, dict):
        return None
    note = details.get("note")
    return note if isinstance(note, str) and note.strip() else None


def _read_evaluation_summary(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    aggregate = payload.get("aggregate")
    if not isinstance(aggregate, dict) or "case_count" not in aggregate:
        return None
    allowed_metrics = {
        "case_count",
        "passed_cases",
        "pass_rate",
        "required_evidence_coverage",
        "evidence_count",
        "all_evidence_traceable",
        "precision",
        "recall",
        "f1",
        "type_accuracy",
        "citation_precision",
        "citation_recall",
    }
    return {
        "file_name": path.name,
        "schema_version": payload.get("schema_version"),
        "dataset_name": payload.get("dataset_name") or payload.get("name"),
        "evaluated_at": payload.get("evaluated_at"),
        "metrics": {
            key: aggregate[key] for key in allowed_metrics if key in aggregate
        },
    }
