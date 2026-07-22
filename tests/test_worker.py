import json
import tempfile
import unittest
from pathlib import Path
from threading import Event
from unittest.mock import patch

from knowledge_workbench.config import WorkspacePaths
from knowledge_workbench.ingest import initialize_workspace
from knowledge_workbench.tasks import enqueue_task
from knowledge_workbench.worker import run_forever, run_once


class WorkerTests(unittest.TestCase):
    def test_worker_executes_faithful_pipeline_and_completes_task(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.md"
            source.write_text("# 规则\n\n知识必须有证据。", encoding="utf-8")
            paths = WorkspacePaths(root / "workspace")
            database = initialize_workspace(paths)
            task_id = enqueue_task(
                database,
                "faithful_pipeline",
                {"source_path": str(source), "classification": "internal"},
            )
            result = run_once(database, paths, worker_id="worker-test")
            self.assertEqual(result.task_id, task_id)
            self.assertEqual(result.status.value, "done")
            with database.connect() as connection:
                task = connection.execute(
                    "SELECT status, result_json FROM tasks WHERE id = ?", (task_id,)
                ).fetchone()
            output = json.loads(task["result_json"])
            self.assertEqual(task["status"], "done")
            self.assertEqual(output["evidence_count"], 1)
            self.assertTrue(Path(output["analysis_path"]).is_file())
            self.assertTrue(Path(output["wiki_generation_path"]).is_file())

    def test_unknown_task_type_fails_without_wasting_retries(self):
        with tempfile.TemporaryDirectory() as temporary:
            paths = WorkspacePaths(Path(temporary) / "workspace")
            database = initialize_workspace(paths)
            task_id = enqueue_task(
                database, "unknown_task", {}, max_attempts=5
            )
            result = run_once(database, paths, worker_id="worker-test")
            self.assertEqual(result.status.value, "failed")
            with database.connect() as connection:
                task = connection.execute(
                    "SELECT status, attempts FROM tasks WHERE id = ?", (task_id,)
                ).fetchone()
            self.assertEqual(task["status"], "failed")
            self.assertEqual(task["attempts"], 1)

    def test_continuous_worker_drains_tasks_and_records_lifecycle(self):
        with tempfile.TemporaryDirectory() as temporary:
            paths = WorkspacePaths(Path(temporary) / "workspace")
            database = initialize_workspace(paths)
            enqueue_task(database, "unknown-a", {})
            enqueue_task(database, "unknown-b", {})

            result = run_forever(
                database,
                paths,
                worker_id="worker-service",
                stop_when_idle=True,
                poll_seconds=0.01,
                max_poll_seconds=0.02,
            )

            self.assertEqual(result.stop_reason, "idle")
            self.assertEqual(result.claimed_tasks, 2)
            self.assertEqual(result.failed_tasks, 2)
            self.assertEqual(result.empty_polls, 1)
            with database.connect() as connection:
                task_statuses = [
                    row[0]
                    for row in connection.execute(
                        "SELECT status FROM tasks ORDER BY id"
                    ).fetchall()
                ]
                lifecycle = [
                    row[0]
                    for row in connection.execute(
                        """
                        SELECT event_type FROM audit_log
                        WHERE entity_type = 'worker' AND entity_id = 'worker-service'
                        ORDER BY id
                        """
                    ).fetchall()
                ]
            self.assertEqual(task_statuses, ["failed", "failed"])
            self.assertEqual(lifecycle, ["worker_started", "worker_stopped"])

    def test_continuous_worker_uses_bounded_exponential_idle_backoff(self):
        class FakeStopEvent:
            def __init__(self):
                self.waits = []

            def is_set(self):
                return False

            def wait(self, timeout):
                self.waits.append(timeout)
                return len(self.waits) == 4

        with tempfile.TemporaryDirectory() as temporary:
            paths = WorkspacePaths(Path(temporary) / "workspace")
            database = initialize_workspace(paths)
            stop_event = FakeStopEvent()

            result = run_forever(
                database,
                paths,
                worker_id="worker-idle",
                poll_seconds=1,
                max_poll_seconds=4,
                stop_event=stop_event,
            )

            self.assertEqual(stop_event.waits, [1, 2, 4, 4])
            self.assertEqual(result.stop_reason, "stop_requested")
            self.assertEqual(result.empty_polls, 4)

    def test_run_once_renews_lease_while_handler_is_running(self):
        with tempfile.TemporaryDirectory() as temporary:
            paths = WorkspacePaths(Path(temporary) / "workspace")
            database = initialize_workspace(paths)
            task_id = enqueue_task(database, "slow-test", {})
            renewed = Event()

            def fake_renew(*_args, **_kwargs):
                renewed.set()
                return "future"

            def slow_dispatch(_task, _paths):
                self.assertTrue(renewed.wait(timeout=1))
                return {"ok": True}

            with patch(
                "knowledge_workbench.worker.renew_task_lease",
                side_effect=fake_renew,
            ) as renewal, patch(
                "knowledge_workbench.worker._dispatch",
                side_effect=slow_dispatch,
            ):
                result = run_once(
                    database,
                    paths,
                    worker_id="worker-heartbeat",
                    heartbeat_interval_seconds=0.01,
                )

            self.assertEqual(result.task_id, task_id)
            self.assertEqual(result.status.value, "done")
            self.assertGreaterEqual(renewal.call_count, 1)


if __name__ == "__main__":
    unittest.main()
