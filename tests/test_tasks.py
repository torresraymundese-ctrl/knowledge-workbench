import tempfile
import unittest
from pathlib import Path

from knowledge_workbench.database import Database
from knowledge_workbench.errors import InvalidTransitionError
from knowledge_workbench.ingest import initialize_workspace
from knowledge_workbench.models import TaskStatus
from knowledge_workbench.config import WorkspacePaths
from knowledge_workbench.tasks import (
    claim_next_task,
    complete_task,
    enqueue_task,
    fail_task,
    recover_expired_tasks,
    renew_task_lease,
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

    def test_task_lease_owner_is_enforced_and_cleared_on_completion(self):
        with tempfile.TemporaryDirectory() as temporary:
            paths = WorkspacePaths(Path(temporary) / "workspace")
            database = initialize_workspace(paths)
            task_id = enqueue_task(database, "test", {})
            claim_next_task(database, worker="worker-1", lease_seconds=30)
            with database.connect() as connection:
                owner = connection.execute(
                    "SELECT lease_owner FROM tasks WHERE id = ?", (task_id,)
                ).fetchone()[0]
            self.assertEqual(owner, "worker-1")

            with self.assertRaisesRegex(InvalidTransitionError, "租约属于 worker-1"):
                complete_task(database, task_id, {}, worker="worker-2")
            renewed_until = renew_task_lease(
                database, task_id, worker="worker-1", lease_seconds=60
            )
            self.assertTrue(renewed_until)
            complete_task(database, task_id, {"ok": True}, worker="worker-1")
            with database.connect() as connection:
                row = connection.execute(
                    "SELECT status, lease_owner, lease_expires_at FROM tasks WHERE id = ?",
                    (task_id,),
                ).fetchone()
                versions = {
                    item[0]
                    for item in connection.execute(
                        "SELECT version FROM schema_migrations"
                    ).fetchall()
                }
            self.assertEqual(row["status"], "done")
            self.assertIsNone(row["lease_owner"])
            self.assertIsNone(row["lease_expires_at"])
            self.assertIn(7, versions)

    def test_expired_lease_cannot_be_renewed_or_completed(self):
        with tempfile.TemporaryDirectory() as temporary:
            paths = WorkspacePaths(Path(temporary) / "workspace")
            database = initialize_workspace(paths)
            task_id = enqueue_task(database, "test", {})
            claim_next_task(database, worker="worker-1", lease_seconds=30)
            with database.transaction() as connection:
                connection.execute(
                    "UPDATE tasks SET lease_expires_at = '2000-01-01T00:00:00+00:00' WHERE id = ?",
                    (task_id,),
                )
            with self.assertRaisesRegex(InvalidTransitionError, "租约已过期"):
                renew_task_lease(database, task_id, worker="worker-1")
            with self.assertRaisesRegex(InvalidTransitionError, "租约已过期"):
                complete_task(database, task_id, {}, worker="worker-1")
            self.assertEqual(recover_expired_tasks(database), 1)
            with database.connect() as connection:
                owner = connection.execute(
                    "SELECT lease_owner FROM tasks WHERE id = ?", (task_id,)
                ).fetchone()[0]
            self.assertIsNone(owner)


if __name__ == "__main__":
    unittest.main()
