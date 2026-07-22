import json
import http.client
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from knowledge_workbench.config import WorkspacePaths
from knowledge_workbench.database import Database
from knowledge_workbench.errors import KnowledgeWorkbenchError
from knowledge_workbench.ingest import ingest_file
from knowledge_workbench.models import Classification
from knowledge_workbench.models import EvidenceStatus
from knowledge_workbench.review import reject_revision
from knowledge_workbench.review import request_revision_review
from knowledge_workbench.review import transition_evidence
from knowledge_workbench.web_service import (
    WorkbenchActionService,
    WorkbenchReadService,
)
from knowledge_workbench.webapp import (
    WorkbenchWebApplication,
    build_web_server,
)


class WorkbenchWebTests(unittest.TestCase):
    def test_read_service_uses_current_runs_and_redacts_restricted_names(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = WorkspacePaths(root / "workspace")
            internal = root / "内部说明.md"
            restricted = root / "绝密项目.md"
            internal.write_text("内部证据一。\n\n内部证据二。", encoding="utf-8")
            restricted.write_text("受限证据。", encoding="utf-8")
            ingest_file(internal, paths, Classification.INTERNAL)
            ingest_file(restricted, paths, Classification.RESTRICTED)
            database = Database(paths.database)
            with database.connect() as connection:
                restricted_evidence_id = connection.execute(
                    """
                    SELECT e.id FROM evidence e
                    JOIN document_versions dv ON dv.id = e.document_version_id
                    JOIN documents d ON d.id = dv.document_id
                    WHERE d.classification = 'restricted'
                    """
                ).fetchone()[0]
            transition_evidence(
                database,
                restricted_evidence_id,
                EvidenceStatus.REVIEWING,
                actor="reviewer-01",
            )

            service = WorkbenchReadService(database, paths)
            summary = service.summary()
            documents = service.documents(limit=20)
            review_queue = service.review_queue(limit=20)

            self.assertEqual(summary["document_count"], 2)
            self.assertEqual(summary["current_evidence_count"], 3)
            self.assertEqual(documents["total"], 2)
            restricted_item = next(
                item
                for item in documents["items"]
                if item["classification"] == "restricted"
            )
            self.assertEqual(restricted_item["display_name"], "[受限资料]")
            serialized = json.dumps(documents, ensure_ascii=False)
            self.assertNotIn("绝密项目", serialized)
            self.assertNotIn("source_path", serialized)
            self.assertNotIn("stored_path", serialized)
            restricted_review = next(
                item
                for item in review_queue["evidence"]
                if item["classification"] == "restricted"
            )
            self.assertEqual(restricted_review["document_name"], "[受限资料]")
            self.assertEqual(restricted_review["locator"], {})
            self.assertEqual(review_queue["totals"]["evidence"], 3)

    def test_review_queue_supports_pagination_filters_and_safe_search(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = WorkspacePaths(root / "workspace")
            internal = root / "内部审核资料.md"
            restricted = root / "绝密代号火星.md"
            internal.write_text("第一条证据。\n\n第二条证据。", encoding="utf-8")
            restricted.write_text("受限证据。", encoding="utf-8")
            ingest_file(internal, paths, Classification.INTERNAL)
            ingest_file(restricted, paths, Classification.RESTRICTED)
            database = Database(paths.database)
            with database.connect() as connection:
                internal_ids = [
                    row[0]
                    for row in connection.execute(
                        """
                        SELECT e.id
                        FROM evidence e
                        JOIN processing_runs pr
                          ON pr.id = e.processing_run_id AND pr.is_current = 1
                        JOIN documents d ON d.current_version_id = pr.document_version_id
                        WHERE d.classification = 'internal'
                        ORDER BY e.run_ordinal
                        """
                    ).fetchall()
                ]
            transition_evidence(
                database,
                internal_ids[0],
                EvidenceStatus.REVIEWING,
                actor="reviewer-01",
            )
            service = WorkbenchReadService(database, paths)

            first_page = service.review_queue_page(
                kind="evidence", limit=1, offset=0, classification="internal"
            )
            second_page = service.review_queue_page(
                kind="evidence", limit=1, offset=1, classification="internal"
            )
            reviewing = service.review_queue_page(
                kind="evidence", status="reviewing", query="内部审核"
            )
            hidden_name = service.review_queue_page(
                kind="evidence", query="绝密代号火星"
            )

            self.assertEqual(first_page["total"], 2)
            self.assertEqual(len(first_page["items"]), 1)
            self.assertFalse(first_page["has_previous"])
            self.assertTrue(first_page["has_next"])
            self.assertTrue(second_page["has_previous"])
            self.assertFalse(second_page["has_next"])
            self.assertEqual(reviewing["total"], 1)
            self.assertEqual(reviewing["items"][0]["status"], "reviewing")
            self.assertEqual(hidden_name["total"], 0)

            restricted_by_class = service.review_queue_page(
                kind="evidence", classification="restricted"
            )
            self.assertEqual(restricted_by_class["items"][0]["document_name"], "[受限资料]")
            self.assertEqual(restricted_by_class["items"][0]["locator"], {})

            application = WorkbenchWebApplication(
                service,
                WorkbenchActionService(database),
                csrf_token="csrf",
            )
            response = application.handle(
                "GET",
                "/api/v1/review-queue?kind=evidence&limit=1&offset=1&classification=internal",
            )
            payload = json.loads(response.body.decode("utf-8"))
            self.assertEqual(response.status, 200)
            self.assertEqual(payload["offset"], 1)
            self.assertEqual(len(payload["items"]), 1)
            invalid_kind = application.handle(
                "GET", "/api/v1/review-queue?kind=unknown"
            )
            self.assertEqual(invalid_kind.status, 400)
            oversized_query = application.handle(
                "GET", f"/api/v1/review-queue?kind=evidence&q={'x' * 121}"
            )
            self.assertEqual(oversized_query.status, 400)
            invalid_wiki_status = application.handle(
                "GET", "/api/v1/review-queue?kind=wiki_revisions&status=pending"
            )
            self.assertEqual(invalid_wiki_status.status, 400)
            invalid_conflict_status = application.handle(
                "GET", "/api/v1/review-queue?kind=conflicts&status=conflicted"
            )
            self.assertEqual(invalid_conflict_status.status, 400)

    def test_review_queue_pages_cover_conflicts_and_current_wiki_revisions(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = WorkspacePaths(root / "workspace")
            source = root / "审核策略.md"
            source.write_text("系统允许用户提交申请。", encoding="utf-8")
            ingest_file(source, paths, Classification.INTERNAL)
            database = Database(paths.database)
            with database.connect() as connection:
                revision_id = connection.execute(
                    """
                    SELECT wr.id
                    FROM wiki_revisions wr
                    JOIN processing_runs pr
                      ON pr.id = wr.processing_run_id AND pr.is_current = 1
                    LIMIT 1
                    """
                ).fetchone()[0]
            service = WorkbenchReadService(database, paths)
            draft_revisions = service.review_queue_page(
                kind="wiki_revisions", query="审核策略", status="draft"
            )
            self.assertEqual(draft_revisions["total"], 1)
            request_revision_review(database, revision_id, actor="reviewer-01")

            revisions = service.review_queue_page(
                kind="wiki_revisions", query="审核策略", status="reviewing"
            )

            self.assertEqual(revisions["total"], 1)
            self.assertEqual(revisions["items"][0]["revision_id"], revision_id)
            self.assertEqual(revisions["items"][0]["status"], "reviewing")

            source.write_text("系统禁止用户提交申请。", encoding="utf-8")
            ingest_file(source, paths, Classification.INTERNAL)
            conflicts = service.review_queue_page(
                kind="conflicts", query="审核策略", status="pending"
            )

            self.assertEqual(conflicts["total"], 1)
            self.assertEqual(conflicts["items"][0]["status"], "pending")
            self.assertEqual(conflicts["items"][0]["document_name"], "审核策略.md")

    def test_rejected_revision_history_is_read_only_searchable_and_redacted(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = WorkspacePaths(root / "workspace")
            internal = root / "内部驳回页面.md"
            restricted = root / "绝密驳回页面.md"
            internal.write_text("需要人工修订的内部知识。", encoding="utf-8")
            restricted.write_text("需要人工修订的受限知识。", encoding="utf-8")
            internal_result = ingest_file(internal, paths, Classification.INTERNAL)
            restricted_result = ingest_file(
                restricted, paths, Classification.RESTRICTED
            )
            database = Database(paths.database)
            request_revision_review(
                database, internal_result.revision_id, actor="author-01"
            )
            reject_revision(
                database,
                internal_result.revision_id,
                actor="reviewer-01",
                note="请补充适用范围和生效日期。",
            )
            request_revision_review(
                database, restricted_result.revision_id, actor="author-02"
            )
            reject_revision(
                database,
                restricted_result.revision_id,
                actor="reviewer-02",
                note="绝密修改建议不得在 Web 暴露。",
            )
            service = WorkbenchReadService(database, paths)

            first_page = service.rejected_revision_history(limit=1, offset=0)
            second_page = service.rejected_revision_history(limit=1, offset=1)
            internal_history = service.rejected_revision_history(
                query="内部驳回页面"
            )
            restricted_by_name = service.rejected_revision_history(
                query="绝密驳回页面"
            )
            restricted_history = service.rejected_revision_history(
                classification="restricted"
            )

            self.assertEqual(first_page["total"], 2)
            self.assertTrue(first_page["has_next"])
            self.assertTrue(second_page["has_previous"])
            self.assertEqual(internal_history["total"], 1)
            self.assertEqual(
                internal_history["items"][0]["review_note"],
                "请补充适用范围和生效日期。",
            )
            self.assertEqual(
                internal_history["items"][0]["rejected_by"], "reviewer-01"
            )
            self.assertTrue(internal_history["items"][0]["source_is_current"])
            self.assertEqual(restricted_by_name["total"], 0)
            self.assertEqual(
                restricted_history["items"][0]["page_title"], "[受限知识页]"
            )
            self.assertIsNone(restricted_history["items"][0]["review_note"])

            internal.write_text("新版本内部知识。", encoding="utf-8")
            ingest_file(internal, paths, Classification.INTERNAL)
            stale_history = service.rejected_revision_history(query="内部驳回页面")
            self.assertFalse(stale_history["items"][0]["source_is_current"])

            application = WorkbenchWebApplication(
                service,
                WorkbenchActionService(database, paths),
                csrf_token="csrf",
            )
            response = application.handle(
                "GET",
                "/api/v1/wiki-revisions/history?limit=1&offset=0&classification=restricted",
            )
            payload = json.loads(response.body.decode("utf-8"))
            self.assertEqual(response.status, 200)
            serialized = json.dumps(payload, ensure_ascii=False)
            self.assertNotIn("绝密驳回页面", serialized)
            self.assertNotIn("绝密修改建议", serialized)
            self.assertNotIn("details_json", serialized)
            self.assertNotIn("markdown_path", serialized)

    def test_wiki_revision_detail_and_submit_review_are_controlled(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = WorkspacePaths(root / "workspace")
            internal = root / "内部知识.md"
            restricted = root / "受限知识.md"
            internal.write_text("可进入复核的知识证据。", encoding="utf-8")
            restricted.write_text("不可通过 Web 复核。", encoding="utf-8")
            internal_result = ingest_file(internal, paths, Classification.INTERNAL)
            restricted_result = ingest_file(
                restricted, paths, Classification.RESTRICTED
            )
            database = Database(paths.database)
            application = WorkbenchWebApplication(
                WorkbenchReadService(database, paths),
                WorkbenchActionService(database, paths),
                csrf_token="csrf",
            )

            detail = application.handle(
                "GET", f"/api/v1/wiki-revisions/{internal_result.revision_id}"
            )
            detail_payload = json.loads(detail.body.decode("utf-8"))
            self.assertEqual(detail.status, 200)
            self.assertEqual(detail_payload["status"], "draft")
            self.assertTrue(detail_payload["can_submit_review"])
            self.assertEqual(detail_payload["content_integrity"], "verified")
            self.assertEqual(detail_payload["evidence_count"], 1)
            self.assertIn("可进入复核的知识证据", detail_payload["content_preview"])
            serialized = json.dumps(detail_payload, ensure_ascii=False)
            self.assertNotIn("markdown_path", serialized)
            self.assertNotIn("source_path", serialized)

            restricted_detail = application.handle(
                "GET", f"/api/v1/wiki-revisions/{restricted_result.revision_id}"
            )
            self.assertEqual(restricted_detail.status, 403)

            route = (
                f"/api/v1/wiki-revisions/{internal_result.revision_id}/submit-review"
            )
            headers = {
                "Content-Type": "application/json",
                "X-Workbench-CSRF": "csrf",
            }
            missing_csrf = application.handle(
                "POST",
                route,
                body=json.dumps({"actor": "reviewer-01"}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
            self.assertEqual(missing_csrf.status, 403)
            with database.connect() as connection:
                markdown_path = connection.execute(
                    "SELECT markdown_path FROM wiki_revisions WHERE id = ?",
                    (internal_result.revision_id,),
                ).fetchone()[0]
            revision_path = paths.root / markdown_path
            original_content = revision_path.read_text(encoding="utf-8")
            revision_path.write_text(
                original_content + "\n未同步编辑。\n", encoding="utf-8"
            )
            hash_mismatch = application.handle(
                "POST",
                route,
                body=json.dumps({"actor": "reviewer-01"}).encode("utf-8"),
                headers=headers,
            )
            self.assertEqual(hash_mismatch.status, 409)
            revision_path.write_text(original_content, encoding="utf-8", newline="\n")

            missing_actor = application.handle(
                "POST",
                route,
                body=json.dumps({"actor": ""}).encode("utf-8"),
                headers=headers,
            )
            self.assertEqual(missing_actor.status, 400)
            submitted = application.handle(
                "POST",
                route,
                body=json.dumps({"actor": "reviewer-01"}).encode("utf-8"),
                headers=headers,
            )
            self.assertEqual(submitted.status, 200)
            with database.connect() as connection:
                revision = connection.execute(
                    "SELECT status FROM wiki_revisions WHERE id = ?",
                    (internal_result.revision_id,),
                ).fetchone()[0]
                audit = connection.execute(
                    """
                    SELECT actor FROM audit_log
                    WHERE event_type = 'wiki_revision_submitted'
                      AND entity_id = ?
                    ORDER BY id DESC LIMIT 1
                    """,
                    (internal_result.revision_id,),
                ).fetchone()
            self.assertEqual(revision, "reviewing")
            self.assertEqual(audit["actor"], "reviewer-01")

            reject_route = (
                f"/api/v1/wiki-revisions/{internal_result.revision_id}/reject"
            )
            missing_note = application.handle(
                "POST",
                reject_route,
                body=json.dumps(
                    {"actor": "reviewer-02", "note": ""}
                ).encode("utf-8"),
                headers=headers,
            )
            self.assertEqual(missing_note.status, 400)
            rejected = application.handle(
                "POST",
                reject_route,
                body=json.dumps(
                    {"actor": "reviewer-02", "note": "引用范围需要重新核对。"},
                    ensure_ascii=False,
                ).encode("utf-8"),
                headers=headers,
            )
            self.assertEqual(rejected.status, 200)
            with database.connect() as connection:
                rejected_status = connection.execute(
                    "SELECT status FROM wiki_revisions WHERE id = ?",
                    (internal_result.revision_id,),
                ).fetchone()[0]
                rejection_audit = connection.execute(
                    """
                    SELECT actor, details_json FROM audit_log
                    WHERE event_type = 'wiki_revision_rejected'
                      AND entity_id = ?
                    ORDER BY id DESC LIMIT 1
                    """,
                    (internal_result.revision_id,),
                ).fetchone()
            self.assertEqual(rejected_status, "rejected")
            self.assertEqual(rejection_audit["actor"], "reviewer-02")
            self.assertEqual(
                json.loads(rejection_audit["details_json"])["note"],
                "引用范围需要重新核对。",
            )

            restricted_submit = application.handle(
                "POST",
                f"/api/v1/wiki-revisions/{restricted_result.revision_id}/submit-review",
                body=json.dumps({"actor": "reviewer-01"}).encode("utf-8"),
                headers=headers,
            )
            self.assertEqual(restricted_submit.status, 403)
            restricted_reject = application.handle(
                "POST",
                f"/api/v1/wiki-revisions/{restricted_result.revision_id}/reject",
                body=json.dumps(
                    {"actor": "reviewer-02", "note": "请通过 CLI 核对。"},
                    ensure_ascii=False,
                ).encode("utf-8"),
                headers=headers,
            )
            self.assertEqual(restricted_reject.status, 403)

    def test_wiki_revision_publish_requires_verified_evidence_and_strong_confirmation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = WorkspacePaths(root / "workspace")
            internal = root / "待发布知识.md"
            restricted = root / "受限待发布知识.md"
            internal.write_text("正式发布必须经过受控复核。", encoding="utf-8")
            restricted.write_text("受限内容不得通过 Web 发布。", encoding="utf-8")
            internal_result = ingest_file(internal, paths, Classification.INTERNAL)
            restricted_result = ingest_file(
                restricted, paths, Classification.RESTRICTED
            )
            self.assertIsNotNone(internal_result.revision_id)
            self.assertIsNotNone(restricted_result.revision_id)
            database = Database(paths.database)
            with database.connect() as connection:
                internal_evidence_id = connection.execute(
                    "SELECT id FROM evidence WHERE processing_run_id = ?",
                    (internal_result.processing_run_id,),
                ).fetchone()[0]
                restricted_evidence_id = connection.execute(
                    "SELECT id FROM evidence WHERE processing_run_id = ?",
                    (restricted_result.processing_run_id,),
                ).fetchone()[0]
            request_revision_review(
                database, internal_result.revision_id, actor="author-01"
            )
            request_revision_review(
                database, restricted_result.revision_id, actor="author-01"
            )
            application = WorkbenchWebApplication(
                WorkbenchReadService(database, paths),
                WorkbenchActionService(database, paths),
                csrf_token="csrf",
            )
            route = f"/api/v1/wiki-revisions/{internal_result.revision_id}/publish"
            phrase = f"发布 {internal_result.revision_id}"
            headers = {
                "Content-Type": "application/json",
                "X-Workbench-CSRF": "csrf",
            }

            blocked_detail = application.handle(
                "GET", f"/api/v1/wiki-revisions/{internal_result.revision_id}"
            )
            blocked_payload = json.loads(blocked_detail.body.decode("utf-8"))
            self.assertFalse(blocked_payload["can_publish"])
            self.assertIn("仍有 1 条引用证据未通过审核", blocked_payload["publish_blockers"])
            self.assertEqual(blocked_payload["publish_confirmation_phrase"], phrase)

            wrong_confirmation = application.handle(
                "POST",
                route,
                body=json.dumps(
                    {"actor": "publisher-01", "confirmation": "确认发布"},
                    ensure_ascii=False,
                ).encode("utf-8"),
                headers=headers,
            )
            self.assertEqual(wrong_confirmation.status, 400)
            unverified_evidence = application.handle(
                "POST",
                route,
                body=json.dumps(
                    {"actor": "publisher-01", "confirmation": phrase},
                    ensure_ascii=False,
                ).encode("utf-8"),
                headers=headers,
            )
            self.assertEqual(unverified_evidence.status, 409)

            transition_evidence(
                database,
                internal_evidence_id,
                EvidenceStatus.REVIEWING,
                actor="reviewer-01",
            )
            transition_evidence(
                database,
                internal_evidence_id,
                EvidenceStatus.VERIFIED,
                actor="reviewer-01",
            )
            ready_detail = application.handle(
                "GET", f"/api/v1/wiki-revisions/{internal_result.revision_id}"
            )
            ready_payload = json.loads(ready_detail.body.decode("utf-8"))
            self.assertTrue(ready_payload["can_publish"])
            self.assertEqual(ready_payload["publish_blockers"], [])

            with database.connect() as connection:
                original_relative_path = connection.execute(
                    "SELECT markdown_path FROM wiki_revisions WHERE id = ?",
                    (internal_result.revision_id,),
                ).fetchone()[0]
            original_path = paths.root / original_relative_path
            original_content = original_path.read_text(encoding="utf-8")
            original_path.write_text(
                original_content + "\n未同步发布编辑。\n", encoding="utf-8"
            )
            hash_mismatch = application.handle(
                "POST",
                route,
                body=json.dumps(
                    {"actor": "publisher-01", "confirmation": phrase},
                    ensure_ascii=False,
                ).encode("utf-8"),
                headers=headers,
            )
            self.assertEqual(hash_mismatch.status, 409)
            original_path.write_text(original_content, encoding="utf-8", newline="\n")

            missing_csrf = application.handle(
                "POST",
                route,
                body=json.dumps(
                    {"actor": "publisher-01", "confirmation": phrase},
                    ensure_ascii=False,
                ).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
            self.assertEqual(missing_csrf.status, 403)
            published = application.handle(
                "POST",
                route,
                body=json.dumps(
                    {"actor": "publisher-01", "confirmation": phrase},
                    ensure_ascii=False,
                ).encode("utf-8"),
                headers=headers,
            )
            published_payload = json.loads(published.body.decode("utf-8"))
            self.assertEqual(published.status, 200)
            self.assertEqual(published_payload["result"]["status"], "verified")
            self.assertNotIn("path", json.dumps(published_payload))
            with database.connect() as connection:
                revision = connection.execute(
                    "SELECT status, markdown_path FROM wiki_revisions WHERE id = ?",
                    (internal_result.revision_id,),
                ).fetchone()
                page = connection.execute(
                    "SELECT status, current_verified_revision_id FROM wiki_pages WHERE id = ?",
                    (internal_result.page_id,),
                ).fetchone()
                audit = connection.execute(
                    """
                    SELECT actor FROM audit_log
                    WHERE event_type = 'wiki_revision_published' AND entity_id = ?
                    ORDER BY id DESC LIMIT 1
                    """,
                    (internal_result.revision_id,),
                ).fetchone()
            self.assertEqual(revision["status"], "verified")
            self.assertEqual(page["status"], "verified")
            self.assertEqual(
                page["current_verified_revision_id"], internal_result.revision_id
            )
            self.assertEqual(audit["actor"], "publisher-01")
            self.assertFalse(original_path.exists())
            verified_path = paths.root / revision["markdown_path"]
            self.assertTrue(verified_path.is_file())
            self.assertIn("status: verified", verified_path.read_text(encoding="utf-8"))

            repeated = application.handle(
                "POST",
                route,
                body=json.dumps(
                    {"actor": "publisher-01", "confirmation": phrase},
                    ensure_ascii=False,
                ).encode("utf-8"),
                headers=headers,
            )
            self.assertEqual(repeated.status, 409)

            transition_evidence(
                database,
                restricted_evidence_id,
                EvidenceStatus.REVIEWING,
                actor="reviewer-01",
            )
            transition_evidence(
                database,
                restricted_evidence_id,
                EvidenceStatus.VERIFIED,
                actor="reviewer-01",
            )
            restricted_publish = application.handle(
                "POST",
                f"/api/v1/wiki-revisions/{restricted_result.revision_id}/publish",
                body=json.dumps(
                    {
                        "actor": "publisher-01",
                        "confirmation": f"发布 {restricted_result.revision_id}",
                    },
                    ensure_ascii=False,
                ).encode("utf-8"),
                headers=headers,
            )
            self.assertEqual(restricted_publish.status, 403)

    def test_web_publish_blocks_revision_when_preview_does_not_cover_full_content(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = WorkspacePaths(root / "workspace")
            source = root / "超长知识.md"
            source.write_text(
                "\n\n".join(
                    f"段落 {index:04d}：" + "超长内容" * 250
                    for index in range(70)
                ),
                encoding="utf-8",
            )
            result = ingest_file(source, paths, Classification.INTERNAL)
            database = Database(paths.database)
            with database.connect() as connection:
                evidence_ids = [
                    row[0]
                    for row in connection.execute(
                        "SELECT id FROM evidence WHERE processing_run_id = ?",
                        (result.processing_run_id,),
                    ).fetchall()
                ]
            for evidence_id in evidence_ids:
                transition_evidence(
                    database, evidence_id, EvidenceStatus.REVIEWING, actor="reviewer-01"
                )
                transition_evidence(
                    database, evidence_id, EvidenceStatus.VERIFIED, actor="reviewer-01"
                )
            request_revision_review(database, result.revision_id, actor="author-01")
            application = WorkbenchWebApplication(
                WorkbenchReadService(database, paths),
                WorkbenchActionService(database, paths),
                csrf_token="csrf",
            )

            detail = application.handle(
                "GET", f"/api/v1/wiki-revisions/{result.revision_id}"
            )
            payload = json.loads(detail.body.decode("utf-8"))
            self.assertTrue(payload["content_truncated"])
            self.assertFalse(payload["can_publish"])
            self.assertIn(
                "Web 预览未覆盖全文，请通过 CLI 核对并发布",
                payload["publish_blockers"],
            )
            blocked = application.handle(
                "POST",
                f"/api/v1/wiki-revisions/{result.revision_id}/publish",
                body=json.dumps(
                    {
                        "actor": "publisher-01",
                        "confirmation": f"发布 {result.revision_id}",
                    },
                    ensure_ascii=False,
                ).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "X-Workbench-CSRF": "csrf",
                },
            )
            self.assertEqual(blocked.status, 409)

    def test_evaluation_projection_exposes_metrics_without_local_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            paths = WorkspacePaths(Path(temporary) / "workspace")
            paths.create()
            (paths.evaluations / "pilot.evaluation.json").write_text(
                json.dumps(
                    {
                        "schema_version": "1.1",
                        "dataset_name": "试点评测",
                        "dataset_path": "D:/private/source.json",
                        "evaluated_at": "2026-07-22T00:00:00+00:00",
                        "aggregate": {
                            "case_count": 10,
                            "passed_cases": 10,
                            "pass_rate": 1.0,
                            "evidence_count": 1320,
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            service = WorkbenchReadService(Database(paths.database), paths)

            reports = service.evaluations()

            self.assertEqual(reports[0]["metrics"]["pass_rate"], 1.0)
            self.assertNotIn("dataset_path", reports[0])
            self.assertNotIn("D:/private", json.dumps(reports))

    def test_application_is_read_only_and_validates_pagination(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = WorkspacePaths(root / "workspace")
            source = root / "source.md"
            source.write_text("证据。", encoding="utf-8")
            ingest_file(source, paths, Classification.INTERNAL)
            application = WorkbenchWebApplication(
                WorkbenchReadService(Database(paths.database), paths),
                WorkbenchActionService(Database(paths.database)),
                csrf_token="test-csrf-token",
            )

            response = application.handle("GET", "/api/v1/bootstrap")
            payload = json.loads(response.body.decode("utf-8"))
            self.assertEqual(response.status, 200)
            self.assertEqual(
                payload["summary"]["access_mode"], "local-controlled-write"
            )
            self.assertEqual(payload["web"]["csrf_token"], "test-csrf-token")

            rejected = application.handle("POST", "/api/v1/summary")
            self.assertEqual(rejected.status, 405)
            invalid = application.handle("GET", "/api/v1/documents?limit=201")
            self.assertEqual(invalid.status, 400)
            missing = application.handle("GET", "/api/v1/missing")
            self.assertEqual(missing.status, 404)

    def test_evidence_detail_and_transition_enforce_csrf_audit_and_restricted_policy(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = WorkspacePaths(root / "workspace")
            internal = root / "internal.md"
            restricted = root / "restricted.md"
            internal.write_text("可审核的内部证据。", encoding="utf-8")
            restricted.write_text("不可在Web展示的受限证据。", encoding="utf-8")
            ingest_file(internal, paths, Classification.INTERNAL)
            ingest_file(restricted, paths, Classification.RESTRICTED)
            database = Database(paths.database)
            with database.connect() as connection:
                rows = connection.execute(
                    """
                    SELECT e.id, d.classification
                    FROM evidence e
                    JOIN processing_runs pr
                      ON pr.id = e.processing_run_id AND pr.is_current = 1
                    JOIN documents d ON d.current_version_id = pr.document_version_id
                    ORDER BY d.classification
                    """
                ).fetchall()
            evidence_ids = {row["classification"]: row["id"] for row in rows}
            application = WorkbenchWebApplication(
                WorkbenchReadService(database, paths),
                WorkbenchActionService(database),
                csrf_token="csrf",
            )

            detail = application.handle(
                "GET", f"/api/v1/evidence/{evidence_ids['internal']}"
            )
            self.assertEqual(detail.status, 200)
            detail_payload = json.loads(detail.body.decode("utf-8"))
            self.assertEqual(detail_payload["excerpt"], "可审核的内部证据。")
            self.assertNotIn("source_path", detail_payload)
            restricted_detail = application.handle(
                "GET", f"/api/v1/evidence/{evidence_ids['restricted']}"
            )
            self.assertEqual(restricted_detail.status, 403)

            route = f"/api/v1/evidence/{evidence_ids['internal']}/transition"
            request_body = json.dumps(
                {"target": "reviewing", "actor": "reviewer-01"}
            ).encode("utf-8")
            missing_csrf = application.handle(
                "POST",
                route,
                body=request_body,
                headers={"Content-Type": "application/json"},
            )
            self.assertEqual(missing_csrf.status, 403)
            wrong_content_type = application.handle(
                "POST",
                route,
                body=request_body,
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "X-Workbench-CSRF": "csrf",
                },
            )
            self.assertEqual(wrong_content_type.status, 415)
            missing_actor = application.handle(
                "POST",
                route,
                body=json.dumps({"target": "reviewing", "actor": ""}).encode(
                    "utf-8"
                ),
                headers={
                    "Content-Type": "application/json",
                    "X-Workbench-CSRF": "csrf",
                },
            )
            self.assertEqual(missing_actor.status, 400)
            wrong_origin = application.handle(
                "POST",
                route,
                body=request_body,
                headers={
                    "Content-Type": "application/json",
                    "X-Workbench-CSRF": "csrf",
                    "Origin": "http://attacker.invalid",
                    "Host": "127.0.0.1:8765",
                },
            )
            self.assertEqual(wrong_origin.status, 403)
            transitioned = application.handle(
                "POST",
                route,
                body=request_body,
                headers={
                    "Content-Type": "application/json",
                    "X-Workbench-CSRF": "csrf",
                },
            )
            self.assertEqual(transitioned.status, 200)
            with database.connect() as connection:
                status = connection.execute(
                    "SELECT status FROM evidence WHERE id = ?",
                    (evidence_ids["internal"],),
                ).fetchone()[0]
                audit = connection.execute(
                    """
                    SELECT actor FROM audit_log
                    WHERE event_type = 'evidence_status_changed'
                      AND entity_id = ?
                    ORDER BY id DESC LIMIT 1
                    """,
                    (evidence_ids["internal"],),
                ).fetchone()
            self.assertEqual(status, "reviewing")
            self.assertEqual(audit["actor"], "reviewer-01")

            restricted_route = (
                f"/api/v1/evidence/{evidence_ids['restricted']}/transition"
            )
            restricted_write = application.handle(
                "POST",
                restricted_route,
                body=request_body,
                headers={
                    "Content-Type": "application/json",
                    "X-Workbench-CSRF": "csrf",
                },
            )
            self.assertEqual(restricted_write.status, 403)

    def test_conflict_transition_delegates_to_existing_state_machine(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = WorkspacePaths(root / "workspace")
            source = root / "policy.md"
            source.write_text("系统允许用户提交申请。", encoding="utf-8")
            ingest_file(source, paths, Classification.INTERNAL)
            source.write_text("系统禁止用户提交申请。", encoding="utf-8")
            ingest_file(source, paths, Classification.INTERNAL)
            database = Database(paths.database)
            with database.connect() as connection:
                conflict_id = connection.execute(
                    "SELECT id FROM conflicts ORDER BY created_at DESC LIMIT 1"
                ).fetchone()[0]
            application = WorkbenchWebApplication(
                WorkbenchReadService(database, paths),
                WorkbenchActionService(database),
                csrf_token="csrf",
            )
            headers = {
                "Content-Type": "application/json",
                "X-Workbench-CSRF": "csrf",
            }
            route = f"/api/v1/conflicts/{conflict_id}/transition"

            reviewing = application.handle(
                "POST",
                route,
                body=json.dumps(
                    {"target": "reviewing", "actor": "reviewer-01"}
                ).encode("utf-8"),
                headers=headers,
            )
            self.assertEqual(reviewing.status, 200)
            missing_note = application.handle(
                "POST",
                route,
                body=json.dumps(
                    {"target": "resolved", "actor": "reviewer-01"}
                ).encode("utf-8"),
                headers=headers,
            )
            self.assertEqual(missing_note.status, 409)
            resolved = application.handle(
                "POST",
                route,
                body=json.dumps(
                    {
                        "target": "resolved",
                        "actor": "reviewer-01",
                        "note": "已核对新版本适用范围。",
                    },
                    ensure_ascii=False,
                ).encode("utf-8"),
                headers=headers,
            )
            self.assertEqual(resolved.status, 200)
            with database.connect() as connection:
                row = connection.execute(
                    "SELECT status, resolution_note FROM conflicts WHERE id = ?",
                    (conflict_id,),
                ).fetchone()
            self.assertEqual(row["status"], "resolved")
            self.assertEqual(row["resolution_note"], "已核对新版本适用范围。")

    def test_http_server_sets_security_headers_and_binds_loopback(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = WorkspacePaths(root / "workspace")
            source = root / "source.md"
            source.write_text("证据。", encoding="utf-8")
            ingest_file(source, paths, Classification.INTERNAL)
            database = Database(paths.database)
            with self.assertRaisesRegex(KnowledgeWorkbenchError, "只能绑定"):
                build_web_server(database, paths, host="0.0.0.0", port=0)

            server = build_web_server(database, paths, port=0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                port = server.server_address[1]
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/api/v1/health", timeout=5
                ) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                    self.assertEqual(payload["status"], "ok")
                    self.assertEqual(response.headers["X-Frame-Options"], "DENY")
                    self.assertIn("default-src 'self'", response.headers["Content-Security-Policy"])

                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/api/v1/bootstrap", timeout=5
                ) as response:
                    bootstrap = json.loads(response.read().decode("utf-8"))
                with database.connect() as connection:
                    evidence_id = connection.execute(
                        "SELECT id FROM evidence ORDER BY created_at LIMIT 1"
                    ).fetchone()[0]
                payload = json.dumps(
                    {"target": "reviewing", "actor": "reviewer-01"}
                ).encode("utf-8")
                request = urllib.request.Request(
                    f"http://127.0.0.1:{port}/api/v1/evidence/{evidence_id}/transition",
                    data=payload,
                    method="POST",
                    headers={
                        "Content-Type": "application/json",
                        "X-Workbench-CSRF": bootstrap["web"]["csrf_token"],
                    },
                )
                with urllib.request.urlopen(request, timeout=5) as response:
                    self.assertEqual(response.status, 200)
                with database.connect() as connection:
                    status = connection.execute(
                        "SELECT status FROM evidence WHERE id = ?", (evidence_id,)
                    ).fetchone()[0]
                self.assertEqual(status, "reviewing")

                connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                try:
                    connection.request(
                        "GET", "/api/v1/health", headers={"Host": "attacker.invalid"}
                    )
                    response = connection.getresponse()
                    response.read()
                    self.assertEqual(response.status, 403)
                finally:
                    connection.close()

                request = urllib.request.Request(
                    f"http://127.0.0.1:{port}/api/v1/summary",
                    method="POST",
                )
                with self.assertRaises(urllib.error.HTTPError) as captured:
                    urllib.request.urlopen(request, timeout=5)
                self.assertEqual(captured.exception.code, 405)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
