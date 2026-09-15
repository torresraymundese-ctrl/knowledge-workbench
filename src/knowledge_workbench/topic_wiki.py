from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .audit import record_event
from .config import WorkspacePaths
from .database import Database
from .errors import KnowledgeWorkbenchError
from .utils import new_id, sha256_text, utc_now
from .wiki import write_text_atomic


GENERATOR = "topic-extractive-v1"
MAX_SOURCE_DOCUMENTS = 6
MIN_SOURCE_DOCUMENTS = 2
MAX_AUTHORITY_CONFIRMATIONS = 12

_CLASSIFICATION_RANK = {
    "public": 0,
    "internal": 1,
    "confidential": 2,
    "restricted": 3,
}
_AUTHORITY_LABELS = {
    "unknown": "尚未确认",
    "reference": "参考资料",
    "authoritative": "主要依据",
    "superseded": "已被替代",
}


@dataclass(frozen=True, slots=True)
class TopicDefinition:
    key: str
    title: str
    description: str
    question_examples: tuple[str, ...]
    keywords: tuple[str, ...]
    source_hints: tuple[str, ...]
    domains: tuple[str, ...]


TOPICS = (
    TopicDefinition(
        key="organization-access",
        title="组织、角色、权限与准入",
        description="帮助使用者了解哪些单位和角色可以进入平台，以及各自能做什么。",
        question_examples=(
            "学校、机构和监管部门分别有哪些权限？",
            "一个新机构进入平台前需要满足什么条件？",
        ),
        keywords=(
            "组织",
            "角色",
            "权限",
            "入驻",
            "准入",
            "部门",
            "机构",
            "学校",
            "授权",
            "菜单",
            "用户",
        ),
        source_hints=(
            "权限矩阵",
            "角色资料提交",
            "数据字典_01",
            "接口设计_01",
            "接口设计_02",
            "接口设计_03",
            "system-base",
        ),
        domains=("business", "technical"),
    ),
    TopicDefinition(
        key="course-route",
        title="课程、基地、导师、线路与授权",
        description="帮助使用者找到研学课程、基地、导师和线路之间的对应关系。",
        question_examples=(
            "课程、基地、导师和线路是怎样关联的？",
            "一条研学线路上线前需要完成哪些授权？",
        ),
        keywords=(
            "课程",
            "基地",
            "导师",
            "线路",
            "路线",
            "行程",
            "景点",
            "课程包",
            "课程授权",
        ),
        source_hints=(
            "基地课程",
            "课程线路授权",
            "数据字典_02",
            "接口设计_06",
            "course-route",
        ),
        domains=("business", "policy", "technical"),
    ),
    TopicDefinition(
        key="trade-contract",
        title="需求、招标、合同与交易",
        description="帮助使用者了解从需求提出到合同履行和交易完成的业务资料。",
        question_examples=(
            "学校需求从发布到成交经过哪些环节？",
            "招标、合同、订单和履约资料在哪里？",
        ),
        keywords=(
            "需求",
            "招标",
            "投标",
            "合同",
            "订单",
            "交易",
            "采购",
            "报价",
            "供应商",
            "履约",
        ),
        source_hints=(
            "PRD",
            "交易主流程",
            "数据字典_03",
            "接口设计_04",
            "trade-main",
            "示范合同",
        ),
        domains=("business", "policy", "technical"),
    ),
    TopicDefinition(
        key="roster-consent",
        title="名单、监护人同意与备案",
        description="帮助使用者了解学生名单、监护人同意和活动备案需要哪些资料。",
        question_examples=(
            "学生名单和监护人同意如何完成备案？",
            "名单发生变化后需要重新做什么？",
        ),
        keywords=(
            "名单",
            "名册",
            "监护人",
            "家长同意",
            "同意书",
            "备案",
            "学生",
            "带队",
        ),
        source_hints=(
            "名单同意",
            "备案名单",
            "数据字典_04",
            "接口设计_05",
            "roster-consent",
        ),
        domains=("business", "policy", "technical"),
    ),
    TopicDefinition(
        key="settlement",
        title="缴费、退款、分账与结算",
        description="帮助使用者了解资金从缴费到退款、分账和结算的处理依据。",
        question_examples=(
            "家长缴费后，平台怎样分账和结算？",
            "发生退费时需要经过哪些状态？",
        ),
        keywords=(
            "缴费",
            "支付",
            "退款",
            "退费",
            "分账",
            "结算",
            "资金",
            "账单",
            "清分",
            "提现",
        ),
        source_hints=(
            "资金结算",
            "时序图_05",
            "状态机_04",
            "数据字典_05",
            "settlement",
        ),
        domains=("business", "policy", "technical"),
    ),
    TopicDefinition(
        key="safety",
        title="安全、应急、保险与资质",
        description="帮助使用者集中查找安全责任、应急处置、保险和资质要求。",
        question_examples=(
            "发生安全事件时应按什么流程处理？",
            "机构开展研学活动需要哪些保险和资质？",
        ),
        keywords=(
            "安全",
            "应急",
            "保险",
            "资质",
            "事故",
            "风险",
            "预案",
            "责任保险",
            "证照",
        ),
        source_hints=(
            "安全规范",
            "安全应急预案",
            "责任保险",
            "安全监督档案",
            "数据字典_06",
            "safety-supervision",
        ),
        domains=("business", "policy", "technical"),
    ),
    TopicDefinition(
        key="policy-supervision",
        title="政策、监管、监督与信用",
        description="帮助使用者了解研学业务适用的政策要求、监管动作和信用规则。",
        question_examples=(
            "目前资料里有哪些政策和监管要求？",
            "监督检查或信用异常会带来什么处理？",
        ),
        keywords=(
            "政策",
            "监管",
            "监督",
            "信用",
            "处罚",
            "教育局",
            "文旅部",
            "标准",
            "规范",
            "星级",
        ),
        source_hints=(
            "教育部",
            "教育局",
            "文旅部",
            "研学旅游服务要求",
            "监督信用",
        ),
        domains=("business", "policy", "technical"),
    ),
    TopicDefinition(
        key="location-privacy",
        title="定位、审计、隐私与合规",
        description="帮助使用者了解位置数据、操作记录、个人信息和合规边界。",
        question_examples=(
            "平台什么时候可以采集位置，谁能查看？",
            "敏感操作和个人信息如何留下合规记录？",
        ),
        keywords=(
            "定位",
            "位置",
            "轨迹",
            "审计",
            "隐私",
            "合规",
            "授权记录",
            "操作日志",
            "个人信息",
            "数据安全",
        ),
        source_hints=(
            "位置绑定审计",
            "数据字典_07",
            "接口设计_04",
            "location-audit",
            "audit-log",
        ),
        domains=("business", "policy", "technical"),
    ),
    TopicDefinition(
        key="product-delivery",
        title="产品、架构、接口与交付导航",
        description="帮助产品和开发人员快速定位平台设计、接口、数据结构与交付资料。",
        question_examples=(
            "某项产品能力对应哪些接口和数据表？",
            "新接手项目时应该先读哪些技术和交付资料？",
        ),
        keywords=(
            "产品",
            "架构",
            "接口",
            "api",
            "数据库",
            "数据字典",
            "部署",
            "交付",
            "开发",
            "系统",
            "状态机",
            "时序图",
        ),
        source_hints=(
            "PRD",
            "技术选型",
            "总体架构",
            "接口设计_00",
            "数据字典_00",
            "交付说明-后端对接",
        ),
        domains=("business", "technical", "process"),
    ),
)

_TOPIC_BY_KEY = {topic.key: topic for topic in TOPICS}

_AUTHORITY_RULES = (
    (
        ("PRD_V3.0",),
        "这份资料说明平台要解决什么问题，是判断功能范围时最常用的总依据。",
        ("organization-access", "trade-contract", "product-delivery"),
    ),
    (
        ("角色资料提交开发指导手册",),
        "这份资料把不同角色要提交的内容集中列出，适合确认准入和职责边界。",
        ("organization-access", "safety"),
    ),
    (
        ("权限矩阵",),
        "这份资料集中说明各类角色可查看和操作的功能，适合确认权限边界。",
        ("organization-access",),
    ),
    (
        ("资料收取核对清单",),
        "这份清单适合确认资料是否齐全，但需要您决定它是不是当前执行版本。",
        ("organization-access", "roster-consent", "safety"),
    ),
    (
        ("数据字典_00总目录",),
        "这份总目录可以带使用者找到各业务数据说明，适合作为技术导航入口。",
        ("product-delivery",),
    ),
    (
        ("接口设计_00总目录",),
        "这份总目录可以带使用者找到不同角色和业务环节的接口说明。",
        ("product-delivery",),
    ),
    (
        ("一图读懂", "研学旅游服务要求"),
        "这份资料用简明形式概括行业服务要求，适合确认政策解释入口。",
        ("policy-supervision", "safety"),
    ),
    (
        ("苏州市教育局",),
        "这份资料来自属地教育主管部门，适合确认当地研学工作的监管口径。",
        ("policy-supervision",),
    ),
    (
        ("教育部办公厅",),
        "这份资料来自教育主管部门，适合确认相关活动的政策背景和适用范围。",
        ("policy-supervision",),
    ),
    (
        ("旅行社安全应急预案",),
        "这份资料集中说明安全事件的应急处理，需要确认其适用主体和版本。",
        ("safety",),
    ),
    (
        ("旅行社安全规范",),
        "这份资料集中说明旅行社安全要求，需要确认它是否作为平台执行依据。",
        ("safety", "policy-supervision"),
    ),
    (
        ("走读苏州", "基地课程资料"),
        "这份资料同时包含基地和课程内容，适合确认课程资料的真实业务样例。",
        ("course-route",),
    ),
)

_CREDENTIAL_ASSIGNMENT = re.compile(
    r"(?i)(?:password|passwd|pwd|secret|token|口令|密码)\s*[:=]\s*"
    r"[\"']?[^\s,\"';]{4,}"
)


def knowledge_setup(database: Database) -> dict[str, Any]:
    """Return the human-facing topic and authority setup state."""
    with database.connect() as connection:
        eligible = _eligible_evidence(connection)
        latest_revisions = _latest_topic_revisions(connection)
        topic_matches = {
            topic.key: _match_topic(topic, eligible) for topic in TOPICS
        }
        authority_confirmations = _authority_confirmations(connection, topic_matches)

    topic_candidates = []
    for topic in TOPICS:
        matches = topic_matches[topic.key]
        documents = _rank_documents(topic, matches)
        latest = latest_revisions.get(topic.key)
        if len(documents) < MIN_SOURCE_DOCUMENTS:
            latest = None
        primary = documents[:MAX_SOURCE_DOCUMENTS]
        authoritative_count = sum(
            item["authority_status"] == "authoritative" for item in primary
        )
        reference_count = sum(
            item["authority_status"] == "reference" for item in primary
        )
        unconfirmed_count = sum(
            item["authority_status"] not in {"authoritative", "reference"}
            for item in primary
        )
        topic_candidates.append(
            {
                "topic_key": topic.key,
                "title": topic.title,
                "plain_description": topic.description,
                "question_examples": list(topic.question_examples),
                "document_count": len(documents),
                "primary_sources": [
                    item["display_name"] for item in primary
                ],
                "authoritative_source_count": authoritative_count,
                "reference_source_count": reference_count,
                # Compatibility field for older local clients. A reference
                # decision is explicit and must not be presented as pending.
                "unconfirmed_primary_count": unconfirmed_count,
                "status_label": _topic_status_label(
                    document_count=len(documents),
                    authoritative_count=authoritative_count,
                    revision_status=latest["status"] if latest else None,
                ),
                "revision_id": latest["revision_id"] if latest else None,
                "revision_status": latest["status"] if latest else None,
            }
        )
    return {
        "authority_confirmations": authority_confirmations,
        "topic_candidates": topic_candidates,
    }


def build_topic_wiki_baseline(
    database: Database,
    paths: WorkspacePaths,
    actor: str,
    *,
    refresh: bool = False,
) -> dict[str, Any]:
    """Create extractive, multi-document topic drafts without publishing them."""
    if not isinstance(actor, str) or not actor.strip():
        raise KnowledgeWorkbenchError("actor 不能为空")
    actor = actor.strip()
    if len(actor) > 80:
        raise KnowledgeWorkbenchError("actor 不能超过 80 个字符")
    if any(character in actor for character in "\r\n"):
        raise KnowledgeWorkbenchError("actor 不能包含换行")
    paths.wiki_drafts.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, Any]] = []

    for topic in TOPICS:
        result = _build_one_topic(
            database,
            paths,
            topic,
            actor,
            refresh=refresh,
        )
        results.append(result)

    return {
        "created_count": sum(
            item["action"] in {"created", "refreshed_draft"}
            for item in results
        ),
        "reused_count": sum(item["action"] == "reused_active_draft" for item in results),
        "skipped_count": sum(
            item["action"]
            not in {
                "created",
                "refreshed_draft",
                "reused_active_draft",
            }
            for item in results
        ),
        "topics": results,
    }


def _build_one_topic(
    database: Database,
    paths: WorkspacePaths,
    topic: TopicDefinition,
    actor: str,
    *,
    refresh: bool,
) -> dict[str, Any]:
    created_file: Path | None = None
    try:
        with database.transaction() as connection:
            existing = _topic_page_state(connection, topic)
            if existing and (
                existing["latest_status"] == "reviewing"
                or (
                    existing["latest_status"] == "draft"
                    and not refresh
                )
            ):
                return _build_result(
                    topic,
                    action="reused_active_draft",
                    page_id=existing["page_id"],
                    revision_id=existing["revision_id"],
                    revision_status=existing["latest_status"],
                    evidence_count=existing["evidence_count"],
                    document_count=existing["document_count"],
                    markdown_path=existing["markdown_path"],
                )
            if (
                existing
                and existing["current_verified_revision_id"]
                and not (refresh and existing["needs_revalidation"])
            ):
                return _build_result(
                    topic,
                    action="already_verified",
                    page_id=existing["page_id"],
                    revision_id=existing["current_verified_revision_id"],
                    revision_status="verified",
                )

            eligible = _eligible_evidence(connection)
            matches = _match_topic(topic, eligible)
            ranked_documents = _rank_documents(topic, matches)
            selected = _select_evidence(ranked_documents)
            document_count = len({item["document_id"] for item in selected})
            if document_count < MIN_SOURCE_DOCUMENTS:
                if (
                    refresh
                    and existing
                    and existing["latest_status"] == "draft"
                    and existing["revision_id"]
                ):
                    now = utc_now()
                    connection.execute(
                        """
                        UPDATE wiki_revisions
                        SET status = 'superseded', updated_at = ?
                        WHERE id = ? AND status = 'draft'
                        """,
                        (now, existing["revision_id"]),
                    )
                    page_status = (
                        "verified"
                        if existing["current_verified_revision_id"]
                        else "archived"
                    )
                    connection.execute(
                        """
                        UPDATE wiki_pages
                        SET status = ?, updated_at = ?
                        WHERE id = ?
                        """,
                        (page_status, now, existing["page_id"]),
                    )
                    record_event(
                        connection,
                        "topic_wiki_draft_retired",
                        "wiki_revision",
                        existing["revision_id"],
                        actor=actor,
                        details={
                            "page_id": existing["page_id"],
                            "topic_key": topic.key,
                            "reason": "insufficient_current_sources",
                            "evidence_count": len(selected),
                            "document_count": document_count,
                        },
                    )
                return _build_result(
                    topic,
                    action="insufficient_sources",
                    document_count=document_count,
                    evidence_count=len(selected),
                )

            now = utc_now()
            if existing:
                page_id = existing["page_id"]
                revision_number = existing["revision_number"] + 1
                connection.execute(
                    """
                    UPDATE wiki_pages
                    SET title = ?, status = 'draft', updated_at = ?
                    WHERE id = ?
                    """,
                    (topic.title, now, page_id),
                )
            else:
                page_id = new_id("page")
                revision_number = 1
                connection.execute(
                    """
                    INSERT INTO wiki_pages(
                        id, source_document_id, slug, title, status,
                        current_verified_revision_id, needs_revalidation,
                        created_at, updated_at
                    ) VALUES (?, NULL, ?, ?, 'draft', NULL, 0, ?, ?)
                    """,
                    (page_id, f"topic-{topic.key}", topic.title, now, now),
                )

            revision_id = new_id("rev")
            classification = _highest_classification(
                item["classification"] for item in selected
            )
            content = _render_topic_draft(
                topic=topic,
                page_id=page_id,
                revision_id=revision_id,
                revision_number=revision_number,
                classification=classification,
                generated_at=now,
                selected=selected,
            )
            created_file = (
                paths.wiki_drafts
                / (
                    f"topic-{topic.key}--r{revision_number}"
                    f"--{revision_id.removeprefix('rev_')[:8]}.md"
                )
            )
            write_text_atomic(created_file, content)
            relative_path = created_file.relative_to(paths.root).as_posix()
            connection.execute(
                """
                INSERT INTO wiki_revisions(
                    id, page_id, revision_number, status, markdown_path,
                    content_sha256, generator, processing_run_id,
                    created_at, updated_at
                ) VALUES (?, ?, ?, 'draft', ?, ?, ?, NULL, ?, ?)
                """,
                (
                    revision_id,
                    page_id,
                    revision_number,
                    relative_path,
                    sha256_text(content),
                    GENERATOR,
                    now,
                    now,
                ),
            )
            connection.executemany(
                """
                INSERT INTO revision_evidence(revision_id, evidence_id)
                VALUES (?, ?)
                """,
                ((revision_id, item["evidence_id"]) for item in selected),
            )
            refreshed_revision_id = None
            if (
                refresh
                and existing
                and existing["latest_status"] == "draft"
                and existing["revision_id"] != revision_id
            ):
                refreshed_revision_id = existing["revision_id"]
                connection.execute(
                    """
                    UPDATE wiki_revisions
                    SET status = 'superseded', updated_at = ?
                    WHERE id = ? AND status = 'draft'
                    """,
                    (now, refreshed_revision_id),
                )
            record_event(
                connection,
                "topic_wiki_draft_created",
                "wiki_revision",
                revision_id,
                actor=actor,
                details={
                    "page_id": page_id,
                    "topic_key": topic.key,
                    "revision": revision_number,
                    "evidence_count": len(selected),
                    "document_count": document_count,
                    "generator": GENERATOR,
                    "refreshed_revision_id": refreshed_revision_id,
                },
            )
            result = _build_result(
                topic,
                action=(
                    "refreshed_draft"
                    if refreshed_revision_id is not None
                    else "created"
                ),
                page_id=page_id,
                revision_id=revision_id,
                revision_status="draft",
                evidence_count=len(selected),
                document_count=document_count,
                markdown_path=relative_path,
            )
        return result
    except Exception:
        if created_file is not None:
            for path in (
                created_file,
                created_file.with_suffix(created_file.suffix + ".tmp"),
            ):
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
        raise


def _eligible_evidence(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT e.id AS evidence_id, e.excerpt, e.locator_json,
               d.id AS document_id, d.original_name, d.classification,
               dv.sha256, dg.authority_status, dg.knowledge_domain,
               COALESCE(
                   (
                       SELECT cf.display_title
                       FROM corpus_files cf
                       JOIN corpus_scans cs ON cs.id = cf.scan_id
                       WHERE cs.is_current = 1
                         AND cf.sha256 = dv.sha256
                         AND cf.scope_status = 'in_scope'
                         AND cf.map_status != 'blocked'
                         AND instr(
                             cf.risk_flags_json, 'credential_material'
                         ) = 0
                       ORDER BY
                           CASE cf.authority_status
                               WHEN 'authoritative' THEN 0
                               WHEN 'reference' THEN 1
                               ELSE 2
                           END,
                           length(cf.relative_path),
                           cf.relative_path
                       LIMIT 1
                   ),
                   d.original_name
               ) AS display_name
        FROM documents d
        JOIN document_governance dg ON dg.document_id = d.id
        JOIN document_versions dv ON dv.id = d.current_version_id
        JOIN processing_runs pr
          ON pr.document_version_id = dv.id
         AND pr.is_current = 1
         AND pr.status = 'completed'
        JOIN evidence e
          ON e.document_version_id = dv.id
         AND e.processing_run_id = pr.id
         AND e.status = 'verified'
        JOIN evidence_technical_validation etv
          ON etv.evidence_id = e.id
         AND etv.status = 'passed'
        WHERE dg.purpose = 'production'
          AND dg.scope_status = 'in_scope'
          AND dg.authority_status IN ('reference', 'authoritative')
          AND d.classification != 'restricted'
        ORDER BY d.original_name, e.run_ordinal, e.id
        """
    ).fetchall()
    eligible: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        if _contains_credential_assignment(item["excerpt"]):
            continue
        item["locator"] = _parse_locator(item.pop("locator_json"))
        eligible.append(item)
    return eligible


def _match_topic(
    topic: TopicDefinition,
    rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    matched = []
    for row in rows:
        if row["knowledge_domain"] not in topic.domains:
            continue
        name = row["original_name"].casefold()
        excerpt = row["excerpt"].casefold()
        name_hits = sum(keyword.casefold() in name for keyword in topic.keywords)
        excerpt_hits = sum(
            keyword.casefold() in excerpt for keyword in topic.keywords
        )
        source_hint_hits = sum(
            hint.casefold() in name for hint in topic.source_hints
        )
        if name_hits == 0 and source_hint_hits == 0 and excerpt_hits < 2:
            continue
        candidate = dict(row)
        candidate["topic_score"] = name_hits * 8 + excerpt_hits + source_hint_hits * 12
        candidate["source_hint_hits"] = source_hint_hits
        matched.append(candidate)
    return matched


def _rank_documents(
    topic: TopicDefinition,
    matches: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    by_document: dict[str, dict[str, Any]] = {}
    for item in matches:
        current = by_document.get(item["document_id"])
        if current is None or _evidence_rank(item) < _evidence_rank(current):
            by_document[item["document_id"]] = item
    return sorted(
        by_document.values(),
        key=lambda item: (
            0 if item["authority_status"] == "authoritative" else 1,
            -item["source_hint_hits"],
            _source_hint_position(topic, item["original_name"]),
            -item["topic_score"],
            item["display_name"].casefold(),
            item["evidence_id"],
        ),
    )


def _select_evidence(ranked_documents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [dict(item) for item in ranked_documents[:MAX_SOURCE_DOCUMENTS]]


def _evidence_rank(item: dict[str, Any]) -> tuple[int, int, int, str]:
    return (
        -item["source_hint_hits"],
        -item["topic_score"],
        len(item["excerpt"]),
        item["evidence_id"],
    )


def _source_hint_position(topic: TopicDefinition, source_name: str) -> int:
    folded = source_name.casefold()
    for index, hint in enumerate(topic.source_hints):
        if hint.casefold() in folded:
            return index
    return len(topic.source_hints)


def _latest_topic_revisions(
    connection: sqlite3.Connection,
) -> dict[str, dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT wp.slug, wr.id AS revision_id, wr.status
        FROM wiki_pages wp
        JOIN wiki_revisions wr ON wr.page_id = wp.id
        WHERE wp.source_document_id IS NULL
          AND wp.slug LIKE 'topic-%'
          AND wr.revision_number = (
              SELECT MAX(newer.revision_number)
              FROM wiki_revisions newer
              WHERE newer.page_id = wp.id
          )
        """
    ).fetchall()
    return {
        row["slug"].removeprefix("topic-"): dict(row)
        for row in rows
        if row["slug"].removeprefix("topic-") in _TOPIC_BY_KEY
    }


def _topic_page_state(
    connection: sqlite3.Connection,
    topic: TopicDefinition,
) -> dict[str, Any] | None:
    page = connection.execute(
        """
        SELECT id, current_verified_revision_id, needs_revalidation
        FROM wiki_pages
        WHERE source_document_id IS NULL AND slug = ?
        """,
        (f"topic-{topic.key}",),
    ).fetchone()
    if page is None:
        return None
    latest = connection.execute(
        """
        SELECT wr.id AS revision_id, wr.revision_number,
               wr.status AS latest_status, wr.markdown_path,
               COUNT(DISTINCT re.evidence_id) AS evidence_count,
               COUNT(DISTINCT dv.document_id) AS document_count
        FROM wiki_revisions wr
        LEFT JOIN revision_evidence re ON re.revision_id = wr.id
        LEFT JOIN evidence e ON e.id = re.evidence_id
        LEFT JOIN document_versions dv ON dv.id = e.document_version_id
        WHERE wr.page_id = ?
        GROUP BY wr.id
        ORDER BY wr.revision_number DESC
        LIMIT 1
        """,
        (page["id"],),
    ).fetchone()
    if latest is None:
        return {
            "page_id": page["id"],
            "current_verified_revision_id": page["current_verified_revision_id"],
            "needs_revalidation": page["needs_revalidation"],
            "revision_id": None,
            "revision_number": 0,
            "latest_status": None,
            "markdown_path": None,
            "evidence_count": 0,
            "document_count": 0,
        }
    return {
        "page_id": page["id"],
        "current_verified_revision_id": page["current_verified_revision_id"],
        "needs_revalidation": page["needs_revalidation"],
        **dict(latest),
    }


def _authority_confirmations(
    connection: sqlite3.Connection,
    topic_matches: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    topic_documents = {
        topic_key: {item["document_id"] for item in matches}
        for topic_key, matches in topic_matches.items()
    }
    rows = connection.execute(
        """
        SELECT d.id AS document_id, d.original_name, dg.authority_status,
               cf.id AS file_id, cf.display_title, cf.relative_path
        FROM documents d
        JOIN document_governance dg ON dg.document_id = d.id
        JOIN document_versions dv ON dv.id = d.current_version_id
        JOIN corpus_files cf ON cf.sha256 = dv.sha256
        JOIN corpus_scans cs ON cs.id = cf.scan_id
        WHERE cs.is_current = 1
          AND dg.purpose = 'production'
          AND dg.scope_status = 'in_scope'
          AND dg.authority_status IN ('unknown', 'reference', 'authoritative')
          AND d.classification != 'restricted'
          AND cf.scope_status = 'in_scope'
          AND cf.map_status != 'blocked'
          AND instr(cf.risk_flags_json, 'credential_material') = 0
          AND EXISTS (
              SELECT 1
              FROM processing_runs pr
              JOIN evidence e
                ON e.processing_run_id = pr.id
               AND e.document_version_id = dv.id
               AND e.status = 'verified'
              JOIN evidence_technical_validation etv
                ON etv.evidence_id = e.id
               AND etv.status = 'passed'
              WHERE pr.document_version_id = dv.id
                AND pr.is_current = 1
                AND pr.status = 'completed'
          )
        ORDER BY d.original_name, length(cf.relative_path), cf.relative_path
        """
    ).fetchall()
    best_by_document: dict[str, dict[str, Any]] = {}
    for row in rows:
        best_by_document.setdefault(row["document_id"], dict(row))

    candidates: list[dict[str, Any]] = []
    seen_documents: set[str] = set()
    for priority, (markers, reason, topic_keys) in enumerate(_AUTHORITY_RULES):
        for row in best_by_document.values():
            if row["document_id"] in seen_documents:
                continue
            folded_name = row["original_name"].casefold()
            if not all(marker.casefold() in folded_name for marker in markers):
                continue
            matched_keys = [
                key
                for key in topic_keys
                if row["document_id"] in topic_documents.get(key, set())
            ]
            if not matched_keys:
                matched_keys = list(topic_keys)
            candidates.append(
                {
                    "priority": priority,
                    "file_id": row["file_id"],
                    "display_title": row["display_title"],
                    "plain_reason": reason,
                    "current_authority": row["authority_status"],
                    "current_authority_label": _AUTHORITY_LABELS[
                        row["authority_status"]
                    ],
                    "topics": [
                        _TOPIC_BY_KEY[key].title
                        for key in matched_keys
                        if key in _TOPIC_BY_KEY
                    ],
                }
            )
            seen_documents.add(row["document_id"])
    candidates.sort(
        key=lambda item: (
            item["priority"],
            item["display_title"].casefold(),
            item["file_id"],
        )
    )
    for item in candidates:
        del item["priority"]
    return candidates[:MAX_AUTHORITY_CONFIRMATIONS]


def _topic_status_label(
    *,
    document_count: int,
    authoritative_count: int,
    revision_status: str | None,
) -> str:
    if revision_status == "verified":
        return "已形成正式主题页"
    if revision_status == "reviewing":
        return "主题草稿正在审核"
    if revision_status == "draft":
        return "主题草稿待审核"
    if document_count < MIN_SOURCE_DOCUMENTS:
        return "资料不足，暂不生成"
    if authoritative_count == 0:
        return "可生成草稿，当前均为参考资料"
    return "可生成主题草稿"


def _render_topic_draft(
    *,
    topic: TopicDefinition,
    page_id: str,
    revision_id: str,
    revision_number: int,
    classification: str,
    generated_at: str,
    selected: list[dict[str, Any]],
) -> str:
    frontmatter = [
        "---",
        f"id: {json.dumps(page_id, ensure_ascii=False)}",
        f"revision_id: {json.dumps(revision_id, ensure_ascii=False)}",
        f"revision: {revision_number}",
        "status: draft",
        f"classification: {classification}",
        f"generator: {json.dumps(GENERATOR)}",
        f"generated_at: {json.dumps(generated_at)}",
        f"source_document_count: {len({item['document_id'] for item in selected})}",
        "---",
        "",
    ]
    body = [
        f"# {topic.title}",
        "",
        "> 这是按主题整理的逐字摘录草稿，尚未经过人工业务审核，"
        "不代表平台已经确认这些内容的适用范围或优先级。",
        "",
        "## 页面用途",
        "<!-- topic-section-evidence: [] -->",
        "",
        topic.description,
        "",
    ]
    for item in selected:
        marker = json.dumps([item["evidence_id"]], ensure_ascii=False)
        quote = item["excerpt"].replace("\r\n", "\n").replace("\r", "\n")
        quote = quote.replace("\n", "\n> ")
        body.extend(
            [
                f"## 原文依据：{_safe_heading(item['display_name'])}",
                f"<!-- topic-section-evidence: {marker} -->",
                "",
                f"- 资料：{item['display_name']}",
                f"- 位置：{_locator_label(item['locator'])}",
                f"- 资料用途：{_AUTHORITY_LABELS[item['authority_status']]}",
                "",
                f"> {quote}",
                "",
            ]
        )
    all_ids = json.dumps(
        [item["evidence_id"] for item in selected],
        ensure_ascii=False,
    )
    body.extend(
        [
            "## 人工审核重点",
            f"<!-- topic-section-evidence: {all_ids} -->",
            "",
            "- [ ] 确认这些资料确实适用于当前企业和当前业务",
            "- [ ] 确认哪些资料是主要依据，哪些只能作为参考",
            "- [ ] 如不同资料说法不一致，明确采用哪一份及原因",
            "- [ ] 通过后再把逐字摘录提炼成面向使用者的正式说明",
            "",
        ]
    )
    return "\n".join(frontmatter + body)


def _parse_locator(value: str) -> dict[str, Any]:
    try:
        locator = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return locator if isinstance(locator, dict) else {}


def _locator_label(locator: dict[str, Any]) -> str:
    labels: list[str] = []
    page = locator.get("page")
    if isinstance(page, int):
        labels.append(f"第 {page} 页")
    slide = locator.get("slide")
    if isinstance(slide, int):
        labels.append(f"第 {slide} 张幻灯片")
    sheet = locator.get("sheet")
    if sheet:
        labels.append(f"工作表“{sheet}”")
    cell_range = locator.get("cell_range") or locator.get("range")
    if cell_range:
        labels.append(f"单元格 {cell_range}")
    headings = locator.get("heading_path")
    if isinstance(headings, list) and headings:
        clean_headings = [str(item).strip() for item in headings if str(item).strip()]
        if clean_headings:
            labels.append(f"章节“{' > '.join(clean_headings)}”")
    paragraph = locator.get("paragraph") or locator.get("paragraph_index")
    if isinstance(paragraph, int):
        labels.append(f"第 {paragraph} 段")
    line_start = locator.get("line_start")
    line_end = locator.get("line_end")
    if isinstance(line_start, int):
        if isinstance(line_end, int) and line_end != line_start:
            labels.append(f"第 {line_start} 至 {line_end} 行")
        else:
            labels.append(f"第 {line_start} 行")
    row = locator.get("row")
    if isinstance(row, int):
        labels.append(f"第 {row} 行")
    return "，".join(labels) if labels else "原文中的对应段落"


def _safe_heading(value: str) -> str:
    return value.replace("\r", " ").replace("\n", " ").strip()


def _highest_classification(values: Iterable[str]) -> str:
    return max(values, key=lambda value: _CLASSIFICATION_RANK[value])


def _contains_credential_assignment(value: str) -> bool:
    return bool(_CREDENTIAL_ASSIGNMENT.search(value))


def _build_result(
    topic: TopicDefinition,
    *,
    action: str,
    page_id: str | None = None,
    revision_id: str | None = None,
    revision_status: str | None = None,
    evidence_count: int = 0,
    document_count: int = 0,
    markdown_path: str | None = None,
) -> dict[str, Any]:
    return {
        "topic_key": topic.key,
        "title": topic.title,
        "action": action,
        "page_id": page_id,
        "revision_id": revision_id,
        "revision_status": revision_status,
        "evidence_count": evidence_count,
        "document_count": document_count,
        "markdown_path": markdown_path,
    }
