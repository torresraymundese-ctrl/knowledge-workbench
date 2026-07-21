import tempfile
import unittest
from pathlib import Path

from knowledge_workbench.database import Database
from knowledge_workbench.ingest import initialize_workspace
from knowledge_workbench.models import TaskStatus
from knowledge_workbench.config import WorkspacePaths
from knowledge_workbench.tasks import (
    claim_next_task,
    complete_task,
    enqueue_task,
    fail_task,
    recover_expired_tasks,
)


class TaskQueueTests(unittest.TestCase):
    def test_idempotent_task_can_retry_then_finish(self):
        with tempfile.TemporaryDirectory() as temporary:
            paths = WorkspacePaths(Path(temporary) / "workspace")
            database = initialize_workspace(paths)
            first = enqueue_task(
                database,
                "analyze_document",
                {"version_id": "ver_1"},
                idempotency_key="analysis:ver_1",
                max_attempts=3,
            )
            second = enqueue_task(
                database,
                "analyze_document",
                {"version_id": "ver_1"},
                idempotency_key="analysis:ver_1",
            )
            self.assertEqual(first, second)

            claimed = claim_next_task(database, worker="worker-1", lease_seconds=30)
            self.assertEqual(claimed.id, first)
            self.assertEqual(
                fail_task(
                    database,
                    first,
                    "temporary error",
                    worker="worker-1",
                    base_delay_seconds=0,
                ),
                TaskStatus.RETRYING,
            )
            claimed = claim_next_task(database, worker="worker-2", lease_seconds=30)
            self.assertEqual(claimed.attempts, 2)
            complete_task(database, first, {"ok": True}, worker="worker-2")

            with database.connect() as connection:
                row = connection.execute(
                    "SELECT status, result_json FROM tasks WHERE id = ?", (first,)
                ).fetchone()
                events = connection.execute(
                    "SELECT event_type FROM audit_log WHERE entity_id = ?", (first,)
                ).fetchall()
            self.assertEqual(row["status"], "done")
            self.assertIn('"ok": true', row["result_json"])
            self.assertEqual(
                [event["event_type"] for event in events],
                ["task_enqueued", "task_claimed", "task_retry_scheduled", "task_claimed", "task_completed"],
            )

    def test_expired_worker_lease_is_recovered(self):
        with tempfile.TemporaryDirectory() as temporary:
            paths = WorkspacePaths(Path(temporary) / "workspace")
            database = initialize_workspace(paths)
            task_id = enqueue_task(database, "test", {})
            claim_next_task(database, worker="dead-worker", lease_seconds=30)
            with database.transaction() as connection:
                connection.execute(
                    "UPDATE tasks SET lease_expires_at = '2000-01-01T00:00:00+00:00' WHERE id = ?",
                    (task_id,),
                )
            self.assertEqual(recover_expired_tasks(database), 1)
            with database.connect() as connection:
                status = connection.execute(
                    "SELECT status FROM tasks WHERE id = ?", (task_id,)
                ).fetchone()[0]
            self.assertEqual(status, "retrying")


if __name__ == "__main__":
    unittest.main()

