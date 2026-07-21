from __future__ import annotations

import argparse
import importlib.util
import json
import shutil
import sqlite3
import statistics
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Sequence

import numpy as np

from .config import WorkspacePaths, resolve_workspace
from .errors import KnowledgeWorkbenchError
from .evaluation import build_labeling_candidate_pack, evaluate_dataset
from .ingest import ingest_file, initialize_workspace
from .linting import lint_workspace
from .labeling import (
    add_forbidden_substring,
    approve_labeling_session,
    create_labeling_session,
    export_labeling_dataset,
    labeling_session_summary,
    list_labeling_candidates,
    list_labeling_sessions,
    reject_labeling_session,
    remove_expected_evidence,
    remove_forbidden_substring,
    select_expected_evidence,
    submit_labeling_session,
)
from .models import (
    Classification,
    ConflictStatus,
    EvidenceStatus,
    TaskStatus,
)
from .conflicts import transition_conflict
from .conflict_evaluation import evaluate_conflict_dataset
from .citation_evaluation import evaluate_citation_dataset
from .model_pipeline import analyze_with_model, generate_wiki_with_model
from .parsers import parse_document
from .parsers.legacy_word import find_word_executable
from .pipeline import faithful_analysis, faithful_wiki_generation
from .providers import AuditedModelGateway, DeepSeekChatModel
from .parsers import supported_extensions
from .review import publish_revision, request_revision_review, transition_evidence
from .search import search_evidence
from .tasks import (
    claim_next_task,
    complete_task,
    enqueue_task,
    fail_task,
    recover_expired_tasks,
    retry_failed_task,
)
from .utils import sha256_file
from .wiki import write_text_atomic
from .wiki_links import add_wiki_link, remove_wiki_link
from .worker import run_once
from .embeddings import OllamaEmbeddingClient
from .vector_store import (
    NumpyFlatVectorStore,
    build_evidence_index,
    semantic_search,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="knowledge",
        description="证据优先的本地知识编译与审核工具",
    )
    parser.add_argument(
        "--workspace",
        default=None,
        help="运行数据目录，默认 ./workspace 或 KNOWLEDGE_WORKSPACE",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("init", help="初始化本地工作区和数据库")
    subparsers.add_parser("status", help="显示工作区统计")
    subparsers.add_parser("doctor", help="检查本机运行依赖")
    subparsers.add_parser("formats", help="显示支持的文件格式")
    lint = subparsers.add_parser("lint", help="只读检查来源、Schema、证据镜像和 Wiki 一致性")
    lint.add_argument("--output", type=Path)

    ingest = subparsers.add_parser("ingest", help="导入一个文件并创建原子证据和 Wiki 草稿")
    ingest.add_argument("path", type=Path)
    ingest.add_argument(
        "--classification",
        choices=[value.value for value in Classification],
        default=Classification.INTERNAL.value,
    )
    ingest.add_argument("--actor", default="cli")
    ingest.add_argument(
        "--reprocess",
        action="store_true",
        help="同一文件版本在解析器或提取器升级后创建新的派生运行",
    )
    ingest.add_argument(
        "--allow-legacy-word-conversion",
        action="store_true",
        help="显式允许使用本机 Microsoft Word 将旧版 .doc 临时转换为 DOCX",
    )

    evidence = subparsers.add_parser("evidence", help="列出或审核原子证据")
    evidence_sub = evidence.add_subparsers(dest="evidence_command", required=True)
    evidence_list = evidence_sub.add_parser("list", help="列出原子证据")
    evidence_list.add_argument("--status", choices=[value.value for value in EvidenceStatus])
    evidence_list.add_argument("--limit", type=int, default=50)
    evidence_set = evidence_sub.add_parser("set-status", help="执行受约束的证据状态变更")
    evidence_set.add_argument("evidence_id")
    evidence_set.add_argument("status", choices=[value.value for value in EvidenceStatus])
    evidence_set.add_argument("--actor", required=True, help="审核人或操作者")

    page = subparsers.add_parser("page", help="列出和审核 Wiki 页面修订")
    page_sub = page.add_subparsers(dest="page_command", required=True)
    page_sub.add_parser("list", help="列出 Wiki 页面和最新修订")
    page_review = page_sub.add_parser("request-review", help="提交 Wiki 修订审核")
    page_review.add_argument("revision_id")
    page_review.add_argument("--actor", required=True)
    page_publish = page_sub.add_parser("publish", help="发布审核通过的 Wiki 修订")
    page_publish.add_argument("revision_id")
    page_publish.add_argument("--actor", required=True)
    page_link_add = page_sub.add_parser("link-add", help="向 draft 修订添加 Obsidian 知识链接")
    page_link_add.add_argument("source_revision_id")
    page_link_add.add_argument("target_page_id")
    page_link_add.add_argument("--relationship", required=True)
    page_link_add.add_argument("--evidence-id", action="append", required=True)
    page_link_add.add_argument("--actor", required=True)
    page_link_remove = page_sub.add_parser("link-remove", help="从 draft 修订移除知识链接")
    page_link_remove.add_argument("link_id")
    page_link_remove.add_argument("--actor", required=True)
    page_links = page_sub.add_parser("links", help="查看页面的出链和反链")
    page_links.add_argument("page_id")

    search = subparsers.add_parser("search", help="使用 SQLite FTS5 搜索原子证据")
    search.add_argument("query")
    search.add_argument("--limit", type=int, default=10)

    semantic = subparsers.add_parser("semantic-search", help="使用 BGE-M3 向量搜索原子证据")
    semantic.add_argument("query")
    semantic.add_argument("--limit", type=int, default=10)
    semantic.add_argument("--model", default="bge-m3")
    semantic.add_argument("--base-url", default="http://127.0.0.1:11434")

    index = subparsers.add_parser("index", help="构建或检查本地向量索引")
    index_sub = index.add_subparsers(dest="index_command", required=True)
    index_build = index_sub.add_parser("build", help="调用 Ollama BGE-M3 构建精确余弦索引")
    index_build.add_argument("--model", default="bge-m3")
    index_build.add_argument("--base-url", default="http://127.0.0.1:11434")
    index_build.add_argument("--batch-size", type=int, default=16)
    index_sub.add_parser("status", help="显示当前向量索引元数据")

    benchmark = subparsers.add_parser("benchmark", help="测量查询向量生成和向量检索延迟")
    benchmark.add_argument("--query", default="知识证据和资料密级规则")
    benchmark.add_argument("--iterations", type=int, default=3)
    benchmark.add_argument("--model", default="bge-m3")
    benchmark.add_argument("--base-url", default="http://127.0.0.1:11434")
    benchmark.add_argument("--synthetic-count", type=int, default=500)

    audit = subparsers.add_parser("audit", help="查看最近的审计事件")
    audit.add_argument("--limit", type=int, default=30)

    pipeline = subparsers.add_parser("pipeline", help="运行并校验两阶段分析与 Wiki JSON 输出")
    pipeline.add_argument("path", type=Path)
    pipeline.add_argument("--mode", choices=["faithful", "deepseek"], default="faithful")
    pipeline.add_argument(
        "--classification",
        choices=[value.value for value in Classification],
        default=Classification.INTERNAL.value,
    )
    pipeline.add_argument("--allow-internal-cloud-once", action="store_true")
    pipeline.add_argument("--actor", default="cli")
    pipeline.add_argument("--output-dir", type=Path)

    task = subparsers.add_parser("task", help="管理持久化后台任务")
    task_sub = task.add_subparsers(dest="task_command", required=True)
    task_list = task_sub.add_parser("list")
    task_list.add_argument("--status", choices=[value.value for value in TaskStatus])
    task_list.add_argument("--limit", type=int, default=50)
    task_enqueue = task_sub.add_parser("enqueue")
    task_enqueue.add_argument("task_type")
    task_enqueue_payload = task_enqueue.add_mutually_exclusive_group()
    task_enqueue_payload.add_argument("--payload", help="JSON 对象")
    task_enqueue_payload.add_argument("--payload-file", type=Path, help="UTF-8 JSON 对象文件")
    task_enqueue.add_argument("--idempotency-key")
    task_enqueue.add_argument("--priority", type=int, default=0)
    task_enqueue.add_argument("--max-attempts", type=int, default=3)
    task_enqueue.add_argument("--actor", default="cli")
    task_claim = task_sub.add_parser("claim")
    task_claim.add_argument("--worker", required=True)
    task_claim.add_argument("--lease-seconds", type=int, default=300)
    task_complete = task_sub.add_parser("complete")
    task_complete.add_argument("task_id")
    task_complete_result = task_complete.add_mutually_exclusive_group()
    task_complete_result.add_argument("--result", help="JSON 对象")
    task_complete_result.add_argument("--result-file", type=Path, help="UTF-8 JSON 对象文件")
    task_complete.add_argument("--worker", required=True)
    task_fail = task_sub.add_parser("fail")
    task_fail.add_argument("task_id")
    task_fail.add_argument("--error", required=True)
    task_fail.add_argument("--worker", required=True)
    task_retry = task_sub.add_parser("retry")
    task_retry.add_argument("task_id")
    task_retry.add_argument("--actor", required=True)
    task_recover = task_sub.add_parser("recover")
    task_recover.add_argument("--actor", default="cli-recovery")

    worker = subparsers.add_parser("worker", help="运行持久化任务工作器")
    worker_sub = worker.add_subparsers(dest="worker_command", required=True)
    worker_once = worker_sub.add_parser("run-once", help="领取并执行一个任务后退出")
    worker_once.add_argument("--worker", required=True)
    worker_once.add_argument("--lease-seconds", type=int, default=300)

    conflict = subparsers.add_parser("conflict", help="查看和处理潜在证据冲突")
    conflict_sub = conflict.add_subparsers(dest="conflict_command", required=True)
    conflict_list = conflict_sub.add_parser("list")
    conflict_list.add_argument("--status", choices=[value.value for value in ConflictStatus])
    conflict_list.add_argument("--limit", type=int, default=50)
    conflict_set = conflict_sub.add_parser("set-status")
    conflict_set.add_argument("conflict_id")
    conflict_set.add_argument("status", choices=[value.value for value in ConflictStatus])
    conflict_set.add_argument("--actor", required=True)
    conflict_set.add_argument("--note")

    conflict_evaluate = subparsers.add_parser(
        "conflict-evaluate", help="评测冲突检测精确率、召回率和类型准确率"
    )
    conflict_evaluate.add_argument("dataset", type=Path)
    conflict_evaluate.add_argument("--output", type=Path)
    conflict_evaluate.add_argument("--allow-failures", action="store_true")

    citation_evaluate = subparsers.add_parser(
        "citation-evaluate", help="评测结论与引用证据的文本支撑精确率和召回率"
    )
    citation_evaluate.add_argument("dataset", type=Path)
    citation_evaluate.add_argument("--output", type=Path)
    citation_evaluate.add_argument("--allow-failures", action="store_true")

    evaluate = subparsers.add_parser("evaluate", help="运行证据保真质量评测并生成 JSON 报告")
    evaluate.add_argument("dataset", type=Path)
    evaluate.add_argument("--output", type=Path)
    evaluate.add_argument("--allow-failures", action="store_true")
    evaluate.add_argument(
        "--allow-legacy-word-conversion",
        action="store_true",
        help="显式允许使用本机 Microsoft Word 临时转换评测集中的旧版 .doc",
    )
    labeling_pack = subparsers.add_parser(
        "labeling-pack", help="从当前证据生成纯本地人工标注候选包"
    )
    labeling_pack.add_argument("dataset", type=Path)
    labeling_pack.add_argument("--output", type=Path)
    labeling_pack.add_argument("--candidates-per-case", type=int, default=20)
    label = subparsers.add_parser("label", help="可审计的人工黄金标注与双人复核")
    label_sub = label.add_subparsers(dest="label_command", required=True)
    label_sub.add_parser("list", help="列出标注会话")
    label_create = label_sub.add_parser("create", help="从模板创建空标注集")
    label_create.add_argument("template", type=Path)
    label_create.add_argument("--actor", required=True)
    label_create.add_argument("--name")
    label_create.add_argument("--minimum-required-per-case", type=int, default=3)
    label_add = label_sub.add_parser("add-evidence", help="选择当前原子证据")
    label_add.add_argument("session_id")
    label_add.add_argument("case_id")
    label_add.add_argument("evidence_id")
    label_add.add_argument("--actor", required=True)
    label_remove = label_sub.add_parser("remove-evidence", help="移除已选证据")
    label_remove.add_argument("session_id")
    label_remove.add_argument("case_id")
    label_remove.add_argument("evidence_id")
    label_remove.add_argument("--actor", required=True)
    label_forbid = label_sub.add_parser("add-forbidden", help="添加禁止生成的内容")
    label_forbid.add_argument("session_id")
    label_forbid.add_argument("case_id")
    label_forbid.add_argument("value")
    label_forbid.add_argument("--actor", required=True)
    label_unforbid = label_sub.add_parser("remove-forbidden", help="移除禁止生成的内容")
    label_unforbid.add_argument("session_id")
    label_unforbid.add_argument("case_id")
    label_unforbid.add_argument("value")
    label_unforbid.add_argument("--actor", required=True)
    label_submit = label_sub.add_parser("submit", help="提交双人复核")
    label_submit.add_argument("session_id")
    label_submit.add_argument("--actor", required=True)
    label_approve = label_sub.add_parser("approve", help="由另一位审核人批准")
    label_approve.add_argument("session_id")
    label_approve.add_argument("--actor", required=True)
    label_reject = label_sub.add_parser("reject", help="驳回并返回draft")
    label_reject.add_argument("session_id")
    label_reject.add_argument("--actor", required=True)
    label_reject.add_argument("--note", required=True)
    label_show = label_sub.add_parser("show", help="查看标注集与各用例进度")
    label_show.add_argument("session_id")
    label_candidates = label_sub.add_parser(
        "candidates", help="分页查看某个用例的当前候选证据"
    )
    label_candidates.add_argument("session_id")
    label_candidates.add_argument("case_id")
    label_candidates.add_argument("--limit", type=int, default=20)
    label_candidates.add_argument("--offset", type=int, default=0)
    label_candidates.add_argument("--only-unselected", action="store_true")
    label_candidates.add_argument(
        "--full", action="store_true", help="显示完整原文；restricted密级始终隐藏"
    )
    label_export = label_sub.add_parser("export", help="导出已批准评测集")
    label_export.add_argument("session_id")
    label_export.add_argument("output", type=Path)
    label_export.add_argument("--actor", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    paths = resolve_workspace(args.workspace)
    try:
        if args.command == "doctor":
            return _doctor()
        if args.command == "formats":
            print("\n".join(supported_extensions()))
            return 0

        database = initialize_workspace(paths)
        if args.command == "init":
            print(f"工作区已初始化：{paths.root}")
            print(f"数据库：{paths.database}")
        elif args.command == "status":
            _status(database, paths)
        elif args.command == "lint":
            _handle_lint(database, paths, args)
        elif args.command == "ingest":
            result = ingest_file(
                args.path,
                paths,
                Classification(args.classification),
                actor=args.actor,
                reprocess=args.reprocess,
                allow_legacy_word_conversion=args.allow_legacy_word_conversion,
            )
            if result.duplicate:
                if args.reprocess:
                    print("当前解析器和提取器的派生结果已存在，已跳过重处理。")
                else:
                    print("检测到相同 SHA-256，已跳过重复导入。")
            elif result.reprocessed:
                print("重处理完成；原始文件版本未重复创建。")
            else:
                print("导入完成。")
            _print_mapping(
                {
                    "document_id": result.document_id,
                    "version_id": result.version_id,
                    "sha256": result.sha256,
                    "evidence_count": result.evidence_count,
                    "page_id": result.page_id,
                    "revision_id": result.revision_id,
                    "processing_run_id": result.processing_run_id,
                    "potential_conflicts": result.conflict_count,
                }
            )
        elif args.command == "evidence":
            _handle_evidence(args, database)
        elif args.command == "page":
            _handle_page(args, database, paths)
        elif args.command == "search":
            _handle_search(database, args.query, args.limit)
        elif args.command == "semantic-search":
            _handle_semantic_search(database, paths, args)
        elif args.command == "index":
            _handle_index(database, paths, args)
        elif args.command == "benchmark":
            _benchmark(database, paths, args)
        elif args.command == "audit":
            _show_audit(database, args.limit)
        elif args.command == "pipeline":
            _handle_pipeline(database, paths, args)
        elif args.command == "task":
            _handle_task(database, args)
        elif args.command == "conflict":
            _handle_conflict(database, args)
        elif args.command == "conflict-evaluate":
            _handle_conflict_evaluate(paths, args)
        elif args.command == "citation-evaluate":
            _handle_citation_evaluate(paths, args)
        elif args.command == "evaluate":
            _handle_evaluate(paths, args)
        elif args.command == "labeling-pack":
            _handle_labeling_pack(database, paths, args)
        elif args.command == "label":
            _handle_label(database, paths, args)
        elif args.command == "worker":
            _handle_worker(database, paths, args)
        return 0
    except KnowledgeWorkbenchError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    except sqlite3.OperationalError as exc:
        print(f"数据库错误：{exc}", file=sys.stderr)
        return 3


def _status(database, paths: WorkspacePaths) -> None:
    with database.connect() as connection:
        counts = {
            "schema_version": connection.execute(
                "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
            ).fetchone()[0],
            "documents": connection.execute("SELECT COUNT(*) FROM documents").fetchone()[0],
            "versions": connection.execute("SELECT COUNT(*) FROM document_versions").fetchone()[0],
            "processing_runs": connection.execute(
                "SELECT COUNT(*) FROM processing_runs"
            ).fetchone()[0],
            "evidence": connection.execute("SELECT COUNT(*) FROM evidence").fetchone()[0],
            "current_evidence": connection.execute(
                """
                SELECT COUNT(*) FROM evidence e
                JOIN processing_runs pr ON pr.id = e.processing_run_id
                JOIN document_versions dv ON dv.id = e.document_version_id
                JOIN documents d ON d.id = dv.document_id
                WHERE pr.is_current = 1 AND d.current_version_id = dv.id
                """
            ).fetchone()[0],
            "draft_evidence": connection.execute(
                """
                SELECT COUNT(*) FROM evidence e
                JOIN processing_runs pr ON pr.id = e.processing_run_id
                JOIN document_versions dv ON dv.id = e.document_version_id
                JOIN documents d ON d.id = dv.document_id
                WHERE pr.is_current = 1 AND d.current_version_id = dv.id
                  AND e.status = 'draft'
                """
            ).fetchone()[0],
            "verified_evidence": connection.execute(
                """
                SELECT COUNT(*) FROM evidence e
                JOIN processing_runs pr ON pr.id = e.processing_run_id
                JOIN document_versions dv ON dv.id = e.document_version_id
                JOIN documents d ON d.id = dv.document_id
                WHERE pr.is_current = 1 AND d.current_version_id = dv.id
                  AND e.status = 'verified'
                """
            ).fetchone()[0],
            "wiki_pages": connection.execute("SELECT COUNT(*) FROM wiki_pages").fetchone()[0],
            "verified_pages": connection.execute(
                "SELECT COUNT(*) FROM wiki_pages WHERE status = 'verified'"
            ).fetchone()[0],
            "open_conflicts": connection.execute(
                "SELECT COUNT(*) FROM conflicts WHERE status IN ('pending', 'reviewing')"
            ).fetchone()[0],
            "active_tasks": connection.execute(
                "SELECT COUNT(*) FROM tasks WHERE status IN ('pending', 'running', 'retrying')"
            ).fetchone()[0],
            "labeling_sessions": connection.execute(
                "SELECT COUNT(*) FROM labeling_sessions"
            ).fetchone()[0],
        }
    print(f"工作区：{paths.root}")
    _print_mapping(counts)


def _handle_lint(database, paths: WorkspacePaths, args) -> None:
    report = lint_workspace(database, paths)
    if args.output:
        output = args.output.expanduser().resolve()
        write_text_atomic(
            output,
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        )
        print(f"Lint 报告：{output}")
    _print_mapping(report["summary"])
    for issue in report["issues"][:50]:
        print(
            f"[{issue['severity']}:{issue['code']}] "
            f"{issue['entity_id']}: {issue['message']}"
        )
    if len(report["issues"]) > 50:
        print(f"另有 {len(report['issues']) - 50} 个问题未在终端展开。")
    if not report["passed"]:
        raise KnowledgeWorkbenchError("工作区 Lint 未通过")


def _handle_evidence(args, database) -> None:
    if args.evidence_command == "set-status":
        transition_evidence(
            database,
            args.evidence_id,
            EvidenceStatus(args.status),
            actor=args.actor,
        )
        print(f"证据 {args.evidence_id} 已变更为 {args.status}")
        return

    query = """
        SELECT e.id, e.status, e.excerpt, e.locator_json, d.original_name
        FROM evidence e
        JOIN processing_runs pr ON pr.id = e.processing_run_id AND pr.is_current = 1
        JOIN document_versions dv ON dv.id = e.document_version_id
        JOIN documents d ON d.id = dv.document_id
        WHERE d.current_version_id = dv.id
    """
    parameters: list[object] = []
    if args.status:
        query += " AND e.status = ?"
        parameters.append(args.status)
    query += " ORDER BY e.created_at DESC, e.ordinal LIMIT ?"
    parameters.append(args.limit)
    with database.connect() as connection:
        rows = connection.execute(query, parameters).fetchall()
    if not rows:
        print("暂无原子证据。")
        return
    for row in rows:
        excerpt = " ".join(row["excerpt"].split())
        if len(excerpt) > 100:
            excerpt = excerpt[:97] + "..."
        print(f"{row['id']}  [{row['status']}]  {row['original_name']}")
        print(f"  {excerpt}")
        print(f"  locator={row['locator_json']}")


def _handle_page(args, database, paths: WorkspacePaths) -> None:
    if args.page_command == "request-review":
        request_revision_review(database, args.revision_id, actor=args.actor)
        print(f"修订 {args.revision_id} 已提交审核")
        return
    if args.page_command == "publish":
        output = publish_revision(
            database, paths, args.revision_id, actor=args.actor
        )
        print(f"修订已发布：{output}")
        return
    if args.page_command == "link-add":
        link_id = add_wiki_link(
            database,
            paths,
            source_revision_id=args.source_revision_id,
            target_page_id=args.target_page_id,
            relationship=args.relationship,
            evidence_ids=args.evidence_id,
            actor=args.actor,
        )
        print(f"知识链接已添加：{link_id}")
        return
    if args.page_command == "link-remove":
        remove_wiki_link(database, paths, args.link_id, actor=args.actor)
        print(f"知识链接已移除：{args.link_id}")
        return
    if args.page_command == "links":
        _show_page_links(database, args.page_id)
        return
    with database.connect() as connection:
        rows = connection.execute(
            """
            SELECT wp.id, wp.title, wp.status, wp.current_verified_revision_id,
                   wr.id AS latest_revision_id, wr.revision_number, wr.status AS revision_status,
                   wr.markdown_path
            FROM wiki_pages wp
            LEFT JOIN wiki_revisions wr ON wr.id = (
                SELECT id FROM wiki_revisions
                WHERE page_id = wp.id ORDER BY revision_number DESC LIMIT 1
            )
            ORDER BY wp.updated_at DESC
            """
        ).fetchall()
    if not rows:
        print("暂无 Wiki 页面。")
        return
    for row in rows:
        print(f"{row['id']}  {row['title']}  [page:{row['status']}]")
        print(
            f"  revision={row['latest_revision_id']} r{row['revision_number']} "
            f"[{row['revision_status']}] {row['markdown_path']}"
        )


def _show_page_links(database, page_id: str) -> None:
    with database.connect() as connection:
        page = connection.execute(
            "SELECT title FROM wiki_pages WHERE id = ?", (page_id,)
        ).fetchone()
        if not page:
            raise KnowledgeWorkbenchError(f"Wiki 页面不存在：{page_id}")
        outgoing = connection.execute(
            """
            SELECT wl.id, wl.relationship, source.revision_number,
                   target.id AS target_page_id, target.title AS target_title
            FROM wiki_links wl
            JOIN wiki_revisions source ON source.id = wl.source_revision_id
            JOIN wiki_pages target ON target.id = wl.target_page_id
            WHERE source.page_id = ?
            ORDER BY source.revision_number DESC, target.title
            """,
            (page_id,),
        ).fetchall()
        incoming = connection.execute(
            """
            SELECT wl.id, wl.relationship, source.revision_number,
                   source_page.id AS source_page_id, source_page.title AS source_title
            FROM wiki_links wl
            JOIN wiki_revisions source ON source.id = wl.source_revision_id
            JOIN wiki_pages source_page ON source_page.id = source.page_id
            WHERE wl.target_page_id = ?
            ORDER BY source_page.title, source.revision_number DESC
            """,
            (page_id,),
        ).fetchall()
    print(f"页面：{page['title']}")
    print("出链：")
    if not outgoing:
        print("  无")
    for row in outgoing:
        print(
            f"  {row['id']} -> {row['target_title']} "
            f"({row['relationship']}, source r{row['revision_number']})"
        )
    print("反链：")
    if not incoming:
        print("  无")
    for row in incoming:
        print(
            f"  {row['id']} <- {row['source_title']} "
            f"({row['relationship']}, source r{row['revision_number']})"
        )


def _handle_search(database, query: str, limit: int) -> None:
    rows = search_evidence(database, query, limit)
    if not rows:
        print("没有匹配的证据。")
        return
    for row in rows:
        excerpt = " ".join(row["excerpt"].split())
        print(f"{row['id']}  [{row['status']}]  score={row['score']:.4f}")
        print(f"  {row['original_name']} ({row['classification']})")
        print(f"  {excerpt[:240]}")


def _handle_semantic_search(database, paths: WorkspacePaths, args) -> None:
    store = NumpyFlatVectorStore(paths.index)
    client = OllamaEmbeddingClient(model=args.model, base_url=args.base_url)
    results = semantic_search(database, store, client, args.query, args.limit)
    if not results:
        print("没有匹配的证据。")
        return
    for row, score in results:
        excerpt = " ".join(row["excerpt"].split())
        print(f"{row['id']}  [{row['status']}]  cosine={score:.4f}")
        print(f"  {row['original_name']} ({row['classification']})")
        print(f"  {excerpt[:240]}")


def _handle_index(database, paths: WorkspacePaths, args) -> None:
    store = NumpyFlatVectorStore(paths.index)
    if args.index_command == "status":
        _print_mapping(store.metadata())
        return
    if args.batch_size < 1:
        raise KnowledgeWorkbenchError("batch-size 必须大于 0")
    client = OllamaEmbeddingClient(model=args.model, base_url=args.base_url)
    stats = build_evidence_index(
        database, store, client, batch_size=args.batch_size
    )
    print("向量索引构建完成。")
    _print_mapping(
        {
            "evidence_count": stats.evidence_count,
            "dimension": stats.dimension,
            "embedding_seconds": f"{stats.embedding_seconds:.3f}",
            "write_seconds": f"{stats.write_seconds:.3f}",
            "total_seconds": f"{stats.total_seconds:.3f}",
        }
    )


def _benchmark(database, paths: WorkspacePaths, args) -> None:
    if args.iterations < 1:
        raise KnowledgeWorkbenchError("iterations 必须大于 0")
    store = NumpyFlatVectorStore(paths.index)
    client = OllamaEmbeddingClient(model=args.model, base_url=args.base_url)
    embedding_times: list[float] = []
    search_times: list[float] = []
    for _ in range(args.iterations):
        started = time.perf_counter()
        vector = client.embed([args.query])[0]
        embedding_times.append(time.perf_counter() - started)
        started = time.perf_counter()
        store.search(vector, limit=30)
        search_times.append(time.perf_counter() - started)
    metadata = store.metadata()
    long_text = (args.query * (1000 // max(len(args.query), 1) + 1))[:1000]
    started = time.perf_counter()
    client.embed([long_text])
    long_embedding_seconds = time.perf_counter() - started

    synthetic_count = max(args.synthetic_count, 1)
    rng = np.random.default_rng(42)
    synthetic = rng.standard_normal(
        (synthetic_count, metadata["dimension"]), dtype=np.float32
    )
    synthetic /= np.linalg.norm(synthetic, axis=1, keepdims=True)
    normalized_query = vector / max(float(np.linalg.norm(vector)), 1e-12)
    synthetic_times: list[float] = []
    for _ in range(max(args.iterations, 20)):
        started = time.perf_counter()
        scores = synthetic @ normalized_query
        count = min(30, synthetic_count)
        np.argpartition(scores, -count)[-count:]
        synthetic_times.append(time.perf_counter() - started)
    _print_mapping(
        {
            "model": args.model,
            "index_count": metadata["count"],
            "dimension": metadata["dimension"],
            "iterations": args.iterations,
            "query_embedding_median_ms": f"{statistics.median(embedding_times) * 1000:.2f}",
            "vector_search_median_ms": f"{statistics.median(search_times) * 1000:.2f}",
            "embedding_min_ms": f"{min(embedding_times) * 1000:.2f}",
            "embedding_max_ms": f"{max(embedding_times) * 1000:.2f}",
            "embedding_1000_chars_ms": f"{long_embedding_seconds * 1000:.2f}",
            "synthetic_vector_count": synthetic_count,
            "synthetic_matrix_mb": f"{synthetic.nbytes / 1024 / 1024:.2f}",
            "synthetic_search_median_ms": f"{statistics.median(synthetic_times) * 1000:.3f}",
        }
    )


def _show_audit(database, limit: int) -> None:
    with database.connect() as connection:
        rows = connection.execute(
            "SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    if not rows:
        print("暂无审计事件。")
        return
    for row in rows:
        details = json.loads(row["details_json"])
        print(
            f"{row['created_at']}  {row['event_type']}  "
            f"{row['entity_type']}:{row['entity_id']}  actor={row['actor']}"
        )
        if details:
            print("  " + json.dumps(details, ensure_ascii=False, sort_keys=True))


def _handle_pipeline(database, paths: WorkspacePaths, args) -> None:
    source = args.path.expanduser().resolve()
    if not source.is_file():
        raise KnowledgeWorkbenchError(f"文件不存在：{source}")
    digest = sha256_file(source)
    version_id = f"ver_{digest[:32]}"
    classification = Classification(args.classification)
    parsed = parse_document(source)
    if args.mode == "faithful":
        analysis = faithful_analysis(
            parsed,
            document_version_id=version_id,
            source_sha256=digest,
            classification=classification,
        )
        generation = faithful_wiki_generation(analysis, title=source.stem)
    else:
        gateway = AuditedModelGateway(database, DeepSeekChatModel.from_environment())
        analysis = analyze_with_model(
            gateway,
            parsed,
            document_version_id=version_id,
            source_sha256=digest,
            classification=classification,
            actor=args.actor,
            allow_internal_cloud_once=args.allow_internal_cloud_once,
        )
        generation = generate_wiki_with_model(
            gateway,
            analysis,
            title=source.stem,
            classification=classification,
            actor=args.actor,
            allow_internal_cloud_once=args.allow_internal_cloud_once,
        )
    output_dir = (args.output_dir or paths.analysis).expanduser().resolve()
    analysis_path = output_dir / f"{digest}.analysis.json"
    generation_path = output_dir / f"{digest}.wiki-generation.json"
    write_text_atomic(
        analysis_path,
        json.dumps(analysis, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )
    write_text_atomic(
        generation_path,
        json.dumps(generation, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )
    print("两阶段输出已通过 Schema 和证据引用校验。")
    print(f"analysis: {analysis_path}")
    print(f"wiki_generation: {generation_path}")


def _handle_task(database, args) -> None:
    if args.task_command == "enqueue":
        task_id = enqueue_task(
            database,
            args.task_type,
            _json_object_input(args.payload, args.payload_file, "payload"),
            idempotency_key=args.idempotency_key,
            priority=args.priority,
            max_attempts=args.max_attempts,
            actor=args.actor,
        )
        print(f"任务已入队：{task_id}")
        return
    if args.task_command == "claim":
        task = claim_next_task(
            database, worker=args.worker, lease_seconds=args.lease_seconds
        )
        if not task:
            print("没有可领取的任务。")
        else:
            print(json.dumps({
                "id": task.id,
                "task_type": task.task_type,
                "payload": task.payload,
                "attempts": task.attempts,
                "max_attempts": task.max_attempts,
            }, ensure_ascii=False, indent=2))
        return
    if args.task_command == "complete":
        complete_task(
            database,
            args.task_id,
            _json_object_input(args.result, args.result_file, "result"),
            worker=args.worker,
        )
        print(f"任务已完成：{args.task_id}")
        return
    if args.task_command == "fail":
        status = fail_task(
            database, args.task_id, args.error, worker=args.worker
        )
        print(f"任务失败状态：{status.value}")
        return
    if args.task_command == "retry":
        retry_failed_task(database, args.task_id, actor=args.actor)
        print(f"任务已重新进入重试队列：{args.task_id}")
        return
    if args.task_command == "recover":
        count = recover_expired_tasks(database, actor=args.actor)
        print(f"已恢复过期租约任务：{count}")
        return
    query = "SELECT * FROM tasks"
    parameters: list[object] = []
    if args.status:
        query += " WHERE status = ?"
        parameters.append(args.status)
    query += " ORDER BY priority DESC, created_at DESC LIMIT ?"
    parameters.append(args.limit)
    with database.connect() as connection:
        rows = connection.execute(query, parameters).fetchall()
    if not rows:
        print("暂无任务。")
    for row in rows:
        print(
            f"{row['id']} [{row['status']}] {row['task_type']} "
            f"attempts={row['attempts']}/{row['max_attempts']}"
        )
        if row["last_error"]:
            print(f"  error={row['last_error']}")


def _handle_conflict(database, args) -> None:
    if args.conflict_command == "set-status":
        transition_conflict(
            database,
            args.conflict_id,
            ConflictStatus(args.status),
            actor=args.actor,
            note=args.note,
        )
        print(f"冲突 {args.conflict_id} 已变更为 {args.status}")
        return
    query = """
        SELECT c.*, old.excerpt AS older_excerpt, new.excerpt AS newer_excerpt
        FROM conflicts c
        JOIN evidence old ON old.id = c.older_evidence_id
        JOIN evidence new ON new.id = c.newer_evidence_id
    """
    parameters: list[object] = []
    if args.status:
        query += " WHERE c.status = ?"
        parameters.append(args.status)
    query += " ORDER BY c.created_at DESC LIMIT ?"
    parameters.append(args.limit)
    with database.connect() as connection:
        rows = connection.execute(query, parameters).fetchall()
    if not rows:
        print("暂无潜在冲突。")
    for row in rows:
        print(
            f"{row['id']} [{row['status']}] {row['conflict_type']} "
            f"similarity={row['similarity_score']}"
        )
        print(f"  旧：{row['older_excerpt']}")
        print(f"  新：{row['newer_excerpt']}")
        print(f"  原因：{row['reason']}")


def _json_object_argument(value: str, name: str) -> dict:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise KnowledgeWorkbenchError(f"{name} 不是有效 JSON") from exc
    if not isinstance(parsed, dict):
        raise KnowledgeWorkbenchError(f"{name} 顶层必须是 JSON 对象")
    return parsed


def _json_object_input(value: str | None, file_path: Path | None, name: str) -> dict:
    if file_path is not None:
        try:
            value = file_path.expanduser().read_text(encoding="utf-8")
        except OSError as exc:
            raise KnowledgeWorkbenchError(f"无法读取 {name} 文件：{exc}") from exc
    return _json_object_argument(value or "{}", name)


def _handle_evaluate(paths: WorkspacePaths, args) -> None:
    report = evaluate_dataset(
        args.dataset,
        allow_legacy_word_conversion=args.allow_legacy_word_conversion,
    )
    output = args.output
    if output is None:
        timestamp = report["evaluated_at"].replace(":", "").replace("+", "-")
        output = paths.evaluations / f"evaluation-{timestamp}.json"
    output = output.expanduser().resolve()
    write_text_atomic(
        output,
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )
    aggregate = report["aggregate"]
    print(f"评测报告：{output}")
    _print_mapping(aggregate)
    if aggregate["pass_rate"] < 1.0 and not args.allow_failures:
        raise KnowledgeWorkbenchError("质量评测未全部通过；报告已保存")


def _handle_conflict_evaluate(paths: WorkspacePaths, args) -> None:
    report = evaluate_conflict_dataset(args.dataset)
    output = args.output
    if output is None:
        timestamp = report["evaluated_at"].replace(":", "").replace("+", "-")
        output = paths.evaluations / f"conflict-evaluation-{timestamp}.json"
    output = output.expanduser().resolve()
    write_text_atomic(
        output,
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )
    aggregate = report["aggregate"]
    print(f"冲突评测报告：{output}")
    _print_mapping(aggregate)
    if aggregate["pass_rate"] < 1.0 and not args.allow_failures:
        raise KnowledgeWorkbenchError("冲突评测未全部通过；报告已保存")


def _handle_citation_evaluate(paths: WorkspacePaths, args) -> None:
    report = evaluate_citation_dataset(args.dataset)
    output = args.output
    if output is None:
        timestamp = report["evaluated_at"].replace(":", "").replace("+", "-")
        output = paths.evaluations / f"citation-evaluation-{timestamp}.json"
    output = output.expanduser().resolve()
    write_text_atomic(
        output,
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )
    aggregate = report["aggregate"]
    print(f"引用支撑评测报告：{output}")
    _print_mapping(aggregate)
    if aggregate["pass_rate"] < 1.0 and not args.allow_failures:
        raise KnowledgeWorkbenchError("引用支撑评测未全部通过；报告已保存")


def _handle_labeling_pack(database, paths: WorkspacePaths, args) -> None:
    pack = build_labeling_candidate_pack(
        database,
        args.dataset,
        candidates_per_case=args.candidates_per_case,
    )
    output = args.output
    if output is None:
        output = paths.evaluations / f"{args.dataset.stem}.candidates.json"
    output = output.expanduser().resolve()
    write_text_atomic(
        output,
        json.dumps(pack, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )
    print(f"标注候选包：{output}")
    _print_mapping(
        {
            "case_count": len(pack["cases"]),
            "candidate_count": sum(
                len(case["candidate_evidence"]) for case in pack["cases"]
            ),
            "selection_method": pack["selection_method"],
        }
    )


def _handle_label(database, paths: WorkspacePaths, args) -> None:
    command = args.label_command
    if command == "list":
        sessions = list_labeling_sessions(database)
        if not sessions:
            print("暂无标注会话。")
        for session in sessions:
            print(
                f"{session['id']} [{session['status']}] {session['name']} | "
                f"cases={session['case_count']} "
                f"selected={session['selected_evidence_count']} "
                f"creator={session['created_by']}"
            )
    elif command == "create":
        session_id = create_labeling_session(
            database,
            args.template,
            actor=args.actor,
            name=args.name,
            minimum_required_per_case=args.minimum_required_per_case,
        )
        print(f"标注集已创建：{session_id}")
    elif command == "add-evidence":
        select_expected_evidence(
            database,
            args.session_id,
            args.case_id,
            args.evidence_id,
            actor=args.actor,
        )
        print("必要证据已选择。")
    elif command == "remove-evidence":
        remove_expected_evidence(
            database,
            args.session_id,
            args.case_id,
            args.evidence_id,
            actor=args.actor,
        )
        print("必要证据已移除。")
    elif command == "add-forbidden":
        add_forbidden_substring(
            database,
            args.session_id,
            args.case_id,
            args.value,
            actor=args.actor,
        )
        print("禁止内容已添加。")
    elif command == "remove-forbidden":
        remove_forbidden_substring(
            database,
            args.session_id,
            args.case_id,
            args.value,
            actor=args.actor,
        )
        print("禁止内容已移除。")
    elif command == "submit":
        submit_labeling_session(database, args.session_id, actor=args.actor)
        print("标注集已提交复核。")
    elif command == "approve":
        approve_labeling_session(database, args.session_id, actor=args.actor)
        print("标注集已由第二位审核人批准。")
    elif command == "reject":
        reject_labeling_session(
            database,
            args.session_id,
            actor=args.actor,
            note=args.note,
        )
        print("标注集已驳回并返回 draft。")
    elif command == "show":
        summary = labeling_session_summary(database, args.session_id)
        _print_mapping(summary["session"])
        for case in summary["cases"]:
            print(
                f"{case['case_id']} | {case['classification']} | "
                f"selected={case['selected_evidence_count']} | "
                f"forbidden={case['forbidden_count']}"
            )
            if case["selected_evidence_ids"]:
                print(f"  evidence: {case['selected_evidence_ids']}")
    elif command == "candidates":
        result = list_labeling_candidates(
            database,
            args.session_id,
            args.case_id,
            limit=args.limit,
            offset=args.offset,
            only_unselected=args.only_unselected,
        )
        case = result["case"]
        _print_mapping(
            {
                "case_id": case["case_id"],
                "classification": case["classification"],
                "source_path": case["source_path"],
                "total": result["total"],
                "offset": result["offset"],
                "returned": len(result["candidates"]),
                "only_unselected": result["only_unselected"],
            }
        )
        for item in result["candidates"]:
            marker = "x" if item["selected"] else " "
            print(
                f"[{marker}] #{item['run_ordinal']} {item['id']} "
                f"[{item['status']}]"
            )
            print(
                "  locator: "
                + json.dumps(item["locator"], ensure_ascii=False, sort_keys=True)
            )
            if case["classification"] == "restricted":
                excerpt = "[restricted 内容不在 CLI 显示，请回原文件核对]"
            elif args.full or len(item["excerpt"]) <= 240:
                excerpt = item["excerpt"]
            else:
                excerpt = item["excerpt"][:237] + "..."
            print(f"  excerpt: {excerpt}")
    elif command == "export":
        output = export_labeling_dataset(
            database,
            paths,
            args.session_id,
            args.output,
            actor=args.actor,
        )
        print(f"正式评测集已导出：{output}")


def _handle_worker(database, paths: WorkspacePaths, args) -> None:
    result = run_once(
        database,
        paths,
        worker_id=args.worker,
        lease_seconds=args.lease_seconds,
    )
    if result.task_id is None:
        print(result.message)
        return
    print(
        f"{result.task_id} {result.task_type} "
        f"[{result.status.value if result.status else 'unknown'}] {result.message}"
    )


def _doctor() -> int:
    packages = {
        "numpy": "向量索引（后续里程碑）",
        "pypdf": "PDF 解析",
        "docx": "Word 解析",
        "openpyxl": "Excel 解析",
        "pptx": "PowerPoint 解析",
    }
    print(f"Python: {sys.version.split()[0]}")
    print(f"Ollama CLI: {_find_ollama() or '未在 PATH 或可读目录中找到'}")
    word_path = find_word_executable()
    print(f"Microsoft Word .doc 转换: {word_path or '不可用'}")
    models = _ollama_models()
    print(f"Ollama API: {'可用（' + ', '.join(models) + '）' if models else '不可用'}")
    for package, purpose in packages.items():
        status = "可用" if importlib.util.find_spec(package) else "未安装"
        print(f"{package}: {status}（{purpose}）")
    try:
        with sqlite3.connect(":memory:") as connection:
            connection.execute("CREATE VIRTUAL TABLE test_fts USING fts5(value)")
        print("SQLite FTS5: 可用")
    except sqlite3.OperationalError:
        print("SQLite FTS5: 不可用")
        return 1
    return 0


def _find_ollama() -> str | None:
    if executable := shutil.which("ollama"):
        return executable
    candidates = [
        Path.home() / "AppData/Local/Programs/Ollama/ollama.exe",
        Path.home() / "AppData/Local/Ollama/ollama.exe",
        Path("C:/Program Files/Ollama/ollama.exe"),
    ]
    for path in candidates:
        try:
            if path.is_file():
                return str(path)
        except OSError:
            continue
    return None


def _ollama_models() -> list[str]:
    try:
        with urllib.request.urlopen(
            "http://127.0.0.1:11434/api/tags", timeout=2
        ) as response:
            payload = json.load(response)
        return [model["name"] for model in payload.get("models", [])]
    except (OSError, urllib.error.URLError, json.JSONDecodeError, KeyError):
        return []


def _print_mapping(values: dict[str, object]) -> None:
    width = max(len(key) for key in values)
    for key, value in values.items():
        print(f"{key:<{width}} : {value}")


if __name__ == "__main__":
    raise SystemExit(main())
