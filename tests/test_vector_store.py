import tempfile
import unittest
from pathlib import Path

import numpy as np

from knowledge_workbench.vector_store import NumpyFlatVectorStore


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


if __name__ == "__main__":
    unittest.main()

