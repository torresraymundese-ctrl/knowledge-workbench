from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA_VERSION = 18


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


MIGRATION_6 = """
CREATE TABLE labeling_case_reviews (
    case_row_id TEXT PRIMARY KEY REFERENCES labeling_cases(id) ON DELETE CASCADE,
    reviewer TEXT NOT NULL CHECK (length(trim(reviewer)) > 0),
    decision TEXT NOT NULL CHECK (decision IN ('approved', 'rejected')),
    note TEXT,
    reviewed_at TEXT NOT NULL,
    CHECK (
        decision = 'approved'
        OR (note IS NOT NULL AND length(trim(note)) > 0)
    )
);

CREATE INDEX idx_labeling_case_reviews_reviewer
    ON labeling_case_reviews(reviewer, decision, reviewed_at);
"""


MIGRATION_7 = """
ALTER TABLE tasks ADD COLUMN lease_owner TEXT;

CREATE INDEX idx_tasks_lease_owner
    ON tasks(lease_owner, status);
"""


MIGRATION_8 = """
CREATE TABLE evidence_locations (
    evidence_id TEXT NOT NULL REFERENCES evidence(id) ON DELETE CASCADE,
    location_ordinal INTEGER NOT NULL CHECK (location_ordinal > 0),
    locator_json TEXT NOT NULL,
    PRIMARY KEY(evidence_id, location_ordinal),
    UNIQUE(evidence_id, locator_json)
);

INSERT INTO evidence_locations(evidence_id, location_ordinal, locator_json)
SELECT id, 1, locator_json FROM evidence;

CREATE INDEX idx_evidence_locations_evidence
    ON evidence_locations(evidence_id, location_ordinal);
"""


MIGRATION_9 = """
CREATE TABLE canonical_entities (
    id TEXT PRIMARY KEY,
    canonical_name TEXT NOT NULL CHECK (length(trim(canonical_name)) > 0),
    normalized_name TEXT NOT NULL CHECK (length(normalized_name) > 0),
    entity_type TEXT NOT NULL CHECK (
        entity_type IN (
            'person', 'organization', 'project', 'product',
            'location', 'concept', 'other'
        )
    ),
    status TEXT NOT NULL DEFAULT 'active' CHECK (
        status IN ('active', 'archived')
    ),
    created_by TEXT NOT NULL CHECK (length(trim(created_by)) > 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(normalized_name, entity_type),
    UNIQUE(id, entity_type)
);

CREATE TABLE entity_aliases (
    id TEXT PRIMARY KEY,
    entity_id TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    alias TEXT NOT NULL CHECK (length(trim(alias)) > 0),
    normalized_alias TEXT NOT NULL CHECK (length(normalized_alias) > 0),
    is_canonical INTEGER NOT NULL DEFAULT 0 CHECK (is_canonical IN (0, 1)),
    created_by TEXT NOT NULL CHECK (length(trim(created_by)) > 0),
    created_at TEXT NOT NULL,
    UNIQUE(entity_type, normalized_alias),
    UNIQUE(id, entity_id),
    FOREIGN KEY(entity_id, entity_type)
        REFERENCES canonical_entities(id, entity_type)
);

CREATE INDEX idx_entity_aliases_entity
    ON entity_aliases(entity_id, is_canonical DESC, normalized_alias);

CREATE TABLE evidence_entity_mentions (
    evidence_id TEXT NOT NULL REFERENCES evidence(id),
    entity_id TEXT NOT NULL REFERENCES canonical_entities(id),
    alias_id TEXT NOT NULL,
    mention_text TEXT NOT NULL CHECK (length(trim(mention_text)) > 0),
    created_by TEXT NOT NULL CHECK (length(trim(created_by)) > 0),
    created_at TEXT NOT NULL,
    PRIMARY KEY(evidence_id, alias_id),
    FOREIGN KEY(alias_id, entity_id)
        REFERENCES entity_aliases(id, entity_id)
);

CREATE INDEX idx_evidence_entity_mentions_entity
    ON evidence_entity_mentions(entity_id, evidence_id);
"""


MIGRATION_10 = """
CREATE TABLE entity_candidates (
    id TEXT PRIMARY KEY,
    analysis_sha256 TEXT NOT NULL CHECK (length(analysis_sha256) = 64),
    document_version_id TEXT NOT NULL REFERENCES document_versions(id),
    processing_run_id TEXT NOT NULL REFERENCES processing_runs(id),
    evidence_id TEXT NOT NULL REFERENCES evidence(id),
    source_candidate_id TEXT NOT NULL,
    suggested_name TEXT NOT NULL CHECK (length(trim(suggested_name)) > 0),
    normalized_name TEXT NOT NULL CHECK (length(normalized_name) > 0),
    suggested_type TEXT NOT NULL CHECK (length(trim(suggested_type)) > 0),
    verbatim_match INTEGER NOT NULL CHECK (verbatim_match IN (0, 1)),
    provider TEXT NOT NULL CHECK (length(trim(provider)) > 0),
    model TEXT,
    prompt_version TEXT NOT NULL CHECK (length(trim(prompt_version)) > 0),
    status TEXT NOT NULL DEFAULT 'pending' CHECK (
        status IN ('pending', 'accepted', 'rejected')
    ),
    resolved_entity_id TEXT REFERENCES canonical_entities(id),
    imported_by TEXT NOT NULL CHECK (length(trim(imported_by)) > 0),
    reviewed_by TEXT,
    review_note_sha256 TEXT,
    created_at TEXT NOT NULL,
    reviewed_at TEXT,
    UNIQUE(
        analysis_sha256, evidence_id, normalized_name, suggested_type
    ),
    CHECK (
        (status = 'pending' AND resolved_entity_id IS NULL
         AND reviewed_by IS NULL AND reviewed_at IS NULL)
        OR
        (status = 'accepted' AND resolved_entity_id IS NOT NULL
         AND reviewed_by IS NOT NULL AND reviewed_at IS NOT NULL)
        OR
        (status = 'rejected' AND resolved_entity_id IS NULL
         AND reviewed_by IS NOT NULL AND review_note_sha256 IS NOT NULL
         AND reviewed_at IS NOT NULL)
    )
);

CREATE INDEX idx_entity_candidates_review
    ON entity_candidates(status, created_at, id);
CREATE INDEX idx_entity_candidates_evidence
    ON entity_candidates(evidence_id, status);
"""


MIGRATION_11 = """
ALTER TABLE entity_aliases
    ADD COLUMN is_merge_anchor INTEGER NOT NULL DEFAULT 0
    CHECK (is_merge_anchor IN (0, 1));

CREATE TABLE entity_merge_requests (
    id TEXT PRIMARY KEY,
    source_entity_id TEXT NOT NULL REFERENCES canonical_entities(id),
    target_entity_id TEXT NOT NULL REFERENCES canonical_entities(id),
    entity_type TEXT NOT NULL CHECK (
        entity_type IN (
            'person', 'organization', 'project', 'product',
            'location', 'concept', 'other'
        )
    ),
    status TEXT NOT NULL DEFAULT 'reviewing' CHECK (
        status IN ('reviewing', 'merged', 'rejected')
    ),
    proposed_by TEXT NOT NULL CHECK (length(trim(proposed_by)) > 0),
    proposal_note_sha256 TEXT NOT NULL CHECK (length(proposal_note_sha256) = 64),
    reviewed_by TEXT,
    review_note_sha256 TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    reviewed_at TEXT,
    CHECK (source_entity_id != target_entity_id),
    CHECK (
        (status = 'reviewing' AND reviewed_by IS NULL
         AND review_note_sha256 IS NULL AND reviewed_at IS NULL)
        OR
        (status IN ('merged', 'rejected') AND reviewed_by IS NOT NULL
         AND review_note_sha256 IS NOT NULL AND reviewed_at IS NOT NULL)
    )
);

CREATE INDEX idx_entity_merge_requests_review
    ON entity_merge_requests(status, created_at, id);
CREATE INDEX idx_entity_merge_requests_source
    ON entity_merge_requests(source_entity_id, status);
CREATE INDEX idx_entity_merge_requests_target
    ON entity_merge_requests(target_entity_id, status);
"""


MIGRATION_12 = """
CREATE TABLE entity_relation_types (
    relation_key TEXT PRIMARY KEY
        CHECK (length(trim(relation_key)) > 0),
    label TEXT NOT NULL CHECK (length(trim(label)) > 0),
    inverse_label TEXT,
    directed INTEGER NOT NULL CHECK (directed IN (0, 1)),
    status TEXT NOT NULL DEFAULT 'active' CHECK (
        status IN ('active', 'archived')
    ),
    created_by TEXT NOT NULL CHECK (length(trim(created_by)) > 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE entity_relationships (
    id TEXT PRIMARY KEY,
    relation_key TEXT NOT NULL REFERENCES entity_relation_types(relation_key),
    source_entity_id TEXT NOT NULL REFERENCES canonical_entities(id),
    target_entity_id TEXT NOT NULL REFERENCES canonical_entities(id),
    status TEXT NOT NULL DEFAULT 'active' CHECK (
        status IN ('active', 'retracted')
    ),
    created_by TEXT NOT NULL CHECK (length(trim(created_by)) > 0),
    creation_note_sha256 TEXT NOT NULL CHECK (
        length(creation_note_sha256) = 64
    ),
    retracted_by TEXT,
    retraction_note_sha256 TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    retracted_at TEXT,
    CHECK (source_entity_id != target_entity_id),
    CHECK (
        (status = 'active' AND retracted_by IS NULL
         AND retraction_note_sha256 IS NULL AND retracted_at IS NULL)
        OR
        (status = 'retracted' AND retracted_by IS NOT NULL
         AND retraction_note_sha256 IS NOT NULL AND retracted_at IS NOT NULL)
    )
);

CREATE UNIQUE INDEX idx_entity_relationships_active_unique
    ON entity_relationships(relation_key, source_entity_id, target_entity_id)
    WHERE status = 'active';
CREATE INDEX idx_entity_relationships_source
    ON entity_relationships(source_entity_id, status, relation_key);
CREATE INDEX idx_entity_relationships_target
    ON entity_relationships(target_entity_id, status, relation_key);

CREATE TABLE entity_relationship_evidence (
    relationship_id TEXT NOT NULL
        REFERENCES entity_relationships(id) ON DELETE CASCADE,
    evidence_id TEXT NOT NULL REFERENCES evidence(id),
    added_by TEXT NOT NULL CHECK (length(trim(added_by)) > 0),
    added_at TEXT NOT NULL,
    PRIMARY KEY(relationship_id, evidence_id)
);

CREATE INDEX idx_entity_relationship_evidence_evidence
    ON entity_relationship_evidence(evidence_id, relationship_id);
"""

MIGRATION_13 = """
CREATE TABLE nas_discoveries (
    id TEXT PRIMARY KEY,
    source_root TEXT NOT NULL CHECK (length(trim(source_root)) > 0),
    relative_path TEXT NOT NULL CHECK (length(trim(relative_path)) > 0),
    file_name TEXT NOT NULL CHECK (length(trim(file_name)) > 0),
    extension TEXT NOT NULL CHECK (length(trim(extension)) > 0),
    sha256 TEXT NOT NULL CHECK (length(sha256) = 64),
    size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0),
    modified_at_ns INTEGER NOT NULL CHECK (modified_at_ns >= 0),
    project TEXT,
    status TEXT NOT NULL DEFAULT 'discovered' CHECK (
        status IN ('discovered', 'admitted', 'ignored', 'imported')
    ),
    classification TEXT CHECK (
        classification IS NULL OR
        classification IN ('public', 'internal', 'confidential', 'restricted')
    ),
    discovered_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    decided_at TEXT,
    decided_by TEXT,
    decision_reason TEXT,
    imported_at TEXT,
    imported_by TEXT,
    document_version_id TEXT REFERENCES document_versions(id),
    UNIQUE(source_root, relative_path, sha256),
    CHECK (
        (status = 'discovered'
         AND classification IS NULL
         AND decided_at IS NULL AND decided_by IS NULL
         AND decision_reason IS NULL
         AND imported_at IS NULL AND imported_by IS NULL
         AND document_version_id IS NULL)
        OR
        (status = 'admitted'
         AND classification IS NOT NULL
         AND decided_at IS NOT NULL AND decided_by IS NOT NULL
         AND decision_reason IS NOT NULL
         AND imported_at IS NULL AND imported_by IS NULL
         AND document_version_id IS NULL)
        OR
        (status = 'ignored'
         AND classification IS NULL
         AND decided_at IS NOT NULL AND decided_by IS NOT NULL
         AND decision_reason IS NOT NULL
         AND imported_at IS NULL AND imported_by IS NULL
         AND document_version_id IS NULL)
        OR
        (status = 'imported'
         AND classification IS NOT NULL
         AND decided_at IS NOT NULL AND decided_by IS NOT NULL
         AND decision_reason IS NOT NULL
         AND imported_at IS NOT NULL AND imported_by IS NOT NULL
         AND document_version_id IS NOT NULL)
    )
);

CREATE INDEX idx_nas_discoveries_status
    ON nas_discoveries(status, discovered_at, id);
CREATE INDEX idx_nas_discoveries_path
    ON nas_discoveries(source_root, relative_path, discovered_at);
CREATE INDEX idx_nas_discoveries_sha256
    ON nas_discoveries(sha256, status);
"""

MIGRATION_14 = """
ALTER TABLE nas_discoveries
    ADD COLUMN risk_flags_json TEXT NOT NULL DEFAULT '[]'
    CHECK (
        json_valid(risk_flags_json)
        AND json_type(risk_flags_json) = 'array'
    );

UPDATE nas_discoveries
SET risk_flags_json = '["credential_material"]'
WHERE instr(file_name, '账号密码') > 0
   OR instr(file_name, '帐号密码') > 0
   OR instr(file_name, '账户密码') > 0
   OR instr(file_name, '用户密码') > 0
   OR instr(file_name, '账号口令') > 0
   OR instr(file_name, '账户口令') > 0
   OR lower(file_name) GLOB '*account*password*'
   OR lower(file_name) GLOB '*user*password*'
   OR lower(file_name) GLOB '*password*list*'
   OR lower(file_name) GLOB '*credential*list*'
   OR lower(file_name) GLOB 'passwords.*'
   OR lower(file_name) GLOB 'credentials.*'
   OR lower(file_name) GLOB 'secrets.*';
"""

MIGRATION_15 = """
CREATE TABLE document_governance (
    document_id TEXT PRIMARY KEY REFERENCES documents(id) ON DELETE CASCADE,
    purpose TEXT NOT NULL CHECK (
        purpose IN ('development_fixture', 'candidate', 'production')
    ),
    scope_status TEXT NOT NULL CHECK (
        scope_status IN ('unreviewed', 'in_scope', 'out_of_scope')
    ),
    authority_status TEXT NOT NULL CHECK (
        authority_status IN (
            'unknown', 'reference', 'authoritative', 'superseded'
        )
    ),
    reviewed_by TEXT,
    reviewed_at TEXT,
    decision_reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

INSERT INTO document_governance(
    document_id, purpose, scope_status, authority_status,
    reviewed_by, reviewed_at, decision_reason, created_at, updated_at
)
SELECT id, 'development_fixture', 'unreviewed', 'unknown',
       NULL, NULL, NULL, created_at, updated_at
FROM documents;

CREATE TABLE evidence_technical_validation (
    evidence_id TEXT PRIMARY KEY REFERENCES evidence(id) ON DELETE CASCADE,
    status TEXT NOT NULL CHECK (status IN ('pending', 'passed', 'failed')),
    validator TEXT NOT NULL,
    checks_json TEXT NOT NULL DEFAULT '{}'
        CHECK (json_valid(checks_json) AND json_type(checks_json) = 'object'),
    validated_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

INSERT INTO evidence_technical_validation(
    evidence_id, status, validator, checks_json,
    validated_at, created_at, updated_at
)
SELECT id,
       CASE WHEN status IN ('verified', 'conflicted', 'deprecated', 'archived')
            THEN 'passed' ELSE 'pending' END,
       'migration-v15-existing-state',
       '{"source_state":"preserved"}',
       CASE WHEN status IN ('verified', 'conflicted', 'deprecated', 'archived')
            THEN updated_at ELSE NULL END,
       created_at,
       updated_at
FROM evidence;

CREATE TABLE corpus_scans (
    id TEXT PRIMARY KEY,
    source_root TEXT NOT NULL CHECK (length(trim(source_root)) > 0),
    root_label TEXT NOT NULL CHECK (length(trim(root_label)) > 0),
    project_name TEXT,
    status TEXT NOT NULL CHECK (status IN ('completed', 'failed')),
    is_current INTEGER NOT NULL DEFAULT 1 CHECK (is_current IN (0, 1)),
    file_count INTEGER NOT NULL CHECK (file_count >= 0),
    folder_count INTEGER NOT NULL CHECK (folder_count >= 0),
    total_size_bytes INTEGER NOT NULL CHECK (total_size_bytes >= 0),
    readable_card_count INTEGER NOT NULL CHECK (readable_card_count >= 0),
    metadata_only_count INTEGER NOT NULL CHECK (metadata_only_count >= 0),
    blocked_count INTEGER NOT NULL CHECK (blocked_count >= 0),
    unreadable_count INTEGER NOT NULL CHECK (unreadable_count >= 0),
    duplicate_group_count INTEGER NOT NULL CHECK (duplicate_group_count >= 0),
    version_group_count INTEGER NOT NULL CHECK (version_group_count >= 0),
    actor TEXT NOT NULL CHECK (length(trim(actor)) > 0),
    created_at TEXT NOT NULL,
    completed_at TEXT NOT NULL
);

CREATE INDEX idx_corpus_scans_current
    ON corpus_scans(is_current, completed_at DESC, id);

CREATE TABLE corpus_files (
    id TEXT PRIMARY KEY,
    scan_id TEXT NOT NULL REFERENCES corpus_scans(id) ON DELETE CASCADE,
    relative_path TEXT NOT NULL CHECK (length(trim(relative_path)) > 0),
    folder_path TEXT NOT NULL,
    file_name TEXT NOT NULL CHECK (length(trim(file_name)) > 0),
    extension TEXT NOT NULL,
    sha256 TEXT NOT NULL CHECK (length(sha256) = 64),
    size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0),
    modified_at_ns INTEGER NOT NULL CHECK (modified_at_ns >= 0),
    map_status TEXT NOT NULL CHECK (
        map_status IN ('readable', 'metadata_only', 'blocked', 'unreadable')
    ),
    document_type TEXT NOT NULL CHECK (length(trim(document_type)) > 0),
    display_title TEXT NOT NULL CHECK (length(trim(display_title)) > 0),
    plain_summary TEXT NOT NULL CHECK (length(trim(plain_summary)) > 0),
    outline_json TEXT NOT NULL DEFAULT '[]'
        CHECK (json_valid(outline_json) AND json_type(outline_json) = 'array'),
    key_signals_json TEXT NOT NULL DEFAULT '{}'
        CHECK (json_valid(key_signals_json) AND json_type(key_signals_json) = 'object'),
    risk_flags_json TEXT NOT NULL DEFAULT '[]'
        CHECK (json_valid(risk_flags_json) AND json_type(risk_flags_json) = 'array'),
    parser_name TEXT,
    parser_version TEXT,
    duplicate_group TEXT,
    version_group TEXT,
    scope_status TEXT NOT NULL DEFAULT 'unreviewed' CHECK (
        scope_status IN ('unreviewed', 'in_scope', 'out_of_scope')
    ),
    authority_status TEXT NOT NULL DEFAULT 'unknown' CHECK (
        authority_status IN (
            'unknown', 'reference', 'authoritative', 'superseded'
        )
    ),
    decided_by TEXT,
    decided_at TEXT,
    decision_reason TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(scan_id, relative_path)
);

CREATE INDEX idx_corpus_files_scan_folder
    ON corpus_files(scan_id, folder_path, relative_path);
CREATE INDEX idx_corpus_files_scan_status
    ON corpus_files(scan_id, scope_status, map_status, relative_path);
CREATE INDEX idx_corpus_files_sha256
    ON corpus_files(scan_id, sha256);

CREATE TABLE corpus_file_relations (
    id TEXT PRIMARY KEY,
    scan_id TEXT NOT NULL REFERENCES corpus_scans(id) ON DELETE CASCADE,
    source_file_id TEXT NOT NULL REFERENCES corpus_files(id) ON DELETE CASCADE,
    target_file_id TEXT NOT NULL REFERENCES corpus_files(id) ON DELETE CASCADE,
    relation_type TEXT NOT NULL CHECK (
        relation_type IN ('exact_duplicate', 'version_candidate')
    ),
    plain_reason TEXT NOT NULL CHECK (length(trim(plain_reason)) > 0),
    created_at TEXT NOT NULL,
    CHECK (source_file_id != target_file_id),
    UNIQUE(scan_id, source_file_id, target_file_id, relation_type)
);

CREATE INDEX idx_corpus_file_relations_source
    ON corpus_file_relations(scan_id, source_file_id, relation_type);
CREATE INDEX idx_corpus_file_relations_target
    ON corpus_file_relations(scan_id, target_file_id, relation_type);
"""


MIGRATION_16 = """
ALTER TABLE document_governance
    ADD COLUMN knowledge_domain TEXT NOT NULL DEFAULT 'business' CHECK (
        knowledge_domain IN (
            'business', 'policy', 'technical',
            'template', 'example', 'process'
        )
    );

UPDATE document_governance
SET knowledge_domain = CASE
    WHEN document_id IN (
        SELECT id FROM documents
        WHERE instr(replace(source_path, char(92), '/'), '/政策文件/') > 0
           OR instr(original_name, '教育部') > 0
           OR instr(original_name, '文旅部') > 0
           OR instr(original_name, '文化和旅游部') > 0
           OR instr(original_name, '教育局') > 0
           OR instr(original_name, '服务要求') > 0
           OR instr(original_name, '安全规范') > 0
           OR instr(original_name, '示范合同') > 0
    ) THEN 'policy'
    WHEN document_id IN (
        SELECT id FROM documents
        WHERE instr(original_name, '演示') > 0
           OR instr(original_name, '示范研学路线') > 0
    ) THEN 'example'
    WHEN document_id IN (
        SELECT id FROM documents
        WHERE instr(original_name, '模板') > 0
           OR instr(original_name, '待确认') > 0
           OR instr(original_name, '核对清单') > 0
    ) THEN 'template'
    WHEN document_id IN (
        SELECT id FROM documents
        WHERE lower(original_name) = 'product.md'
           OR lower(original_name) LIKE 'prd%'
    ) THEN 'business'
    WHEN document_id IN (
        SELECT id FROM documents
        WHERE instr(replace(source_path, char(92), '/'), '/工作计划/') > 0
           OR instr(replace(source_path, char(92), '/'), '/archive/') > 0
           OR instr(replace(source_path, char(92), '/'), '/archives/') > 0
           OR instr(replace(source_path, char(92), '/'), '/tests/') > 0
           OR instr(replace(source_path, char(92), '/'), '/docs/qa/') > 0
           OR lower(original_name) IN (
               'agents.md', 'claude.md', 'design-qa.md',
               'frontend-ui-v1.0.md', 'readme.md', 'readme-运行说明.md'
           )
           OR instr(original_name, '工作计划') > 0
           OR instr(original_name, '交付清单') > 0
           OR instr(original_name, '交付说明') > 0
           OR instr(original_name, '验收报告') > 0
           OR instr(original_name, '里程碑报告') > 0
           OR instr(original_name, '完成度评审') > 0
    ) THEN 'process'
    WHEN document_id IN (
        SELECT id FROM documents
        WHERE instr(replace(source_path, char(92), '/'), '/开发数据/') > 0
           OR instr(replace(source_path, char(92), '/'), '/ddl/') > 0
           OR lower(original_name) LIKE '%.sql'
           OR instr(original_name, '接口设计') > 0
           OR instr(original_name, '技术架构') > 0
           OR instr(original_name, '技术选型') > 0
           OR instr(original_name, '数据字典') > 0
           OR instr(original_name, '时序图') > 0
           OR instr(original_name, '状态机') > 0
    ) THEN 'technical'
    ELSE 'business'
END;
CREATE INDEX idx_document_governance_domain
    ON document_governance(knowledge_domain, purpose, scope_status);
"""

MIGRATION_17 = """
UPDATE document_governance
SET knowledge_domain = 'business'
WHERE document_id IN (
    SELECT id
    FROM documents
    WHERE lower(original_name) = 'product.md'
       OR lower(original_name) LIKE 'prd%'
);

UPDATE document_governance
SET knowledge_domain = 'process'
WHERE document_id IN (
    SELECT id
    FROM documents
    WHERE instr(original_name, '交付说明') > 0
);
"""

MIGRATION_18 = """
ALTER TABLE corpus_files
    ADD COLUMN content_preview_json TEXT NOT NULL DEFAULT '[]'
    CHECK (
        json_valid(content_preview_json)
        AND json_type(content_preview_json) = 'array'
    );
"""


class ClosingConnection(sqlite3.Connection):
    """Makes ``with database.connect()`` close the file handle on Windows."""

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


def _apply_schema_migration(
    connection: sqlite3.Connection,
    *,
    version: int,
    script: str,
    applied_at: str,
    allow_existing: bool = False,
) -> None:
    """Apply schema DDL and its version marker in one SQLite transaction."""
    marker = (
        "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)"
        if allow_existing
        else "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)"
    )
    try:
        connection.executescript(f"BEGIN IMMEDIATE;\n{script}")
        connection.execute(marker, (version, applied_at))
        connection.commit()
    except Exception:
        connection.rollback()
        raise


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
            _apply_schema_migration(
                connection,
                version=1,
                script=SCHEMA,
                applied_at=applied_at,
                allow_existing=True,
            )
            applied = {
                row[0]
                for row in connection.execute(
                    "SELECT version FROM schema_migrations"
                ).fetchall()
            }
            migrations = (
                (2, MIGRATION_2),
                (3, MIGRATION_3),
                (4, MIGRATION_4),
                (5, MIGRATION_5),
                (6, MIGRATION_6),
                (7, MIGRATION_7),
                (8, MIGRATION_8),
                (9, MIGRATION_9),
                (10, MIGRATION_10),
                (11, MIGRATION_11),
                (12, MIGRATION_12),
                (13, MIGRATION_13),
                (14, MIGRATION_14),
                (15, MIGRATION_15),
                (16, MIGRATION_16),
                (17, MIGRATION_17),
                (18, MIGRATION_18),
            )
            for version, script in migrations:
                if version in applied:
                    continue
                _apply_schema_migration(
                    connection,
                    version=version,
                    script=script,
                    applied_at=applied_at,
                )
                applied.add(version)

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
