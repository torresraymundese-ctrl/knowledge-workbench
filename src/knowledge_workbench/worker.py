from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from threading import Event, Thread
from typing import Callable

from .audit import record_event
from .config import WorkspacePaths
from .database import Database
from .errors import InvalidTransitionError, KnowledgeWorkbenchError
from .models import Classification, ClaimedTask, TaskStatus
from .parsers import parse_document
from .pipeline import faithful_analysis, faithful_wiki_generation
from .tasks import claim_next_task, complete_task, fail_task, renew_task_lease
from .utils import sha256_file, utc_now
from .wiki import write_text_atomic


class PermanentTaskError(KnowledgeWorkbenchError):
    pass


@dataclass(frozen=True, slots=True)
class WorkerRunResult:
    task_id: str | None
    task_type: str | None
    status: TaskStatus | None
    message: str


@dataclass(frozen=True, slots=True)
class WorkerServiceResult:
    worker_id: str
    started_at: str
    stopped_at: str
    stop_reason: str
    iterations: int
    claimed_tasks: int
    completed_tasks: int
    retrying_tasks: int
    failed_tasks: int
    lease_errors: int
    empty_polls: int


def run_once(
    database: Database,
    paths: WorkspacePaths,
    *,
    worker_id: str,
    lease_seconds: int = 300,
    heartbeat_interval_seconds: float | None = None,
) -> WorkerRunResult:
    if heartbeat_interval_seconds is not None and heartbeat_interval_seconds <= 0:
        raise KnowledgeWorkbenchError("heartbeat_interval_seconds 必须大于 0")
    task = claim_next_task(database, worker=worker_id, lease_seconds=lease_seconds)
    if task is None:
        return WorkerRunResult(None, None, None, "没有可执行任务")
    heartbeat = _LeaseHeartbeat(
        database,
        task.id,
        worker_id=worker_id,
        lease_seconds=lease_seconds,
        interval_seconds=heartbeat_interval_seconds,
    )
    heartbeat.start()
    failure: tuple[Exception, bool] | None = None
    result: dict | None = None
    try:
        result = _dispatch(task, paths)
    except PermanentTaskError as exc:
        failure = (exc, False)
    except Exception as exc:
        failure = (exc, True)
    finally:
        heartbeat.stop()
    if heartbeat.error is not None:
        return WorkerRunResult(
            task.id,
            task.task_type,
            None,
            f"任务租约续期失败：{heartbeat.error}",
        )
    if failure is not None:
        error, retryable = failure
        try:
            status = fail_task(
                database,
                task.id,
                str(error),
                worker=worker_id,
                retryable=retryable,
            )
        except InvalidTransitionError as exc:
            return WorkerRunResult(task.id, task.task_type, None, str(exc))
        return WorkerRunResult(task.id, task.task_type, status, str(error))
    try:
        complete_task(database, task.id, result, worker=worker_id)
    except InvalidTransitionError as exc:
        return WorkerRunResult(task.id, task.task_type, None, str(exc))
    return WorkerRunResult(task.id, task.task_type, TaskStatus.DONE, "任务完成")


def run_forever(
    database: Database,
    paths: WorkspacePaths,
    *,
    worker_id: str,
    lease_seconds: int = 300,
    poll_seconds: float = 1.0,
    max_poll_seconds: float = 10.0,
    stop_event: Event | None = None,
    stop_when_idle: bool = False,
    max_tasks: int | None = None,
    on_result: Callable[[WorkerRunResult], None] | None = None,
) -> WorkerServiceResult:
    worker_id = _validated_worker_id(worker_id)
    if lease_seconds < 1:
        raise KnowledgeWorkbenchError("lease_seconds 必须大于 0")
    if poll_seconds <= 0:
        raise KnowledgeWorkbenchError("poll_seconds 必须大于 0")
    if max_poll_seconds < poll_seconds:
        raise KnowledgeWorkbenchError("max_poll_seconds 不能小于 poll_seconds")
    if max_tasks is not None and max_tasks < 1:
        raise KnowledgeWorkbenchError("max_tasks 必须大于 0")
    stop_event = stop_event or Event()
    started_at = utc_now()
    _record_worker_event(
        database,
        "worker_started",
        worker_id,
        {
            "lease_seconds": lease_seconds,
            "poll_seconds": poll_seconds,
            "max_poll_seconds": max_poll_seconds,
            "stop_when_idle": stop_when_idle,
            "max_tasks": max_tasks,
        },
    )
    iterations = 0
    claimed_tasks = 0
    completed_tasks = 0
    retrying_tasks = 0
    failed_tasks = 0
    lease_errors = 0
    empty_polls = 0
    idle_delay = poll_seconds
    stop_reason = "stop_requested"
    while not stop_event.is_set():
        iterations += 1
        result = run_once(
            database,
            paths,
            worker_id=worker_id,
            lease_seconds=lease_seconds,
        )
        if on_result is not None:
            on_result(result)
        if result.task_id is None:
            empty_polls += 1
            if stop_when_idle:
                stop_reason = "idle"
                break
            if stop_event.wait(idle_delay):
                stop_reason = "stop_requested"
                break
            idle_delay = min(max_poll_seconds, idle_delay * 2)
            continue
        idle_delay = poll_seconds
        claimed_tasks += 1
        if result.status is TaskStatus.DONE:
            completed_tasks += 1
        elif result.status is TaskStatus.RETRYING:
            retrying_tasks += 1
        elif result.status is TaskStatus.FAILED:
            failed_tasks += 1
        else:
            lease_errors += 1
            stop_reason = "lease_error"
            break
        if max_tasks is not None and claimed_tasks >= max_tasks:
            stop_reason = "max_tasks"
            break
    stopped_at = utc_now()
    service_result = WorkerServiceResult(
        worker_id=worker_id,
        started_at=started_at,
        stopped_at=stopped_at,
        stop_reason=stop_reason,
        iterations=iterations,
        claimed_tasks=claimed_tasks,
        completed_tasks=completed_tasks,
        retrying_tasks=retrying_tasks,
        failed_tasks=failed_tasks,
        lease_errors=lease_errors,
        empty_polls=empty_polls,
    )
    _record_worker_event(
        database,
        "worker_stopped",
        worker_id,
        {
            "stop_reason": stop_reason,
            "iterations": iterations,
            "claimed_tasks": claimed_tasks,
            "completed_tasks": completed_tasks,
            "retrying_tasks": retrying_tasks,
            "failed_tasks": failed_tasks,
            "lease_errors": lease_errors,
            "empty_polls": empty_polls,
        },
    )
    return service_result


class _LeaseHeartbeat:
    def __init__(
        self,
        database: Database,
        task_id: str,
        *,
        worker_id: str,
        lease_seconds: int,
        interval_seconds: float | None,
    ):
        if interval_seconds is not None and interval_seconds <= 0:
            raise KnowledgeWorkbenchError("heartbeat_interval_seconds 必须大于 0")
        self.database = database
        self.task_id = task_id
        self.worker_id = worker_id
        self.lease_seconds = lease_seconds
        self.interval_seconds = interval_seconds or max(0.5, lease_seconds / 3)
        self.error: Exception | None = None
        self._stop_event = Event()
        self._thread = Thread(
            target=self._run,
            name=f"lease-heartbeat-{worker_id}",
            daemon=True,
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        self._thread.join()

    def _run(self) -> None:
        while not self._stop_event.wait(self.interval_seconds):
            try:
                renew_task_lease(
                    self.database,
                    self.task_id,
                    worker=self.worker_id,
                    lease_seconds=self.lease_seconds,
                )
            except Exception as exc:
                self.error = exc
                return


def _record_worker_event(
    database: Database,
    event_type: str,
    worker_id: str,
    details: dict,
) -> None:
    with database.transaction() as connection:
        record_event(
            connection,
            event_type,
            "worker",
            worker_id,
            actor=worker_id,
            details=details,
        )


def _validated_worker_id(value: str) -> str:
    worker_id = value.strip()
    if not worker_id:
        raise KnowledgeWorkbenchError("worker 不能为空")
    if len(worker_id) > 80:
        raise KnowledgeWorkbenchError("worker 不能超过 80 个字符")
    return worker_id


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
