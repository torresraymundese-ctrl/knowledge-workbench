from __future__ import annotations

import json
import re
import stat
from dataclasses import asdict, dataclass
from pathlib import Path

from .config import WorkspacePaths
from .database import Database
from .entities import normalize_entity_name
from .errors import KnowledgeWorkbenchError
from .labeling import validate_labeling_session_ready
from .schema_validation import validate_analysis, validate_wiki_generation
from .utils import sha256_file, sha256_text, utc_now


@dataclass(frozen=True, slots=True)
class LintIssue:
    code: str
    entity_id: str
    message: str
    severity: str = "error"


def lint_workspace(database: Database, paths: WorkspacePaths) -> dict:
    issues: list[LintIssue] = []
    checked_runs = 0
    checked_evidence = 0
    checked_entities = 0
    checked_entity_mentions = 0
    checked_entity_candidates = 0
    checked_entity_merge_requests = 0
    checked_entity_relation_types = 0
    checked_entity_relationships = 0
    checked_topic_revisions = 0
    labeling_sessions = []
    with database.connect() as connection:
        foreign_key_issues = connection.execute("PRAGMA foreign_key_check").fetchall()
        for row in foreign_key_issues:
            issues.append(
                LintIssue(
                    "database_foreign_key",
                    str(row[0]),
                    f"外键检查失败：表={row[0]} rowid={row[1]} parent={row[2]}",
                )
            )
        documents = connection.execute(
            """
            SELECT d.id AS document_id, d.classification, d.current_version_id,
                   dv.sha256, dv.stored_path
            FROM documents d
            LEFT JOIN document_versions dv ON dv.id = d.current_version_id
            ORDER BY d.id
            """
        ).fetchall()
        labeling_sessions = connection.execute(
            """
            SELECT id, status FROM labeling_sessions
            WHERE status IN ('reviewing', 'approved')
            ORDER BY id
            """
        ).fetchall()
        entities = connection.execute(
            """
            SELECT ce.id, ce.canonical_name, ce.normalized_name, ce.status,
                   SUM(CASE WHEN ea.is_canonical = 1 THEN 1 ELSE 0 END)
                       AS canonical_alias_count,
                   MAX(CASE WHEN ea.is_canonical = 1 THEN ea.normalized_alias END)
                       AS canonical_alias_normalized,
                   (SELECT COUNT(*) FROM entity_merge_requests emr
                    WHERE emr.source_entity_id = ce.id
                      AND emr.status = 'merged') AS merged_as_source_count
            FROM canonical_entities ce
            LEFT JOIN entity_aliases ea ON ea.entity_id = ce.id
            GROUP BY ce.id
            ORDER BY ce.id
            """
        ).fetchall()
        checked_entities = len(entities)
        for entity in entities:
            normalized_name_valid = (
                normalize_entity_name(entity["canonical_name"])
                == entity["normalized_name"]
            )
            if entity["status"] == "active" and (
                entity["canonical_alias_count"] != 1
                or entity["canonical_alias_normalized"] != entity["normalized_name"]
                or not normalized_name_valid
                or entity["merged_as_source_count"] != 0
            ):
                issues.append(
                    LintIssue(
                        "canonical_entity_alias_invalid",
                        entity["id"],
                        "规范实体必须有且仅有一个与规范名称一致的 canonical 别名",
                    )
                )
            if entity["status"] == "archived" and (
                entity["canonical_alias_count"] != 0
                or not normalized_name_valid
                or entity["merged_as_source_count"] != 1
            ):
                issues.append(
                    LintIssue(
                        "merged_entity_archive_invalid",
                        entity["id"],
                        "已归档实体必须由一次已批准合并产生且不再持有别名",
                    )
                )
        mentions = connection.execute(
            """
            SELECT eem.evidence_id, eem.entity_id, eem.mention_text,
                   e.excerpt, ea.normalized_alias
            FROM evidence_entity_mentions eem
            JOIN evidence e ON e.id = eem.evidence_id
            JOIN entity_aliases ea
              ON ea.id = eem.alias_id AND ea.entity_id = eem.entity_id
            ORDER BY eem.entity_id, eem.evidence_id
            """
        ).fetchall()
        checked_entity_mentions = len(mentions)
        for mention in mentions:
            if (
                mention["mention_text"] not in mention["excerpt"]
                or normalize_entity_name(mention["mention_text"])
                != mention["normalized_alias"]
            ):
                issues.append(
                    LintIssue(
                        "evidence_entity_mention_invalid",
                        f"{mention['evidence_id']}:{mention['entity_id']}",
                        "实体提及必须逐字存在于证据原文并匹配已登记别名",
                    )
                )
        candidates = connection.execute(
            """
            SELECT ec.id, ec.status, ec.suggested_name, ec.normalized_name,
                   ec.verbatim_match, e.excerpt,
                   CASE WHEN e.processing_run_id = ec.processing_run_id
                                  AND e.document_version_id = ec.document_version_id
                        THEN 1 ELSE 0 END AS source_identity_valid,
                   CASE WHEN ec.status = 'accepted' AND EXISTS (
                       SELECT 1 FROM evidence_entity_mentions eem
                       JOIN entity_aliases ea
                         ON ea.id = eem.alias_id AND ea.entity_id = eem.entity_id
                       WHERE eem.evidence_id = ec.evidence_id
                         AND eem.entity_id = ec.resolved_entity_id
                         AND eem.mention_text = ec.suggested_name
                         AND ea.normalized_alias = ec.normalized_name
                   ) THEN 1 ELSE 0 END AS accepted_link_exists
            FROM entity_candidates ec
            JOIN evidence e ON e.id = ec.evidence_id
            ORDER BY ec.id
            """
        ).fetchall()
        checked_entity_candidates = len(candidates)
        for candidate in candidates:
            expected_normalized = normalize_entity_name(candidate["suggested_name"])
            expected_verbatim = int(candidate["suggested_name"] in candidate["excerpt"])
            if (
                candidate["normalized_name"] != expected_normalized
                or candidate["verbatim_match"] != expected_verbatim
                or not candidate["source_identity_valid"]
            ):
                issues.append(
                    LintIssue(
                        "entity_candidate_source_invalid",
                        candidate["id"],
                        "实体候选规范名称或逐字匹配标记与来源证据不一致",
                    )
                )
            if candidate["status"] == "accepted" and not candidate["accepted_link_exists"]:
                issues.append(
                    LintIssue(
                        "accepted_entity_candidate_link_missing",
                        candidate["id"],
                        "已接受实体候选缺少对应别名或证据关联",
                    )
                )
        merge_requests = connection.execute(
            """
            SELECT emr.*, source.entity_type AS source_type,
                   source.status AS source_status,
                   target.entity_type AS target_type,
                   target.status AS target_status,
                   (SELECT COUNT(*) FROM entity_aliases ea
                    WHERE ea.entity_id = emr.source_entity_id) AS source_alias_count,
                   (SELECT COUNT(*) FROM evidence_entity_mentions eem
                    WHERE eem.entity_id = emr.source_entity_id)
                       AS source_mention_count,
                   (SELECT COUNT(*) FROM entity_candidates ec
                    WHERE ec.status = 'accepted'
                      AND ec.resolved_entity_id = emr.source_entity_id)
                       AS source_candidate_count
            FROM entity_merge_requests emr
            JOIN canonical_entities source ON source.id = emr.source_entity_id
            JOIN canonical_entities target ON target.id = emr.target_entity_id
            ORDER BY emr.id
            """
        ).fetchall()
        checked_entity_merge_requests = len(merge_requests)
        for request in merge_requests:
            type_valid = (
                request["source_type"] == request["entity_type"]
                and request["target_type"] == request["entity_type"]
            )
            if request["proposed_by"] == request["reviewed_by"]:
                issues.append(
                    LintIssue(
                        "entity_merge_same_reviewer",
                        request["id"],
                        "实体合并提议人不能复核自己的请求",
                    )
                )
            if request["status"] == "reviewing" and (
                not type_valid
                or request["source_status"] != "active"
                or request["target_status"] != "active"
            ):
                issues.append(
                    LintIssue(
                        "entity_merge_reviewing_invalid",
                        request["id"],
                        "待复核实体合并的两端必须仍为同类型 active 实体",
                    )
                )
            if request["status"] == "merged" and (
                not type_valid
                or request["source_status"] != "archived"
                or request["source_alias_count"] != 0
                or request["source_mention_count"] != 0
                or request["source_candidate_count"] != 0
            ):
                issues.append(
                    LintIssue(
                        "entity_merge_projection_invalid",
                        request["id"],
                        "已合并源实体仍持有别名、提及或候选指向，或归档状态不正确",
                    )
                )
        checked_entity_relation_types = connection.execute(
            "SELECT COUNT(*) FROM entity_relation_types"
        ).fetchone()[0]
        relationships = connection.execute(
            """
            SELECT er.id, er.status, er.source_entity_id,
                   er.target_entity_id, ert.status AS relation_type_status,
                   source.status AS source_status,
                   target.status AS target_status,
                   COUNT(ere.evidence_id) AS support_count,
                   SUM(
                       CASE WHEN ere.evidence_id IS NOT NULL AND (
                           NOT EXISTS (
                               SELECT 1 FROM evidence_entity_mentions source_mention
                               WHERE source_mention.evidence_id = ere.evidence_id
                                 AND source_mention.entity_id = er.source_entity_id
                           )
                           OR NOT EXISTS (
                               SELECT 1 FROM evidence_entity_mentions target_mention
                               WHERE target_mention.evidence_id = ere.evidence_id
                                 AND target_mention.entity_id = er.target_entity_id
                           )
                       ) THEN 1 ELSE 0 END
                   ) AS invalid_support_count,
                   SUM(
                       CASE WHEN ere.evidence_id IS NOT NULL
                                  AND e.status = 'verified'
                                  AND pr.is_current = 1
                                  AND d.current_version_id = dv.id
                            THEN 1 ELSE 0 END
                   ) AS current_verified_support_count
            FROM entity_relationships er
            JOIN entity_relation_types ert
              ON ert.relation_key = er.relation_key
            JOIN canonical_entities source
              ON source.id = er.source_entity_id
            JOIN canonical_entities target
              ON target.id = er.target_entity_id
            LEFT JOIN entity_relationship_evidence ere
              ON ere.relationship_id = er.id
            LEFT JOIN evidence e ON e.id = ere.evidence_id
            LEFT JOIN processing_runs pr ON pr.id = e.processing_run_id
            LEFT JOIN document_versions dv ON dv.id = e.document_version_id
            LEFT JOIN documents d ON d.id = dv.document_id
            GROUP BY er.id
            ORDER BY er.id
            """
        ).fetchall()
        checked_entity_relationships = len(relationships)
        for relationship in relationships:
            if (
                relationship["support_count"] < 1
                or relationship["invalid_support_count"] > 0
            ):
                issues.append(
                    LintIssue(
                        "entity_relationship_support_invalid",
                        relationship["id"],
                        "业务关系必须至少有一条同时关联两个端点的证据支持",
                    )
                )
            if relationship["status"] == "active" and (
                relationship["relation_type_status"] != "active"
                or relationship["source_status"] != "active"
                or relationship["target_status"] != "active"
            ):
                issues.append(
                    LintIssue(
                        "entity_relationship_endpoint_invalid",
                        relationship["id"],
                        "active 业务关系的类型和两个端点都必须是 active 状态",
                    )
                )
            if (
                relationship["status"] == "active"
                and relationship["current_verified_support_count"] < 1
            ):
                issues.append(
                    LintIssue(
                        "entity_relationship_needs_revalidation",
                        relationship["id"],
                        "active 业务关系已没有当前 verified 证据支持",
                        severity="warning",
                    )
                )
        for document in documents:
            document_id = document["document_id"]
            version_id = document["current_version_id"]
            if version_id is None:
                issues.append(
                    LintIssue("missing_current_version", document_id, "文档没有当前文件版本")
                )
                continue
            _check_raw_copy(paths, document, issues)
            runs = connection.execute(
                """
                SELECT * FROM processing_runs
                WHERE document_version_id = ? AND is_current = 1
                """,
                (version_id,),
            ).fetchall()
            if len(runs) != 1:
                issues.append(
                    LintIssue(
                        "current_processing_run_count",
                        version_id,
                        f"当前文件版本应有且仅有一个当前处理运行，实际为 {len(runs)}",
                    )
                )
                continue
            run = runs[0]
            checked_runs += 1
            evidence = connection.execute(
                """
                SELECT id, run_ordinal, excerpt, locator_json, extraction_method, status
                FROM evidence WHERE processing_run_id = ? ORDER BY run_ordinal
                """,
                (run["id"],),
            ).fetchall()
            location_rows = connection.execute(
                """
                SELECT el.evidence_id, el.location_ordinal, el.locator_json
                FROM evidence_locations el
                JOIN evidence e ON e.id = el.evidence_id
                WHERE e.processing_run_id = ?
                ORDER BY el.evidence_id, el.location_ordinal
                """,
                (run["id"],),
            ).fetchall()
            locations_by_evidence: dict[str, list] = {}
            for location_row in location_rows:
                locations_by_evidence.setdefault(
                    location_row["evidence_id"], []
                ).append(location_row)
            checked_evidence += len(evidence)
            revisions = connection.execute(
                """
                SELECT id, markdown_path, content_sha256
                FROM wiki_revisions
                WHERE processing_run_id = ?
                ORDER BY revision_number DESC
                """,
                (run["id"],),
            ).fetchall()
            if len(revisions) != 1:
                issues.append(
                    LintIssue(
                        "processing_run_revision_count",
                        run["id"],
                        f"当前处理运行应关联一个 Wiki 修订，实际为 {len(revisions)}",
                    )
                )
                continue
            _check_run_artifacts(
                paths,
                document,
                run,
                evidence,
                locations_by_evidence,
                revisions[0],
                issues,
            )
        topic_revisions = connection.execute(
            """
            WITH topic_revision_ids(page_id, revision_id) AS (
                SELECT wp.id,
                       (
                           SELECT candidate.id
                           FROM wiki_revisions candidate
                           WHERE candidate.page_id = wp.id
                             AND candidate.status IN (
                                 'draft', 'reviewing', 'verified'
                             )
                           ORDER BY candidate.revision_number DESC,
                                    candidate.created_at DESC,
                                    candidate.id DESC
                           LIMIT 1
                       )
                FROM wiki_pages wp
                WHERE wp.source_document_id IS NULL
                UNION
                SELECT wp.id, wp.current_verified_revision_id
                FROM wiki_pages wp
                WHERE wp.source_document_id IS NULL
                  AND wp.current_verified_revision_id IS NOT NULL
            )
            SELECT wr.id, wr.markdown_path, wr.content_sha256, wr.generator,
                   ids.page_id
            FROM topic_revision_ids ids
            JOIN wiki_revisions wr ON wr.id = ids.revision_id
            ORDER BY ids.page_id, wr.revision_number, wr.id
            """
        ).fetchall()
        checked_topic_revisions = len(topic_revisions)
        for revision in topic_revisions:
            _check_topic_revision(
                connection,
                paths,
                revision,
                issues,
            )

    for session in labeling_sessions:
        try:
            validate_labeling_session_ready(database, session["id"])
        except KnowledgeWorkbenchError as exc:
            issues.append(
                LintIssue(
                    "labeling_session_invalid",
                    session["id"],
                    str(exc),
                )
            )

    error_count = sum(issue.severity == "error" for issue in issues)
    warning_count = sum(issue.severity == "warning" for issue in issues)
    return {
        "schema_version": "1.0",
        "checked_at": utc_now(),
        "workspace": str(paths.root),
        "passed": error_count == 0,
        "summary": {
            "document_count": len(documents),
            "current_processing_run_count": checked_runs,
            "current_evidence_count": checked_evidence,
            "issue_count": len(issues),
            "error_count": error_count,
            "warning_count": warning_count,
            "reviewing_or_approved_labeling_session_count": len(labeling_sessions),
            "canonical_entity_count": checked_entities,
            "entity_evidence_mention_count": checked_entity_mentions,
            "entity_candidate_count": checked_entity_candidates,
            "entity_merge_request_count": checked_entity_merge_requests,
            "entity_relation_type_count": checked_entity_relation_types,
            "entity_relationship_count": checked_entity_relationships,
            "topic_wiki_revision_count": checked_topic_revisions,
        },
        "issues": [asdict(issue) for issue in issues],
    }


def _check_raw_copy(paths: WorkspacePaths, document, issues: list[LintIssue]) -> None:
    version_id = document["current_version_id"]
    raw_path = (paths.root / document["stored_path"]).resolve()
    try:
        raw_path.relative_to(paths.root)
    except ValueError:
        issues.append(
            LintIssue("raw_path_escape", version_id, "原始副本路径越出工作区")
        )
        return
    if not raw_path.is_file():
        issues.append(LintIssue("raw_missing", version_id, "原始只读副本不存在"))
        return
    if sha256_file(raw_path) != document["sha256"]:
        issues.append(LintIssue("raw_sha256_mismatch", version_id, "原始副本 SHA-256 不一致"))
    file_stat = raw_path.stat()
    windows_readonly = bool(
        getattr(file_stat, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_READONLY", 0)
    )
    mode_readonly = not bool(file_stat.st_mode & stat.S_IWUSR)
    if not windows_readonly and not mode_readonly:
        issues.append(LintIssue("raw_not_readonly", version_id, "原始副本不是只读文件"))


def _check_run_artifacts(
    paths: WorkspacePaths,
    document,
    run,
    evidence,
    locations_by_evidence,
    revision,
    issues: list[LintIssue],
) -> None:
    run_id = run["id"]
    analysis_path = paths.analysis / f"{run_id}.analysis.json"
    generation_path = paths.analysis / f"{revision['id']}.wiki-generation.json"
    mirror_path = paths.evidence / f"{run_id}.jsonl"
    legacy_run = run_id.startswith("run_legacy_")
    if legacy_run and not mirror_path.is_file():
        mirror_path = paths.evidence / f"{document['current_version_id']}.jsonl"
    missing_severity = "warning" if legacy_run else "error"
    analysis = _read_json(
        analysis_path,
        run_id,
        "analysis",
        issues,
        missing_severity=missing_severity,
    )
    generation = _read_json(
        generation_path,
        revision["id"],
        "wiki_generation",
        issues,
        missing_severity=missing_severity,
    )
    if analysis is not None:
        try:
            validate_analysis(analysis)
        except KnowledgeWorkbenchError as exc:
            issues.append(LintIssue("analysis_schema", run_id, str(exc)))
        _check_analysis_database_consistency(
            analysis,
            document,
            evidence,
            locations_by_evidence,
            run_id,
            issues,
        )
    if generation is not None and analysis is not None:
        try:
            validate_wiki_generation(generation, analysis)
        except KnowledgeWorkbenchError as exc:
            issues.append(LintIssue("wiki_generation_schema", revision["id"], str(exc)))
    _check_evidence_locations(evidence, locations_by_evidence, issues)
    _check_mirror(
        mirror_path, run_id, evidence, locations_by_evidence, issues
    )
    markdown_path = (paths.root / revision["markdown_path"]).resolve()
    if not markdown_path.is_file():
        issues.append(LintIssue("wiki_markdown_missing", revision["id"], "Wiki Markdown 不存在"))
    elif sha256_text(markdown_path.read_text(encoding="utf-8")) != revision["content_sha256"]:
        issues.append(
            LintIssue("wiki_markdown_sha256_mismatch", revision["id"], "Wiki Markdown 哈希与数据库不一致")
        )


def _check_topic_revision(
    connection,
    paths: WorkspacePaths,
    revision,
    issues: list[LintIssue],
) -> None:
    revision_id = revision["id"]
    if revision["generator"] != "topic-extractive-v1":
        issues.append(
            LintIssue(
                "topic_wiki_generator_invalid",
                revision_id,
                "主题知识页必须由逐字摘录生成器创建",
            )
        )
    markdown_path = (paths.root / revision["markdown_path"]).resolve()
    try:
        markdown_path.relative_to(paths.root.resolve())
    except ValueError:
        issues.append(
            LintIssue(
                "topic_wiki_path_escape",
                revision_id,
                "主题知识页路径越出工作区",
            )
        )
        return
    try:
        content = markdown_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        issues.append(
            LintIssue(
                "topic_wiki_markdown_missing",
                revision_id,
                "主题知识页 Markdown 不存在或不可读取",
            )
        )
        return
    if not re.search(
        r'^generator:\s*"topic-extractive-v1"\s*$',
        content,
        flags=re.MULTILINE,
    ):
        issues.append(
            LintIssue(
                "topic_wiki_generator_invalid",
                revision_id,
                "主题知识页 Markdown 缺少可信的逐字摘录生成器声明",
            )
        )
    if sha256_text(content) != revision["content_sha256"]:
        issues.append(
            LintIssue(
                "topic_wiki_markdown_sha256_mismatch",
                revision_id,
                "主题知识页 Markdown 哈希与数据库不一致",
            )
        )

    expected_evidence_count = connection.execute(
        """
        SELECT COUNT(*)
        FROM revision_evidence
        WHERE revision_id = ?
        """,
        (revision_id,),
    ).fetchone()[0]
    rows = connection.execute(
        """
        SELECT e.id, e.excerpt, e.status AS evidence_status,
               etv.status AS validation_status,
               dv.document_id, pr.status AS run_status,
               pr.is_current AS run_is_current,
               (d.current_version_id = dv.id) AS version_is_current,
               (pr.document_version_id = e.document_version_id)
                   AS run_version_matches,
               d.classification, dg.purpose, dg.scope_status,
               dg.authority_status
        FROM revision_evidence re
        JOIN evidence e ON e.id = re.evidence_id
        JOIN document_versions dv ON dv.id = e.document_version_id
        JOIN documents d ON d.id = dv.document_id
        LEFT JOIN evidence_technical_validation etv ON etv.evidence_id = e.id
        LEFT JOIN processing_runs pr ON pr.id = e.processing_run_id
        LEFT JOIN document_governance dg ON dg.document_id = d.id
        WHERE re.revision_id = ?
        ORDER BY e.id
        """,
        (revision_id,),
    ).fetchall()
    if (
        len(rows) != expected_evidence_count
        or len({row["document_id"] for row in rows}) < 2
    ):
        issues.append(
            LintIssue(
                "topic_wiki_source_count",
                revision_id,
                "主题知识页必须引用至少两份不同资料",
            )
        )
    invalid_rows = [
        row
        for row in rows
        if row["evidence_status"] != "verified"
        or row["validation_status"] != "passed"
        or row["run_status"] != "completed"
        or not row["run_is_current"]
        or not row["version_is_current"]
        or not row["run_version_matches"]
        or row["classification"] == "restricted"
        or row["purpose"] != "production"
        or row["scope_status"] != "in_scope"
        or row["authority_status"] not in {"reference", "authoritative"}
    ]
    if invalid_rows:
        issues.append(
            LintIssue(
                "topic_wiki_source_invalid",
                revision_id,
                f"主题知识页有 {len(invalid_rows)} 条来源依据不再有效",
            )
        )

    database_ids = {row["id"] for row in rows}
    excerpts = {row["id"]: row["excerpt"].strip() for row in rows}
    lines = content.splitlines()
    heading_indexes = [
        index for index, line in enumerate(lines) if line.startswith("## ")
    ]
    marker_ids: set[str] = set()
    source_marker_ids: set[str] = set()
    marker_invalid = not heading_indexes
    section_excerpt_missing = False
    marker_pattern = re.compile(
        r"<!-- topic-section-evidence: (?P<ids>\[.*\]) -->"
    )
    for position, heading_index in enumerate(heading_indexes):
        if heading_index + 1 >= len(lines):
            marker_invalid = True
            continue
        marker_match = marker_pattern.fullmatch(lines[heading_index + 1].strip())
        if not marker_match:
            marker_invalid = True
            continue
        try:
            parsed = json.loads(marker_match.group("ids"))
        except json.JSONDecodeError:
            marker_invalid = True
            continue
        if (
            not isinstance(parsed, list)
            or any(not isinstance(item, str) or not item.strip() for item in parsed)
            or len(parsed) != len(set(parsed))
            or not set(parsed).issubset(database_ids)
        ):
            marker_invalid = True
            continue
        marker_ids.update(parsed)
        if not lines[heading_index].startswith("## 原文依据："):
            continue
        if not parsed:
            marker_invalid = True
            continue
        source_marker_ids.update(parsed)
        next_heading = (
            heading_indexes[position + 1]
            if position + 1 < len(heading_indexes)
            else len(lines)
        )
        section_text = "\n".join(
            line[2:] if line.startswith("> ") else line
            for line in lines[heading_index + 2 : next_heading]
        )
        if any(excerpts[evidence_id] not in section_text for evidence_id in parsed):
            section_excerpt_missing = True

    if (
        marker_invalid
        or marker_ids != database_ids
        or source_marker_ids != database_ids
    ):
        issues.append(
            LintIssue(
                "topic_wiki_evidence_marker_mismatch",
                revision_id,
                "主题知识页每个二级章节必须紧邻声明依据，且逐项展示本修订全部来源",
            )
        )
    missing_quotes = [
        row["id"]
        for row in rows
        if row["excerpt"] not in content
        and row["excerpt"]
        .replace("\r\n", "\n")
        .replace("\r", "\n")
        .replace("\n", "\n> ")
        not in content
    ]
    if missing_quotes or section_excerpt_missing:
        issues.append(
            LintIssue(
                "topic_wiki_excerpt_missing",
                revision_id,
                "主题知识页缺少逐字原文，或原文未放在其声明的来源章节中",
            )
        )


def _read_json(
    path: Path,
    entity_id: str,
    kind: str,
    issues: list[LintIssue],
    *,
    missing_severity: str = "error",
) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        issues.append(
            LintIssue(
                f"{kind}_missing",
                entity_id,
                f"{kind} 文件不存在",
                missing_severity,
            )
        )
    except (OSError, json.JSONDecodeError) as exc:
        issues.append(LintIssue(f"{kind}_invalid_json", entity_id, str(exc)))
    return None


def _check_analysis_database_consistency(
    analysis: dict,
    document,
    evidence,
    locations_by_evidence,
    run_id: str,
    issues: list[LintIssue],
) -> None:
    source = analysis.get("source", {})
    classification_rank = {
        "public": 0,
        "internal": 1,
        "confidential": 2,
        "restricted": 3,
    }
    source_classification = source.get("classification")
    current_classification = document["classification"]
    source_identity_matches = (
        source.get("document_version_id") == document["current_version_id"]
        and source.get("sha256") == document["sha256"]
        and set(source) == {
            "document_version_id",
            "sha256",
            "classification",
        }
    )
    classification_is_preserved_or_tightened = (
        source_classification in classification_rank
        and current_classification in classification_rank
        and classification_rank[current_classification]
        >= classification_rank[source_classification]
    )
    if not source_identity_matches or not classification_is_preserved_or_tightened:
        issues.append(
            LintIssue(
                "analysis_source_mismatch",
                run_id,
                "分析来源身份或当前密级收紧关系与数据库不一致",
            )
        )
    items = analysis.get("evidence", [])
    if len(items) != len(evidence):
        issues.append(
            LintIssue(
                "analysis_evidence_count_mismatch",
                run_id,
                f"分析证据数 {len(items)} 与数据库证据数 {len(evidence)} 不一致",
            )
        )
        return
    for item, row in zip(items, evidence, strict=True):
        try:
            locator = json.loads(row["locator_json"])
        except json.JSONDecodeError:
            issues.append(LintIssue("database_locator_invalid_json", row["id"], "定位字段不是有效 JSON"))
            continue
        locators = _parsed_locations(row["id"], locations_by_evidence, issues)
        item_locators = item.get("locators") or [item.get("locator")]
        if (
            item.get("excerpt") != row["excerpt"]
            or item.get("locator") != locator
            or item_locators != locators
        ):
            issues.append(
                LintIssue("analysis_evidence_mismatch", row["id"], "分析证据与数据库原文或定位不一致")
            )


def _check_mirror(
    path: Path,
    run_id: str,
    evidence,
    locations_by_evidence,
    issues: list[LintIssue],
) -> None:
    try:
        # JSON Lines records are separated by LF. ``str.splitlines()`` also
        # treats Unicode line/paragraph separators embedded in valid JSON
        # strings as record boundaries, which corrupts otherwise valid mirrors.
        lines = [line for line in path.read_text(encoding="utf-8").split("\n") if line]
        mirror = [json.loads(line) for line in lines]
    except FileNotFoundError:
        issues.append(LintIssue("evidence_mirror_missing", run_id, "证据 JSONL 镜像不存在"))
        return
    except (OSError, json.JSONDecodeError) as exc:
        issues.append(LintIssue("evidence_mirror_invalid", run_id, str(exc)))
        return
    if len(mirror) != len(evidence):
        issues.append(
            LintIssue(
                "evidence_mirror_count_mismatch",
                run_id,
                f"JSONL 证据数 {len(mirror)} 与数据库证据数 {len(evidence)} 不一致",
            )
        )
        return
    for item, row in zip(mirror, evidence, strict=True):
        locators = _parsed_locations(row["id"], locations_by_evidence, issues)
        item_locators = item.get("locators") or [item.get("locator")]
        if (
            item.get("id") != row["id"]
            or item.get("excerpt") != row["excerpt"]
            or item_locators != locators
        ):
            issues.append(LintIssue("evidence_mirror_mismatch", row["id"], "JSONL 与数据库证据不一致"))


def _check_evidence_locations(evidence, locations_by_evidence, issues) -> None:
    for row in evidence:
        location_rows = locations_by_evidence.get(row["id"], [])
        if not location_rows:
            issues.append(
                LintIssue("evidence_location_missing", row["id"], "证据没有来源定位")
            )
            continue
        expected_ordinals = list(range(1, len(location_rows) + 1))
        ordinals = [item["location_ordinal"] for item in location_rows]
        if ordinals != expected_ordinals:
            issues.append(
                LintIssue(
                    "evidence_location_ordinal_gap",
                    row["id"],
                    "证据定位序号不连续",
                )
            )
        locators = _parsed_locations(row["id"], locations_by_evidence, issues)
        try:
            primary = json.loads(row["locator_json"])
        except json.JSONDecodeError:
            continue
        if locators and locators[0] != primary:
            issues.append(
                LintIssue(
                    "evidence_primary_location_mismatch",
                    row["id"],
                    "证据首定位与兼容定位字段不一致",
                )
            )


def _parsed_locations(evidence_id: str, locations_by_evidence, issues) -> list[dict]:
    output: list[dict] = []
    for row in locations_by_evidence.get(evidence_id, []):
        try:
            locator = json.loads(row["locator_json"])
        except json.JSONDecodeError:
            issues.append(
                LintIssue(
                    "evidence_location_invalid_json",
                    evidence_id,
                    "证据定位不是有效 JSON",
                )
            )
            continue
        if not isinstance(locator, dict):
            issues.append(
                LintIssue(
                    "evidence_location_not_object",
                    evidence_id,
                    "证据定位必须是 JSON 对象",
                )
            )
            continue
        output.append(locator)
    return output
