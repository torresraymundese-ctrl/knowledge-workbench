from __future__ import annotations

from .database import Database


def search_evidence(database: Database, query: str, limit: int = 10):
    with database.connect() as connection:
        try:
            rows = connection.execute(
                """
                SELECT e.id, e.status, e.excerpt, e.locator_json,
                       d.original_name, d.classification,
                       bm25(evidence_fts) AS score
                FROM evidence_fts
                JOIN evidence e ON e.id = evidence_fts.evidence_id
                JOIN document_versions dv ON dv.id = e.document_version_id
                JOIN documents d ON d.id = dv.document_id
                WHERE evidence_fts MATCH ?
                ORDER BY score
                LIMIT ?
                """,
                (query, limit),
            ).fetchall()
        except Exception:
            rows = []
        if rows:
            return rows
        # unicode61 can treat an unspaced Chinese sentence as one token. LIKE is a
        # deterministic substring fallback until the tokenizer is configurable.
        return connection.execute(
            """
            SELECT e.id, e.status, e.excerpt, e.locator_json,
                   d.original_name, d.classification, 0.0 AS score
            FROM evidence e
            JOIN document_versions dv ON dv.id = e.document_version_id
            JOIN documents d ON d.id = dv.document_id
            WHERE e.excerpt LIKE ? ESCAPE '\\'
            ORDER BY e.created_at DESC
            LIMIT ?
            """,
            (f"%{_escape_like(query)}%", limit),
        ).fetchall()


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
