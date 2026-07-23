from __future__ import annotations

import json
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
            SELECT ce.id, ce.canonical_name, ce.normalized_name,
                   SUM(CASE WHEN ea.is_canonical = 1 THEN 1 ELSE 0 END)
                       AS canonical_alias_count,
                   MAX(CASE WHEN ea.is_canonical = 1 THEN ea.normalized_alias END)
                       AS canonical_alias_normalized
            FROM canonical_entities ce
            LEFT JOIN entity_aliases ea ON ea.entity_id = ce.id
            GROUP BY ce.id
            ORDER BY ce.id
            """
        ).fetchall()
        checked_entities = len(entities)
        for entity in entities:
            if (
                entity["canonical_alias_count"] != 1
                or entity["canonical_alias_normalized"] != entity["normalized_name"]
                or normalize_entity_name(entity["canonical_name"])
                != entity["normalized_name"]
            ):
                issues.append(
                    LintIssue(
                        "canonical_entity_alias_invalid",
                        entity["id"],
                        "规范实体必须有且仅有一个与规范名称一致的 canonical 别名",
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
    expected_source = {
        "document_version_id": document["current_version_id"],
        "sha256": document["sha256"],
        "classification": document["classification"],
    }
    if source != expected_source:
        issues.append(LintIssue("analysis_source_mismatch", run_id, "分析来源与数据库当前版本不一致"))
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
        lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line]
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
