from __future__ import annotations

import json
import math
import re
import unicodedata
from collections import Counter
from typing import Any, Sequence

from .audit import record_event
from .config import WorkspacePaths
from .conflict_candidates import (
    cross_document_candidate_page,
    list_cross_document_candidate_packs,
)
from .conflicts import _meaningful_values
from .citation_support import MINIMUM_CITATION_SUPPORT, assess_citation_texts
from .database import Database
from .errors import KnowledgeWorkbenchError, PolicyDeniedError
from .models import Classification
from .policy import most_restrictive
from .providers import AuditedModelGateway
from .topic_wiki import TOPICS
from .utils import new_id, sha256_text


MAX_QUESTION_CHARACTERS = 500
MAX_HISTORY_TURNS = 6
MAX_CITATIONS = 8
DEFAULT_CITATIONS = 5
MAX_EXCERPT_CHARACTERS = 1200
MAX_WIKI_MATCHES = 3
MAX_WIKI_EXCERPT_CHARACTERS = 900
MIN_RELEVANCE_SCORE = 0.28
MAX_ADVISORY_ANSWER_CHARACTERS = 6_000
MAX_ADVISORY_TOPIC_EXCERPT_CHARACTERS = 700

_MODEL_SOURCE_PATTERN = re.compile(r"\[([SW])([1-9]\d*)\]")
_MODEL_SOURCE_TOKEN_PATTERN = re.compile(r"\[[SW][^\]]*\]")
_NUMBER_TOKEN_PATTERN = re.compile(r"\d+(?:\.\d+)?")
_GREETING_ONLY = frozenset(
    {
        "你好",
        "您好",
        "嗨",
        "哈喽",
        "hello",
        "hi",
        "在吗",
    }
)
_WORKSPACE_NOUNS = ("知识库", "数据库", "资料库", "工作区")
_WORKSPACE_INVENTORY_MARKERS = (
    "有哪些数据",
    "有什么数据",
    "有哪些资料",
    "有什么资料",
    "当前数据",
    "目前数据",
    "数据概况",
    "资料概况",
    "数据量",
    "多少份资料",
    "多少条证据",
    "导入了哪些",
    "收录了哪些",
)
_REALTIME_WEATHER_MARKERS = (
    "天气",
    "气温",
    "下雨",
    "降雨",
    "下雪",
    "台风",
    "空气质量",
)
_REALTIME_TIME_MARKERS = (
    "今天",
    "明天",
    "后天",
    "今晚",
    "实时",
    "现在",
    "当前",
)
_POLICY_CONTEXT_MARKERS = ("预案", "制度", "规定", "流程", "措施", "应急")
_DOMAIN_LABELS = {
    "business": "业务资料",
    "policy": "政策资料",
    "technical": "技术资料",
    "template": "模板",
    "example": "示例",
    "process": "过程记录",
}

_KNOWLEDGE_DOMAIN_ORDER = (
    "business",
    "policy",
    "technical",
    "template",
    "example",
    "process",
)
_DEFAULT_KNOWLEDGE_DOMAINS = ("business", "policy")
_TECHNICAL_QUERY_MARKERS = (
    "数据库",
    "数据表",
    "表结构",
    "字段设计",
    "技术架构",
    "系统架构",
    "架构设计",
    "技术选型",
    "接口设计",
    "接口文档",
    "接口调用",
    "后端",
    "前端",
    "服务端",
    "微服务",
    "时序图",
    "状态机",
    "数据字典",
    "缓存",
)
_TECHNICAL_ASCII_PATTERN = re.compile(
    r"(?<![a-z0-9])(?:api|sql|ddl|schema|endpoint|mysql|postgresql|"
    r"redis|kafka|elasticsearch)(?![a-z0-9])",
    re.IGNORECASE,
)
_TEMPLATE_QUERY_MARKERS = (
    "模板",
    "样表",
    "范本",
    "空白表",
    "表单样式",
    "填写格式",
)
_EXAMPLE_QUERY_MARKERS = (
    "示例",
    "样例",
    "演示数据",
    "演示项目",
    "测试数据",
)
_PROCESS_QUERY_MARKERS = (
    "项目进度",
    "开发进度",
    "实施进度",
    "当前进展",
    "工作计划",
    "交付历史",
    "交付记录",
    "交付清单",
    "里程碑",
    "完成度",
    "版本历史",
    "开发记录",
)
_EVIDENCE_ID_PATTERN = re.compile(r"\bev_[a-z0-9_]+\b", re.IGNORECASE)
_WIKI_EVIDENCE_MARKER_PATTERN = re.compile(
    r"<!--\s*(?:topic-section-evidence|evidence_ids?|evidence)\s*:"
    r"\s*(.*?)\s*-->",
    re.IGNORECASE,
)

_CJK_PATTERN = re.compile(r"[\u3400-\u9fff]+")
_ASCII_TERM_PATTERN = re.compile(r"[a-z0-9][a-z0-9._-]*", re.IGNORECASE)
_VALUE_PATTERN = re.compile(
    r"\d+(?:\.\d+)?(?:%|％|亿元|万元|元|个工作日|工作日|天|小时|分钟|秒|人|份|次|项|套|台|个)?"
)
_QUESTION_FILLERS = (
    "请问",
    "我想知道",
    "告诉我",
    "根据资料",
    "现有资料",
    "知识库里",
    "知识库中",
    "是什么",
    "有什么",
    "有哪些",
    "怎么样",
    "怎么做",
    "如何",
    "是否",
    "多少",
    "什么时候",
    "为什么",
    "关于",
    "有关",
    "目前",
    "现在",
    "其中",
    "一下",
    "的",
    "吗",
    "呢",
)
_STOP_NGRAMS = frozenset(
    {
        "这个",
        "那个",
        "哪些",
        "什么",
        "怎么",
        "如何",
        "是否",
        "可以",
        "需要",
        "目前",
        "现在",
        "情况",
        "内容",
        "资料",
        "知识",
        "一下",
        "以及",
        "还有",
        "进行",
        "相关",
        "包含",
        "包括",
        "列出",
    }
)
_CONTEXT_PREFIXES = (
    "那",
    "它",
    "这个",
    "该",
    "其中",
    "还有",
    "然后",
    "具体",
    "什么时候",
    "多少",
)
_VALUE_QUERY_MARKERS = (
    "多少",
    "几人",
    "几天",
    "多久",
    "何时",
    "什么时候",
    "金额",
    "预算",
    "人数",
)


def answer_question(
    database: Database,
    question: str,
    *,
    actor: str,
    history: Sequence[dict[str, object]] | None = None,
    limit: int = DEFAULT_CITATIONS,
    paths: WorkspacePaths | None = None,
    audit: bool = True,
    model_gateway: AuditedModelGateway | None = None,
    cloud_model_requested: bool = False,
    allow_internal_cloud_once: bool = False,
    cloud_model_unavailable_reason: str | None = None,
) -> dict[str, Any]:
    normalized_actor = _required_actor(actor)
    normalized_question = _required_question(question)
    normalized_history = _validated_history(history or ())
    if limit < 1 or limit > MAX_CITATIONS:
        raise ValueError(f"limit 必须在 1 到 {MAX_CITATIONS} 之间")
    query_id = new_id("qa")
    cloud_model_requested = bool(cloud_model_requested or model_gateway is not None)
    interaction_route = _interaction_route(normalized_question)
    if interaction_route != "knowledge":
        return _answer_non_knowledge_interaction(
            database,
            normalized_question,
            actor=normalized_actor,
            history=normalized_history,
            query_id=query_id,
            interaction_route=interaction_route,
            cloud_model_requested=cloud_model_requested,
            audit=audit,
        )

    previous_questions = [
        str(item["content"])
        for item in normalized_history
        if item["role"] == "user"
    ]
    context_used = bool(previous_questions) and _needs_context(normalized_question)
    retrieval_question = (
        f"{previous_questions[-1]} {normalized_question}"
        if context_used
        else normalized_question
    )
    effective_paths = paths or WorkspacePaths(database.path.parent)
    knowledge_domains = _query_knowledge_domains(retrieval_question)
    with database.transaction() as connection:
        eligible_document_count = _eligible_document_count(
            connection,
            knowledge_domains,
        )
        entity_context = _entity_navigation_context(
            connection,
            retrieval_question,
            knowledge_domains,
        )
        expanded_query = " ".join(
            [
                retrieval_question,
                *[
                    str(item["name"])
                    for item in entity_context["related_entities"]
                ],
            ]
        ).strip()
        wiki_sections = _load_wiki_sections(
            connection,
            effective_paths,
            knowledge_domains,
        )
        reasoning_topics = (
            _advisory_topic_profiles(wiki_sections)
            if cloud_model_requested
            else []
        )
        reasoning_inventory = (
            _workspace_inventory(connection)
            if cloud_model_requested
            else None
        )
        ranked_wiki = _rank_wiki_sections(wiki_sections, expanded_query)
        wiki_matches = _select_wiki_matches(ranked_wiki)
        candidates = _load_candidates(connection, knowledge_domains)
        ranked = _promote_wiki_evidence(
            _rank_candidates(candidates, expanded_query),
            candidates,
            wiki_matches,
        )
        citations = _select_citations(ranked, limit=limit)
        conflicts = _matching_conflicts(
            connection,
            {item["evidence_id"] for item in citations},
        )
        if paths:
            conflicts.extend(
                _matching_reviewed_cross_document_conflicts(
                    database,
                    paths,
                    {item["evidence_id"] for item in citations},
                    existing_pair_ids={
                        frozenset(
                            (
                                item["older"]["evidence_id"],
                                item["newer"]["evidence_id"],
                            )
                        )
                        for item in conflicts
                    },
                )
            )
        ambiguities = _detect_value_ambiguities(
            normalized_question,
            citations,
        )
        if conflicts:
            answer_type = "conflict"
            answer = (
                "我先查了企业知识页并回到原始依据，发现相关内容存在冲突。"
                "我不会替你选择其中一条，请并列核对不同说法、适用范围和来源位置。"
            )
        elif ambiguities:
            answer_type = "ambiguous"
            answer = (
                "我在相关知识页和原始依据中找到了多个不同数值，但暂时无法判断"
                "哪个适用于你的问题。我不会擅自选择，请分别核对引用及适用范围。"
            )
        elif wiki_matches and citations:
            answer_type = "evidence"
            primary = wiki_matches[0]
            answer = (
                f"我先查到知识页《{primary['page_title']}》中的"
                f"“{primary['section_title']}”。最相关的内容是："
                f"{_answer_excerpt(primary['excerpt'])}"
                f" 我又核对了 {len(citations)} 条原始依据，详情和位置列在下方。"
            )
        elif wiki_matches:
            answer_type = "insufficient"
            answer = (
                "我找到了相关企业知识页，但暂时无法回到原始依据完成核对。"
                "为了避免只根据整理页作答，我先不下结论；请稍后重试或到"
                "“业务复核”中检查该知识页的来源状态。"
            )
        elif citations:
            answer_type = "evidence"
            answer = (
                "现有企业知识页里没有直接写出答案。"
                f"我继续回到已纳入业务范围的原始资料，找到 {len(citations)} 条"
                "相关依据；请结合资料名称和位置核对。"
            )
        elif eligible_document_count == 0:
            answer_type = "insufficient"
            answer = (
                "当前还没有完成业务范围确认的正式资料。为了避免把开发样例当成"
                "企业知识，我暂时不回答这个业务问题。请先在“全库资料地图”中"
                "确认应纳入的文件和权威版本。"
            )
        else:
            answer_type = "insufficient"
            answer = (
                "我先查了企业知识页，也回到已纳入范围的原始资料，但没有找到"
                "足够相关的依据。我不能在缺少依据时猜答案；你可以补充业务对象、"
                "时间范围或换一种说法。"
            )

        navigation = _navigation_steps(
            wiki_matches=wiki_matches,
            entity_context=entity_context,
            citations=citations,
            eligible_document_count=eligible_document_count,
        )
        follow_up_suggestions = _follow_up_suggestions(
            wiki_matches=wiki_matches,
            entity_context=entity_context,
            citations=citations,
        )

    cloud_model_used = False
    cloud_model_provider = (
        model_gateway.provider.name if model_gateway is not None else "deepseek-chat"
    )
    cloud_model_fallback_reason: str | None = None
    model_response_mode: str | None = None
    final_interaction_route = "knowledge"
    final_retrieval_mode = "wiki-first-agent-v1"
    if cloud_model_requested:
        if model_gateway is None:
            cloud_model_fallback_reason = (
                cloud_model_unavailable_reason or "not_configured"
            )
        elif conflicts or ambiguities:
            cloud_model_fallback_reason = "answer_not_eligible"
        elif reasoning_inventory is not None:
            classification = _citation_classification_for_model(
                [*citations, *reasoning_topics]
            )
            prompt = _reasoning_prompt(
                retrieval_question,
                citations,
                reasoning_topics,
                reasoning_inventory,
            )
            try:
                raw_output = model_gateway.generate(
                    prompt,
                    classification,
                    actor=normalized_actor,
                    allow_internal_cloud_once=allow_internal_cloud_once,
                )
            except PolicyDeniedError:
                cloud_model_fallback_reason = "policy_denied"
            except Exception:
                cloud_model_fallback_reason = "provider_error"
            else:
                try:
                    reasoning_result = _validated_reasoning_answer(
                        raw_output,
                        question=retrieval_question,
                        citations=citations,
                        topics=reasoning_topics,
                        inventory=reasoning_inventory,
                    )
                except KnowledgeWorkbenchError:
                    cloud_model_fallback_reason = "invalid_model_output"
                else:
                    cloud_model_used = True
                    model_response_mode = reasoning_result["response_mode"]
                    answer = reasoning_result["answer"]
                    citations = _citations_for_handles(
                        citations,
                        reasoning_result["evidence_handles"],
                    )
                    wiki_matches = _advisory_wiki_matches(
                        reasoning_topics,
                        reasoning_result["wiki_handles"],
                    )
                    conflicts = []
                    ambiguities = []
                    (
                        answer_type,
                        final_interaction_route,
                    ) = _reasoning_response_classification(
                        model_response_mode,
                    )
                    final_retrieval_mode = "wiki-reasoning-agent-v2"
                    navigation = _reasoning_navigation(
                        model_response_mode,
                        topic_count=len(reasoning_topics),
                        citation_count=len(citations),
                    )
                    follow_up_suggestions = _reasoning_follow_ups(
                        model_response_mode
                    )
        else:
            cloud_model_fallback_reason = "answer_not_eligible"

    if audit:
        with database.transaction() as connection:
            record_event(
                connection,
                "knowledge_question_answered",
                "qa_query",
                query_id,
                actor=normalized_actor,
                details={
                    "question_sha256": sha256_text(normalized_question),
                    "retrieval_question_sha256": sha256_text(retrieval_question),
                    "answer_type": answer_type,
                    "retrieval_mode": final_retrieval_mode,
                    "interaction_route": final_interaction_route,
                    "model_response_mode": model_response_mode,
                    "knowledge_domains": list(knowledge_domains),
                    "context_used": context_used,
                    "history_turn_count": len(normalized_history),
                    "eligible_document_count": eligible_document_count,
                    "wiki_page_ids": sorted(
                        {item["page_id"] for item in wiki_matches}
                    ),
                    "entity_ids": [
                        item["entity_id"] for item in entity_context["matched_entities"]
                    ],
                    "candidate_count": len(candidates),
                    "evidence_ids": [item["evidence_id"] for item in citations],
                    "classifications": sorted(
                        {item["classification"] for item in citations}
                    ),
                    "conflict_ids": [item["conflict_id"] for item in conflicts],
                    "ambiguity_count": len(ambiguities),
                    "cloud_model_requested": cloud_model_requested,
                    "cloud_model_used": cloud_model_used,
                    "cloud_model_provider": (
                        cloud_model_provider if cloud_model_requested else None
                    ),
                    "cloud_model_fallback_reason": cloud_model_fallback_reason,
                },
            )

    return {
        "query_id": query_id,
        "answer_type": answer_type,
        "answer": answer,
        "knowledge_path": navigation,
        "wiki_matches": wiki_matches,
        "entity_context": entity_context,
        "follow_up_suggestions": follow_up_suggestions,
        "citations": citations,
        "conflicts": conflicts,
        "ambiguities": ambiguities,
        "retrieval": {
            "mode": final_retrieval_mode,
            "interaction_route": final_interaction_route,
            "model_response_mode": model_response_mode,
            "candidate_count": len(candidates),
            "eligible_document_count": eligible_document_count,
            "wiki_section_count": len(wiki_sections),
            "wiki_match_count": len(wiki_matches),
            "matched_entity_count": len(entity_context["matched_entities"]),
            "related_entity_count": len(entity_context["related_entities"]),
            "context_used": context_used,
            "knowledge_domains": list(knowledge_domains),
            "cloud_model_requested": cloud_model_requested,
            "cloud_model_used": cloud_model_used,
            "cloud_model_provider": (
                cloud_model_provider if cloud_model_requested else None
            ),
            "cloud_model_fallback_reason": cloud_model_fallback_reason,
            "restricted_evidence_included": False,
        },
        "conversation_persisted": False,
    }


def _interaction_route(question: str) -> str:
    normalized = unicodedata.normalize("NFKC", question).strip().casefold()
    compact = re.sub(r"[\s,.!?，。！？～~]+", "", normalized)
    if compact in _GREETING_ONLY:
        return "greeting"
    if (
        any(noun in normalized for noun in _WORKSPACE_NOUNS)
        and any(
            marker in normalized for marker in _WORKSPACE_INVENTORY_MARKERS
        )
    ):
        return "workspace_status"
    if (
        any(marker in normalized for marker in _REALTIME_WEATHER_MARKERS)
        and any(marker in normalized for marker in _REALTIME_TIME_MARKERS)
        and not any(marker in normalized for marker in _POLICY_CONTEXT_MARKERS)
    ):
        return "external_realtime"
    return "knowledge"


def _answer_non_knowledge_interaction(
    database: Database,
    question: str,
    *,
    actor: str,
    history: Sequence[dict[str, object]],
    query_id: str,
    interaction_route: str,
    cloud_model_requested: bool,
    audit: bool,
) -> dict[str, Any]:
    with database.connect() as connection:
        inventory = _workspace_inventory(connection)

    if interaction_route == "greeting":
        answer_type = "conversation"
        answer = (
            "你好！我是研学平台知识助手。你不需要记住文件名，可以直接问课程、"
            "基地、导师、线路、报名、缴费退款、安全保障、组织权限等业务问题；"
            "也可以问我“当前知识库有哪些资料”。"
        )
        navigation = [
            {
                "title": "识别为日常交流",
                "status": "found",
                "detail": "这是问候，不需要检索企业资料或调用云模型。",
            }
        ]
        suggestions = [
            "当前知识库有哪些资料？",
            "平台缴费、退款和结算分别怎么处理？",
            "研学活动需要哪些安全保障？",
        ]
        fallback_reason = "not_needed_for_greeting"
    elif interaction_route == "external_realtime":
        answer_type = "external_unavailable"
        answer = (
            "我当前连接的是研学平台企业知识库，没有接入实时天气服务，因此不能"
            "可靠回答明天的天气。请使用天气应用查询；如果你想了解研学行程遇到"
            "恶劣天气时的应急预案、安全措施或取消规则，我可以继续查询库内资料。"
        )
        navigation = [
            {
                "title": "识别为实时外部信息",
                "status": "found",
                "detail": "实时天气不在企业知识库中，也没有可用的天气数据接口。",
            },
            {
                "title": "说明当前能力边界",
                "status": "blocked",
                "detail": "没有实时来源时不让模型猜测天气。",
            },
        ]
        suggestions = [
            "研学活动遇到恶劣天气时有哪些安全措施？",
            "行程取消和退款规则是什么？",
        ]
        fallback_reason = "external_realtime_unavailable"
    elif interaction_route == "workspace_status":
        answer_type = "workspace_status"
        domain_summary = "、".join(
            f"{_DOMAIN_LABELS.get(item['knowledge_domain'], item['knowledge_domain'])}"
            f"{item['document_count']}份"
            for item in inventory["domains"]
        )
        topic_summary = "、".join(inventory["topic_titles"])
        answer = (
            f"当前技术数据库共有 {inventory['all_document_count']} 份文档记录、"
            f"{inventory['all_current_evidence_count']} 条当前证据；其中真正进入研学"
            f"平台业务问答范围的是 {inventory['formal_document_count']} 份正式资料、"
            f"{inventory['qualified_evidence_count']} 条可回溯依据。正式资料构成为："
            f"{domain_summary}。目前已发布 {inventory['published_topic_count']} 个知识"
            f"主题：{topic_summary}。其余开发与历史背景记录默认不会参与业务回答。"
        )
        navigation = [
            {
                "title": "读取当前工作区",
                "status": "found",
                "detail": "直接从 SQLite 读取实时计数，没有使用业务资料关键词猜测。",
            },
            {
                "title": "区分正式业务范围",
                "status": "found",
                "detail": (
                    f"{inventory['formal_document_count']} 份资料和 "
                    f"{inventory['qualified_evidence_count']} 条依据可用于当前问答。"
                ),
            },
            {
                "title": "汇总正式知识主题",
                "status": "found",
                "detail": (
                    f"当前有 {inventory['published_topic_count']} 个已发布主题。"
                ),
            },
        ]
        suggestions = [
            "当前已发布的知识主题分别能回答什么？",
            "哪些资料属于正式业务范围？",
            "哪些数据默认不会进入业务问答？",
        ]
        fallback_reason = "workspace_status_local"
    else:
        raise ValueError(f"未知会话路由：{interaction_route}")

    if audit:
        with database.transaction() as connection:
            record_event(
                connection,
                "knowledge_question_answered",
                "qa_query",
                query_id,
                actor=actor,
                details={
                    "question_sha256": sha256_text(question),
                    "answer_type": answer_type,
                    "retrieval_mode": "conversation-router-v2",
                    "interaction_route": interaction_route,
                    "context_used": False,
                    "history_turn_count": len(history),
                    "eligible_document_count": inventory[
                        "formal_document_count"
                    ],
                    "candidate_count": 0,
                    "wiki_page_ids": [],
                    "entity_ids": [],
                    "evidence_ids": [],
                    "classifications": [],
                    "conflict_ids": [],
                    "ambiguity_count": 0,
                    "cloud_model_requested": cloud_model_requested,
                    "cloud_model_used": False,
                    "cloud_model_provider": (
                        "deepseek-chat" if cloud_model_requested else None
                    ),
                    "cloud_model_fallback_reason": (
                        fallback_reason if cloud_model_requested else None
                    ),
                },
            )

    return {
        "query_id": query_id,
        "answer_type": answer_type,
        "answer": answer,
        "knowledge_path": navigation,
        "wiki_matches": [],
        "entity_context": {
            "matched_entities": [],
            "related_entities": [],
            "relationships": [],
        },
        "follow_up_suggestions": suggestions,
        "citations": [],
        "conflicts": [],
        "ambiguities": [],
        "retrieval": {
            "mode": "conversation-router-v2",
            "interaction_route": interaction_route,
            "candidate_count": 0,
            "eligible_document_count": inventory["formal_document_count"],
            "wiki_section_count": inventory["published_topic_count"],
            "wiki_match_count": 0,
            "matched_entity_count": 0,
            "related_entity_count": 0,
            "context_used": False,
            "knowledge_domains": [],
            "cloud_model_requested": cloud_model_requested,
            "cloud_model_used": False,
            "cloud_model_provider": (
                "deepseek-chat" if cloud_model_requested else None
            ),
            "cloud_model_fallback_reason": (
                fallback_reason if cloud_model_requested else None
            ),
            "restricted_evidence_included": False,
            "workspace_inventory": (
                inventory if interaction_route == "workspace_status" else None
            ),
        },
        "conversation_persisted": False,
    }


def _workspace_inventory(connection) -> dict[str, Any]:
    formal_filter = """
        d.current_version_id IS NOT NULL
        AND d.classification != 'restricted'
        AND dg.purpose = 'production'
        AND dg.scope_status = 'in_scope'
        AND dg.authority_status != 'superseded'
    """
    formal_document_count = int(
        connection.execute(
            f"""
            SELECT COUNT(*)
            FROM documents d
            JOIN document_governance dg ON dg.document_id = d.id
            WHERE {formal_filter}
            """
        ).fetchone()[0]
    )
    qualified_evidence_count = int(
        connection.execute(
            f"""
            SELECT COUNT(*)
            FROM evidence e
            JOIN evidence_technical_validation etv
              ON etv.evidence_id = e.id AND etv.status = 'passed'
            JOIN processing_runs pr
              ON pr.id = e.processing_run_id AND pr.is_current = 1
            JOIN document_versions dv ON dv.id = e.document_version_id
            JOIN documents d
              ON d.id = dv.document_id AND d.current_version_id = dv.id
            JOIN document_governance dg ON dg.document_id = d.id
            WHERE e.status = 'verified' AND {formal_filter}
            """
        ).fetchone()[0]
    )
    domains = [
        {
            "knowledge_domain": str(row["knowledge_domain"]),
            "document_count": int(row["document_count"]),
        }
        for row in connection.execute(
            f"""
            SELECT dg.knowledge_domain, COUNT(*) AS document_count
            FROM documents d
            JOIN document_governance dg ON dg.document_id = d.id
            WHERE {formal_filter}
            GROUP BY dg.knowledge_domain
            ORDER BY document_count DESC, dg.knowledge_domain
            """
        ).fetchall()
    ]
    topic_titles = [
        str(row["title"])
        for row in connection.execute(
            """
            SELECT title
            FROM wiki_pages
            WHERE status = 'verified'
              AND needs_revalidation = 0
              AND current_verified_revision_id IS NOT NULL
              AND source_document_id IS NULL
            ORDER BY title
            """
        ).fetchall()
    ]
    return {
        "all_document_count": int(
            connection.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
        ),
        "all_current_evidence_count": int(
            connection.execute(
                """
                SELECT COUNT(*)
                FROM evidence e
                JOIN processing_runs pr
                  ON pr.id = e.processing_run_id AND pr.is_current = 1
                """
            ).fetchone()[0]
        ),
        "formal_document_count": formal_document_count,
        "qualified_evidence_count": qualified_evidence_count,
        "published_topic_count": len(topic_titles),
        "domains": domains,
        "topic_titles": topic_titles,
    }


def _advisory_topic_profiles(
    sections: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for section in sections:
        page_id = str(section["page_id"])
        profile = grouped.setdefault(
            page_id,
            {
                "page_id": page_id,
                "page_title": str(section["page_title"]),
                "classification_values": [],
                "authority_values": [],
                "section_titles": [],
                "excerpts": [],
            },
        )
        profile["classification_values"].append(
            str(section["classification"])
        )
        profile["authority_values"].append(
            str(section["section_authority_status"])
        )
        section_title = str(section["section_title"]).strip()
        if section_title and section_title not in profile["section_titles"]:
            profile["section_titles"].append(section_title)
        excerpt = re.sub(r"\s+", " ", str(section["excerpt"])).strip()
        if (
            excerpt
            and excerpt not in profile["excerpts"]
            and len(profile["excerpts"]) < 2
        ):
            profile["excerpts"].append(
                excerpt[:MAX_ADVISORY_TOPIC_EXCERPT_CHARACTERS]
            )

    profiles: list[dict[str, Any]] = []
    for index, profile in enumerate(
        sorted(grouped.values(), key=lambda item: item["page_title"]),
        start=1,
    ):
        classification = _most_restrictive_classification(
            profile.pop("classification_values")
        )
        authority_values = profile.pop("authority_values")
        profiles.append(
            {
                **profile,
                "handle": f"W{index}",
                "classification": classification,
                "authority_status": (
                    "authoritative"
                    if authority_values
                    and all(
                        value == "authoritative"
                        for value in authority_values
                    )
                    else "reference"
                ),
            }
        )
    return profiles


def _reasoning_prompt(
    question: str,
    citations: Sequence[dict[str, Any]],
    topics: Sequence[dict[str, Any]],
    inventory: dict[str, Any],
) -> str:
    context = {
        "question": question,
        "assistant_scope": (
            "当前对话对象是研学平台。用户省略主语时，默认是在询问研学平台。"
        ),
        "workspace": {
            "formal_document_count": inventory["formal_document_count"],
            "qualified_evidence_count": inventory["qualified_evidence_count"],
            "published_topic_count": inventory["published_topic_count"],
            "document_categories": inventory["domains"],
        },
        "direct_evidence": [
            {
                "handle": f"S{index}",
                "excerpt": str(citation["excerpt"]),
            }
            for index, citation in enumerate(citations, start=1)
        ],
        "topics": [
            {
                "handle": topic["handle"],
                "title": topic["page_title"],
                "covered_sections": topic["section_titles"],
                "sample_excerpts": topic["excerpts"],
            }
            for topic in topics
        ],
    }
    return (
        "你是研学平台的知识问答智能体。不要依赖固定关键词判断问题类型；请根据"
        "用户真实意图和给定上下文，在一次回答中选择最合适的 response_mode。\n"
        "response_mode 只能是：fact（资料中已有明确事实）、advice（方案、增长、"
        "改进、比较、规划或开放式分析）、clarify（缺少关键对象，必须先追问）、"
        "out_of_scope（确实超出研学平台及现有知识范围）。不要仅因为没有命中"
        "直接证据就选择 out_of_scope；开放式问题应优先使用 advice。用户省略"
        "“研学平台”等主语时按 assistant_scope 理解，不要无谓追问。\n"
        "fact 只能陈述 direct_evidence 或 topics 明确支持的内容。advice 必须"
        "区分两类表述：资料明确显示的现状写成“现有资料显示”；分析意见写成"
        "“建议”或“可考虑”。主题没有提到某项能力时，只能说“现有资料未充分"
        "覆盖，可考虑补充”，不能断言平台一定没有该能力。不得虚构用户反馈、"
        "运营数据、完成度、故障、收益、时间或预算。\n"
        "如果输入没有任何 direct_evidence 和 topics，仍要完成意图判断：事实"
        "问题选择 clarify 或 out_of_scope；开放式问题可以给出 advice，但必须"
        "明确写“以下仅为一般建议，不代表现有资料结论”，且 source_handles"
        "返回空数组。\n"
        "fact 和 advice 使用多个简短段落，每个非空段落末尾必须带一个或多个"
        "来源标记：直接证据用 [S1]，知识主题用 [W1]。clarify 和 out_of_scope"
        "可以不带来源。不要使用数字编号，只使用输入中存在的 S/W 编号。\n"
        '只返回 JSON 对象：{"response_mode":"fact|advice|clarify|out_of_scope",'
        '"answer":"回答正文","source_handles":["S1","W1"]}。source_handles 必须'
        "与 answer 实际使用的全部标记完全一致；没有使用来源时返回空数组。\n"
        "输入："
        + json.dumps(context, ensure_ascii=False, separators=(",", ":"))
    )


def _validated_reasoning_answer(
    raw_output: str,
    *,
    question: str,
    citations: Sequence[dict[str, Any]],
    topics: Sequence[dict[str, Any]],
    inventory: dict[str, Any],
) -> dict[str, Any]:
    try:
        payload = json.loads(raw_output)
    except json.JSONDecodeError as exc:
        raise KnowledgeWorkbenchError("模型推理回答不是有效 JSON") from exc
    if not isinstance(payload, dict):
        raise KnowledgeWorkbenchError("模型推理回答必须是 JSON 对象")
    response_mode = payload.get("response_mode")
    answer = payload.get("answer")
    handles = payload.get("source_handles")
    allowed_modes = {"fact", "advice", "clarify", "out_of_scope"}
    if response_mode not in allowed_modes:
        raise KnowledgeWorkbenchError("模型推理回答包含未知 response_mode")
    if not isinstance(answer, str) or not answer.strip():
        raise KnowledgeWorkbenchError("模型推理回答缺少 answer")
    answer = answer.strip()
    if len(answer) > MAX_ADVISORY_ANSWER_CHARACTERS:
        raise KnowledgeWorkbenchError("模型推理回答过长")
    if (
        not isinstance(handles, list)
        or any(not isinstance(handle, str) for handle in handles)
    ):
        raise KnowledgeWorkbenchError("模型推理回答缺少 source_handles")
    has_sources = bool(citations or topics)
    if response_mode == "advice":
        if not any(marker in answer for marker in ("建议", "可考虑")):
            raise KnowledgeWorkbenchError("模型建议回答没有标明建议属性")
        if has_sources:
            if not any(
                marker in answer for marker in ("基于", "现有资料", "知识主题")
            ):
                raise KnowledgeWorkbenchError("模型建议回答没有说明分析范围")
        elif "以下仅为一般建议，不代表现有资料结论" not in answer:
            raise KnowledgeWorkbenchError("无来源建议没有明确说明一般建议属性")

    allowed_sources = {
        **{
            f"S{index}": {
                "kind": "evidence",
                "text": str(citation["excerpt"]),
            }
            for index, citation in enumerate(citations, start=1)
        },
        **{
            str(topic["handle"]): {
                "kind": "wiki",
                "text": " ".join(
                    [
                        str(topic["page_title"]),
                        *[str(item) for item in topic["section_titles"]],
                        *[str(item) for item in topic["excerpts"]],
                    ]
                ),
            }
            for topic in topics
        },
    }
    normalized_handles = [handle.strip() for handle in handles]
    if len(normalized_handles) != len(set(normalized_handles)):
        raise KnowledgeWorkbenchError("模型推理回答包含重复来源标记")
    if any(handle not in allowed_sources for handle in normalized_handles):
        raise KnowledgeWorkbenchError("模型推理回答引用了不存在的来源")

    answer_handles = {
        f"{kind}{number}"
        for kind, number in _MODEL_SOURCE_PATTERN.findall(answer)
    }
    marker_tokens = set(_MODEL_SOURCE_TOKEN_PATTERN.findall(answer))
    valid_marker_tokens = {f"[{handle}]" for handle in answer_handles}
    if marker_tokens != valid_marker_tokens:
        raise KnowledgeWorkbenchError("模型推理回答含有无效来源标记")
    if answer_handles != set(normalized_handles):
        raise KnowledgeWorkbenchError("回答正文与 source_handles 不一致")
    if response_mode == "fact" and not answer_handles:
        raise KnowledgeWorkbenchError("事实回答没有引用来源")
    if response_mode == "advice" and has_sources and not answer_handles:
        raise KnowledgeWorkbenchError("有知识上下文的建议回答没有引用来源")

    source_text = " ".join(
        [
            question,
            str(inventory["formal_document_count"]),
            str(inventory["qualified_evidence_count"]),
            str(inventory["published_topic_count"]),
            *[str(citation["excerpt"]) for citation in citations],
            *[
                source["text"]
                for handle, source in allowed_sources.items()
                if handle.startswith("W")
            ],
        ]
    )
    supported_numbers = set(_NUMBER_TOKEN_PATTERN.findall(source_text))
    paragraphs = [item.strip() for item in re.split(r"\n+", answer) if item.strip()]
    for paragraph in paragraphs:
        paragraph_handles = {
            f"{kind}{number}"
            for kind, number in _MODEL_SOURCE_PATTERN.findall(paragraph)
        }
        if (
            response_mode == "fact"
            or (response_mode == "advice" and has_sources)
        ) and not paragraph_handles:
            raise KnowledgeWorkbenchError("模型推理回答存在没有来源的段落")
        plain_text = _MODEL_SOURCE_PATTERN.sub("", paragraph)
        answer_numbers = set(_NUMBER_TOKEN_PATTERN.findall(plain_text))
        if not answer_numbers.issubset(supported_numbers):
            raise KnowledgeWorkbenchError("模型推理回答加入了来源中不存在的数值")
        if response_mode == "fact" and paragraph_handles:
            cited_texts = [
                allowed_sources[handle]["text"]
                for handle in sorted(paragraph_handles)
            ]
            assessment = assess_citation_texts(plain_text, cited_texts)
            if assessment["signal_conflict_count"] > 0:
                raise KnowledgeWorkbenchError("模型事实回答与来源关键信号不一致")
            if assessment["support_score"] < MINIMUM_CITATION_SUPPORT:
                raise KnowledgeWorkbenchError("模型事实回答与来源缺少文本关联")

    evidence_handles = sorted(
        (handle for handle in normalized_handles if handle.startswith("S")),
        key=lambda handle: int(handle[1:]),
    )
    wiki_handles = sorted(
        (handle for handle in normalized_handles if handle.startswith("W")),
        key=lambda handle: int(handle[1:]),
    )
    evidence_labels = {
        handle: f"【依据{index}】"
        for index, handle in enumerate(evidence_handles, start=1)
    }
    wiki_labels = {
        handle: f"【主题{index}】"
        for index, handle in enumerate(wiki_handles, start=1)
    }
    labels = {**evidence_labels, **wiki_labels}
    rendered = _MODEL_SOURCE_PATTERN.sub(
        lambda match: labels[f"{match.group(1)}{match.group(2)}"],
        answer,
    )
    return {
        "response_mode": response_mode,
        "answer": rendered,
        "evidence_handles": evidence_handles,
        "wiki_handles": wiki_handles,
    }


def _citations_for_handles(
    citations: Sequence[dict[str, Any]],
    handles: Sequence[str],
) -> list[dict[str, Any]]:
    return [
        dict(citations[int(handle[1:]) - 1])
        for handle in handles
    ]


def _reasoning_response_classification(
    response_mode: str,
) -> tuple[str, str]:
    mapping = {
        "fact": ("evidence", "knowledge"),
        "advice": ("advisory", "advisory"),
        "clarify": ("clarification", "clarification"),
        "out_of_scope": ("insufficient", "model_out_of_scope"),
    }
    try:
        return mapping[response_mode]
    except KeyError as exc:
        raise KnowledgeWorkbenchError("未知模型回答模式") from exc


def _reasoning_navigation(
    response_mode: str,
    *,
    topic_count: int,
    citation_count: int,
) -> list[dict[str, str]]:
    first_details = {
        "fact": "模型判断这是事实查询，回答必须由正式知识页或原始依据支撑。",
        "advice": "模型判断这是方案、增长或开放式分析问题，不要求存在单一原文答案。",
        "clarify": "模型判断缺少会改变答案的关键对象，先提出一个澄清问题。",
        "out_of_scope": "模型判断问题确实超出研学平台及当前知识范围。",
    }
    result = [
        {
            "title": "理解问题真实意图",
            "status": "found",
            "detail": first_details[response_mode],
        },
        {
            "title": "阅读受控知识上下文",
            "status": "found",
            "detail": (
                f"本次可参考 {topic_count} 个正式知识主题"
                f"和 {citation_count} 条直接原始依据；未发送数据库 ID 或文件路径。"
            ),
        },
    ]
    if response_mode in {"fact", "advice"}:
        result.append(
            {
                "title": "校验回答与来源",
                "status": "found",
                "detail": (
                    "本地检查了来源编号、逐段引用和关键数值；"
                    + (
                        "建议被明确标为分析意见。"
                        if response_mode == "advice"
                        else "事实回答还通过了文本支撑检查。"
                    )
                ),
            }
        )
    return result


def _reasoning_follow_ups(response_mode: str) -> list[str]:
    if response_mode == "advice":
        return [
            "把这些建议按近期、中期和后期排序。",
            "哪些建议有现有资料依据，哪些需要用户调研？",
            "把最优先的建议拆成可执行任务。",
        ]
    if response_mode == "fact":
        return [
            "请把回答所依据的资料和位置逐条展开。",
            "这些规则分别适用于哪些对象？",
        ]
    if response_mode == "out_of_scope":
        return [
            "当前知识库最适合回答哪些问题？",
            "我需要补充什么资料才能回答这个问题？",
        ]
    return []


def _advisory_wiki_matches(
    topics: Sequence[dict[str, Any]],
    used_handles: Sequence[str],
) -> list[dict[str, Any]]:
    used = set(used_handles)
    matches = []
    for topic in topics:
        if topic["handle"] not in used:
            continue
        coverage = "、".join(topic["section_titles"][:8])
        excerpt = f"本次回答参考了该正式知识主题当前覆盖的内容：{coverage}。"
        matches.append(
            {
                "page_id": topic["page_id"],
                "page_title": topic["page_title"],
                "revision_id": None,
                "section_title": "本次推理使用的主题概览",
                "excerpt": excerpt,
                "excerpt_truncated": False,
                "document_name": "已发布企业知识页",
                "classification": topic["classification"],
                "authority_status": topic["authority_status"],
                "evidence_ids": [],
                "score": 1.0,
                "matched_terms": [],
            }
        )
    return matches


def _citation_classification_for_model(
    citations: Sequence[dict[str, Any]],
) -> Classification:
    if not citations:
        return Classification.INTERNAL
    classification = Classification.PUBLIC
    for citation in citations:
        try:
            current = Classification(str(citation["classification"]))
        except (KeyError, ValueError) as exc:
            raise KnowledgeWorkbenchError("引用依据包含未知密级") from exc
        classification = most_restrictive(classification, current)
    return classification


def _query_knowledge_domains(question: str) -> tuple[str, ...]:
    """Select source profiles without silently widening ordinary business QA."""

    normalized = unicodedata.normalize("NFKC", question).lower()
    selected = set(_DEFAULT_KNOWLEDGE_DOMAINS)
    technical_context = (
        any(marker in normalized for marker in _TECHNICAL_QUERY_MARKERS)
        or bool(_TECHNICAL_ASCII_PATTERN.search(normalized))
        or (
            "架构" in normalized
            and not any(
                marker in normalized
                for marker in ("组织架构", "治理架构", "权责架构")
            )
        )
    )
    if technical_context:
        selected.add("technical")
    if any(marker in normalized for marker in _TEMPLATE_QUERY_MARKERS):
        selected.add("template")
    if any(marker in normalized for marker in _EXAMPLE_QUERY_MARKERS):
        selected.add("example")
    if any(marker in normalized for marker in _PROCESS_QUERY_MARKERS):
        selected.add("process")
    return tuple(
        domain for domain in _KNOWLEDGE_DOMAIN_ORDER if domain in selected
    )


def _domain_placeholders(knowledge_domains: Sequence[str]) -> str:
    if not knowledge_domains:
        raise ValueError("knowledge_domains 不能为空")
    unknown = set(knowledge_domains) - set(_KNOWLEDGE_DOMAIN_ORDER)
    if unknown:
        raise ValueError("未知资料类型：" + ", ".join(sorted(unknown)))
    return ",".join("?" for _ in knowledge_domains)


def _eligible_document_count(
    connection,
    knowledge_domains: Sequence[str],
) -> int:
    placeholders = _domain_placeholders(knowledge_domains)
    return int(
        connection.execute(
            f"""
            SELECT COUNT(*)
            FROM documents d
            JOIN document_governance dg ON dg.document_id = d.id
            WHERE dg.purpose = 'production'
              AND dg.scope_status = 'in_scope'
              AND dg.authority_status != 'superseded'
              AND d.classification != 'restricted'
              AND dg.knowledge_domain IN ({placeholders})
            """,
            tuple(knowledge_domains),
        ).fetchone()[0]
    )


def _load_wiki_sections(
    connection,
    paths: WorkspacePaths,
    knowledge_domains: Sequence[str],
) -> list[dict[str, Any]]:
    selected_domains = set(knowledge_domains)
    _domain_placeholders(knowledge_domains)
    rows = connection.execute(
        """
        SELECT wp.id AS page_id, wp.title AS page_title,
               wr.id AS revision_id, wr.markdown_path, wr.content_sha256,
               wp.source_document_id, wr.processing_run_id,
               re.evidence_id, e.status AS evidence_status,
               etv.status AS technical_status,
               e.processing_run_id AS evidence_run_id,
               pr.is_current AS run_is_current,
               pr.status AS run_status,
               pr.document_version_id AS run_document_version_id,
               dv.id AS document_version_id,
               d.id AS document_id, d.original_name AS document_name,
               d.classification, d.current_version_id,
               dg.purpose, dg.scope_status, dg.authority_status,
               dg.knowledge_domain
        FROM wiki_pages wp
        JOIN wiki_revisions wr ON wr.id = wp.current_verified_revision_id
        LEFT JOIN revision_evidence re ON re.revision_id = wr.id
        LEFT JOIN evidence e ON e.id = re.evidence_id
        LEFT JOIN evidence_technical_validation etv
          ON etv.evidence_id = e.id
        LEFT JOIN processing_runs pr ON pr.id = e.processing_run_id
        LEFT JOIN document_versions dv ON dv.id = e.document_version_id
        LEFT JOIN documents d ON d.id = dv.document_id
        LEFT JOIN document_governance dg ON dg.document_id = d.id
        WHERE wp.status = 'verified'
          AND wp.needs_revalidation = 0
          AND wr.status = 'verified'
        ORDER BY wp.title, wp.id, re.evidence_id
        """
    ).fetchall()
    revisions: dict[str, dict[str, Any]] = {}
    for row in rows:
        revision_id = str(row["revision_id"])
        revision = revisions.setdefault(
            revision_id,
            {
                "page_id": row["page_id"],
                "page_title": row["page_title"],
                "revision_id": row["revision_id"],
                "markdown_path": row["markdown_path"],
                "content_sha256": row["content_sha256"],
                "source_document_id": row["source_document_id"],
                "processing_run_id": row["processing_run_id"],
                "sources": [],
            },
        )
        revision["sources"].append(dict(row))

    sections: list[dict[str, Any]] = []
    for revision in revisions.values():
        source_rows = revision["sources"]
        if not source_rows or any(
            not _wiki_source_is_eligible(
                row,
                source_document_id=revision["source_document_id"],
                revision_run_id=revision["processing_run_id"],
            )
            for row in source_rows
        ):
            continue
        if revision["source_document_id"] is None:
            if revision["processing_run_id"] is not None:
                continue
        elif revision["processing_run_id"] is None:
            continue

        evidence_ids = tuple(str(row["evidence_id"]) for row in source_rows)
        all_documents = _wiki_source_documents(source_rows)
        if not all_documents:
            continue
        if (
            revision["source_document_id"] is None
            and len(all_documents) < 2
        ):
            continue
        profile_rows = [
            row
            for row in source_rows
            if row["knowledge_domain"] in selected_domains
        ]
        documents = _wiki_source_documents(profile_rows)
        if not documents:
            continue
        classification = _most_restrictive_classification(
            [str(item["classification"]) for item in documents]
        )
        authority_status = (
            "authoritative"
            if all(
                item["authority_status"] == "authoritative"
                for item in documents
            )
            else "reference"
        )
        document_name = _wiki_source_label(
            documents,
            aggregate=revision["source_document_id"] is None,
        )
        content = _read_current_wiki(
            paths,
            str(revision["markdown_path"]),
            str(revision["content_sha256"]),
        )
        if content is None:
            continue
        source_by_evidence = {
            str(row["evidence_id"]): row for row in source_rows
        }
        for section in _split_wiki_sections(
            content,
            allowed_evidence_ids=evidence_ids,
        ):
            if not section["evidence_ids"]:
                continue
            section_sources = [
                source_by_evidence[evidence_id]
                for evidence_id in section["evidence_ids"]
            ]
            if any(
                row["knowledge_domain"] not in selected_domains
                for row in section_sources
            ):
                continue
            section_documents = _wiki_source_documents(section_sources)
            section_authority_status = (
                "authoritative"
                if all(
                    item["authority_status"] == "authoritative"
                    for item in section_documents
                )
                else "reference"
            )
            sections.append(
                {
                    "page_id": revision["page_id"],
                    "page_title": revision["page_title"],
                    "revision_id": revision["revision_id"],
                    "section_title": section["title"],
                    "excerpt": section["text"],
                    "document_name": document_name,
                    "section_document_name": _wiki_source_label(
                        section_documents,
                        aggregate=len(section_documents) > 1,
                    ),
                    "classification": classification,
                    "authority_status": authority_status,
                    "section_authority_status": section_authority_status,
                    "knowledge_domains": tuple(
                        domain
                        for domain in _KNOWLEDGE_DOMAIN_ORDER
                        if domain
                        in {
                            str(item["knowledge_domain"])
                            for item in documents
                        }
                    ),
                    "evidence_ids": section["evidence_ids"],
                }
            )
    return sections


def _wiki_source_is_eligible(
    row: dict[str, Any],
    *,
    source_document_id: object,
    revision_run_id: object,
) -> bool:
    required = (
        row.get("evidence_id"),
        row.get("document_id"),
        row.get("document_version_id"),
        row.get("evidence_run_id"),
        row.get("knowledge_domain"),
    )
    if any(value is None for value in required):
        return False
    if row["evidence_status"] != "verified":
        return False
    if (
        row["technical_status"] != "passed"
        or row["run_is_current"] != 1
        or row["run_status"] != "completed"
    ):
        return False
    if row["run_document_version_id"] != row["document_version_id"]:
        return False
    if row["current_version_id"] != row["document_version_id"]:
        return False
    if row["classification"] == "restricted":
        return False
    if row["purpose"] != "production" or row["scope_status"] != "in_scope":
        return False
    if row["authority_status"] not in {"reference", "authoritative"}:
        return False
    if source_document_id is not None:
        return (
            row["document_id"] == source_document_id
            and row["evidence_run_id"] == revision_run_id
        )
    return True


def _wiki_source_documents(
    source_rows: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {}
    for row in source_rows:
        document_id = str(row["document_id"])
        by_id.setdefault(
            document_id,
            {
                "document_id": document_id,
                "document_name": str(row["document_name"]),
                "classification": str(row["classification"]),
                "authority_status": str(row["authority_status"]),
                "knowledge_domain": str(row["knowledge_domain"]),
            },
        )
    return [by_id[key] for key in sorted(by_id)]


def _wiki_source_label(
    documents: Sequence[dict[str, Any]],
    *,
    aggregate: bool,
) -> str:
    document_count = len(
        {str(item["document_id"]) for item in documents}
    )
    names = sorted(
        {str(item["document_name"]).strip() for item in documents}
    )
    if not aggregate and document_count == 1:
        return names[0]
    preview = "、".join(names[:3])
    if len(names) > 3:
        preview += "等"
    return f"汇总自 {document_count} 份资料（{preview}）"


def _most_restrictive_classification(values: Sequence[str]) -> str:
    order = {
        "public": 0,
        "internal": 1,
        "confidential": 2,
        "restricted": 3,
    }
    return max(values, key=lambda value: order.get(value, 99))


def _read_current_wiki(
    paths: WorkspacePaths,
    markdown_path: str,
    expected_sha256: str,
) -> str | None:
    root = paths.root.resolve()
    candidate = (root / markdown_path).resolve()
    if candidate != root and root not in candidate.parents:
        return None
    try:
        content = candidate.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None
    if sha256_text(content) != expected_sha256:
        return None
    return content


def _split_wiki_sections(
    content: str,
    *,
    allowed_evidence_ids: Sequence[str] = (),
) -> list[dict[str, Any]]:
    lines = content.splitlines()
    if lines and lines[0].strip() == "---":
        for index in range(1, len(lines)):
            if lines[index].strip() == "---":
                lines = lines[index + 1 :]
                break
    allowed = set(allowed_evidence_ids)
    page_title = "知识页摘要"
    current_title = page_title
    current_lines: list[str] = []
    current_evidence_ids: list[str] = []
    sections: list[dict[str, Any]] = []

    def flush() -> None:
        text = _plain_wiki_text(current_lines)
        if text:
            referenced = tuple(dict.fromkeys(current_evidence_ids))
            references_valid = not allowed or all(
                evidence_id in allowed for evidence_id in referenced
            )
            selected = referenced if references_valid else ()
            sections.append(
                {
                    "title": current_title,
                    "text": text,
                    "evidence_ids": selected,
                }
            )

    for raw_line in lines:
        heading = re.match(r"^\s{0,3}(#{1,4})\s+(.+?)\s*$", raw_line)
        if heading:
            flush()
            title = _plain_wiki_inline(heading.group(2))
            if len(heading.group(1)) == 1:
                page_title = title or page_title
                current_title = "页面摘要"
            else:
                current_title = title or page_title
            current_lines = []
            current_evidence_ids = []
            heading_evidence = _evidence_ids_from_value(title)
            current_evidence_ids.extend(heading_evidence)
            continue
        marker_ids = _wiki_marker_evidence_ids(raw_line)
        if marker_ids:
            current_evidence_ids.extend(marker_ids)
            raw_line = _WIKI_EVIDENCE_MARKER_PATTERN.sub("", raw_line)
        current_lines.append(raw_line)
    flush()
    if not sections:
        text = _plain_wiki_text(lines)
        if text:
            sections.append(
                {
                    "title": "页面摘要",
                    "text": text,
                    "evidence_ids": (),
                }
            )
    return sections


def _wiki_marker_evidence_ids(value: str) -> tuple[str, ...]:
    evidence_ids: list[str] = []
    for match in _WIKI_EVIDENCE_MARKER_PATTERN.finditer(value):
        raw = match.group(1).strip()
        if raw.startswith("["):
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, list):
                evidence_ids.extend(
                    str(item)
                    for item in parsed
                    if isinstance(item, str)
                    and _EVIDENCE_ID_PATTERN.fullmatch(item)
                )
                continue
        evidence_ids.extend(_evidence_ids_from_value(raw))
    return tuple(dict.fromkeys(evidence_ids))


def _evidence_ids_from_value(value: str) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            match.group(0) for match in _EVIDENCE_ID_PATTERN.finditer(value)
        )
    )


def _plain_wiki_text(lines: Sequence[str]) -> str:
    cleaned: list[str] = []
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line == "---":
            continue
        line = _WIKI_EVIDENCE_MARKER_PATTERN.sub("", line).strip()
        if not line:
            continue
        line = re.sub(r"^\s*(?:[-*+]|\d+\.)\s+", "", line)
        line = re.sub(r"^\s*>\s?", "", line)
        line = _plain_wiki_inline(line)
        if not line:
            continue
        if line.startswith(
            (
                "SHA-256：",
                "文件版本：",
                "定位：",
                "定位（",
                "资料：",
                "位置：",
                "资料用途：",
            )
        ):
            continue
        if re.fullmatch(r"ev_[a-z0-9_]+", line):
            continue
        cleaned.append(line)
    return " ".join(cleaned)[:4000]


def _plain_wiki_inline(value: str) -> str:
    value = re.sub(r"!\[[^\]]*]\([^)]*\)", "", value)
    value = re.sub(r"\[([^\]]+)]\([^)]*\)", r"\1", value)
    value = re.sub(
        r"\[\[([^\]|]+)(?:\|([^\]]+))?]]",
        lambda match: match.group(2) or match.group(1),
        value,
    )
    value = value.replace("`", "").replace("**", "").replace("__", "")
    value = re.sub(r"\s+", " ", value).strip()
    return value


def _rank_wiki_sections(
    sections: list[dict[str, Any]],
    question: str,
) -> list[dict[str, Any]]:
    terms = _question_terms(question)
    if not terms:
        return []
    normalized_question = _normalize_for_match(question)
    topic_priorities, strict_topic_route = _topic_page_priorities(question)
    ranked: list[dict[str, Any]] = []
    for section in sections:
        page_title_value = str(section["page_title"])
        if strict_topic_route and page_title_value not in topic_priorities:
            continue
        excerpt = _normalize_for_match(str(section["excerpt"]))
        page_title = _normalize_for_match(page_title_value)
        section_title = _normalize_for_match(
            f"{section['section_title']} {section['section_document_name']}"
        )
        body_matches = [term for term in terms if term in excerpt]
        page_matches = [term for term in terms if term in page_title]
        section_matches = [term for term in terms if term in section_title]
        if (
            not body_matches
            and not page_matches
            and not section_matches
            and page_title_value not in topic_priorities
        ):
            continue
        coverage = len(set(body_matches)) / max(len(terms), 1)
        page_coverage = len(set(page_matches)) / max(len(terms), 1)
        section_coverage = len(set(section_matches)) / max(len(terms), 1)
        exact_bonus = (
            0.3
            if len(normalized_question) >= 3
            and normalized_question in excerpt
            else 0.0
        )
        topic_bonus = topic_priorities.get(page_title_value, 0.0)
        authority_bonus = (
            0.08
            if section["section_authority_status"] == "authoritative"
            else 0.0
        )
        score = min(
            1.0,
            coverage * 0.72
            + section_coverage * 0.34
            + page_coverage * 0.08
            + exact_bonus
            + topic_bonus
            + authority_bonus,
        )
        if score < 0.16:
            continue
        ranked.append(
            {
                **section,
                "score": round(score, 6),
                "matched_terms": sorted(
                    set(body_matches + section_matches + page_matches),
                    key=lambda item: (-len(item), item),
                )[:12],
            }
        )
    ranked.sort(
        key=lambda item: (
            -float(item["score"]),
            str(item["page_title"]),
            str(item["section_title"]),
        )
    )
    return ranked


def _topic_page_priorities(
    question: str,
) -> tuple[dict[str, float], bool]:
    normalized_question = _normalize_for_match(question)
    raw_scores: dict[str, float] = {}
    keyword_counts: dict[str, int] = {}
    for topic in TOPICS:
        matched_keywords = {
            normalized
            for keyword in topic.keywords
            if (normalized := _normalize_for_match(keyword))
            and normalized in normalized_question
        }
        if not matched_keywords:
            continue
        title_parts = {
            normalized
            for part in re.split(r"[、，,与和及/\s]+", topic.title)
            if (normalized := _normalize_for_match(part))
            and normalized in normalized_question
        }
        raw_scores[topic.title] = sum(
            min(len(keyword), 4) for keyword in matched_keywords
        ) + sum(min(len(part), 4) for part in title_parts)
        keyword_counts[topic.title] = len(matched_keywords)
    if not raw_scores:
        return {}, False
    maximum = max(raw_scores.values())
    minimum = max(2.0, maximum * 0.8)
    priorities = {
        title: round(0.48 * score / maximum, 6)
        for title, score in raw_scores.items()
        if score >= minimum
    }
    strict = any(
        keyword_counts.get(title, 0) >= 2 for title in priorities
    )
    return priorities, strict


def _select_wiki_matches(
    ranked: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not ranked:
        return []
    minimum = max(0.16, float(ranked[0]["score"]) * 0.55)
    selected: list[dict[str, Any]] = []
    seen_sections: set[tuple[str, str]] = set()
    for item in ranked:
        if float(item["score"]) < minimum:
            break
        identity = (str(item["page_id"]), str(item["section_title"]))
        if identity in seen_sections:
            continue
        seen_sections.add(identity)
        excerpt = str(item["excerpt"])
        selected.append(
            {
                "page_id": item["page_id"],
                "page_title": item["page_title"],
                "revision_id": item["revision_id"],
                "section_title": item["section_title"],
                "excerpt": excerpt[:MAX_WIKI_EXCERPT_CHARACTERS],
                "excerpt_truncated": len(excerpt) > MAX_WIKI_EXCERPT_CHARACTERS,
                "document_name": item["document_name"],
                "classification": item["classification"],
                "authority_status": item["authority_status"],
                "evidence_ids": list(item["evidence_ids"]),
                "score": item["score"],
                "matched_terms": item["matched_terms"],
            }
        )
        if len(selected) >= MAX_WIKI_MATCHES:
            break
    return selected


def _entity_navigation_context(
    connection,
    question: str,
    knowledge_domains: Sequence[str],
) -> dict[str, Any]:
    normalized_question = _normalize_for_match(question)
    placeholders = _domain_placeholders(knowledge_domains)
    alias_rows = connection.execute(
        f"""
        SELECT DISTINCT ce.id AS entity_id, ce.canonical_name, ce.entity_type,
                        ea.alias, ea.normalized_alias
        FROM canonical_entities ce
        JOIN entity_aliases ea ON ea.entity_id = ce.id
        JOIN evidence_entity_mentions eem ON eem.entity_id = ce.id
        JOIN evidence e ON e.id = eem.evidence_id AND e.status = 'verified'
        JOIN evidence_technical_validation etv
          ON etv.evidence_id = e.id AND etv.status = 'passed'
        JOIN processing_runs pr
          ON pr.id = e.processing_run_id AND pr.is_current = 1
        JOIN document_versions dv ON dv.id = e.document_version_id
        JOIN documents d
          ON d.id = dv.document_id AND d.current_version_id = dv.id
        JOIN document_governance dg ON dg.document_id = d.id
        WHERE ce.status = 'active'
          AND d.classification != 'restricted'
          AND dg.purpose = 'production'
          AND dg.scope_status = 'in_scope'
          AND dg.authority_status != 'superseded'
          AND dg.knowledge_domain IN ({placeholders})
        ORDER BY length(ea.normalized_alias) DESC, ce.id
        """,
        tuple(knowledge_domains),
    ).fetchall()
    matched_by_id: dict[str, dict[str, Any]] = {}
    for row in alias_rows:
        alias = _normalize_for_match(str(row["alias"]))
        if len(alias) < 2 or alias not in normalized_question:
            continue
        matched_by_id.setdefault(
            str(row["entity_id"]),
            {
                "entity_id": row["entity_id"],
                "name": row["canonical_name"],
                "entity_type": row["entity_type"],
                "matched_alias": row["alias"],
            },
        )
    matched = list(matched_by_id.values())[:8]
    if not matched:
        return {"matched_entities": [], "related_entities": [], "relationships": []}

    ids = [str(item["entity_id"]) for item in matched]
    placeholders = ",".join("?" for _ in ids)
    domain_placeholders = _domain_placeholders(knowledge_domains)
    relation_rows = connection.execute(
        f"""
        SELECT DISTINCT er.id AS relationship_id, er.source_entity_id,
                        er.target_entity_id, ert.label, ert.inverse_label,
                        ert.directed, source.canonical_name AS source_name,
                        target.canonical_name AS target_name
        FROM entity_relationships er
        JOIN entity_relation_types ert
          ON ert.relation_key = er.relation_key AND ert.status = 'active'
        JOIN canonical_entities source ON source.id = er.source_entity_id
        JOIN canonical_entities target ON target.id = er.target_entity_id
        JOIN entity_relationship_evidence ere ON ere.relationship_id = er.id
        JOIN evidence e ON e.id = ere.evidence_id AND e.status = 'verified'
        JOIN evidence_technical_validation etv
          ON etv.evidence_id = e.id AND etv.status = 'passed'
        JOIN processing_runs pr
          ON pr.id = e.processing_run_id AND pr.is_current = 1
        JOIN document_versions dv ON dv.id = e.document_version_id
        JOIN documents d
          ON d.id = dv.document_id AND d.current_version_id = dv.id
        JOIN document_governance dg ON dg.document_id = d.id
        WHERE er.status = 'active'
          AND source.status = 'active'
          AND target.status = 'active'
          AND d.classification != 'restricted'
          AND dg.purpose = 'production'
          AND dg.scope_status = 'in_scope'
          AND dg.authority_status != 'superseded'
          AND dg.knowledge_domain IN ({domain_placeholders})
          AND (
              er.source_entity_id IN ({placeholders})
              OR er.target_entity_id IN ({placeholders})
          )
        ORDER BY er.id
        LIMIT 30
        """,
        tuple(knowledge_domains) + tuple(ids) + tuple(ids),
    ).fetchall()
    relationships: list[dict[str, Any]] = []
    related_by_id: dict[str, dict[str, Any]] = {}
    matched_ids = set(ids)
    for row in relation_rows:
        source_id = str(row["source_entity_id"])
        target_id = str(row["target_entity_id"])
        relationships.append(
            {
                "relationship_id": row["relationship_id"],
                "source_entity_id": source_id,
                "source_name": row["source_name"],
                "label": row["label"],
                "target_entity_id": target_id,
                "target_name": row["target_name"],
                "directed": bool(row["directed"]),
            }
        )
        if source_id not in matched_ids:
            related_by_id[source_id] = {
                "entity_id": source_id,
                "name": row["source_name"],
            }
        if target_id not in matched_ids:
            related_by_id[target_id] = {
                "entity_id": target_id,
                "name": row["target_name"],
            }
    return {
        "matched_entities": matched,
        "related_entities": list(related_by_id.values())[:12],
        "relationships": relationships,
    }


def _navigation_steps(
    *,
    wiki_matches: list[dict[str, Any]],
    entity_context: dict[str, Any],
    citations: list[dict[str, Any]],
    eligible_document_count: int,
) -> list[dict[str, str]]:
    if wiki_matches:
        pages = len({item["page_id"] for item in wiki_matches})
        wiki_detail = f"找到 {pages} 个相关知识页，优先阅读最匹配的章节。"
        wiki_status = "found"
    elif eligible_document_count:
        wiki_detail = "没有找到能直接回答问题的正式知识页。"
        wiki_status = "not_found"
    else:
        wiki_detail = "尚无完成业务范围确认的正式知识页。"
        wiki_status = "blocked"

    matched = entity_context["matched_entities"]
    related = entity_context["related_entities"]
    if matched:
        entity_detail = f"识别出 {len(matched)} 个业务对象"
        if related:
            entity_detail += f"，并沿已确认关系找到 {len(related)} 个相关对象"
        entity_detail += "。"
        entity_status = "found"
    else:
        entity_detail = "问题中没有命中已经人工确认的业务对象，未做关系扩展。"
        entity_status = "not_found"

    if citations:
        evidence_detail = (
            f"回到 {len(citations)} 条原始依据核对原文和文档内位置。"
        )
        evidence_status = "found"
    else:
        evidence_detail = "没有找到足够相关且可用于回答的原始依据。"
        evidence_status = "not_found"
    return [
        {"title": "先查企业知识页", "status": wiki_status, "detail": wiki_detail},
        {
            "title": "理解业务对象与关系",
            "status": entity_status,
            "detail": entity_detail,
        },
        {
            "title": "回到原始依据核对",
            "status": evidence_status,
            "detail": evidence_detail,
        },
    ]


def _follow_up_suggestions(
    *,
    wiki_matches: list[dict[str, Any]],
    entity_context: dict[str, Any],
    citations: list[dict[str, Any]],
) -> list[str]:
    suggestions: list[str] = []
    if wiki_matches:
        suggestions.append(f"《{wiki_matches[0]['page_title']}》还包括哪些内容？")
    if entity_context["related_entities"]:
        suggestions.append(
            f"{entity_context['related_entities'][0]['name']}与这个问题有什么关系？"
        )
    if citations:
        suggestions.append("这些依据分别来自哪些文件和位置？")
    if not suggestions:
        suggestions.extend(
            [
                "可以按项目、制度名称或业务对象换一种问法。",
                "也可以先到资料地图确认相关文件是否已纳入范围。",
            ]
        )
    return suggestions[:3]


def _answer_excerpt(value: str) -> str:
    normalized = re.sub(r"\s+", " ", value).strip()
    if len(normalized) <= 260:
        return normalized
    stop = max(
        normalized.rfind("。", 0, 260),
        normalized.rfind("；", 0, 260),
    )
    if stop >= 80:
        return normalized[: stop + 1]
    return normalized[:260].rstrip() + "…"


def _load_candidates(
    connection,
    knowledge_domains: Sequence[str],
) -> list[dict[str, Any]]:
    placeholders = _domain_placeholders(knowledge_domains)
    rows = connection.execute(
        f"""
        SELECT e.id AS evidence_id, e.run_ordinal AS ordinal, e.excerpt,
               e.locator_json, d.original_name AS document_name,
               d.classification, dg.authority_status,
               COALESCE((
                   SELECT GROUP_CONCAT(DISTINCT ea.alias)
                   FROM evidence_entity_mentions eem
                   JOIN entity_aliases ea ON ea.id = eem.alias_id
                   JOIN canonical_entities ce ON ce.id = eem.entity_id
                   WHERE eem.evidence_id = e.id AND ce.status = 'active'
               ), '') AS aliases,
               COALESCE((
                   SELECT COUNT(*) FROM evidence_locations el
                   WHERE el.evidence_id = e.id
               ), 0) AS location_count
        FROM evidence e
        JOIN evidence_technical_validation etv
          ON etv.evidence_id = e.id AND etv.status = 'passed'
        JOIN processing_runs pr
          ON pr.id = e.processing_run_id AND pr.is_current = 1
        JOIN document_versions dv ON dv.id = e.document_version_id
        JOIN documents d
          ON d.id = dv.document_id AND d.current_version_id = dv.id
        JOIN document_governance dg ON dg.document_id = d.id
        WHERE e.status = 'verified'
          AND d.classification != 'restricted'
          AND dg.purpose = 'production'
          AND dg.scope_status = 'in_scope'
          AND dg.authority_status != 'superseded'
          AND dg.knowledge_domain IN ({placeholders})
        ORDER BY e.id
        """,
        tuple(knowledge_domains),
    ).fetchall()
    return [
        {
            "evidence_id": row["evidence_id"],
            "ordinal": row["ordinal"],
            "excerpt": row["excerpt"],
            "locator": _safe_locator(row["locator_json"]),
            "document_name": row["document_name"],
            "classification": row["classification"],
            "authority_status": row["authority_status"],
            "knowledge_scope": "正式业务资料",
            "aliases": tuple(
                item for item in str(row["aliases"]).split(",") if item
            ),
            "location_count": max(int(row["location_count"]), 1),
        }
        for row in rows
    ]


def _rank_candidates(
    candidates: list[dict[str, Any]],
    question: str,
) -> list[dict[str, Any]]:
    terms = _question_terms(question)
    if not terms:
        return []
    question_normalized = _normalize_for_match(question)
    values = tuple(_VALUE_PATTERN.findall(question))

    searchable = [
        _normalize_for_match(
            f"{item['excerpt']} {item['document_name']} {' '.join(item['aliases'])}"
        )
        for item in candidates
    ]
    document_frequency = {
        term: sum(1 for text in searchable if term in text) for term in terms
    }
    total_candidates = max(len(candidates), 1)
    weights = {
        term: (1.0 + math.log((total_candidates + 1) / (frequency + 1)))
        * min(len(term), 4)
        for term, frequency in document_frequency.items()
    }
    total_weight = sum(weights.values()) or 1.0

    ranked: list[dict[str, Any]] = []
    for item in candidates:
        excerpt_normalized = _normalize_for_match(str(item["excerpt"]))
        document_normalized = _normalize_for_match(str(item["document_name"]))
        alias_normalized = tuple(
            _normalize_for_match(alias) for alias in item["aliases"]
        )
        excerpt_matches = [
            term for term in terms if term and term in excerpt_normalized
        ]
        document_matches = [
            term for term in terms if term and term in document_normalized
        ]
        alias_matches = [
            alias
            for alias in alias_normalized
            if len(alias) >= 2 and alias in question_normalized
        ]
        evidence_coverage = (
            sum(weights[term] for term in excerpt_matches) / total_weight
        )
        document_coverage = (
            sum(weights[term] for term in document_matches) / total_weight
        )
        value_coverage = (
            sum(1 for value in values if value in str(item["excerpt"]))
            / len(values)
            if values
            else 0.0
        )
        exact_bonus = (
            0.35
            if len(question_normalized) >= 3
            and question_normalized in excerpt_normalized
            else 0.0
        )
        alias_bonus = 0.18 if alias_matches else 0.0
        score = min(
            1.0,
            evidence_coverage * 0.76
            + document_coverage * 0.14
            + value_coverage * 0.12
            + exact_bonus
            + alias_bonus,
        )
        if not excerpt_matches and not alias_matches:
            score *= 0.35
        if score < MIN_RELEVANCE_SCORE:
            continue
        ranked.append(
            {
                **item,
                "score": round(score, 6),
                "matched_terms": sorted(
                    set(excerpt_matches + document_matches),
                    key=lambda value: (-len(value), value),
                )[:12],
            }
        )
    ranked.sort(
        key=lambda item: (
            -float(item["score"]),
            str(item["document_name"]),
            int(item["ordinal"]),
            str(item["evidence_id"]),
        )
    )
    return ranked


def _promote_wiki_evidence(
    ranked: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
    wiki_matches: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not wiki_matches:
        return ranked
    promotion_scores: dict[str, float] = {}
    promotion_terms: dict[str, set[str]] = {}
    for position, match in enumerate(wiki_matches):
        score = max(0.42, 0.72 - position * 0.1)
        terms = {str(item) for item in match.get("matched_terms", ())}
        for evidence_id in match.get("evidence_ids", ()):
            key = str(evidence_id)
            promotion_scores[key] = max(promotion_scores.get(key, 0.0), score)
            promotion_terms.setdefault(key, set()).update(terms)

    ranked_by_id = {
        str(item["evidence_id"]): dict(item) for item in ranked
    }
    for candidate in candidates:
        evidence_id = str(candidate["evidence_id"])
        promotion = promotion_scores.get(evidence_id)
        if promotion is None:
            continue
        current = ranked_by_id.get(evidence_id)
        if current is None:
            ranked_by_id[evidence_id] = {
                **candidate,
                "score": round(promotion, 6),
                "matched_terms": sorted(
                    promotion_terms.get(evidence_id, ()),
                    key=lambda value: (-len(value), value),
                )[:12],
            }
            continue
        current["score"] = round(
            min(1.0, max(float(current["score"]) + 0.24, promotion)),
            6,
        )
        current["matched_terms"] = sorted(
            set(current.get("matched_terms", ()))
            | promotion_terms.get(evidence_id, set()),
            key=lambda value: (-len(value), value),
        )[:12]

    promoted = list(ranked_by_id.values())
    promoted.sort(
        key=lambda item: (
            -float(item["score"]),
            str(item["document_name"]),
            int(item["ordinal"]),
            str(item["evidence_id"]),
        )
    )
    return promoted


def _select_citations(
    ranked: list[dict[str, Any]],
    *,
    limit: int,
) -> list[dict[str, Any]]:
    if not ranked:
        return []
    minimum = max(MIN_RELEVANCE_SCORE, float(ranked[0]["score"]) * 0.58)
    document_counts: Counter[str] = Counter()
    selected: list[dict[str, Any]] = []
    for item in ranked:
        if float(item["score"]) < minimum:
            break
        document_name = str(item["document_name"])
        if document_counts[document_name] >= 3:
            continue
        excerpt = str(item["excerpt"])
        selected.append(
            {
                "evidence_id": item["evidence_id"],
                "document_name": document_name,
                "classification": item["classification"],
                "authority_status": item["authority_status"],
                "knowledge_scope": item["knowledge_scope"],
                "ordinal": item["ordinal"],
                "locator": item["locator"],
                "location_count": item["location_count"],
                "excerpt": excerpt[:MAX_EXCERPT_CHARACTERS],
                "excerpt_truncated": len(excerpt) > MAX_EXCERPT_CHARACTERS,
                "score": item["score"],
                "matched_terms": item["matched_terms"],
            }
        )
        document_counts[document_name] += 1
        if len(selected) >= limit:
            break
    return selected


def _matching_conflicts(
    connection,
    evidence_ids: set[str],
) -> list[dict[str, Any]]:
    if not evidence_ids:
        return []
    placeholders = ",".join("?" for _ in evidence_ids)
    rows = connection.execute(
        f"""
        SELECT c.id AS conflict_id, c.conflict_type, c.reason, c.status,
               older.id AS older_evidence_id,
               older.excerpt AS older_excerpt,
               older.locator_json AS older_locator_json,
               newer.id AS newer_evidence_id,
               newer.excerpt AS newer_excerpt,
               newer.locator_json AS newer_locator_json,
               d.original_name AS document_name,
               d.classification
        FROM conflicts c
        JOIN evidence older ON older.id = c.older_evidence_id
        JOIN evidence newer ON newer.id = c.newer_evidence_id
        JOIN documents d ON d.id = c.document_id
        WHERE c.status IN ('pending', 'reviewing')
          AND d.classification != 'restricted'
          AND (
              c.older_evidence_id IN ({placeholders})
              OR c.newer_evidence_id IN ({placeholders})
          )
        ORDER BY c.created_at, c.id
        """,
        tuple(evidence_ids) + tuple(evidence_ids),
    ).fetchall()
    return [
        {
            "conflict_id": row["conflict_id"],
            "conflict_type": row["conflict_type"],
            "reason": row["reason"],
            "status": row["status"],
            "document_name": row["document_name"],
            "classification": row["classification"],
            "older": {
                "evidence_id": row["older_evidence_id"],
                "excerpt": row["older_excerpt"][:MAX_EXCERPT_CHARACTERS],
                "excerpt_truncated": (
                    len(row["older_excerpt"]) > MAX_EXCERPT_CHARACTERS
                ),
                "locator": _safe_locator(row["older_locator_json"]),
            },
            "newer": {
                "evidence_id": row["newer_evidence_id"],
                "excerpt": row["newer_excerpt"][:MAX_EXCERPT_CHARACTERS],
                "excerpt_truncated": (
                    len(row["newer_excerpt"]) > MAX_EXCERPT_CHARACTERS
                ),
                "locator": _safe_locator(row["newer_locator_json"]),
            },
        }
        for row in rows
    ]


def _matching_reviewed_cross_document_conflicts(
    database: Database,
    paths: WorkspacePaths,
    evidence_ids: set[str],
    *,
    existing_pair_ids: set[frozenset[str]],
) -> list[dict[str, Any]]:
    if not evidence_ids:
        return []
    packs = list_cross_document_candidate_packs(database, paths)
    conflicts: list[dict[str, Any]] = []
    seen_packs: set[str] = set()
    for summary in packs["items"]:
        pack_id = summary["pack_id"]
        if pack_id in seen_packs or summary["phase"] != "reviewed":
            continue
        seen_packs.add(pack_id)
        offset = 0
        while True:
            page = cross_document_candidate_page(
                database,
                paths,
                pack_id,
                limit=100,
                offset=offset,
                state="approved",
            )
            for candidate in page["items"]:
                if candidate["label"]["expected_conflict"] is not True:
                    continue
                pair = frozenset(
                    (
                        candidate["left"]["evidence_id"],
                        candidate["right"]["evidence_id"],
                    )
                )
                if pair in existing_pair_ids or not pair.issubset(evidence_ids):
                    continue
                existing_pair_ids.add(pair)
                conflicts.append(
                    {
                        "conflict_id": candidate["candidate_id"],
                        "conflict_type": candidate["label"]["expected_type"],
                        "reason": (
                            "该证据对已经进入分层冲突样本，并通过人工标注和复核。"
                        ),
                        "status": "reviewed",
                        "source": "reviewed_cross_document_sample",
                        "document_name": (
                            f"{candidate['left']['document_name']} ↔ "
                            f"{candidate['right']['document_name']}"
                        ),
                        "classification": candidate["left"]["classification"],
                        "older": _candidate_conflict_side(candidate["left"]),
                        "newer": _candidate_conflict_side(candidate["right"]),
                    }
                )
            if not page["has_next"]:
                break
            offset += page["limit"]
    return conflicts


def _candidate_conflict_side(side: dict[str, Any]) -> dict[str, Any]:
    excerpt = str(side["excerpt"])
    locators = side.get("locators") or [{}]
    return {
        "evidence_id": side["evidence_id"],
        "excerpt": excerpt[:MAX_EXCERPT_CHARACTERS],
        "excerpt_truncated": len(excerpt) > MAX_EXCERPT_CHARACTERS,
        "locator": locators[0] if isinstance(locators[0], dict) else {},
    }


def _detect_value_ambiguities(
    question: str,
    citations: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not any(marker in question for marker in _VALUE_QUERY_MARKERS):
        return []
    evidence_by_value: dict[str, list[str]] = {}
    for citation in citations:
        for value in _meaningful_values(str(citation["excerpt"])):
            evidence_by_value.setdefault(value, []).append(citation["evidence_id"])
    if len(evidence_by_value) < 2:
        return []
    return [
        {
            "kind": "multiple_values",
            "values": [
                {
                    "value": value,
                    "evidence_ids": sorted(set(evidence_id_values)),
                }
                for value, evidence_id_values in sorted(evidence_by_value.items())
            ],
        }
    ]


def _question_terms(question: str) -> tuple[str, ...]:
    lowered = unicodedata.normalize("NFKC", question).lower()
    ascii_terms = {
        item
        for item in _ASCII_TERM_PATTERN.findall(lowered)
        if len(item) >= 2 or item.isdigit()
    }
    cjk_terms: set[str] = set()
    for sequence in _CJK_PATTERN.findall(lowered):
        cleaned = sequence
        # Remove question scaffolding before producing character n-grams.
        # Merely filtering the final n-grams still leaves cross-boundary noise
        # such as “提交哪些” and “哪些资料”, which can drown out business terms.
        noise_phrases = sorted(
            set(_QUESTION_FILLERS) | _STOP_NGRAMS,
            key=lambda value: (-len(value), value),
        )
        for phrase in noise_phrases:
            cleaned = cleaned.replace(phrase, " ")
        for segment in cleaned.split():
            for length in (4, 3, 2):
                if len(segment) < length:
                    continue
                for index in range(len(segment) - length + 1):
                    term = segment[index : index + length]
                    if term not in _STOP_NGRAMS:
                        cjk_terms.add(term)
    return tuple(sorted(ascii_terms | cjk_terms, key=lambda value: (-len(value), value)))


def _needs_context(question: str) -> bool:
    normalized = _normalize_for_match(question)
    return len(normalized) <= 8 or normalized.startswith(_CONTEXT_PREFIXES)


def _validated_history(
    history: Sequence[dict[str, object]],
) -> tuple[dict[str, str], ...]:
    if isinstance(history, (str, bytes)) or not isinstance(history, Sequence):
        raise ValueError("history 必须是数组")
    if len(history) > MAX_HISTORY_TURNS:
        raise ValueError(f"history 最多保留 {MAX_HISTORY_TURNS} 轮")
    normalized: list[dict[str, str]] = []
    for item in history:
        if not isinstance(item, dict):
            raise ValueError("history 项必须是对象")
        role = item.get("role")
        content = item.get("content")
        if role not in {"user", "assistant"} or not isinstance(content, str):
            raise ValueError("history 项必须包含有效的 role 和 content")
        content = content.strip()
        if not content or len(content) > MAX_QUESTION_CHARACTERS:
            raise ValueError("history 内容不能为空且不能超过 500 字")
        normalized.append({"role": str(role), "content": content})
    return tuple(normalized)


def _required_question(question: str) -> str:
    if not isinstance(question, str):
        raise ValueError("question 必须是字符串")
    normalized = unicodedata.normalize("NFKC", question).strip()
    if len(normalized) < 2:
        raise ValueError("问题至少需要 2 个字符")
    if len(normalized) > MAX_QUESTION_CHARACTERS:
        raise ValueError(f"问题不能超过 {MAX_QUESTION_CHARACTERS} 个字符")
    return normalized


def _required_actor(actor: str) -> str:
    if not isinstance(actor, str):
        raise ValueError("actor 必须是字符串")
    normalized = actor.strip()
    if not normalized:
        raise ValueError("操作者不能为空")
    if len(normalized) > 80:
        raise ValueError("操作者不能超过 80 个字符")
    return normalized


def _normalize_for_match(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).lower()
    return re.sub(r"[\W_]+", "", normalized, flags=re.UNICODE)


def _safe_locator(raw: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {}
    if not isinstance(value, dict):
        return {}
    safe: dict[str, Any] = {}
    for key, item in value.items():
        if isinstance(key, str) and isinstance(
            item, (str, int, float, bool, type(None))
        ):
            safe[key] = item
        elif isinstance(key, str) and isinstance(item, list):
            safe[key] = [
                child
                for child in item
                if isinstance(child, (str, int, float, bool, type(None)))
            ][:20]
    return safe
