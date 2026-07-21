from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA_VERSION = 5


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS documents (
    id TEXT PRIMARY KEY,
    original_name TEXT NOT NULL,
    source_path TEXT NOT NULL UNIQUE,
    classification TEXT NOT NULL CHECK (
        classification IN ('public', 'internal', 'confidential', 'restricted')
    ),
    current_version_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS document_versions (
    id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL REFERENCES documents(id),
    sha256 TEXT NOT NULL UNIQUE,
    stored_path TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    media_type TEXT,
    parser_name TEXT NOT NULL,
    parser_version TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'parsed',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_document_versions_document
    ON document_versions(document_id, created_at);

CREATE TABLE IF NOT EXISTS evidence (
    id TEXT PRIMARY KEY,
    document_version_id TEXT NOT NULL REFERENCES document_versions(id),
    ordinal INTEGER NOT NULL,
    excerpt TEXT NOT NULL CHECK (length(trim(excerpt)) > 0),
    locator_json TEXT NOT NULL DEFAULT '{}',
    extraction_method TEXT NOT NULL,
    extraction_model TEXT,
    status TEXT NOT NULL DEFAULT 'draft' CHECK (
        status IN ('draft', 'reviewing', 'verified', 'conflicted', 'deprecated', 'archived')
    ),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(document_version_id, ordinal)
);

CREATE INDEX IF NOT EXISTS idx_evidence_status ON evidence(status);

CREATE VIRTUAL TABLE IF NOT EXISTS evidence_fts USING fts5(
    excerpt,
    evidence_id UNINDEXED,
    tokenize = 'unicode61'
);

CREATE TABLE IF NOT EXISTS wiki_pages (
    id TEXT PRIMARY KEY,
    source_document_id TEXT UNIQUE REFERENCES documents(id),
    slug TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft',
    current_verified_revision_id TEXT,
    needs_revalidation INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS wiki_revisions (
    id TEXT PRIMARY KEY,
    page_id TEXT NOT NULL REFERENCES wiki_pages(id),
    revision_number INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft' CHECK (
        status IN ('draft', 'reviewing', 'verified', 'rejected', 'superseded', 'archived')
    ),
    markdown_path TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    generator TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(page_id, revision_number)
);

CREATE TABLE IF NOT EXISTS revision_evidence (
    revision_id TEXT NOT NULL REFERENCES wiki_revisions(id) ON DELETE CASCADE,
    evidence_id TEXT NOT NULL REFERENCES evidence(id),
    PRIMARY KEY(revision_id, evidence_id)
);

CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    task_type TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('queued', 'running', 'succeeded', 'failed')),
    payload_json TEXT NOT NULL DEFAULT '{}',
    attempts INTEGER NOT NULL DEFAULT 0,
    error_message TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    actor TEXT NOT NULL,
    details_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_audit_entity
    ON audit_log(entity_type, entity_id, created_at);
"""


MIGRATION_2 = """
ALTER TABLE tasks RENAME TO tasks_v1;

CREATE TABLE tasks (
    id TEXT PRIMARY KEY,
    task_type TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN ('pending', 'running', 'retrying', 'done', 'failed')
    ),
    payload_json TEXT NOT NULL DEFAULT '{}',
    result_json TEXT,
    idempotency_key TEXT UNIQUE,
    priority INTEGER NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3 CHECK (max_attempts > 0),
    next_attempt_at TEXT,
    lease_expires_at TEXT,
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

INSERT INTO tasks(
    id, task_type, status, payload_json, attempts, last_error, created_at, updated_at
)
SELECT id, task_type,
       CASE status
           WHEN 'queued' THEN 'pending'
           WHEN 'succeeded' THEN 'done'
           ELSE status
       END,
       payload_json, attempts, error_message, created_at, updated_at
FROM tasks_v1;

DROP TABLE tasks_v1;

CREATE INDEX idx_tasks_claim
    ON tasks(status, next_attempt_at, priority DESC, created_at);

CREATE TABLE conflicts (
    id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL REFERENCES documents(id),
    older_evidence_id TEXT NOT NULL REFERENCES evidence(id),
    newer_evidence_id TEXT NOT NULL REFERENCES evidence(id),
    conflict_type TEXT NOT NULL CHECK (
        conflict_type IN ('polarity_change', 'value_change', 'model_flagged', 'manual')
    ),
    similarity_score REAL,
    reason TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (
        status IN ('pending', 'reviewing', 'resolved', 'dismissed')
    ),
    resolution_note TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(older_evidence_id, newer_evidence_id, conflict_type)
);

CREATE INDEX idx_conflicts_status ON conflicts(status, created_at);
"""


MIGRATION_3 = """
CREATE TABLE wiki_links (
    id TEXT PRIMARY KEY,
    source_revision_id TEXT NOT NULL REFERENCES wiki_revisions(id) ON DELETE CASCADE,
    target_page_id TEXT NOT NULL REFERENCES wiki_pages(id),
    relationship TEXT NOT NULL,
    evidence_ids_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(source_revision_id, target_page_id, relationship)
);

CREATE INDEX idx_wiki_links_target ON wiki_links(target_page_id, created_at);
"""


MIGRATION_4 = """
CREATE TABLE processing_runs (
    id TEXT PRIMARY KEY,
    document_version_id TEXT NOT NULL REFERENCES document_versions(id),
    parser_name TEXT NOT NULL,
    parser_version TEXT NOT NULL,
    extraction_method TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN ('completed', 'superseded', 'failed')
    ),
    is_current INTEGER NOT NULL DEFAULT 0 CHECK (is_current IN (0, 1)),
    created_at TEXT NOT NULL,
    completed_at TEXT,
    UNIQUE(document_version_id, parser_name, parser_version, extraction_method)
);

ALTER TABLE evidence
    ADD COLUMN processing_run_id TEXT REFERENCES processing_runs(id);
ALTER TABLE evidence
    ADD COLUMN run_ordinal INTEGER;
ALTER TABLE wiki_revisions
    ADD COLUMN processing_run_id TEXT REFERENCES processing_runs(id);

INSERT INTO processing_runs(
    id, document_version_id, parser_name, parser_version,
    extraction_method, status, is_current, created_at, completed_at
)
SELECT 'run_legacy_' || dv.id,
       dv.id,
       dv.parser_name,
       dv.parser_version,
       COALESCE(
           (SELECT MIN(e.extraction_method)
            FROM evidence e
            WHERE e.document_version_id = dv.id),
           'legacy-unknown'
       ),
       'completed',
       1,
       dv.created_at,
       dv.created_at
FROM document_versions dv;

UPDATE evidence
SET processing_run_id = 'run_legacy_' || document_version_id,
    run_ordinal = ordinal
WHERE processing_run_id IS NULL;

UPDATE wiki_revisions
SET processing_run_id = (
    SELECT e.processing_run_id
    FROM revision_evidence re
    JOIN evidence e ON e.id = re.evidence_id
    WHERE re.revision_id = wiki_revisions.id
    LIMIT 1
)
WHERE processing_run_id IS NULL;

CREATE UNIQUE INDEX idx_processing_runs_current
    ON processing_runs(document_version_id)
    WHERE is_current = 1;
CREATE UNIQUE INDEX idx_evidence_run_ordinal
    ON evidence(processing_run_id, run_ordinal)
    WHERE processing_run_id IS NOT NULL;
CREATE INDEX idx_evidence_processing_run
    ON evidence(processing_run_id, status);
CREATE INDEX idx_wiki_revisions_processing_run
    ON wiki_revisions(processing_run_id);
"""


MIGRATION_5 = """
CREATE TABLE labeling_sessions (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    template_path TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft' CHECK (
        status IN ('draft', 'reviewing', 'approved')
    ),
    minimum_required_per_case INTEGER NOT NULL DEFAULT 3 CHECK (
        minimum_required_per_case > 0
    ),
    created_by TEXT NOT NULL,
    submitted_by TEXT,
    approved_by TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE labeling_cases (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES labeling_sessions(id) ON DELETE CASCADE,
    case_id TEXT NOT NULL,
    source_path TEXT NOT NULL,
    source_sha256 TEXT NOT NULL,
    document_version_id TEXT NOT NULL REFERENCES document_versions(id),
    classification TEXT NOT NULL CHECK (
        classification IN ('public', 'internal', 'confidential', 'restricted')
    ),
    max_duplicate_rate REAL NOT NULL CHECK (
        max_duplicate_rate >= 0 AND max_duplicate_rate <= 1
    ),
    UNIQUE(session_id, case_id)
);

CREATE TABLE labeling_expected_evidence (
    case_row_id TEXT NOT NULL REFERENCES labeling_cases(id) ON DELETE CASCADE,
    evidence_id TEXT NOT NULL REFERENCES evidence(id),
    selected_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(case_row_id, evidence_id)
);

CREATE TABLE labeling_forbidden_substrings (
    id TEXT PRIMARY KEY,
    case_row_id TEXT NOT NULL REFERENCES labeling_cases(id) ON DELETE CASCADE,
    value TEXT NOT NULL CHECK (length(trim(value)) > 0),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(case_row_id, value)
);

CREATE INDEX idx_labeling_sessions_status
    ON labeling_sessions(status, updated_at);
CREATE INDEX idx_labeling_cases_session
    ON labeling_cases(session_id, case_id);
CREATE INDEX idx_labeling_expected_evidence
    ON labeling_expected_evidence(evidence_id);
"""


class ClosingConnection(sqlite3.Connection):
    """Makes ``with database.connect()`` close the file handle on Windows."""

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


class Database:
    def __init__(self, path: Path):
        self.path = path

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, factory=ClosingConnection)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    def initialize(self, applied_at: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.executescript(SCHEMA)
            connection.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                (1, applied_at),
            )
            applied = {
                row[0]
                for row in connection.execute(
                    "SELECT version FROM schema_migrations"
                ).fetchall()
            }
            if 2 not in applied:
                connection.executescript(MIGRATION_2)
                connection.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (2, applied_at),
                )
                applied.add(2)
            if 3 not in applied:
                connection.executescript(MIGRATION_3)
                connection.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (3, applied_at),
                )
                applied.add(3)
            if 4 not in applied:
                connection.executescript(MIGRATION_4)
                connection.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (4, applied_at),
                )
                applied.add(4)
            if 5 not in applied:
                connection.executescript(MIGRATION_5)
                connection.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (5, applied_at),
                )

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
