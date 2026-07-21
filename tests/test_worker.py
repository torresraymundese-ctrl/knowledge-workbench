import json
import tempfile
import unittest
from pathlib import Path

from knowledge_workbench.config import WorkspacePaths
from knowledge_workbench.ingest import initialize_workspace
from knowledge_workbench.tasks import enqueue_task
from knowledge_workbench.worker import run_once


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


if __name__ == "__main__":
    unittest.main()

