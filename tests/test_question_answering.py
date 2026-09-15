import json
import os
import tempfile
import unittest
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import patch

from knowledge_workbench.config import WorkspacePaths
from knowledge_workbench.database import Database
from knowledge_workbench.ingest import ingest_file
from knowledge_workbench.models import Classification, EvidenceStatus
from knowledge_workbench.policy import ProviderLocation
from knowledge_workbench.providers import AuditedModelGateway
from knowledge_workbench.question_answering import (
    _query_knowledge_domains,
    _question_terms,
    answer_question,
)
from knowledge_workbench.review import (
    publish_revision,
    request_revision_review,
    transition_evidence,
)
from knowledge_workbench.utils import sha256_text, utc_now
from knowledge_workbench.web_service import (
    WorkbenchActionService,
    WorkbenchReadService,
)
from knowledge_workbench.webapp import WorkbenchWebApplication


@dataclass
class StaticAnswerModel:
    response: str
    name: str = "deepseek-chat"
    location: ProviderLocation = ProviderLocation.CLOUD
    calls: list[str] = field(default_factory=list)

    def generate(self, prompt: str) -> str:
        self.calls.append(prompt)
        return self.response


class QuestionAnsweringTests(unittest.TestCase):
    def test_question_selects_source_profiles_only_when_explicit(self):
        self.assertEqual(
            _query_knowledge_domains("研学团付款期限是多少？"),
            ("business", "policy"),
        )
        self.assertEqual(
            _query_knowledge_domains("数据库 API 的付款字段如何设计？"),
            ("business", "policy", "technical"),
        )
        self.assertEqual(
            _query_knowledge_domains("给我一份付款申请模板和填写示例"),
            ("business", "policy", "template", "example"),
        )
        self.assertEqual(
            _query_knowledge_domains("项目进度和交付历史是什么？"),
            ("business", "policy", "process"),
        )
        self.assertEqual(
            _query_knowledge_domains("公司的组织架构是什么？"),
            ("business", "policy"),
        )

    def test_question_terms_do_not_cross_interrogative_noise(self):
        terms = set(_question_terms("旅行社需要提交哪些资料？"))

        self.assertIn("旅行社", terms)
        self.assertIn("提交", terms)
        self.assertNotIn("提交哪些", terms)
        self.assertNotIn("哪些资料", terms)

    def test_conversation_router_handles_greeting_and_realtime_weather(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = WorkspacePaths(root / "workspace")
            source = root / "业务资料.md"
            source.write_text("研学活动需要制定安全预案。", encoding="utf-8")
            ingest_file(source, paths, Classification.INTERNAL)
            database = Database(paths.database)
            model = StaticAnswerModel('{"answer":"不应调用","citation_handles":[]}')
            gateway = AuditedModelGateway(database, model)

            greeting = answer_question(
                database,
                "你好",
                actor="qa-user",
                model_gateway=gateway,
                cloud_model_requested=True,
                allow_internal_cloud_once=True,
            )
            weather = answer_question(
                database,
                "明天天气怎么样",
                actor="qa-user",
                history=[
                    {"role": "user", "content": "你好"},
                    {"role": "assistant", "content": greeting["answer"]},
                ],
                model_gateway=gateway,
                cloud_model_requested=True,
                allow_internal_cloud_once=True,
            )

            self.assertEqual(greeting["answer_type"], "conversation")
            self.assertEqual(
                greeting["retrieval"]["interaction_route"],
                "greeting",
            )
            self.assertIn("你好", greeting["answer"])
            self.assertNotIn("没有找到足够", greeting["answer"])
            self.assertEqual(weather["answer_type"], "external_unavailable")
            self.assertEqual(
                weather["retrieval"]["interaction_route"],
                "external_realtime",
            )
            self.assertIn("没有接入实时天气服务", weather["answer"])
            self.assertIn("恶劣天气", weather["answer"])
            self.assertEqual(model.calls, [])

    def test_workspace_status_question_reads_live_database_inventory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = WorkspacePaths(root / "workspace")
            source = root / "平台业务.md"
            source.write_text(
                "研学课程由导师带队实施。",
                encoding="utf-8",
            )
            result = ingest_file(source, paths, Classification.INTERNAL)
            database = Database(paths.database)
            self._verify_run_evidence(database, result.processing_run_id)
            model = StaticAnswerModel('{"answer":"不应调用","citation_handles":[]}')

            response = answer_question(
                database,
                "数据库目前有哪些数据",
                actor="qa-user",
                model_gateway=AuditedModelGateway(database, model),
                cloud_model_requested=True,
                allow_internal_cloud_once=True,
            )

            inventory = response["retrieval"]["workspace_inventory"]
            self.assertEqual(response["answer_type"], "workspace_status")
            self.assertEqual(
                response["retrieval"]["interaction_route"],
                "workspace_status",
            )
            self.assertEqual(inventory["formal_document_count"], 1)
            self.assertEqual(inventory["qualified_evidence_count"], 1)
            self.assertIn("真正进入研学平台业务问答范围", response["answer"])
            self.assertIn("1 份正式资料", response["answer"])
            self.assertEqual(response["wiki_matches"], [])
            self.assertEqual(response["citations"], [])
            self.assertEqual(model.calls, [])

    def test_development_samples_are_not_used_as_business_answers(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = WorkspacePaths(root / "workspace")
            source = root / "开发样例.md"
            source.write_text("正式付款期限为20个工作日。", encoding="utf-8")
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
                    database,
                    evidence_id,
                    EvidenceStatus.REVIEWING,
                    actor="reviewer",
                )
                transition_evidence(
                    database,
                    evidence_id,
                    EvidenceStatus.VERIFIED,
                    actor="reviewer",
                )

            response = answer_question(
                database,
                "付款期限是多少？",
                actor="qa-user",
                paths=paths,
            )

            self.assertEqual(response["answer_type"], "insufficient")
            self.assertEqual(response["retrieval"]["eligible_document_count"], 0)
            self.assertIn("开发样例", response["answer"])
            self.assertEqual(response["citations"], [])

    def test_published_wiki_is_searched_before_evidence_fallback(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = WorkspacePaths(root / "workspace")
            source = root / "付款制度.md"
            source.write_text(
                "合同款应在项目验收后10个工作日内支付。",
                encoding="utf-8",
            )
            result = ingest_file(source, paths, Classification.INTERNAL)
            database = Database(paths.database)
            self._verify_run_evidence(database, result.processing_run_id)
            request_revision_review(
                database,
                result.revision_id,
                actor="wiki-editor",
            )
            publish_revision(
                database,
                paths,
                result.revision_id,
                actor="wiki-reviewer",
            )

            response = answer_question(
                database,
                "合同款什么时候支付？",
                actor="qa-user",
                paths=paths,
            )

            self.assertEqual(response["answer_type"], "evidence")
            self.assertEqual(response["retrieval"]["mode"], "wiki-first-agent-v1")
            self.assertGreater(response["retrieval"]["wiki_match_count"], 0)
            self.assertTrue(response["wiki_matches"])
            self.assertEqual(
                response["knowledge_path"][0]["title"],
                "先查企业知识页",
            )
            self.assertEqual(response["knowledge_path"][0]["status"], "found")
            self.assertIn("知识页", response["answer"])
            self.assertIn("10个工作日", response["citations"][0]["excerpt"])
            self.assertEqual(
                response["wiki_matches"][0]["evidence_ids"],
                [response["citations"][0]["evidence_id"]],
            )

    def test_default_business_question_excludes_other_source_profiles(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = WorkspacePaths(root / "workspace")
            sources = (
                (
                    "业务规则.md",
                    "研学团付款期限为7个工作日。",
                    "business",
                ),
                (
                    "政策依据.md",
                    "政策规定研学团付款期限为8个工作日。",
                    "policy",
                ),
                (
                    "技术设计.md",
                    "数据库 API 的付款状态字段名为payment_status。",
                    "technical",
                ),
                (
                    "申请模板.md",
                    "付款申请模板中的期限示例为99个工作日。",
                    "template",
                ),
                (
                    "演示数据.md",
                    "演示数据的付款期限为88个工作日。",
                    "example",
                ),
                (
                    "交付记录.md",
                    "历史交付记录写过付款期限为77个工作日。",
                    "process",
                ),
            )
            database = None
            for name, body, domain in sources:
                source = root / name
                source.write_text(body, encoding="utf-8")
                result = ingest_file(source, paths, Classification.INTERNAL)
                database = Database(paths.database)
                self._verify_run_evidence(
                    database,
                    result.processing_run_id,
                    knowledge_domain=domain,
                )
            assert database is not None

            response = answer_question(
                database,
                "研学团付款期限是多少？",
                actor="qa-user",
                paths=paths,
            )

            self.assertEqual(
                response["retrieval"]["knowledge_domains"],
                ["business", "policy"],
            )
            self.assertEqual(
                {item["document_name"] for item in response["citations"]},
                {"业务规则.md", "政策依据.md"},
            )

            technical = answer_question(
                database,
                "数据库 API 的付款状态字段是什么？",
                actor="qa-user",
                paths=paths,
            )
            self.assertEqual(
                technical["retrieval"]["knowledge_domains"],
                ["business", "policy", "technical"],
            )
            self.assertIn(
                "技术设计.md",
                {item["document_name"] for item in technical["citations"]},
            )
            self.assertNotIn(
                "申请模板.md",
                {item["document_name"] for item in technical["citations"]},
            )

    def test_multi_document_topic_wiki_uses_section_evidence_and_live_sources(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = WorkspacePaths(root / "workspace")
            payment = root / "付款制度.md"
            policy = root / "付款政策.md"
            payment.write_text(
                "合同款应在项目验收后10个工作日内支付。",
                encoding="utf-8",
            )
            policy.write_text(
                "付款申请须附项目验收记录。",
                encoding="utf-8",
            )
            first = ingest_file(payment, paths, Classification.INTERNAL)
            second = ingest_file(policy, paths, Classification.CONFIDENTIAL)
            database = Database(paths.database)
            self._verify_run_evidence(database, first.processing_run_id)
            self._verify_run_evidence(
                database,
                second.processing_run_id,
                authority_status="reference",
            )
            first_evidence = self._run_evidence_ids(
                database,
                first.processing_run_id,
            )[0]
            second_evidence = self._run_evidence_ids(
                database,
                second.processing_run_id,
            )[0]
            content = "\n".join(
                (
                    "---",
                    'status: "verified"',
                    'generator: "topic-extractive-v1"',
                    "---",
                    "# 缴费、退款、分账与结算",
                    "",
                    "## 支付期限",
                    f'<!-- topic-section-evidence: ["{first_evidence}"] -->',
                    "合同款应在项目验收后10个工作日内支付。",
                    "",
                    "## 申请材料",
                    f"<!-- evidence:{second_evidence} -->",
                    "付款申请须附项目验收记录。",
                    "",
                )
            )
            relative_path = Path("wiki") / "topic-payment.md"
            target = paths.root / relative_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            now = utc_now()
            with database.transaction() as connection:
                connection.execute(
                    """
                    INSERT INTO wiki_pages(
                        id, source_document_id, slug, title, status,
                        current_verified_revision_id, needs_revalidation,
                        created_at, updated_at
                    )
                    VALUES (?, NULL, ?, ?, 'verified', ?, 0, ?, ?)
                    """,
                    (
                        "page_topic_payment",
                        "topic-payment",
                        "缴费、退款、分账与结算",
                        "rev_topic_payment",
                        now,
                        now,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO wiki_revisions(
                        id, page_id, revision_number, status, markdown_path,
                        content_sha256, generator, created_at, updated_at,
                        processing_run_id
                    )
                    VALUES (?, ?, 1, 'verified', ?, ?, ?, ?, ?, NULL)
                    """,
                    (
                        "rev_topic_payment",
                        "page_topic_payment",
                        relative_path.as_posix(),
                        sha256_text(content),
                        "topic-extractive-v1",
                        now,
                        now,
                    ),
                )
                connection.executemany(
                    """
                    INSERT INTO revision_evidence(revision_id, evidence_id)
                    VALUES ('rev_topic_payment', ?)
                    """,
                    ((first_evidence,), (second_evidence,)),
                )

            response = answer_question(
                database,
                "合同款什么时候支付？",
                actor="qa-user",
                paths=paths,
            )

            topic_match = response["wiki_matches"][0]
            self.assertEqual(
                topic_match["page_title"],
                "缴费、退款、分账与结算",
            )
            self.assertEqual(topic_match["section_title"], "支付期限")
            self.assertEqual(topic_match["evidence_ids"], [first_evidence])
            self.assertEqual(topic_match["classification"], "confidential")
            self.assertEqual(topic_match["authority_status"], "reference")
            self.assertIn("汇总自 2 份资料", topic_match["document_name"])

            routed = answer_question(
                database,
                "平台缴费、退款、分账和结算分别怎么处理？",
                actor="qa-user",
                paths=paths,
            )
            self.assertEqual(routed["answer_type"], "evidence")
            self.assertEqual(
                routed["wiki_matches"][0]["page_title"],
                "缴费、退款、分账与结算",
            )
            self.assertTrue(routed["citations"])
            self.assertNotIn("核对了 0 条", routed["answer"])
            self.assertIn(
                routed["citations"][0]["evidence_id"],
                {
                    evidence_id
                    for match in routed["wiki_matches"]
                    for evidence_id in match["evidence_ids"]
                },
            )

            unsupported_topic = answer_question(
                database,
                "平台如何记录用户定位和隐私授权？",
                actor="qa-user",
                paths=paths,
            )
            self.assertEqual(unsupported_topic["answer_type"], "insufficient")
            self.assertEqual(
                unsupported_topic["retrieval"]["wiki_match_count"],
                0,
            )

            with database.transaction() as connection:
                connection.execute(
                    """
                    UPDATE evidence_technical_validation
                    SET status = 'failed'
                    WHERE evidence_id = ?
                    """,
                    (second_evidence,),
                )
            stale = answer_question(
                database,
                "合同款什么时候支付？",
                actor="qa-user",
                paths=paths,
            )
            self.assertEqual(stale["retrieval"]["wiki_match_count"], 0)
            self.assertEqual(stale["wiki_matches"], [])

    def test_answers_only_from_current_verified_non_restricted_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = WorkspacePaths(root / "workspace")
            internal = root / "验收规则.md"
            restricted = root / "受限项目.md"
            internal.write_text(
                "项目验收期限为30个工作日，验收完成后归档。",
                encoding="utf-8",
            )
            restricted.write_text(
                "受限项目验收期限为90个工作日，机密代号为北辰。",
                encoding="utf-8",
            )
            internal_result = ingest_file(
                internal, paths, Classification.INTERNAL
            )
            restricted_result = ingest_file(
                restricted, paths, Classification.RESTRICTED
            )
            database = Database(paths.database)
            self._verify_run_evidence(database, internal_result.processing_run_id)
            self._verify_run_evidence(database, restricted_result.processing_run_id)

            result = answer_question(
                database,
                "项目验收期限是多少？",
                actor="qa-user",
            )

            self.assertEqual(result["answer_type"], "evidence")
            self.assertTrue(result["citations"])
            self.assertEqual(
                {item["classification"] for item in result["citations"]},
                {"internal"},
            )
            self.assertIn("30个工作日", result["citations"][0]["excerpt"])
            self.assertFalse(result["retrieval"]["cloud_model_used"])
            self.assertFalse(result["retrieval"]["restricted_evidence_included"])
            self.assertFalse(result["conversation_persisted"])

            hidden = answer_question(
                database,
                "机密代号北辰是什么？",
                actor="qa-user",
            )
            self.assertEqual(hidden["answer_type"], "insufficient")
            self.assertEqual(hidden["citations"], [])

            with database.connect() as connection:
                audit = connection.execute(
                    """
                    SELECT details_json FROM audit_log
                    WHERE event_type = 'knowledge_question_answered'
                    ORDER BY id
                    """
                ).fetchall()
            serialized_audit = "\n".join(row["details_json"] for row in audit)
            self.assertNotIn("项目验收期限是多少", serialized_audit)
            self.assertNotIn("30个工作日", serialized_audit)
            self.assertNotIn("机密代号", serialized_audit)

    def test_short_follow_up_uses_recent_user_question_without_persisting_history(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = WorkspacePaths(root / "workspace")
            source = root / "预算说明.md"
            source.write_text(
                "天河项目批准预算为350万元。",
                encoding="utf-8",
            )
            result = ingest_file(source, paths, Classification.INTERNAL)
            database = Database(paths.database)
            self._verify_run_evidence(database, result.processing_run_id)

            response = answer_question(
                database,
                "那是多少？",
                actor="qa-user",
                history=[
                    {"role": "user", "content": "天河项目批准预算是多少？"},
                    {"role": "assistant", "content": "上一轮回答。"},
                ],
            )

            self.assertEqual(response["answer_type"], "evidence")
            self.assertTrue(response["retrieval"]["context_used"])
            self.assertIn("350万元", response["citations"][0]["excerpt"])
            with database.connect() as connection:
                details = connection.execute(
                    """
                    SELECT details_json FROM audit_log
                    WHERE entity_id = ?
                    """,
                    (response["query_id"],),
                ).fetchone()[0]
            self.assertNotIn("天河项目", details)
            self.assertNotIn("上一轮回答", details)

    def test_open_conflict_is_presented_instead_of_silently_resolved(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = WorkspacePaths(root / "workspace")
            source = root / "申请规则.md"
            source.write_text("系统允许用户提交申请。", encoding="utf-8")
            first = ingest_file(source, paths, Classification.INTERNAL)
            database = Database(paths.database)
            self._verify_run_evidence(database, first.processing_run_id)

            source.write_text("系统禁止用户提交申请。", encoding="utf-8")
            second = ingest_file(source, paths, Classification.INTERNAL)
            self._verify_run_evidence(database, second.processing_run_id)

            response = answer_question(
                database,
                "用户提交申请的规则是什么？",
                actor="qa-user",
            )

            self.assertEqual(response["answer_type"], "conflict")
            self.assertEqual(len(response["conflicts"]), 1)
            conflict = response["conflicts"][0]
            self.assertIn("允许", conflict["older"]["excerpt"])
            self.assertIn("禁止", conflict["newer"]["excerpt"])

    def test_multiple_relevant_values_are_marked_ambiguous(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = WorkspacePaths(root / "workspace")
            first_source = root / "实施方案.md"
            second_source = root / "采购要求.md"
            first_source.write_text(
                "本项目团队人员不少于12人。",
                encoding="utf-8",
            )
            second_source.write_text(
                "项目团队人员不少于8人。",
                encoding="utf-8",
            )
            first = ingest_file(
                first_source, paths, Classification.INTERNAL
            )
            second = ingest_file(
                second_source, paths, Classification.INTERNAL
            )
            database = Database(paths.database)
            self._verify_run_evidence(database, first.processing_run_id)
            self._verify_run_evidence(database, second.processing_run_id)

            response = answer_question(
                database,
                "项目团队人员不少于多少人？",
                actor="qa-user",
            )

            self.assertEqual(response["answer_type"], "ambiguous")
            self.assertEqual(
                {
                    item["value"]
                    for item in response["ambiguities"][0]["values"]
                },
                {"12人", "8人"},
            )

    def test_deepseek_synthesizes_only_after_one_time_authorization(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = WorkspacePaths(root / "workspace")
            source = root / "付款规则.md"
            source.write_text(
                "合同款应在验收后10个工作日内支付。",
                encoding="utf-8",
            )
            result = ingest_file(source, paths, Classification.INTERNAL)
            database = Database(paths.database)
            self._verify_run_evidence(database, result.processing_run_id)
            model = StaticAnswerModel(
                json.dumps(
                    {
                        "response_mode": "fact",
                        "answer": "合同款应在验收后10个工作日内支付。[S1]",
                        "source_handles": ["S1"],
                    },
                    ensure_ascii=False,
                )
            )

            response = answer_question(
                database,
                "合同款什么时候支付？",
                actor="qa-user",
                paths=paths,
                model_gateway=AuditedModelGateway(database, model),
                cloud_model_requested=True,
                allow_internal_cloud_once=True,
            )

            self.assertEqual(response["answer_type"], "evidence")
            self.assertEqual(
                response["answer"],
                "合同款应在验收后10个工作日内支付。【依据1】",
            )
            self.assertTrue(response["retrieval"]["cloud_model_requested"])
            self.assertTrue(response["retrieval"]["cloud_model_used"])
            self.assertEqual(
                response["retrieval"]["cloud_model_provider"],
                "deepseek-chat",
            )
            self.assertIsNone(
                response["retrieval"]["cloud_model_fallback_reason"]
            )
            self.assertEqual(len(model.calls), 1)
            self.assertNotIn(result.processing_run_id, model.calls[0])
            with database.connect() as connection:
                events = connection.execute(
                    """
                    SELECT event_type, details_json
                    FROM audit_log
                    WHERE event_type IN (
                        'model_call_succeeded',
                        'knowledge_question_answered'
                    )
                    ORDER BY id
                    """
                ).fetchall()
            self.assertEqual(
                [item["event_type"] for item in events[-2:]],
                ["model_call_succeeded", "knowledge_question_answered"],
            )
            self.assertNotIn("合同款什么时候支付", events[-1]["details_json"])
            self.assertNotIn("10个工作日", events[-1]["details_json"])

    def test_invalid_deepseek_answer_falls_back_to_local_extract(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = WorkspacePaths(root / "workspace")
            source = root / "付款规则.md"
            source.write_text(
                "合同款应在验收后10个工作日内支付。",
                encoding="utf-8",
            )
            result = ingest_file(source, paths, Classification.INTERNAL)
            database = Database(paths.database)
            self._verify_run_evidence(database, result.processing_run_id)
            model = StaticAnswerModel(
                json.dumps(
                    {
                        "response_mode": "fact",
                        "answer": "合同款应在验收后20个工作日内支付。[S1]",
                        "source_handles": ["S1"],
                    },
                    ensure_ascii=False,
                )
            )

            response = answer_question(
                database,
                "合同款什么时候支付？",
                actor="qa-user",
                paths=paths,
                model_gateway=AuditedModelGateway(database, model),
                cloud_model_requested=True,
                allow_internal_cloud_once=True,
            )

            self.assertFalse(response["retrieval"]["cloud_model_used"])
            self.assertEqual(
                response["retrieval"]["cloud_model_fallback_reason"],
                "invalid_model_output",
            )
            self.assertNotIn("20个工作日", response["answer"])
            self.assertIn("原始资料", response["answer"])

    def test_deepseek_uses_global_wiki_context_for_advisory_question(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = WorkspacePaths(root / "workspace")
            results = []
            for name, body in (
                ("安全规则.md", "研学活动应制定安全预案和应急措施。"),
                ("退款规则.md", "用户取消符合条件的订单后可以申请退款。"),
            ):
                source = root / name
                source.write_text(body, encoding="utf-8")
                result = ingest_file(source, paths, Classification.INTERNAL)
                database = Database(paths.database)
                self._verify_run_evidence(database, result.processing_run_id)
                request_revision_review(
                    database,
                    result.revision_id,
                    actor="wiki-editor",
                )
                publish_revision(
                    database,
                    paths,
                    result.revision_id,
                    actor="wiki-reviewer",
                )
                results.append(result)

            model = StaticAnswerModel(
                json.dumps(
                    {
                        "response_mode": "advice",
                        "answer": (
                            "基于现有知识主题，平台资料已经覆盖安全预案和退款规则。"
                            "[W1][W2]\n"
                            "建议：现有资料未充分覆盖用户反馈闭环，可考虑补充调研、"
                            "问题追踪和改进复盘机制。[W1][W2]"
                        ),
                        "source_handles": ["W1", "W2"],
                    },
                    ensure_ascii=False,
                )
            )

            response = answer_question(
                database,
                "怎么扩大受众群体",
                actor="qa-user",
                paths=paths,
                model_gateway=AuditedModelGateway(database, model),
                cloud_model_requested=True,
                allow_internal_cloud_once=True,
            )

            self.assertEqual(response["answer_type"], "advisory")
            self.assertEqual(
                response["retrieval"]["interaction_route"],
                "advisory",
            )
            self.assertTrue(response["retrieval"]["cloud_model_used"])
            self.assertIn("用户反馈闭环", response["answer"])
            self.assertIn("【主题1】", response["answer"])
            self.assertEqual(len(response["wiki_matches"]), 2)
            self.assertEqual(response["citations"], [])
            self.assertEqual(len(model.calls), 1)
            self.assertNotIn(results[0].processing_run_id, model.calls[0])
            self.assertNotIn(str(paths.root), model.calls[0])

    def test_deepseek_can_give_clearly_labeled_general_advice_without_sources(self):
        with tempfile.TemporaryDirectory() as temporary:
            paths = WorkspacePaths(Path(temporary) / "workspace")
            paths.create()
            database = Database(paths.database)
            database.initialize("t1")
            model = StaticAnswerModel(
                json.dumps(
                    {
                        "response_mode": "advice",
                        "answer": (
                            "以下仅为一般建议，不代表现有资料结论。"
                            "建议先访谈目标用户，再验证核心需求。"
                        ),
                        "source_handles": [],
                    },
                    ensure_ascii=False,
                )
            )

            response = answer_question(
                database,
                "怎么提升用户留存",
                actor="qa-user",
                paths=paths,
                model_gateway=AuditedModelGateway(database, model),
                cloud_model_requested=True,
                allow_internal_cloud_once=True,
            )

            self.assertEqual(response["answer_type"], "advisory")
            self.assertEqual(
                response["retrieval"]["interaction_route"],
                "advisory",
            )
            self.assertTrue(response["retrieval"]["cloud_model_used"])
            self.assertEqual(response["citations"], [])
            self.assertEqual(response["wiki_matches"], [])
            self.assertEqual(len(model.calls), 1)

    def test_web_api_requires_csrf_and_returns_cited_answer(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = WorkspacePaths(root / "workspace")
            source = root / "付款规则.md"
            source.write_text("合同款应在验收后10个工作日内支付。", encoding="utf-8")
            result = ingest_file(source, paths, Classification.INTERNAL)
            database = Database(paths.database)
            self._verify_run_evidence(database, result.processing_run_id)
            application = WorkbenchWebApplication(
                WorkbenchReadService(database, paths),
                WorkbenchActionService(database, paths),
                csrf_token="qa-csrf",
            )
            body = json.dumps(
                {
                    "question": "合同款什么时候支付？",
                    "actor": "qa-user",
                    "history": [],
                },
                ensure_ascii=False,
            ).encode("utf-8")

            missing_csrf = application.handle(
                "POST",
                "/api/v1/qa/ask",
                body=body,
                headers={"Content-Type": "application/json"},
            )
            self.assertEqual(missing_csrf.status, 403)
            answered = application.handle(
                "POST",
                "/api/v1/qa/ask",
                body=body,
                headers={
                    "Content-Type": "application/json",
                    "X-Workbench-CSRF": "qa-csrf",
                },
            )
            payload = json.loads(answered.body.decode("utf-8"))
            self.assertEqual(answered.status, 200)
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["result"]["answer_type"], "evidence")
            self.assertIn(
                "10个工作日",
                payload["result"]["citations"][0]["excerpt"],
            )
            bootstrap = json.loads(
                application.handle("GET", "/api/v1/bootstrap").body.decode("utf-8")
            )
            self.assertIn(
                "knowledge-question-answer",
                bootstrap["web"]["write_capabilities"],
            )

    def test_web_api_passes_explicit_deepseek_authorization_once(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = WorkspacePaths(root / "workspace")
            source = root / "付款规则.md"
            source.write_text(
                "合同款应在验收后10个工作日内支付。",
                encoding="utf-8",
            )
            result = ingest_file(source, paths, Classification.INTERNAL)
            database = Database(paths.database)
            self._verify_run_evidence(database, result.processing_run_id)
            model = StaticAnswerModel(
                json.dumps(
                    {
                        "response_mode": "fact",
                        "answer": "合同款应在验收后10个工作日内支付。[S1]",
                        "source_handles": ["S1"],
                    },
                    ensure_ascii=False,
                )
            )
            actions = WorkbenchActionService(
                database,
                paths,
                deepseek_gateway_factory=lambda: AuditedModelGateway(
                    database,
                    model,
                ),
            )
            application = WorkbenchWebApplication(
                WorkbenchReadService(database, paths),
                actions,
                csrf_token="qa-csrf",
            )
            body = json.dumps(
                {
                    "question": "合同款什么时候支付？",
                    "actor": "qa-user",
                    "history": [],
                    "allow_deepseek_once": True,
                },
                ensure_ascii=False,
            ).encode("utf-8")

            with patch.dict(
                os.environ,
                {"DEEPSEEK_API_KEY": "test-key"},
                clear=False,
            ):
                answered = application.handle(
                    "POST",
                    "/api/v1/qa/ask",
                    body=body,
                    headers={
                        "Content-Type": "application/json",
                        "X-Workbench-CSRF": "qa-csrf",
                    },
                )
                bootstrap = json.loads(
                    application.handle(
                        "GET",
                        "/api/v1/bootstrap",
                    ).body.decode("utf-8")
                )

            payload = json.loads(answered.body.decode("utf-8"))
            self.assertEqual(answered.status, 200)
            self.assertTrue(payload["result"]["retrieval"]["cloud_model_used"])
            self.assertEqual(len(model.calls), 1)
            self.assertTrue(
                bootstrap["web"]["providers"]["deepseek"]["configured"]
            )

    @staticmethod
    def _verify_run_evidence(
        database: Database,
        processing_run_id: str,
        *,
        knowledge_domain: str = "business",
        authority_status: str = "authoritative",
    ) -> None:
        with database.transaction() as connection:
            evidence_ids = [
                row[0]
                for row in connection.execute(
                    """
                    SELECT id FROM evidence
                    WHERE processing_run_id = ?
                    ORDER BY run_ordinal
                    """,
                    (processing_run_id,),
                ).fetchall()
            ]
            document_id = connection.execute(
                """
                SELECT dv.document_id
                FROM processing_runs pr
                JOIN document_versions dv ON dv.id = pr.document_version_id
                WHERE pr.id = ?
                """,
                (processing_run_id,),
            ).fetchone()[0]
            connection.execute(
                """
                UPDATE document_governance
                SET purpose = 'production',
                    scope_status = 'in_scope',
                    authority_status = ?,
                    knowledge_domain = ?
                WHERE document_id = ?
                """,
                (authority_status, knowledge_domain, document_id),
            )
        for evidence_id in evidence_ids:
            transition_evidence(
                database,
                evidence_id,
                EvidenceStatus.REVIEWING,
                actor="reviewer",
            )
            transition_evidence(
                database,
                evidence_id,
                EvidenceStatus.VERIFIED,
                actor="reviewer",
            )

    @staticmethod
    def _run_evidence_ids(
        database: Database,
        processing_run_id: str,
    ) -> list[str]:
        with database.connect() as connection:
            return [
                str(row[0])
                for row in connection.execute(
                    """
                    SELECT id FROM evidence
                    WHERE processing_run_id = ?
                    ORDER BY run_ordinal
                    """,
                    (processing_run_id,),
                ).fetchall()
            ]


if __name__ == "__main__":
    unittest.main()
