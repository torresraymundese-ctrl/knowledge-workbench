from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .config import WorkspacePaths
from .database import Database
from .errors import KnowledgeWorkbenchError
from .models import Classification, ClaimedTask, TaskStatus
from .parsers import parse_document
from .pipeline import faithful_analysis, faithful_wiki_generation
from .tasks import claim_next_task, complete_task, fail_task
from .utils import sha256_file
from .wiki import write_text_atomic


class PermanentTaskError(KnowledgeWorkbenchError):
    pass


@dataclass(frozen=True, slots=True)
class WorkerRunResult:
    task_id: str | None
    task_type: str | None
    status: TaskStatus | None
    message: str


def run_once(
    database: Database,
    paths: WorkspacePaths,
    *,
    worker_id: str,
    lease_seconds: int = 300,
) -> WorkerRunResult:
    task = claim_next_task(database, worker=worker_id, lease_seconds=lease_seconds)
    if task is None:
        return WorkerRunResult(None, None, None, "没有可执行任务")
    try:
        result = _dispatch(task, paths)
    except PermanentTaskError as exc:
        status = fail_task(
            database,
            task.id,
            str(exc),
            worker=worker_id,
            retryable=False,
        )
        return WorkerRunResult(task.id, task.task_type, status, str(exc))
    except Exception as exc:
        status = fail_task(
            database,
            task.id,
            str(exc),
            worker=worker_id,
            retryable=True,
        )
        return WorkerRunResult(task.id, task.task_type, status, str(exc))
    complete_task(database, task.id, result, worker=worker_id)
    return WorkerRunResult(task.id, task.task_type, TaskStatus.DONE, "任务完成")


def _dispatch(task: ClaimedTask, paths: WorkspacePaths) -> dict:
    handlers: dict[str, Callable[[dict, WorkspacePaths], dict]] = {
        "faithful_pipeline": _faithful_pipeline,
    }
    handler = handlers.get(task.task_type)
    if handler is None:
        raise PermanentTaskError(f"未知任务类型：{task.task_type}")
    return handler(task.payload, paths)


def _faithful_pipeline(payload: dict, paths: WorkspacePaths) -> dict:
    source_value = payload.get("source_path")
    if not isinstance(source_value, str) or not source_value.strip():
        raise PermanentTaskError("faithful_pipeline 缺少 source_path")
    try:
        classification = Classification(payload.get("classification", "internal"))
    except ValueError as exc:
        raise PermanentTaskError("faithful_pipeline classification 无效") from exc
    source = Path(source_value).expanduser().resolve()
    if not source.is_file():
        raise PermanentTaskError(f"来源文件不存在：{source}")
    parsed = parse_document(source)
    digest = sha256_file(source)
    analysis = faithful_analysis(
        parsed,
        document_version_id=f"ver_{digest[:32]}",
        source_sha256=digest,
        classification=classification,
    )
    generation = faithful_wiki_generation(analysis, title=source.stem)
    output_dir_value = payload.get("output_dir")
    output_dir = (
        Path(output_dir_value).expanduser().resolve()
        if isinstance(output_dir_value, str) and output_dir_value.strip()
        else paths.analysis
    )
    if not output_dir.is_relative_to(paths.root):
        raise PermanentTaskError("后台任务 output_dir 必须位于知识工作区内部")
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
    return {
        "analysis_path": str(analysis_path),
        "wiki_generation_path": str(generation_path),
        "evidence_count": len(analysis["evidence"]),
        "schema_version": analysis["schema_version"],
    }
