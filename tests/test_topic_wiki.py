from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from knowledge_workbench.config import WorkspacePaths
from knowledge_workbench.database import Database
from knowledge_workbench.errors import InvalidTransitionError
from knowledge_workbench.models import EvidenceStatus
from knowledge_workbench.review import (
    publish_revision,
    reject_revision,
    request_revision_review,
    transition_evidence,
)
from knowledge_workbench.topic_wiki import (
    build_topic_wiki_baseline,
    knowledge_setup,
)
from knowledge_workbench.utils import sha256_text, utc_now
from knowledge_workbench.web_service import (
    WorkbenchActionService,
    WorkbenchReadService,
)


class TopicWikiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.paths = WorkspacePaths(Path(self.temporary.name) / "workspace")
        self.paths.create()
        self.database = Database(self.paths.database)
        self.database.initialize(utc_now())
        self.now = utc_now()
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO corpus_scans(
                    id, source_root, root_label, project_name, status, is_current,
                    file_count, folder_count, total_size_bytes,
                    readable_card_count, metadata_only_count, blocked_count,
                    unreadable_count, duplicate_group_count, version_group_count,
                    actor, created_at, completed_at
                ) VALUES (
                    'scan_current', 'D:/研学平台数据', '研学平台数据', '研学平台',
                    'completed', 1, 0, 1, 0, 0, 0, 0, 0, 0, 0,
                    'tester', ?, ?
                )
                """,
                (self.now, self.now),
            )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_builds_extract_only_multi_document_draft_and_is_idempotent(self):
        self._add_source(
            number=1,
            name="苏州研学平台_权限矩阵.docx",
            display_title="研学平台角色权限矩阵",
            excerpt="学校管理员可以维护本校用户角色与业务权限。",
            locator={"page": 3, "heading_path": ["角色权限"]},
            classification="internal",
            authority="authoritative",
        )
        self._add_source(
            number=2,
            name="研学通平台角色资料提交开发指导手册.docx",
            display_title="角色资料提交指导手册",
            excerpt="机构入驻时，应提交机构资料并由平台完成准入审核。",
            locator={"page": 8, "paragraph": 4},
            classification="confidential",
            authority="reference",
        )
        self._add_source(
            number=3,
            name="不合格的权限说明.md",
            display_title="不合格的权限说明",
            excerpt="这条权限资料没有通过技术校验，因此不能被主题页引用。",
            locator={"line_start": 2},
            validation_status="failed",
        )
        self._add_source(
            number=4,
            name="受限权限说明.md",
            display_title="受限权限说明",
            excerpt="这条权限资料属于受限内容，不能进入主题页。",
            locator={"line_start": 2},
            classification="restricted",
        )

        first = build_topic_wiki_baseline(
            self.database,
            self.paths,
            actor="tester",
        )
        organization = self._topic_result(first, "organization-access")

        self.assertEqual(first["created_count"], 1)
        self.assertEqual(organization["action"], "created")
        self.assertEqual(organization["document_count"], 2)
        self.assertEqual(organization["evidence_count"], 2)

        markdown_path = self.paths.root / organization["markdown_path"]
        content = markdown_path.read_text(encoding="utf-8")
        self.assertIn("classification: confidential", content)
        self.assertIn("学校管理员可以维护本校用户角色与业务权限。", content)
        self.assertIn("机构入驻时，应提交机构资料并由平台完成准入审核。", content)
        self.assertNotIn("没有通过技术校验", content)
        self.assertNotIn("属于受限内容", content)
        self.assertIn("第 3 页，章节“角色权限”", content)
        self.assertIn("第 8 页，第 4 段", content)

        lines = content.splitlines()
        for index, line in enumerate(lines):
            if line.startswith("## "):
                self.assertRegex(
                    lines[index + 1],
                    r"^<!-- topic-section-evidence: \[.*\] -->$",
                )

        with self.database.connect() as connection:
            page = connection.execute(
                """
                SELECT source_document_id, current_verified_revision_id, status
                FROM wiki_pages WHERE id = ?
                """,
                (organization["page_id"],),
            ).fetchone()
            revision = connection.execute(
                """
                SELECT status, processing_run_id, generator
                FROM wiki_revisions WHERE id = ?
                """,
                (organization["revision_id"],),
            ).fetchone()
            source_count = connection.execute(
                """
                SELECT COUNT(DISTINCT dv.document_id)
                FROM revision_evidence re
                JOIN evidence e ON e.id = re.evidence_id
                JOIN document_versions dv ON dv.id = e.document_version_id
                WHERE re.revision_id = ?
                """,
                (organization["revision_id"],),
            ).fetchone()[0]
            event_count = connection.execute(
                """
                SELECT COUNT(*) FROM audit_log
                WHERE event_type = 'topic_wiki_draft_created'
                  AND entity_id = ?
                """,
                (organization["revision_id"],),
            ).fetchone()[0]

        self.assertIsNone(page["source_document_id"])
        self.assertIsNone(page["current_verified_revision_id"])
        self.assertEqual(page["status"], "draft")
        self.assertEqual(revision["status"], "draft")
        self.assertIsNone(revision["processing_run_id"])
        self.assertEqual(revision["generator"], "topic-extractive-v1")
        self.assertEqual(source_count, 2)
        self.assertEqual(event_count, 1)

        second = build_topic_wiki_baseline(
            self.database,
            self.paths,
            actor="tester",
        )
        second_organization = self._topic_result(
            second,
            "organization-access",
        )
        self.assertEqual(second["created_count"], 0)
        self.assertEqual(second_organization["action"], "reused_active_draft")
        self.assertEqual(
            second_organization["revision_id"],
            organization["revision_id"],
        )
        with self.database.connect() as connection:
            revision_count = connection.execute(
                "SELECT COUNT(*) FROM wiki_revisions WHERE page_id = ?",
                (organization["page_id"],),
            ).fetchone()[0]
        self.assertEqual(revision_count, 1)

        refreshed = build_topic_wiki_baseline(
            self.database,
            self.paths,
            actor="tester",
            refresh=True,
        )
        refreshed_organization = self._topic_result(
            refreshed,
            "organization-access",
        )
        self.assertEqual(refreshed_organization["action"], "refreshed_draft")
        self.assertNotEqual(
            refreshed_organization["revision_id"],
            organization["revision_id"],
        )
        with self.database.connect() as connection:
            statuses = connection.execute(
                """
                SELECT id, status
                FROM wiki_revisions
                WHERE page_id = ?
                ORDER BY revision_number
                """,
                (organization["page_id"],),
            ).fetchall()
        self.assertEqual(
            [(row["id"], row["status"]) for row in statuses],
            [
                (organization["revision_id"], "superseded"),
                (refreshed_organization["revision_id"], "draft"),
            ],
        )

    def test_knowledge_setup_uses_current_corpus_file_ids_and_plain_labels(self):
        first = self._add_source(
            number=1,
            name="PRD_V3.0.md",
            display_title="研学平台产品需求 V3.0",
            excerpt="学校和机构角色需要按照权限完成平台准入。",
            locator={"heading_path": ["角色体系"]},
            authority="reference",
        )
        second = self._add_source(
            number=2,
            name="苏州研学服务交易平台_权限矩阵_V2.0.docx",
            display_title="苏州研学服务交易平台权限矩阵 V2.0",
            excerpt="学校管理员和机构管理员拥有不同的功能权限。",
            locator={"page": 2},
            authority="authoritative",
        )

        setup = knowledge_setup(self.database)
        confirmations = {
            item["file_id"]: item for item in setup["authority_confirmations"]
        }
        self.assertIn(first["file_id"], confirmations)
        self.assertIn(second["file_id"], confirmations)
        self.assertEqual(
            confirmations[first["file_id"]]["current_authority"],
            "reference",
        )
        self.assertEqual(
            confirmations[second["file_id"]]["current_authority_label"],
            "主要依据",
        )
        self.assertIn(
            "组织、角色、权限与准入",
            confirmations[second["file_id"]]["topics"],
        )
        self.assertNotIn("document_id", confirmations[first["file_id"]])

        topic = next(
            item
            for item in setup["topic_candidates"]
            if item["topic_key"] == "organization-access"
        )
        self.assertEqual(topic["document_count"], 2)
        self.assertEqual(topic["authoritative_source_count"], 1)
        self.assertEqual(topic["reference_source_count"], 1)
        self.assertEqual(topic["unconfirmed_primary_count"], 0)
        self.assertEqual(topic["status_label"], "可生成主题草稿")
        self.assertIn("研学平台产品需求 V3.0", topic["primary_sources"])
        self.assertTrue(topic["question_examples"])

        build_topic_wiki_baseline(
            self.database,
            self.paths,
            actor="tester",
        )
        after_build = knowledge_setup(self.database)
        built_topic = next(
            item
            for item in after_build["topic_candidates"]
            if item["topic_key"] == "organization-access"
        )
        self.assertEqual(built_topic["revision_status"], "draft")
        self.assertEqual(built_topic["status_label"], "主题草稿待审核")

    def test_topic_requires_two_eligible_documents(self):
        self._add_source(
            number=1,
            name="旅行社安全应急预案.pdf",
            display_title="旅行社安全应急预案",
            excerpt="发生安全事故后，应立即启动应急预案。",
            locator={"page": 1},
            domain="policy",
        )
        self._add_source(
            number=2,
            name="未确认的安全材料.md",
            display_title="未确认的安全材料",
            excerpt="这份安全材料尚未完成业务范围确认。",
            locator={"line_start": 1},
            scope="unreviewed",
        )

        result = build_topic_wiki_baseline(
            self.database,
            self.paths,
            actor="tester",
        )
        safety = self._topic_result(result, "safety")
        self.assertEqual(safety["action"], "insufficient_sources")
        self.assertEqual(safety["document_count"], 1)
        self.assertIsNone(safety["revision_id"])

    def test_knowledge_setup_hides_stale_topic_revision_after_source_exclusion(self):
        self._add_source(
            number=1,
            name="旅行社安全应急预案.pdf",
            display_title="旅行社安全应急预案",
            excerpt="发生安全事故后，应立即启动应急预案。",
            locator={"page": 1},
            domain="policy",
        )
        excluded = self._add_source(
            number=2,
            name="开发数据安全流程.md",
            display_title="开发数据安全流程",
            excerpt="开发阶段记录安全工单。",
            locator={"line_start": 1},
            domain="technical",
        )
        built = build_topic_wiki_baseline(
            self.database,
            self.paths,
            actor="tester",
        )
        built_safety = self._topic_result(built, "safety")
        self.assertIsNotNone(built_safety["revision_id"])

        with self.database.transaction() as connection:
            connection.execute(
                """
                UPDATE document_governance
                SET purpose = 'development_fixture',
                    scope_status = 'out_of_scope'
                WHERE document_id = ?
                """,
                (excluded["document_id"],),
            )

        refreshed = build_topic_wiki_baseline(
            self.database,
            self.paths,
            actor="tester",
            refresh=True,
        )
        self.assertEqual(
            self._topic_result(refreshed, "safety")["action"],
            "insufficient_sources",
        )
        setup = knowledge_setup(self.database)
        safety = next(
            item
            for item in setup["topic_candidates"]
            if item["topic_key"] == "safety"
        )
        self.assertEqual(safety["document_count"], 1)
        self.assertIsNone(safety["revision_id"])
        self.assertIsNone(safety["revision_status"])
        with self.database.connect() as connection:
            revision_status = connection.execute(
                "SELECT status FROM wiki_revisions WHERE id = ?",
                (built_safety["revision_id"],),
            ).fetchone()[0]
            page_status = connection.execute(
                "SELECT status FROM wiki_pages WHERE id = ?",
                (built_safety["page_id"],),
            ).fetchone()[0]
        self.assertEqual(revision_status, "superseded")
        self.assertEqual(page_status, "archived")

    def test_topic_can_be_reviewed_published_and_revalidated(self):
        first = self._add_source(
            number=1,
            name="研学平台权限矩阵.docx",
            display_title="研学平台权限矩阵",
            excerpt="学校管理员可以维护本校角色和权限。",
            locator={"page": 2},
            authority="authoritative",
        )
        self._add_source(
            number=2,
            name="角色资料提交开发指导手册.docx",
            display_title="角色资料提交指导手册",
            excerpt="机构完成资料提交后进入平台准入审核。",
            locator={"page": 4},
            authority="authoritative",
        )
        result = build_topic_wiki_baseline(
            self.database,
            self.paths,
            actor="organizer-01",
        )
        topic = self._topic_result(result, "organization-access")

        submitted = WorkbenchActionService(
            self.database,
            self.paths,
        ).submit_revision_review(
            topic["revision_id"],
            actor="author-01",
        )
        self.assertEqual(submitted["status"], "reviewing")
        published = publish_revision(
            self.database,
            self.paths,
            topic["revision_id"],
            actor="reviewer-01",
        )

        self.assertTrue(published.is_file())
        self.assertEqual(published.parent, self.paths.wiki_verified)
        with self.database.connect() as connection:
            page = connection.execute(
                """
                SELECT status, current_verified_revision_id, needs_revalidation
                FROM wiki_pages WHERE id = ?
                """,
                (topic["page_id"],),
            ).fetchone()
        self.assertEqual(page["status"], "verified")
        self.assertEqual(
            page["current_verified_revision_id"],
            topic["revision_id"],
        )
        self.assertEqual(page["needs_revalidation"], 0)

        transition_evidence(
            self.database,
            first["evidence_id"],
            EvidenceStatus.CONFLICTED,
            actor="reviewer-02",
        )
        with self.database.connect() as connection:
            needs_revalidation = connection.execute(
                "SELECT needs_revalidation FROM wiki_pages WHERE id = ?",
                (topic["page_id"],),
            ).fetchone()[0]
        self.assertEqual(needs_revalidation, 1)

    def test_topic_rejects_missing_validation_and_web_redacts_newly_restricted_source(self):
        first = self._add_source(
            number=1,
            name="研学平台权限矩阵.docx",
            display_title="研学平台权限矩阵",
            excerpt="学校管理员可以维护本校角色和权限。",
            locator={"page": 2},
        )
        second = self._add_source(
            number=2,
            name="角色资料提交开发指导手册.docx",
            display_title="角色资料提交指导手册",
            excerpt="机构提交资料后进入平台准入审核。",
            locator={"page": 4},
        )
        result = build_topic_wiki_baseline(
            self.database,
            self.paths,
            actor="organizer-01",
        )
        topic = self._topic_result(result, "organization-access")
        with self.database.transaction() as connection:
            connection.execute(
                """
                DELETE FROM evidence_technical_validation
                WHERE evidence_id = ?
                """,
                (first["evidence_id"],),
            )

        with self.assertRaisesRegex(
            InvalidTransitionError,
            "来源依据已失效",
        ):
            request_revision_review(
                self.database,
                topic["revision_id"],
                actor="author-01",
            )

        with self.database.transaction() as connection:
            connection.execute(
                """
                UPDATE documents
                SET classification = 'restricted'
                WHERE id = ?
                """,
                (second["document_id"],),
            )
        with self.assertRaises(PermissionError):
            WorkbenchReadService(
                self.database,
                self.paths,
            ).revision_detail(topic["revision_id"])

    def test_topic_web_preview_and_rejected_history_are_human_readable_and_redacted(self):
        first = self._add_source(
            number=1,
            name="研学平台权限矩阵.docx",
            display_title="研学平台权限矩阵",
            excerpt="学校管理员可以维护本校角色和权限。",
            locator={"page": 2},
        )
        self._add_source(
            number=2,
            name="角色资料提交开发指导手册.docx",
            display_title="角色资料提交指导手册",
            excerpt="机构提交资料后进入平台准入审核。",
            locator={"page": 4},
        )
        result = build_topic_wiki_baseline(
            self.database,
            self.paths,
            actor="organizer-01",
        )
        topic = self._topic_result(result, "organization-access")
        service = WorkbenchReadService(self.database, self.paths)

        detail = service.revision_detail(topic["revision_id"])

        self.assertEqual(detail["page_kind"], "topic")
        self.assertEqual(detail["content_preview_format"], "plain_text")
        self.assertEqual(detail["generator_label"], "系统按主题整理")
        self.assertIn("组织、角色、权限与准入", detail["content_preview"])
        self.assertIn("学校管理员可以维护本校角色和权限", detail["content_preview"])
        self.assertIn("待确认：确认这些资料确实适用于当前企业", detail["content_preview"])
        self.assertNotIn("---", detail["content_preview"])
        self.assertNotIn("revision_id", detail["content_preview"])
        self.assertNotIn("topic-section-evidence", detail["content_preview"])
        self.assertNotIn(first["evidence_id"], detail["content_preview"])
        self.assertNotIn("##", detail["content_preview"])

        request_revision_review(
            self.database,
            topic["revision_id"],
            actor="author-01",
        )
        reject_revision(
            self.database,
            topic["revision_id"],
            actor="reviewer-01",
            note="请确认角色权限的适用组织范围。",
        )

        history = service.rejected_revision_history(query="组织、角色")
        self.assertEqual(history["total"], 1)
        self.assertEqual(history["items"][0]["page_title"], topic["title"])
        self.assertEqual(
            history["items"][0]["review_note"],
            "请确认角色权限的适用组织范围。",
        )
        self.assertTrue(history["items"][0]["source_is_current"])

        with self.database.transaction() as connection:
            connection.execute(
                """
                UPDATE documents SET classification = 'restricted'
                WHERE id = ?
                """,
                (first["document_id"],),
            )
        restricted_history = service.rejected_revision_history(
            classification="restricted"
        )
        hidden_by_title = service.rejected_revision_history(query="组织、角色")
        self.assertEqual(restricted_history["total"], 1)
        self.assertEqual(
            restricted_history["items"][0]["page_title"],
            "[受限知识页]",
        )
        self.assertIsNone(restricted_history["items"][0]["review_note"])
        self.assertEqual(hidden_by_title["total"], 0)

    def test_topic_review_requires_evidence_marker_next_to_every_section(self):
        self._add_source(
            number=1,
            name="研学平台权限矩阵.docx",
            display_title="研学平台权限矩阵",
            excerpt="学校管理员可以维护本校角色和权限。",
            locator={"page": 2},
        )
        self._add_source(
            number=2,
            name="角色资料提交开发指导手册.docx",
            display_title="角色资料提交指导手册",
            excerpt="机构提交资料后进入平台准入审核。",
            locator={"page": 4},
        )
        result = build_topic_wiki_baseline(
            self.database,
            self.paths,
            actor="organizer-01",
        )
        topic = self._topic_result(result, "organization-access")
        markdown_path = self.paths.root / topic["markdown_path"]
        content = markdown_path.read_text(encoding="utf-8")
        tampered = content.replace(
            "## 页面用途\n<!-- topic-section-evidence: [] -->",
            "## 页面用途\n",
            1,
        )
        markdown_path.write_text(tampered, encoding="utf-8")
        with self.database.transaction() as connection:
            connection.execute(
                """
                UPDATE wiki_revisions
                SET content_sha256 = ?
                WHERE id = ?
                """,
                (sha256_text(tampered), topic["revision_id"]),
            )

        with self.assertRaisesRegex(
            InvalidTransitionError,
            "每个二级章节必须紧邻声明依据",
        ):
            request_revision_review(
                self.database,
                topic["revision_id"],
                actor="author-01",
                paths=self.paths,
            )

    def _add_source(
        self,
        *,
        number: int,
        name: str,
        display_title: str,
        excerpt: str,
        locator: dict[str, object],
        classification: str = "internal",
        purpose: str = "production",
        scope: str = "in_scope",
        authority: str = "reference",
        domain: str = "business",
        evidence_status: str = "verified",
        validation_status: str = "passed",
        run_current: int = 1,
    ) -> dict[str, str]:
        document_id = f"doc_{number}"
        version_id = f"ver_{number}"
        run_id = f"run_{number}"
        evidence_id = f"ev_{number}"
        file_id = f"corpus_{number}"
        digest = sha256_text(f"source-{number}-{name}")
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO documents(
                    id, original_name, source_path, classification,
                    current_version_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, NULL, ?, ?)
                """,
                (
                    document_id,
                    name,
                    f"D:/研学平台数据/{name}",
                    classification,
                    self.now,
                    self.now,
                ),
            )
            connection.execute(
                """
                INSERT INTO document_versions(
                    id, document_id, sha256, stored_path, size_bytes,
                    media_type, parser_name, parser_version, status, created_at
                ) VALUES (?, ?, ?, ?, 100, 'text/plain',
                          'test-parser', '1', 'parsed', ?)
                """,
                (
                    version_id,
                    document_id,
                    digest,
                    f"raw/{digest}/{name}",
                    self.now,
                ),
            )
            connection.execute(
                "UPDATE documents SET current_version_id = ? WHERE id = ?",
                (version_id, document_id),
            )
            connection.execute(
                """
                INSERT INTO processing_runs(
                    id, document_version_id, parser_name, parser_version,
                    extraction_method, status, is_current,
                    created_at, completed_at
                ) VALUES (?, ?, 'test-parser', '1', 'test-extract',
                          'completed', ?, ?, ?)
                """,
                (run_id, version_id, run_current, self.now, self.now),
            )
            connection.execute(
                """
                INSERT INTO evidence(
                    id, document_version_id, ordinal, excerpt, locator_json,
                    extraction_method, extraction_model, status,
                    created_at, updated_at, processing_run_id, run_ordinal
                ) VALUES (?, ?, 1, ?, ?, 'test-extract', NULL, ?, ?, ?, ?, 1)
                """,
                (
                    evidence_id,
                    version_id,
                    excerpt,
                    json.dumps(locator, ensure_ascii=False),
                    evidence_status,
                    self.now,
                    self.now,
                    run_id,
                ),
            )
            connection.execute(
                """
                INSERT INTO evidence_technical_validation(
                    evidence_id, status, validator, checks_json,
                    validated_at, created_at, updated_at
                ) VALUES (?, ?, 'test-validator', '{}', ?, ?, ?)
                """,
                (
                    evidence_id,
                    validation_status,
                    self.now,
                    self.now,
                    self.now,
                ),
            )
            connection.execute(
                """
                INSERT INTO document_governance(
                    document_id, purpose, scope_status, authority_status,
                    reviewed_by, reviewed_at, decision_reason,
                    created_at, updated_at, knowledge_domain
                ) VALUES (?, ?, ?, ?, 'tester', ?, 'test setup', ?, ?, ?)
                """,
                (
                    document_id,
                    purpose,
                    scope,
                    authority,
                    self.now,
                    self.now,
                    self.now,
                    domain,
                ),
            )
            connection.execute(
                """
                INSERT INTO corpus_files(
                    id, scan_id, relative_path, folder_path, file_name,
                    extension, sha256, size_bytes, modified_at_ns,
                    map_status, document_type, display_title, plain_summary,
                    outline_json, key_signals_json, risk_flags_json,
                    parser_name, parser_version, duplicate_group, version_group,
                    scope_status, authority_status, decided_by, decided_at,
                    decision_reason, created_at
                ) VALUES (
                    ?, 'scan_current', ?, '', ?, '.md', ?, 100, ?,
                    'readable', '测试资料', ?, '测试资料说明',
                    '[]', '{}', '[]', 'test-parser', '1', NULL, NULL,
                    ?, ?, 'tester', ?, 'test setup', ?
                )
                """,
                (
                    file_id,
                    name,
                    name,
                    digest,
                    number,
                    display_title,
                    scope,
                    authority,
                    self.now,
                    self.now,
                ),
            )
        return {
            "document_id": document_id,
            "version_id": version_id,
            "evidence_id": evidence_id,
            "file_id": file_id,
        }

    @staticmethod
    def _topic_result(result: dict, topic_key: str) -> dict:
        return next(
            item for item in result["topics"] if item["topic_key"] == topic_key
        )


if __name__ == "__main__":
    unittest.main()
