import unittest

from knowledge_workbench.extraction import FaithfulEvidenceExtractor
from knowledge_workbench.models import ParsedUnit, ParseResult


class FaithfulEvidenceExtractorTests(unittest.TestCase):
    def test_long_sql_chunks_remain_verbatim_source_substrings(self):
        source = "\n".join(
            f"CREATE TABLE item_{index} (id BIGINT PRIMARY KEY);"
            for index in range(20)
        )
        unit = ParsedUnit(source, {"line_start": 1, "line_end": 20})

        parsed = ParseResult("plain-text", "1", (unit,))
        candidates = FaithfulEvidenceExtractor(max_chars=120).extract(parsed)

        self.assertGreater(len(candidates), 1)
        self.assertTrue(all(candidate.excerpt in source for candidate in candidates))
        self.assertTrue(all(len(candidate.excerpt) <= 120 for candidate in candidates))
        self.assertIn("\n", candidates[0].excerpt)


if __name__ == "__main__":
    unittest.main()
