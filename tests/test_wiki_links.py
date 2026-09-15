import tempfile
import unittest
from pathlib import Path

from knowledge_workbench.config import WorkspacePaths
from knowledge_workbench.database import Database
from knowledge_workbench.errors import InvalidTransitionError
from knowledge_workbench.ingest import ingest_file
from knowledge_workbench.models import Classification, EvidenceStatus
from knowledge_workbench.review import (
    publish_revision,
    request_revision_review,
    transition_evidence,
)
from knowledge_workbench.wiki_links import add_wiki_link, remove_wiki_link


class WikiLinkTests(unittest.TestCase):
    @staticmethod
    def _admit_all_documents(database: Database) -> None:
        with database.transaction() as connection:
            connection.execute(
                """
                UPDATE document_governance
                SET purpose = 'production',
                    scope_status = 'in_scope',
                    authority_status = 'reference',
                    reviewed_by = 'scope-reviewer',
                    reviewed_at = updated_at,
                    decision_reason = '测试中明确准入'
                """
            )

    def test_target_update_marks_verified_downstream_page_for_revalidation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = WorkspacePaths(root / "workspace")
            target_source = root / "target.md"
            downstream_source = root / "downstream.md"
            target_source.write_text("目标规则第一版。", encoding="utf-8")
            downstream_source.write_text("下游规则依赖目标规则。", encoding="utf-8")
            target = ingest_file(target_source, paths, Classification.INTERNAL)
            downstream = ingest_file(
                downstream_source, paths, Classification.INTERNAL
            )
            database = Database(paths.database)
            self._admit_all_documents(database)
            with database.connect() as connection:
                target_evidence = connection.execute(
                    "SELECT id FROM evidence WHERE document_version_id = ?",
                    (target.version_id,),
                ).fetchone()[0]
                downstream_evidence = connection.execute(
                    "SELECT id FROM evidence WHERE document_version_id = ?",
                    (downstream.version_id,),
                ).fetchone()[0]

            add_wiki_link(
                database,
                paths,
                source_revision_id=downstream.revision_id,
                target_page_id=target.page_id,
                relationship="依赖",
                evidence_ids=[downstream_evidence],
                actor="reviewer",
            )
            for evidence_id in (target_evidence, downstream_evidence):
                transition_evidence(
                    database, evidence_id, EvidenceStatus.REVIEWING, actor="reviewer"
                )
                transition_evidence(
                    database, evidence_id, EvidenceStatus.VERIFIED, actor="reviewer"
                )
            for revision_id in (target.revision_id, downstream.revision_id):
                request_revision_review(database, revision_id, actor="reviewer")
                publish_revision(database, paths, revision_id, actor="reviewer")

            target_source.write_text("目标规则第二版。", encoding="utf-8")
            ingest_file(target_source, paths, Classification.INTERNAL)

            with database.connect() as connection:
                rows = connection.execute(
                    """
                    SELECT id, needs_revalidation FROM wiki_pages
                    WHERE id IN (?, ?)
                    """,
                    (target.page_id, downstream.page_id),
                ).fetchall()
            self.assertEqual(
                {row["id"]: row["needs_revalidation"] for row in rows},
                {target.page_id: 1, downstream.page_id: 1},
            )

    def test_draft_link_creates_obsidian_link_and_database_backlink(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = WorkspacePaths(root / "workspace")
            source_a = root / "policy.md"
            source_b = root / "security.md"
            source_a.write_text("资料处理需要遵循安全规则。", encoding="utf-8")
            source_b.write_text("机密资料不得发送到云端。", encoding="utf-8")
            first = ingest_file(source_a, paths, Classification.INTERNAL)
            second = ingest_file(source_b, paths, Classification.INTERNAL)
            database = Database(paths.database)
            self._admit_all_documents(database)
            with database.connect() as connection:
                evidence_id = connection.execute(
                    "SELECT id FROM evidence WHERE document_version_id = ?",
                    (first.version_id,),
                ).fetchone()[0]

            link_id = add_wiki_link(
                database,
                paths,
                source_revision_id=first.revision_id,
                target_page_id=second.page_id,
                relationship="受其约束",
                evidence_ids=[evidence_id],
                actor="reviewer",
            )

            with database.connect() as connection:
                link = connection.execute(
                    "SELECT * FROM wiki_links WHERE id = ?", (link_id,)
                ).fetchone()
                draft_path = paths.root / connection.execute(
                    "SELECT markdown_path FROM wiki_revisions WHERE id = ?",
                    (first.revision_id,),
                ).fetchone()[0]
            self.assertEqual(link["target_page_id"], second.page_id)
            markdown = draft_path.read_text(encoding="utf-8")
            self.assertIn("[[security|security]]", markdown)
            self.assertIn("受其约束", markdown)
            self.assertIn(evidence_id, markdown)

            remove_wiki_link(database, paths, link_id, actor="reviewer")
            removed_markdown = draft_path.read_text(encoding="utf-8")
            self.assertNotIn("[[security|security]]", removed_markdown)
            self.assertNotIn("## 相关知识", removed_markdown)

    def test_verified_revision_links_cannot_be_silently_changed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = WorkspacePaths(root / "workspace")
            source_a = root / "a.md"
            source_b = root / "b.md"
            source_a.write_text("规则A。", encoding="utf-8")
            source_b.write_text("规则B。", encoding="utf-8")
            first = ingest_file(source_a, paths, Classification.INTERNAL)
            second = ingest_file(source_b, paths, Classification.INTERNAL)
            database = Database(paths.database)
            self._admit_all_documents(database)
            with database.connect() as connection:
                evidence_id = connection.execute(
                    "SELECT id FROM evidence WHERE document_version_id = ?",
                    (first.version_id,),
                ).fetchone()[0]
            transition_evidence(
                database, evidence_id, EvidenceStatus.REVIEWING, actor="reviewer"
            )
            transition_evidence(
                database, evidence_id, EvidenceStatus.VERIFIED, actor="reviewer"
            )
            request_revision_review(database, first.revision_id, actor="reviewer")
            publish_revision(database, paths, first.revision_id, actor="reviewer")

            with self.assertRaises(InvalidTransitionError):
                add_wiki_link(
                    database,
                    paths,
                    source_revision_id=first.revision_id,
                    target_page_id=second.page_id,
                    relationship="相关",
                    evidence_ids=[evidence_id],
                    actor="reviewer",
                )


if __name__ == "__main__":
    unittest.main()
