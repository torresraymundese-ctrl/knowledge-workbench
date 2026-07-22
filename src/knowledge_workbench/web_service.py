from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .config import WorkspacePaths
from .conflicts import transition_conflict
from .database import Database
from .errors import KnowledgeWorkbenchError
from .models import ConflictStatus, EvidenceStatus
from .review import transition_evidence


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
            "summary": self.summary(),
            "documents": self.documents(limit=12),
            "review_queue": self.review_queue(limit=12),
            "evaluations": self.evaluations(limit=6),
            "activity": self.activity(limit=8),
        }

    def summary(self) -> dict[str, Any]:
        with self.database.connect() as connection:
            document_count = connection.execute(
                "SELECT COUNT(*) FROM documents WHERE current_version_id IS NOT NULL"
            ).fetchone()[0]
            evidence_rows = connection.execute(
                """
                SELECT e.status, COUNT(*) AS count
                FROM evidence e
                JOIN processing_runs pr
                  ON pr.id = e.processing_run_id AND pr.is_current = 1
                JOIN documents d ON d.current_version_id = pr.document_version_id
                GROUP BY e.status
                """
            ).fetchall()
            page_rows = connection.execute(
                "SELECT status, COUNT(*) AS count FROM wiki_pages GROUP BY status"
            ).fetchall()
            active_conflicts = connection.execute(
                "SELECT COUNT(*) FROM conflicts WHERE status IN ('pending', 'reviewing')"
            ).fetchone()[0]
            active_tasks = connection.execute(
                "SELECT COUNT(*) FROM tasks WHERE status IN ('pending', 'running', 'retrying')"
            ).fetchone()[0]
            needs_revalidation = connection.execute(
                "SELECT COUNT(*) FROM wiki_pages WHERE needs_revalidation = 1"
            ).fetchone()[0]
            approved_labeling_sessions = connection.execute(
                "SELECT COUNT(*) FROM labeling_sessions WHERE status = 'approved'"
            ).fetchone()[0]
        evidence_by_status = {row["status"]: row["count"] for row in evidence_rows}
        pages_by_status = {row["status"]: row["count"] for row in page_rows}
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
            "access_mode": "local-controlled-write",
        }

    def documents(self, *, limit: int = 20, offset: int = 0) -> dict[str, Any]:
        limit, offset = _pagination(limit, offset)
        with self.database.connect() as connection:
            total = connection.execute(
                "SELECT COUNT(*) FROM documents WHERE current_version_id IS NOT NULL"
            ).fetchone()[0]
            rows = connection.execute(
                """
                SELECT d.id, d.original_name, d.classification, d.updated_at,
                       dv.id AS version_id, dv.size_bytes, dv.media_type,
                       pr.id AS processing_run_id, pr.parser_name, pr.parser_version,
                       COUNT(e.id) AS evidence_count,
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
                LEFT JOIN evidence e ON e.processing_run_id = pr.id
                LEFT JOIN wiki_pages wp ON wp.source_document_id = d.id
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
                WHERE e.status IN ('draft', 'reviewing', 'conflicted')
                """
            ).fetchone()[0]
            conflict_total = connection.execute(
                "SELECT COUNT(*) FROM conflicts WHERE status IN ('pending', 'reviewing')"
            ).fetchone()[0]
            revision_total = connection.execute(
                """
                SELECT COUNT(*)
                FROM wiki_revisions wr
                JOIN wiki_pages wp ON wp.id = wr.page_id
                JOIN documents d ON d.id = wp.source_document_id
                JOIN processing_runs pr
                  ON pr.id = wr.processing_run_id AND pr.is_current = 1
                WHERE wr.status = 'reviewing'
                  AND d.current_version_id = pr.document_version_id
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
                WHERE e.status IN ('draft', 'reviewing', 'conflicted')
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
                WHERE c.status IN ('pending', 'reviewing')
                ORDER BY c.updated_at DESC, c.id
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            revisions = connection.execute(
                """
                SELECT wr.id, wr.status, wr.revision_number, wr.updated_at,
                       wp.title, d.classification
                FROM wiki_revisions wr
                JOIN wiki_pages wp ON wp.id = wr.page_id
                JOIN documents d ON d.id = wp.source_document_id
                JOIN processing_runs pr
                  ON pr.id = wr.processing_run_id AND pr.is_current = 1
                WHERE wr.status = 'reviewing'
                  AND d.current_version_id = pr.document_version_id
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
        if not row:
            raise KnowledgeWorkbenchError(f"当前原子证据不存在：{evidence_id}")
        if row["classification"] == "restricted":
            raise PermissionError("restricted 证据不能通过 Web 查看原文")
        return {
            "evidence_id": row["id"],
            "status": row["status"],
            "ordinal": row["run_ordinal"],
            "excerpt": row["excerpt"],
            "locator": _safe_locator(row["locator_json"]),
            "document_name": row["original_name"],
            "classification": row["classification"],
            "updated_at": row["updated_at"],
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

    def __init__(self, database: Database):
        self.database = database

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


def _pagination(limit: int, offset: int) -> tuple[int, int]:
    if limit < 1 or limit > 200:
        raise ValueError("limit 必须在 1 到 200 之间")
    if offset < 0:
        raise ValueError("offset 不能小于 0")
    return limit, offset


def _required_actor(value: str) -> str:
    actor = value.strip()
    if not actor:
        raise ValueError("actor 不能为空")
    if len(actor) > 80:
        raise ValueError("actor 不能超过 80 个字符")
    return actor


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
