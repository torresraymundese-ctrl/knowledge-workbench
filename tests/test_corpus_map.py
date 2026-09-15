import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from knowledge_workbench.config import WorkspacePaths
from knowledge_workbench.corpus_map import (
    corpus_file_card,
    decide_corpus_file,
    import_corpus_file,
    latest_corpus_map,
    scan_corpus_source,
)
from knowledge_workbench.errors import KnowledgeWorkbenchError
from knowledge_workbench.ingest import ingest_file, initialize_workspace
from knowledge_workbench.models import Classification
from knowledge_workbench.question_answering import answer_question


class CorpusMapTests(unittest.TestCase):
    def test_scan_builds_bounded_representative_content_preview(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "资料"
            source.mkdir()
            (source / "制度.md").write_text(
                "# 适用范围\n本制度适用于研学机构。\n\n"
                "# 安全要求\n活动前必须完成风险评估和应急预案。\n\n"
                "# 费用与退款\n退款应按合同约定处理。\n",
                encoding="utf-8",
            )
            paths = WorkspacePaths(root / "workspace")
            database = initialize_workspace(paths)

            scan_corpus_source(database, source, actor="mapper")
            file_id = latest_corpus_map(database)["items"][0]["file_id"]
            detail = corpus_file_card(database, file_id)

            self.assertGreaterEqual(len(detail["content_preview"]), 2)
            self.assertLessEqual(len(detail["content_preview"]), 8)
            self.assertTrue(
                any("安全要求" in item["label"] for item in detail["content_preview"])
            )
            self.assertTrue(
                any("风险评估" in item["text"] for item in detail["content_preview"])
            )
            self.assertLessEqual(
                sum(len(item["text"]) for item in detail["content_preview"]),
                2400,
            )

    def test_restricted_import_hides_content_preview_in_detail_card(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "restricted-source"
            source.mkdir()
            (source / "restricted-policy.md").write_text(
                "# 保密条款\nrestricted-preview-must-not-leak",
                encoding="utf-8",
            )
            paths = WorkspacePaths(root / "workspace")
            database = initialize_workspace(paths)

            scan_corpus_source(database, source, actor="mapper")
            file_id = latest_corpus_map(database)["items"][0]["file_id"]
            decide_corpus_file(
                database,
                file_id,
                scope_status="in_scope",
                authority_status="authoritative",
                actor="owner",
                reason="批准受限资料导入",
            )
            imported = import_corpus_file(
                database,
                paths,
                file_id,
                classification="restricted",
                actor="importer",
            )

            self.assertEqual(imported["classification"], "restricted")
            self.assertEqual(
                corpus_file_card(database, file_id)["content_preview"],
                [],
            )

    def test_scan_limits_structured_preview_to_eight_items(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "structured-preview"
            source.mkdir()
            bodies = [f"body-{number}-" + "x" * 100 for number in range(1, 10)]
            (source / "many-sections.md").write_text(
                "\n\n".join(
                    f"# section-{number}\n{body}"
                    for number, body in enumerate(bodies, start=1)
                ),
                encoding="utf-8",
            )
            database = initialize_workspace(WorkspacePaths(root / "workspace"))

            scan_corpus_source(database, source, actor="mapper")
            file_id = latest_corpus_map(database)["items"][0]["file_id"]
            preview = corpus_file_card(database, file_id)["content_preview"]

            self.assertEqual(len(preview), 8)
            self.assertEqual(
                [item["label"] for item in preview],
                [f"section-{number}" for number in range(1, 9)],
            )
            self.assertEqual(
                [item["text"] for item in preview],
                bodies[:8],
            )

    def test_scan_clips_each_preview_item_to_360_characters(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "item-limit"
            source.mkdir()
            body = "body-" + "x" * 400
            (source / "long-section.md").write_text(
                f"# long-section\n{body}",
                encoding="utf-8",
            )
            database = initialize_workspace(WorkspacePaths(root / "workspace"))

            scan_corpus_source(database, source, actor="mapper")
            file_id = latest_corpus_map(database)["items"][0]["file_id"]
            preview = corpus_file_card(database, file_id)["content_preview"]

            self.assertEqual(preview, [{"label": "long-section", "text": body[:359] + "…"}])
            self.assertEqual(len(preview[0]["text"]), 360)

    def test_scan_caps_total_preview_text_at_2400_characters(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "total-limit"
            source.mkdir()
            bodies = [f"body-{number}-" + "x" * 400 for number in range(1, 9)]
            (source / "large-sections.md").write_text(
                "\n\n".join(
                    f"# section-{number}\n{body}"
                    for number, body in enumerate(bodies, start=1)
                ),
                encoding="utf-8",
            )
            database = initialize_workspace(WorkspacePaths(root / "workspace"))

            scan_corpus_source(database, source, actor="mapper")
            file_id = latest_corpus_map(database)["items"][0]["file_id"]
            preview = corpus_file_card(database, file_id)["content_preview"]

            self.assertEqual(
                [item["label"] for item in preview],
                [f"section-{number}" for number in range(1, 8)],
            )
            self.assertEqual([len(item["text"]) for item in preview], [360] * 6 + [240])
            self.assertEqual(sum(len(item["text"]) for item in preview), 2400)

    def test_scan_samples_first_middle_and_last_unstructured_preview_units(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "unstructured-preview"
            source.mkdir()
            paragraphs = [f"paragraph-{number}" for number in range(1, 6)]
            (source / "notes.txt").write_text(
                "\n\n".join(paragraphs),
                encoding="utf-8",
            )
            database = initialize_workspace(WorkspacePaths(root / "workspace"))

            scan_corpus_source(database, source, actor="mapper")
            file_id = latest_corpus_map(database)["items"][0]["file_id"]
            preview = corpus_file_card(database, file_id)["content_preview"]

            self.assertEqual(
                preview,
                [
                    {"label": "正文片段 1", "text": "paragraph-1"},
                    {"label": "正文片段 3", "text": "paragraph-3"},
                    {"label": "正文片段 5", "text": "paragraph-5"},
                ],
            )

    def test_content_credential_risk_quarantines_an_already_imported_document(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "企业资料"
            source.mkdir()
            risky = source / "00-init-database.sql"
            fake_value = "fake-test-only-password"
            risky.write_text(
                f"password = '{fake_value}';",
                encoding="utf-8",
            )
            paths = WorkspacePaths(root / "workspace")
            with patch(
                "knowledge_workbench.nas_admission.detect_content_credential_risk",
                return_value=False,
            ):
                imported = ingest_file(
                    risky,
                    paths,
                    Classification.INTERNAL,
                    actor="legacy-import",
                )
            database = initialize_workspace(paths)
            with database.transaction() as connection:
                connection.execute(
                    """
                    UPDATE document_governance
                    SET purpose = 'production', scope_status = 'in_scope',
                        authority_status = 'authoritative'
                    WHERE document_id = ?
                    """,
                    (imported.document_id,),
                )

            report = scan_corpus_source(
                database,
                source,
                actor="security-scan",
            )
            self.assertEqual(report["blocked_count"], 1)
            self.assertEqual(report["quarantined_document_count"], 1)
            card = latest_corpus_map(database)["items"][0]
            self.assertEqual(card["map_status"], "blocked")
            self.assertEqual(card["scope_status"], "out_of_scope")
            self.assertEqual(card["authority_status"], "unknown")
            self.assertFalse(card["imported"])
            self.assertNotIn(fake_value, card["plain_summary"])
            self.assertEqual(
                corpus_file_card(database, card["file_id"])["content_preview"],
                [],
            )
            with self.assertRaisesRegex(
                KnowledgeWorkbenchError, "不能纳入知识范围"
            ):
                decide_corpus_file(
                    database,
                    card["file_id"],
                    scope_status="in_scope",
                    authority_status="unknown",
                    actor="owner",
                    reason="错误恢复",
                )
            with self.assertRaisesRegex(KnowledgeWorkbenchError, "禁止导入"):
                import_corpus_file(
                    database,
                    paths,
                    card["file_id"],
                    classification="internal",
                    actor="importer",
                )

            with database.connect() as connection:
                governance = connection.execute(
                    """
                    SELECT d.classification, dg.scope_status, dg.authority_status
                    FROM documents d
                    JOIN document_governance dg ON dg.document_id = d.id
                    WHERE d.id = ?
                    """,
                    (imported.document_id,),
                ).fetchone()
                event = connection.execute(
                    """
                    SELECT entity_id, details_json
                    FROM audit_log
                    WHERE event_type = 'credential_document_quarantined'
                    """
                ).fetchone()
            self.assertEqual(
                tuple(governance),
                ("restricted", "out_of_scope", "unknown"),
            )
            details = json.loads(event["details_json"])
            self.assertEqual(event["entity_id"], imported.document_id)
            self.assertEqual(
                set(details),
                {"risk_code", "sha256"},
            )
            self.assertEqual(details["risk_code"], "credential_material")
            self.assertNotIn(fake_value, event["details_json"])

    def test_generic_ingest_rejects_content_credentials_before_creating_knowledge(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "ordinary-settings.md"
            fake_value = "fake-direct-token-value"
            source.write_text(
                f"token: {fake_value}",
                encoding="utf-8",
            )
            paths = WorkspacePaths(root / "workspace")

            with self.assertRaisesRegex(
                KnowledgeWorkbenchError, "正文存在高置信凭据风险"
            ) as raised:
                ingest_file(source, paths, Classification.INTERNAL)

            self.assertNotIn(fake_value, str(raised.exception))
            self.assertFalse(paths.database.exists())

    def test_duplicate_import_projection_prefers_the_in_scope_direct_card(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "资料"
            archive = source / "archive"
            archive.mkdir(parents=True)
            content = "# 现行制度\n验收后十日内付款。"
            (source / "付款制度.md").write_text(content, encoding="utf-8")
            (archive / "付款制度.md").write_text(content, encoding="utf-8")
            paths = WorkspacePaths(root / "workspace")
            database = initialize_workspace(paths)
            scan_corpus_source(database, source, actor="mapper")
            page = latest_corpus_map(database)
            direct = next(item for item in page["items"] if item["folder"] == "根目录")
            archived = next(item for item in page["items"] if item["folder"] == "archive")
            decide_corpus_file(
                database,
                archived["file_id"],
                scope_status="out_of_scope",
                authority_status="unknown",
                actor="owner",
                reason="归档副本不纳入",
            )
            decide_corpus_file(
                database,
                direct["file_id"],
                scope_status="in_scope",
                authority_status="authoritative",
                actor="owner",
                reason="直接文件为现行版本",
            )
            imported = import_corpus_file(
                database,
                paths,
                direct["file_id"],
                classification="internal",
                actor="importer",
            )
            decide_corpus_file(
                database,
                archived["file_id"],
                scope_status="out_of_scope",
                authority_status="unknown",
                actor="archive-owner",
                reason="再次确认归档副本不纳入",
            )

            projected = latest_corpus_map(database)
            direct_after = next(
                item for item in projected["items"] if item["folder"] == "根目录"
            )
            archive_after = next(
                item for item in projected["items"] if item["folder"] == "archive"
            )
            self.assertTrue(direct_after["imported"])
            self.assertFalse(archive_after["imported"])
            self.assertIsNone(archive_after["imported_document_id"])
            with database.connect() as connection:
                governance_scope = connection.execute(
                    """
                    SELECT scope_status FROM document_governance
                    WHERE document_id = ?
                    """,
                    (imported["document_id"],),
                ).fetchone()[0]
            self.assertEqual(governance_scope, "in_scope")

    def test_scope_decision_syncs_governance_for_an_imported_document(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "资料"
            technical = source / "开发数据" / "DDL" / "schema.sql"
            technical.parent.mkdir(parents=True)
            technical.write_text(
                "CREATE TABLE courses (id INTEGER PRIMARY KEY);",
                encoding="utf-8",
            )
            paths = WorkspacePaths(root / "workspace")
            database = initialize_workspace(paths)
            scan_corpus_source(database, source, actor="mapper")
            card = latest_corpus_map(database)["items"][0]
            decide_corpus_file(
                database,
                card["file_id"],
                scope_status="in_scope",
                authority_status="reference",
                actor="owner",
                reason="技术参考资料",
            )
            imported = import_corpus_file(
                database,
                paths,
                card["file_id"],
                classification="internal",
                actor="importer",
            )
            with database.transaction() as connection:
                page = connection.execute(
                    """
                    SELECT wp.id AS page_id, wr.id AS revision_id
                    FROM wiki_pages wp
                    JOIN wiki_revisions wr ON wr.page_id = wp.id
                    WHERE wp.source_document_id = ?
                    ORDER BY wr.revision_number DESC
                    LIMIT 1
                    """,
                    (imported["document_id"],),
                ).fetchone()
                connection.execute(
                    """
                    UPDATE wiki_revisions SET status = 'verified'
                    WHERE id = ?
                    """,
                    (page["revision_id"],),
                )
                connection.execute(
                    """
                    UPDATE wiki_pages
                    SET status = 'verified',
                        current_verified_revision_id = ?,
                        needs_revalidation = 0
                    WHERE id = ?
                    """,
                    (page["revision_id"], page["page_id"]),
                )
            decide_corpus_file(
                database,
                card["file_id"],
                scope_status="out_of_scope",
                authority_status="superseded",
                actor="owner-02",
                reason="已由新版本替代",
            )

            with database.connect() as connection:
                governance = connection.execute(
                    """
                    SELECT purpose, scope_status, authority_status, reviewed_by,
                           decision_reason, knowledge_domain
                    FROM document_governance
                    WHERE document_id = ?
                    """,
                    (imported["document_id"],),
                ).fetchone()
                page_state = connection.execute(
                    """
                    SELECT needs_revalidation
                    FROM wiki_pages WHERE id = ?
                    """,
                    (page["page_id"],),
                ).fetchone()[0]
                revalidation_events = connection.execute(
                    """
                    SELECT COUNT(*)
                    FROM audit_log
                    WHERE event_type = 'wiki_revalidation_required'
                      AND entity_id = ?
                    """,
                    (page["page_id"],),
                ).fetchone()[0]
            self.assertEqual(
                tuple(governance),
                (
                    "development_fixture",
                    "out_of_scope",
                    "superseded",
                    "owner-02",
                    "已由新版本替代",
                    "technical",
                ),
            )
            self.assertEqual(page_state, 1)
            self.assertEqual(revalidation_events, 1)

    def test_explicit_import_promotes_only_an_approved_current_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "正式资料"
            source.mkdir()
            policy = source / "付款制度.md"
            policy.write_text(
                "# 付款期限\n验收后10个工作日内完成付款。",
                encoding="utf-8",
            )
            paths = WorkspacePaths(root / "workspace")
            database = initialize_workspace(paths)
            scan_corpus_source(database, source, actor="mapper-01")
            file_id = latest_corpus_map(database)["items"][0]["file_id"]

            with self.assertRaisesRegex(
                KnowledgeWorkbenchError, "明确纳入业务范围"
            ):
                import_corpus_file(
                    database,
                    paths,
                    file_id,
                    classification="internal",
                    actor="importer-01",
                )

            decide_corpus_file(
                database,
                file_id,
                scope_status="in_scope",
                authority_status="authoritative",
                actor="owner-01",
                reason="财务负责人确认这是现行版本",
            )
            imported = import_corpus_file(
                database,
                paths,
                file_id,
                classification="internal",
                actor="importer-01",
            )

            self.assertFalse(imported["duplicate"])
            self.assertGreater(imported["evidence_count"], 0)
            self.assertEqual(
                imported["technically_validated_evidence_count"],
                imported["evidence_count"],
            )
            self.assertEqual(
                imported["technical_status_promoted_count"],
                imported["evidence_count"],
            )
            self.assertEqual(imported["next_action"], "review_wiki")
            detail = corpus_file_card(database, file_id)
            self.assertTrue(detail["imported"])
            with database.connect() as connection:
                governance = connection.execute(
                    """
                    SELECT purpose, scope_status, authority_status
                    FROM document_governance
                    WHERE document_id = ?
                    """,
                    (imported["document_id"],),
                ).fetchone()
                event = connection.execute(
                    """
                    SELECT details_json FROM audit_log
                    WHERE event_type = 'corpus_file_imported'
                    """
                ).fetchone()
                evidence_states = connection.execute(
                    """
                    SELECT e.status, etv.status AS validation_status
                    FROM evidence e
                    JOIN evidence_technical_validation etv
                      ON etv.evidence_id = e.id
                    WHERE e.processing_run_id = ?
                    """,
                    (imported["processing_run_id"],),
                ).fetchall()
                automated_audit = connection.execute(
                    """
                    SELECT COUNT(*)
                    FROM audit_log
                    WHERE event_type = 'evidence_status_changed'
                      AND actor = 'system:faithful-ingest-v1'
                    """
                ).fetchone()[0]
            self.assertEqual(
                tuple(governance),
                ("production", "in_scope", "authoritative"),
            )
            self.assertTrue(evidence_states)
            self.assertEqual(
                {(item["status"], item["validation_status"]) for item in evidence_states},
                {("verified", "passed")},
            )
            self.assertEqual(automated_audit, imported["evidence_count"] * 2)
            self.assertIsNotNone(event)
            self.assertNotIn("财务负责人确认", event["details_json"])

            answer = answer_question(
                database,
                "验收后多少个工作日内完成付款？",
                actor="qa-user",
                paths=paths,
            )
            self.assertEqual(answer["answer_type"], "evidence")
            self.assertEqual(answer["retrieval"]["wiki_match_count"], 0)
            self.assertTrue(answer["citations"])
            self.assertIn("10个工作日", answer["citations"][0]["excerpt"])

    def test_read_only_scan_builds_plain_cards_and_candidate_relationships(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "企业资料"
            project = source / "项目甲"
            project.mkdir(parents=True)
            first = project / "实施方案-v1.md"
            second = project / "实施方案-v2.md"
            duplicate = project / "实施方案-v2-副本.md"
            first.write_text(
                "# 项目目标\n建设统一平台。\n\n## 时间安排\n2026年8月完成试点。",
                encoding="utf-8",
            )
            second.write_text(
                "# 项目目标\n建设统一知识平台。\n\n## 时间安排\n2026年9月完成试点。",
                encoding="utf-8",
            )
            duplicate.write_bytes(second.read_bytes())
            secret = project / "各公司账号密码.txt"
            secret.write_text("用户名：admin\n密码：不要进入说明卡", encoding="utf-8")
            (project / "下载中.tmp").write_text("临时内容", encoding="utf-8")
            (project / "设计图.dwg").write_bytes(b"not parsed")

            paths = WorkspacePaths(root / "workspace")
            database = initialize_workspace(paths)
            report = scan_corpus_source(
                database,
                source,
                actor="mapper-01",
                project_name="项目甲",
            )

            self.assertEqual(report["file_count"], 6)
            self.assertEqual(report["readable_card_count"], 3)
            self.assertEqual(report["blocked_count"], 1)
            self.assertGreaterEqual(report["metadata_only_count"], 2)
            self.assertEqual(report["duplicate_group_count"], 1)
            self.assertEqual(report["version_group_count"], 1)

            page = latest_corpus_map(database, limit=20)
            self.assertEqual(page["scan"]["project_name"], "项目甲")
            self.assertEqual(page["total"], 6)
            self.assertEqual(page["folders"][0]["display_name"], "项目甲")
            cards = {item["display_title"]: item for item in page["items"]}
            v1_card = cards["实施方案 v1"]
            self.assertIn("系统读取了", v1_card["plain_summary"])
            self.assertIn("项目目标", v1_card["outline"])
            self.assertIn("2026年8月", v1_card["key_signals"]["dates"])
            self.assertIsNotNone(v1_card["version_group"])

            secret_card = next(
                item
                for item in page["items"]
                if "credential_material" in item["risk_flags"]
            )
            self.assertEqual(secret_card["display_title"], "疑似账号、密码或密钥资料")
            self.assertNotIn("不要进入说明卡", secret_card["plain_summary"])
            secret_detail = corpus_file_card(database, secret_card["file_id"])
            self.assertIsNone(secret_detail["relative_path"])

            duplicate_card = next(
                item for item in page["items"] if item["duplicate_group"]
            )
            detail = corpus_file_card(database, duplicate_card["file_id"])
            self.assertGreaterEqual(len(detail["relations"]), 1)

            with database.connect() as connection:
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM documents").fetchone()[0],
                    0,
                )
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM evidence").fetchone()[0],
                    0,
                )

    def test_latest_corpus_map_filters_fixed_summary_views(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "资料"
            source.mkdir()
            duplicate_content = "# 副本\n两份文件的正文相同。"
            (source / "副本一.md").write_text(duplicate_content, encoding="utf-8")
            (source / "副本二.md").write_text(duplicate_content, encoding="utf-8")
            (source / "流程-v1.md").write_text(
                "# 流程\n第一版流程。", encoding="utf-8"
            )
            (source / "流程-v2.md").write_text(
                "# 流程\n第二版流程。", encoding="utf-8"
            )
            (source / "损坏.pdf").write_bytes(b"not a valid PDF")
            database = initialize_workspace(WorkspacePaths(root / "workspace"))

            scan_corpus_source(database, source, actor="mapper-01")

            self.assertEqual(latest_corpus_map(database, view="all")["total"], 5)
            self.assertEqual(
                latest_corpus_map(database, view="readable")["total"], 4
            )
            self.assertEqual(
                latest_corpus_map(database, view="duplicates")["total"], 2
            )
            self.assertEqual(
                latest_corpus_map(database, view="versions")["total"], 2
            )
            self.assertEqual(
                latest_corpus_map(database, view="attention")["total"], 1
            )
            with self.assertRaisesRegex(KnowledgeWorkbenchError, "资料地图视图"):
                latest_corpus_map(database, view="sql-fragment")

    def test_generic_readmes_in_different_modules_are_not_versions(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "开发资料"
            first = source / "模块甲" / "README.md"
            second = source / "模块乙" / "README.md"
            first.parent.mkdir(parents=True)
            second.parent.mkdir(parents=True)
            first.write_text("# 模块甲\n负责课程管理。", encoding="utf-8")
            second.write_text("# 模块乙\n负责订单管理。", encoding="utf-8")
            database = initialize_workspace(WorkspacePaths(root / "workspace"))

            report = scan_corpus_source(database, source, actor="mapper-01")

            self.assertEqual(report["file_count"], 2)
            self.assertEqual(report["version_group_count"], 0)
            page = latest_corpus_map(database)
            self.assertTrue(
                all(item["version_group"] is None for item in page["items"])
            )

    def test_scope_decision_is_audited_carried_forward_and_blocks_credentials(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "资料"
            source.mkdir()
            (source / "制度.md").write_text(
                "# 适用范围\n本制度适用于项目甲。", encoding="utf-8"
            )
            (source / "账号密码.txt").write_text(
                "password=hidden", encoding="utf-8"
            )
            database = initialize_workspace(WorkspacePaths(root / "workspace"))
            scan_corpus_source(database, source, actor="mapper-01")
            page = latest_corpus_map(database)
            normal = next(
                item
                for item in page["items"]
                if "credential_material" not in item["risk_flags"]
            )
            protected = next(
                item
                for item in page["items"]
                if "credential_material" in item["risk_flags"]
            )

            decided = decide_corpus_file(
                database,
                normal["file_id"],
                scope_status="in_scope",
                authority_status="authoritative",
                actor="owner-01",
                reason="这是现行正式制度",
            )
            self.assertEqual(decided["scope_status"], "in_scope")
            self.assertEqual(decided["authority_status"], "authoritative")
            with self.assertRaisesRegex(
                KnowledgeWorkbenchError, "不能纳入知识范围"
            ):
                decide_corpus_file(
                    database,
                    protected["file_id"],
                    scope_status="in_scope",
                    authority_status="unknown",
                    actor="owner-01",
                    reason="错误操作",
                )

            scan_corpus_source(database, source, actor="mapper-02")
            rescanned = latest_corpus_map(database)
            carried = next(
                item
                for item in rescanned["items"]
                if item["display_title"] == "制度"
            )
            self.assertEqual(carried["scope_status"], "in_scope")
            self.assertEqual(carried["authority_status"], "authoritative")
            with database.connect() as connection:
                event = connection.execute(
                    """
                    SELECT details_json FROM audit_log
                    WHERE event_type = 'corpus_file_scope_decided'
                    ORDER BY id DESC LIMIT 1
                    """
                ).fetchone()
            self.assertIsNotNone(event)
            self.assertNotIn("这是现行正式制度", event["details_json"])


if __name__ == "__main__":
    unittest.main()
