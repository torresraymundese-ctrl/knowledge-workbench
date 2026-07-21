import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from knowledge_workbench.database import (
    MIGRATION_2,
    MIGRATION_3,
    SCHEMA,
    Database,
)


class DatabaseMigrationTests(unittest.TestCase):
    def test_v4_backfills_processing_run_for_existing_evidence_and_revision(self):
        with tempfile.TemporaryDirectory() as temporary:
            database_path = Path(temporary) / "knowledge.sqlite3"
            with closing(sqlite3.connect(database_path)) as connection:
                connection.execute("PRAGMA foreign_keys = ON")
                connection.executescript(SCHEMA)
                connection.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (1, 't1')"
                )
                connection.executescript(MIGRATION_2)
                connection.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (2, 't2')"
                )
                connection.executescript(MIGRATION_3)
                connection.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (3, 't3')"
                )
                connection.execute(
                    """
                    INSERT INTO documents(
                        id, original_name, source_path, classification,
                        current_version_id, created_at, updated_at
                    ) VALUES ('doc_1', 'source.md', 'source.md', 'internal',
                              'ver_1', 't1', 't1')
                    """
                )
                connection.execute(
                    """
                    INSERT INTO document_versions(
                        id, document_id, sha256, stored_path, size_bytes,
                        parser_name, parser_version, created_at
                    ) VALUES ('ver_1', 'doc_1', 'abc', 'raw/source.md', 10,
                              'markdown', '1', 't1')
                    """
                )
                connection.execute(
                    """
                    INSERT INTO evidence(
                        id, document_version_id, ordinal, excerpt, locator_json,
                        extraction_method, status, created_at, updated_at
                    ) VALUES ('ev_1', 'ver_1', 1, '必须保留原文。', '{}',
                              'faithful-schema-v1', 'draft', 't1', 't1')
                    """
                )
                connection.execute(
                    """
                    INSERT INTO wiki_pages(
                        id, source_document_id, slug, title, status,
                        created_at, updated_at
                    ) VALUES ('page_1', 'doc_1', 'source', 'Source', 'draft', 't1', 't1')
                    """
                )
                connection.execute(
                    """
                    INSERT INTO wiki_revisions(
                        id, page_id, revision_number, status, markdown_path,
                        content_sha256, generator, created_at, updated_at
                    ) VALUES ('rev_1', 'page_1', 1, 'draft', 'wiki/source.md',
                              'hash', 'faithful-draft-v1', 't1', 't1')
                    """
                )
                connection.execute(
                    "INSERT INTO revision_evidence(revision_id, evidence_id) VALUES ('rev_1', 'ev_1')"
                )
                connection.commit()

            database = Database(database_path)
            database.initialize("t4")

            with database.connect() as connection:
                versions = {
                    row[0]
                    for row in connection.execute(
                        "SELECT version FROM schema_migrations"
                    ).fetchall()
                }
                self.assertEqual(versions, {1, 2, 3, 4, 5})
                run = connection.execute(
                    "SELECT * FROM processing_runs WHERE document_version_id = 'ver_1'"
                ).fetchone()
                self.assertEqual(run["status"], "completed")
                self.assertEqual(run["is_current"], 1)
                evidence = connection.execute(
                    "SELECT processing_run_id, run_ordinal FROM evidence WHERE id = 'ev_1'"
                ).fetchone()
                self.assertEqual(evidence["processing_run_id"], run["id"])
                self.assertEqual(evidence["run_ordinal"], 1)
                revision_run = connection.execute(
                    "SELECT processing_run_id FROM wiki_revisions WHERE id = 'rev_1'"
                ).fetchone()[0]
                self.assertEqual(revision_run, run["id"])
                labeling_tables = {
                    row[0]
                    for row in connection.execute(
                        """
                        SELECT name FROM sqlite_master
                        WHERE type = 'table' AND name LIKE 'labeling_%'
                        """
                    ).fetchall()
                }
                self.assertEqual(
                    labeling_tables,
                    {
                        "labeling_sessions",
                        "labeling_cases",
                        "labeling_expected_evidence",
                        "labeling_forbidden_substrings",
                    },
                )

            database.initialize("t5-repeat")
            with database.connect() as connection:
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM schema_migrations WHERE version = 5"
                    ).fetchone()[0],
                    1,
                )


if __name__ == "__main__":
    unittest.main()
