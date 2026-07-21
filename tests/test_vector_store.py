import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from knowledge_workbench.config import WorkspacePaths
from knowledge_workbench.database import Database
from knowledge_workbench.errors import KnowledgeWorkbenchError
from knowledge_workbench.ingest import ingest_file
from knowledge_workbench.models import Classification, ParsedUnit, ParseResult
from knowledge_workbench.vector_store import (
    NumpyFlatVectorStore,
    build_evidence_index,
    semantic_search,
)


class FakeEmbeddingClient:
    model = "fake-embedding"

    def embed(self, texts):
        return np.asarray(
            [[1.0, float(index + 1)] for index, _ in enumerate(texts)],
            dtype=np.float32,
        )


class VectorStoreTests(unittest.TestCase):
    def test_exact_cosine_search(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = NumpyFlatVectorStore(Path(temporary))
            store.replace(
                ["first", "second", "third"],
                np.asarray(
                    [[1.0, 0.0], [0.0, 1.0], [0.8, 0.2]], dtype=np.float32
                ),
                {"model": "test", "dimension": 2, "count": 3},
            )
            results = store.search(np.asarray([1.0, 0.0]), limit=2)
            self.assertEqual([result[0] for result in results], ["first", "third"])
            self.assertAlmostEqual(results[0][1], 1.0)

    def test_semantic_search_rejects_index_after_processing_run_changes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.txt"
            source.write_text("必须保留原始证据。", encoding="utf-8")
            paths = WorkspacePaths(root / "workspace")
            first = ingest_file(source, paths, Classification.INTERNAL)
            database = Database(paths.database)
            store = NumpyFlatVectorStore(paths.index)
            client = FakeEmbeddingClient()
            build_evidence_index(database, store, client)

            self.assertEqual(
                len(semantic_search(database, store, client, "原始证据", limit=1)),
                1,
            )

            upgraded = ParseResult(
                "test-parser",
                "2",
                (ParsedUnit("必须保留原始证据。", {"line": 1}),),
            )
            with patch(
                "knowledge_workbench.ingest.parse_document", return_value=upgraded
            ):
                second = ingest_file(
                    source,
                    paths,
                    Classification.INTERNAL,
                    reprocess=True,
                )

            self.assertEqual(first.version_id, second.version_id)
            with self.assertRaisesRegex(KnowledgeWorkbenchError, "向量索引已过期"):
                semantic_search(database, store, client, "原始证据", limit=1)


if __name__ == "__main__":
    unittest.main()
