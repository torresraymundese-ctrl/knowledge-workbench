from __future__ import annotations

import json
import mimetypes
import os
import shutil
import stat
from pathlib import Path

from .audit import record_event
from .config import WorkspacePaths
from .conflicts import detect_version_conflicts
from .database import Database
from .errors import KnowledgeWorkbenchError
from .models import Classification, EvidenceCandidate, IngestResult
from .parsers import parse_document
from .pipeline import faithful_analysis, faithful_wiki_generation
from .policy import most_restrictive
from .utils import new_id, sha256_file, sha256_text, slugify, utc_now
from .wiki import RenderedEvidence, render_draft, write_text_atomic


EXTRACTION_METHOD = "faithful-schema-v1"


def initialize_workspace(paths: WorkspacePaths) -> Database:
    paths.create()
    database = Database(paths.database)
    database.initialize(utc_now())
    return database


def ingest_file(
    source: Path,
    paths: WorkspacePaths,
    classification: Classification,
    *,
    actor: str = "cli",
    reprocess: bool = False,
    allow_legacy_word_conversion: bool = False,
) -> IngestResult:
    source = source.expanduser().resolve()
    if not source.is_file():
        raise KnowledgeWorkbenchError(f"文件不存在：{source}")

    database = initialize_workspace(paths)
    digest = sha256_file(source)
    normalized_source = os.path.normcase(str(source))

    with database.connect() as connection:
        duplicate = connection.execute(
            """
            SELECT dv.id AS version_id, dv.document_id, pr.id AS processing_run_id,
                   (SELECT COUNT(*) FROM evidence e
                    WHERE e.processing_run_id = pr.id) AS evidence_count,
                   wp.id AS page_id,
                   (SELECT wr.id FROM wiki_revisions wr
                    WHERE wr.page_id = wp.id AND wr.processing_run_id = pr.id
                    ORDER BY wr.revision_number DESC LIMIT 1) AS revision_id
            FROM document_versions dv
            LEFT JOIN processing_runs pr
                   ON pr.document_version_id = dv.id AND pr.is_current = 1
            LEFT JOIN wiki_pages wp ON wp.source_document_id = dv.document_id
            WHERE dv.sha256 = ?
            """,
            (digest,),
        ).fetchone()
    if duplicate:
        if reprocess:
            return _reprocess_existing(
                source,
                paths,
                database,
                duplicate,
                digest,
                classification,
                actor=actor,
                allow_legacy_word_conversion=allow_legacy_word_conversion,
            )
        with database.transaction() as connection:
            record_event(
                connection,
                "duplicate_import_skipped",
                "document_version",
                duplicate["version_id"],
                actor=actor,
                details={"incoming_path": normalized_source, "sha256": digest},
            )
        return IngestResult(
            document_id=duplicate["document_id"],
            version_id=duplicate["version_id"],
            sha256=digest,
            duplicate=True,
            evidence_count=duplicate["evidence_count"],
            page_id=duplicate["page_id"],
            revision_id=duplicate["revision_id"],
            processing_run_id=duplicate["processing_run_id"],
        )

    with database.connect() as connection:
        existing_document = connection.execute(
            "SELECT classification FROM documents WHERE source_path = ?",
            (normalized_source,),
        ).fetchone()
    analysis_classification = (
        most_restrictive(
            Classification(existing_document["classification"]), classification
        )
        if existing_document
        else classification
    )

    version_id = new_id("ver")
    parsed = parse_document(
        source,
        allow_legacy_word_conversion=allow_legacy_word_conversion,
    )
    analysis = faithful_analysis(
        parsed,
        document_version_id=version_id,
        source_sha256=digest,
        classification=analysis_classification,
    )
    candidates = tuple(
        EvidenceCandidate(
            excerpt=item["excerpt"],
            locator=item["locator"],
            extraction_method=EXTRACTION_METHOD,
        )
        for item in analysis["evidence"]
    )
    if not candidates:
        raise KnowledgeWorkbenchError(f"文件 {source.name} 未生成任何原子证据")

    now = utc_now()
    processing_run_id = new_id("run")
    evidence_ids = tuple(new_id("ev") for _ in candidates)
    copied_path: Path | None = None
    draft_path: Path | None = None
    mirror_path: Path | None = None
    analysis_path: Path | None = None
    generation_path: Path | None = None

    try:
        with database.transaction() as connection:
            document = connection.execute(
                "SELECT * FROM documents WHERE source_path = ?",
                (normalized_source,),
            ).fetchone()
            if document:
                document_id = document["id"]
                previous_version_id = document["current_version_id"]
                stored_classification = most_restrictive(
                    Classification(document["classification"]), classification
                )
                connection.execute(
                    "UPDATE documents SET classification = ?, updated_at = ? WHERE id = ?",
                    (stored_classification.value, now, document_id),
                )
            else:
                document_id = new_id("doc")
                previous_version_id = None
                stored_classification = classification
                connection.execute(
                    """
                    INSERT INTO documents(
                        id, original_name, source_path, classification,
                        current_version_id, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, NULL, ?, ?)
                    """,
                    (
                        document_id,
                        source.name,
                        normalized_source,
                        classification.value,
                        now,
                        now,
                    ),
                )

            raw_directory = paths.raw / document_id
            raw_directory.mkdir(parents=True, exist_ok=True)
            copied_path = raw_directory / f"{digest}{source.suffix.lower()}"
            shutil.copy2(source, copied_path)
            copied_path.chmod(stat.S_IREAD | stat.S_IRGRP | stat.S_IROTH)
            relative_raw_path = copied_path.relative_to(paths.root).as_posix()

            connection.execute(
                """
                INSERT INTO document_versions(
                    id, document_id, sha256, stored_path, size_bytes, media_type,
                    parser_name, parser_version, status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'parsed', ?)
                """,
                (
                    version_id,
                    document_id,
                    digest,
                    relative_raw_path,
                    source.stat().st_size,
                    mimetypes.guess_type(source.name)[0],
                    parsed.parser_name,
                    parsed.parser_version,
                    now,
                ),
            )
            connection.execute(
                "UPDATE documents SET current_version_id = ?, updated_at = ? WHERE id = ?",
                (version_id, now, document_id),
            )
            connection.execute(
                """
                INSERT INTO processing_runs(
                    id, document_version_id, parser_name, parser_version,
                    extraction_method, status, is_current, created_at, completed_at
                ) VALUES (?, ?, ?, ?, ?, 'completed', 1, ?, ?)
                """,
                (
                    processing_run_id,
                    version_id,
                    parsed.parser_name,
                    parsed.parser_version,
                    EXTRACTION_METHOD,
                    now,
                    now,
                ),
            )

            for ordinal, (evidence_id, candidate) in enumerate(
                zip(evidence_ids, candidates, strict=True), start=1
            ):
                connection.execute(
                    """
                    INSERT INTO evidence(
                        id, document_version_id, processing_run_id, ordinal, run_ordinal,
                        excerpt, locator_json,
                        extraction_method, extraction_model, status, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'draft', ?, ?)
                    """,
                    (
                        evidence_id,
                        version_id,
                        processing_run_id,
                        ordinal,
                        ordinal,
                        candidate.excerpt,
                        json.dumps(candidate.locator, ensure_ascii=False, sort_keys=True),
                        candidate.extraction_method,
                        candidate.extraction_model,
                        now,
                        now,
                    ),
                )
                connection.execute(
                    "INSERT INTO evidence_fts(excerpt, evidence_id) VALUES (?, ?)",
                    (candidate.excerpt, evidence_id),
                )

            conflict_ids = detect_version_conflicts(
                connection,
                document_id=document_id,
                older_version_id=previous_version_id,
                newer_version_id=version_id,
                actor=actor,
            )

            page = connection.execute(
                "SELECT * FROM wiki_pages WHERE source_document_id = ?",
                (document_id,),
            ).fetchone()
            if page:
                page_id = page["id"]
                slug = page["slug"]
                _mark_revalidation_required(
                    connection,
                    page_id=page_id,
                    actor=actor,
                    reason="document_version_changed",
                    cause_id=version_id,
                    now=now,
                )
                revision_number = connection.execute(
                    "SELECT COALESCE(MAX(revision_number), 0) + 1 FROM wiki_revisions WHERE page_id = ?",
                    (page_id,),
                ).fetchone()[0]
            else:
                page_id = new_id("page")
                slug = _available_slug(connection, slugify(source.stem), document_id)
                revision_number = 1
                connection.execute(
                    """
                    INSERT INTO wiki_pages(
                        id, source_document_id, slug, title, status,
                        current_verified_revision_id, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, 'draft', NULL, ?, ?)
                    """,
                    (page_id, document_id, slug, source.stem, now, now),
                )

            revision_id = new_id("rev")
            generation = faithful_wiki_generation(analysis, title=source.stem)
            rendered_evidence = tuple(
                RenderedEvidence(evidence_id, candidate)
                for evidence_id, candidate in zip(evidence_ids, candidates, strict=True)
            )
            content = render_draft(
                title=source.stem,
                page_id=page_id,
                revision_id=revision_id,
                revision_number=revision_number,
                document_version_id=version_id,
                source_name=source.name,
                source_sha256=digest,
                classification=stored_classification.value,
                generated_at=now,
                evidence=rendered_evidence,
            )
            draft_path = paths.wiki_drafts / f"{slug}--r{revision_number}.md"
            write_text_atomic(draft_path, content)
            relative_draft_path = draft_path.relative_to(paths.root).as_posix()
            connection.execute(
                """
                INSERT INTO wiki_revisions(
                    id, page_id, revision_number, status, markdown_path,
                    content_sha256, generator, processing_run_id, created_at, updated_at
                ) VALUES (?, ?, ?, 'draft', ?, ?, 'faithful-draft-v1', ?, ?, ?)
                """,
                (
                    revision_id,
                    page_id,
                    revision_number,
                    relative_draft_path,
                    sha256_text(content),
                    processing_run_id,
                    now,
                    now,
                ),
            )
            connection.executemany(
                "INSERT INTO revision_evidence(revision_id, evidence_id) VALUES (?, ?)",
                ((revision_id, evidence_id) for evidence_id in evidence_ids),
            )

            mirror_path = paths.evidence / f"{processing_run_id}.jsonl"
            mirror_content = "".join(
                json.dumps(
                    {
                        "id": evidence_id,
                        "document_version_id": version_id,
                        "processing_run_id": processing_run_id,
                        "source_sha256": digest,
                        "excerpt": candidate.excerpt,
                        "locator": candidate.locator,
                        "extraction_method": candidate.extraction_method,
                        "status": "draft",
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
                + "\n"
                for evidence_id, candidate in zip(evidence_ids, candidates, strict=True)
            )
            write_text_atomic(mirror_path, mirror_content)

            analysis_path = paths.analysis / f"{processing_run_id}.analysis.json"
            generation_path = paths.analysis / f"{revision_id}.wiki-generation.json"
            write_text_atomic(
                analysis_path,
                json.dumps(analysis, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            )
            write_text_atomic(
                generation_path,
                json.dumps(generation, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            )

            record_event(
                connection,
                "document_imported",
                "document_version",
                version_id,
                actor=actor,
                details={
                    "document_id": document_id,
                    "source_path": normalized_source,
                    "sha256": digest,
                    "classification": stored_classification.value,
                    "evidence_count": len(candidates),
                    "parser": parsed.parser_name,
                    "parser_version": parsed.parser_version,
                    "processing_run_id": processing_run_id,
                    "potential_conflict_count": len(conflict_ids),
                    "analysis_schema_version": analysis["schema_version"],
                },
            )
            record_event(
                connection,
                "wiki_draft_created",
                "wiki_revision",
                revision_id,
                actor=actor,
                details={"page_id": page_id, "revision": revision_number},
            )
    except Exception:
        for generated in (
            draft_path,
            mirror_path,
            analysis_path,
            generation_path,
            copied_path,
        ):
            if generated and generated.exists():
                generated.chmod(stat.S_IWRITE | stat.S_IREAD)
                generated.unlink()
        raise

    return IngestResult(
        document_id=document_id,
        version_id=version_id,
        sha256=digest,
        duplicate=False,
        evidence_count=len(candidates),
        page_id=page_id,
        revision_id=revision_id,
        conflict_count=len(conflict_ids),
        processing_run_id=processing_run_id,
    )


def _reprocess_existing(
    source: Path,
    paths: WorkspacePaths,
    database: Database,
    duplicate,
    digest: str,
    classification: Classification,
    *,
    actor: str,
    allow_legacy_word_conversion: bool,
) -> IngestResult:
    version_id = duplicate["version_id"]
    document_id = duplicate["document_id"]
    parsed = parse_document(
        source,
        allow_legacy_word_conversion=allow_legacy_word_conversion,
    )

    with database.connect() as connection:
        document = connection.execute(
            "SELECT classification FROM documents WHERE id = ?", (document_id,)
        ).fetchone()
        matching_run = connection.execute(
            """
            SELECT pr.id, pr.is_current,
                   (SELECT COUNT(*) FROM evidence e
                    WHERE e.processing_run_id = pr.id) AS evidence_count,
                   wp.id AS page_id,
                   (SELECT wr.id FROM wiki_revisions wr
                    WHERE wr.processing_run_id = pr.id
                    ORDER BY wr.revision_number DESC LIMIT 1) AS revision_id
            FROM processing_runs pr
            LEFT JOIN document_versions dv ON dv.id = pr.document_version_id
            LEFT JOIN wiki_pages wp ON wp.source_document_id = dv.document_id
            WHERE pr.document_version_id = ?
              AND pr.parser_name = ?
              AND pr.parser_version = ?
              AND pr.extraction_method = ?
            """,
            (
                version_id,
                parsed.parser_name,
                parsed.parser_version,
                EXTRACTION_METHOD,
            ),
        ).fetchone()
    if document is None:
        raise KnowledgeWorkbenchError(f"文档不存在：{document_id}")
    if matching_run:
        if not matching_run["is_current"]:
            raise KnowledgeWorkbenchError(
                "相同解析器与提取器的历史结果已存在但不是当前结果；"
                "系统不会自动回滚派生版本"
            )
        with database.transaction() as connection:
            record_event(
                connection,
                "reprocess_skipped_same_pipeline",
                "processing_run",
                matching_run["id"],
                actor=actor,
                details={
                    "document_version_id": version_id,
                    "sha256": digest,
                    "parser": parsed.parser_name,
                    "parser_version": parsed.parser_version,
                    "extraction_method": EXTRACTION_METHOD,
                },
            )
        return IngestResult(
            document_id=document_id,
            version_id=version_id,
            sha256=digest,
            duplicate=True,
            evidence_count=matching_run["evidence_count"],
            page_id=matching_run["page_id"],
            revision_id=matching_run["revision_id"],
            processing_run_id=matching_run["id"],
        )

    stored_classification = most_restrictive(
        Classification(document["classification"]), classification
    )
    analysis = faithful_analysis(
        parsed,
        document_version_id=version_id,
        source_sha256=digest,
        classification=stored_classification,
    )
    candidates = tuple(
        EvidenceCandidate(
            excerpt=item["excerpt"],
            locator=item["locator"],
            extraction_method=EXTRACTION_METHOD,
        )
        for item in analysis["evidence"]
    )
    if not candidates:
        raise KnowledgeWorkbenchError(f"文件 {source.name} 未生成任何原子证据")

    now = utc_now()
    processing_run_id = new_id("run")
    evidence_ids = tuple(new_id("ev") for _ in candidates)
    draft_path: Path | None = None
    mirror_path: Path | None = None
    analysis_path: Path | None = None
    generation_path: Path | None = None

    try:
        with database.transaction() as connection:
            current_run = connection.execute(
                """
                SELECT id FROM processing_runs
                WHERE document_version_id = ? AND is_current = 1
                """,
                (version_id,),
            ).fetchone()
            first_ordinal = connection.execute(
                """
                SELECT COALESCE(MAX(ordinal), 0) + 1
                FROM evidence WHERE document_version_id = ?
                """,
                (version_id,),
            ).fetchone()[0]
            if current_run:
                connection.execute(
                    """
                    UPDATE processing_runs
                    SET status = 'superseded', is_current = 0
                    WHERE id = ?
                    """,
                    (current_run["id"],),
                )
            connection.execute(
                """
                INSERT INTO processing_runs(
                    id, document_version_id, parser_name, parser_version,
                    extraction_method, status, is_current, created_at, completed_at
                ) VALUES (?, ?, ?, ?, ?, 'completed', 1, ?, ?)
                """,
                (
                    processing_run_id,
                    version_id,
                    parsed.parser_name,
                    parsed.parser_version,
                    EXTRACTION_METHOD,
                    now,
                    now,
                ),
            )
            connection.execute(
                "UPDATE documents SET classification = ?, updated_at = ? WHERE id = ?",
                (stored_classification.value, now, document_id),
            )

            for run_ordinal, (evidence_id, candidate) in enumerate(
                zip(evidence_ids, candidates, strict=True), start=1
            ):
                connection.execute(
                    """
                    INSERT INTO evidence(
                        id, document_version_id, processing_run_id, ordinal, run_ordinal,
                        excerpt, locator_json, extraction_method, extraction_model,
                        status, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'draft', ?, ?)
                    """,
                    (
                        evidence_id,
                        version_id,
                        processing_run_id,
                        first_ordinal + run_ordinal - 1,
                        run_ordinal,
                        candidate.excerpt,
                        json.dumps(candidate.locator, ensure_ascii=False, sort_keys=True),
                        candidate.extraction_method,
                        candidate.extraction_model,
                        now,
                        now,
                    ),
                )
                connection.execute(
                    "INSERT INTO evidence_fts(excerpt, evidence_id) VALUES (?, ?)",
                    (candidate.excerpt, evidence_id),
                )

            page = connection.execute(
                "SELECT * FROM wiki_pages WHERE source_document_id = ?",
                (document_id,),
            ).fetchone()
            if page:
                page_id = page["id"]
                slug = page["slug"]
                _mark_revalidation_required(
                    connection,
                    page_id=page_id,
                    actor=actor,
                    reason="processing_run_changed",
                    cause_id=processing_run_id,
                    now=now,
                )
                revision_number = connection.execute(
                    """
                    SELECT COALESCE(MAX(revision_number), 0) + 1
                    FROM wiki_revisions WHERE page_id = ?
                    """,
                    (page_id,),
                ).fetchone()[0]
            else:
                page_id = new_id("page")
                slug = _available_slug(connection, slugify(source.stem), document_id)
                revision_number = 1
                connection.execute(
                    """
                    INSERT INTO wiki_pages(
                        id, source_document_id, slug, title, status,
                        current_verified_revision_id, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, 'draft', NULL, ?, ?)
                    """,
                    (page_id, document_id, slug, source.stem, now, now),
                )

            revision_id = new_id("rev")
            generation = faithful_wiki_generation(analysis, title=source.stem)
            rendered_evidence = tuple(
                RenderedEvidence(evidence_id, candidate)
                for evidence_id, candidate in zip(evidence_ids, candidates, strict=True)
            )
            content = render_draft(
                title=source.stem,
                page_id=page_id,
                revision_id=revision_id,
                revision_number=revision_number,
                document_version_id=version_id,
                source_name=source.name,
                source_sha256=digest,
                classification=stored_classification.value,
                generated_at=now,
                evidence=rendered_evidence,
            )
            draft_path = paths.wiki_drafts / f"{slug}--r{revision_number}.md"
            write_text_atomic(draft_path, content)
            relative_draft_path = draft_path.relative_to(paths.root).as_posix()
            connection.execute(
                """
                INSERT INTO wiki_revisions(
                    id, page_id, revision_number, status, markdown_path,
                    content_sha256, generator, processing_run_id, created_at, updated_at
                ) VALUES (?, ?, ?, 'draft', ?, ?, 'faithful-draft-v1', ?, ?, ?)
                """,
                (
                    revision_id,
                    page_id,
                    revision_number,
                    relative_draft_path,
                    sha256_text(content),
                    processing_run_id,
                    now,
                    now,
                ),
            )
            connection.executemany(
                "INSERT INTO revision_evidence(revision_id, evidence_id) VALUES (?, ?)",
                ((revision_id, evidence_id) for evidence_id in evidence_ids),
            )

            mirror_path = paths.evidence / f"{processing_run_id}.jsonl"
            mirror_content = "".join(
                json.dumps(
                    {
                        "id": evidence_id,
                        "document_version_id": version_id,
                        "processing_run_id": processing_run_id,
                        "source_sha256": digest,
                        "excerpt": candidate.excerpt,
                        "locator": candidate.locator,
                        "extraction_method": candidate.extraction_method,
                        "status": "draft",
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
                + "\n"
                for evidence_id, candidate in zip(evidence_ids, candidates, strict=True)
            )
            write_text_atomic(mirror_path, mirror_content)
            analysis_path = paths.analysis / f"{processing_run_id}.analysis.json"
            generation_path = paths.analysis / f"{revision_id}.wiki-generation.json"
            write_text_atomic(
                analysis_path,
                json.dumps(analysis, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            )
            write_text_atomic(
                generation_path,
                json.dumps(generation, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            )

            record_event(
                connection,
                "document_reprocessed",
                "processing_run",
                processing_run_id,
                actor=actor,
                details={
                    "document_id": document_id,
                    "document_version_id": version_id,
                    "previous_processing_run_id": (
                        current_run["id"] if current_run else None
                    ),
                    "sha256": digest,
                    "parser": parsed.parser_name,
                    "parser_version": parsed.parser_version,
                    "extraction_method": EXTRACTION_METHOD,
                    "evidence_count": len(candidates),
                    "revision_id": revision_id,
                },
            )
            record_event(
                connection,
                "wiki_draft_created",
                "wiki_revision",
                revision_id,
                actor=actor,
                details={
                    "page_id": page_id,
                    "revision": revision_number,
                    "processing_run_id": processing_run_id,
                },
            )
    except Exception:
        for generated in (draft_path, mirror_path, analysis_path, generation_path):
            if generated and generated.exists():
                generated.chmod(stat.S_IWRITE | stat.S_IREAD)
                generated.unlink()
        raise

    return IngestResult(
        document_id=document_id,
        version_id=version_id,
        sha256=digest,
        duplicate=False,
        evidence_count=len(candidates),
        page_id=page_id,
        revision_id=revision_id,
        processing_run_id=processing_run_id,
        reprocessed=True,
    )


def _available_slug(connection, base: str, document_id: str) -> str:
    row = connection.execute("SELECT 1 FROM wiki_pages WHERE slug = ?", (base,)).fetchone()
    return f"{base}-{document_id[-8:]}" if row else base


def _mark_revalidation_required(
    connection,
    *,
    page_id: str,
    actor: str,
    reason: str,
    cause_id: str,
    now: str,
) -> None:
    page = connection.execute(
        "SELECT current_verified_revision_id FROM wiki_pages WHERE id = ?",
        (page_id,),
    ).fetchone()
    if not page or page["current_verified_revision_id"] is None:
        return

    downstream = connection.execute(
        """
        SELECT DISTINCT source_page.id
        FROM wiki_links wl
        JOIN wiki_revisions source_revision ON source_revision.id = wl.source_revision_id
        JOIN wiki_pages source_page ON source_page.id = source_revision.page_id
        WHERE wl.target_page_id = ?
          AND source_page.current_verified_revision_id = source_revision.id
        """,
        (page_id,),
    ).fetchall()
    affected_page_ids = [page_id, *(row["id"] for row in downstream)]
    connection.executemany(
        "UPDATE wiki_pages SET needs_revalidation = 1, updated_at = ? WHERE id = ?",
        ((now, affected_page_id) for affected_page_id in affected_page_ids),
    )
    for affected_page_id in affected_page_ids:
        record_event(
            connection,
            "wiki_revalidation_required",
            "wiki_page",
            affected_page_id,
            actor=actor,
            details={
                "reason": reason,
                "cause_id": cause_id,
                "changed_page_id": page_id,
            },
        )
