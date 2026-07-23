from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from .audit import record_event
from .config import WorkspacePaths
from .database import Database
from .errors import KnowledgeWorkbenchError
from .schema_validation import validate_graph_pilot_pack
from .utils import sha256_text, utc_now
from .wiki import write_text_atomic


PILOT_EVIDENCE_STATUSES = frozenset(
    {
        "draft",
        "reviewing",
        "verified",
        "conflicted",
        "deprecated",
        "archived",
    }
)


def list_graph_pilot_packs(
    database: Database,
    paths: WorkspacePaths,
) -> dict:
    items = []
    invalid_pack_count = 0
    seen: set[str] = set()
    for pack_path in sorted(paths.evaluations.glob("*.json")):
        try:
            raw = json.loads(pack_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if (
            not isinstance(raw, dict)
            or raw.get("kind") != "graph-pilot-evidence-pack"
        ):
            continue
        try:
            status = inspect_graph_pilot_pack(
                database, paths, pack_path
            )
            _, _, pack = _read_pack(paths, pack_path)
        except KnowledgeWorkbenchError:
            invalid_pack_count += 1
            continue
        if pack["pack_id"] in seen:
            invalid_pack_count += 1
            continue
        seen.add(pack["pack_id"])
        items.append(
            {
                "pack_id": pack["pack_id"],
                "generated_at": pack["generated_at"],
                "generated_by": pack["generated_by"],
                "source_labeling_session_id": pack[
                    "source_labeling_session"
                ]["session_id"],
                "source_labeling_session_name": pack[
                    "source_labeling_session"
                ]["name"],
                "summary": status["summary"],
            }
        )
    items.sort(
        key=lambda item: (item["generated_at"], item["pack_id"]),
        reverse=True,
    )
    return {
        "items": items,
        "total": len(items),
        "invalid_pack_count": invalid_pack_count,
    }


def graph_pilot_pack_page(
    database: Database,
    paths: WorkspacePaths,
    pack_id: str,
    *,
    limit: int = 10,
    offset: int = 0,
    status: str | None = None,
    query: str | None = None,
) -> dict:
    limit, offset = _pagination(limit, offset)
    normalized_status = (status or "all").strip().casefold()
    if (
        normalized_status != "all"
        and normalized_status not in PILOT_EVIDENCE_STATUSES
    ):
        raise ValueError("图谱试点证据状态筛选无效")
    normalized_query = (query or "").strip()
    if len(normalized_query) > 120:
        raise ValueError("图谱试点搜索不能超过 120 个字符")
    pack_path = _find_pack_path(paths, pack_id)
    pack_path, _, pack = _read_pack(paths, pack_path)
    status_report = inspect_graph_pilot_pack(
        database, paths, pack_path
    )
    summary = status_report["summary"]
    if (
        not summary["source_snapshot_passed"]
        or not summary["classification_boundary_passed"]
    ):
        raise KnowledgeWorkbenchError(
            "图谱试点证据包来源快照或密级边界已失效"
        )
    current_by_id = {
        item["evidence_id"]: item for item in status_report["items"]
    }
    query_folded = normalized_query.casefold()
    items = []
    for candidate in pack["candidates"]:
        current = current_by_id[candidate["evidence_id"]]
        if (
            normalized_status != "all"
            and current["status"] != normalized_status
        ):
            continue
        searchable = (
            candidate["evidence_id"],
            candidate["document_name"],
            str(candidate["run_ordinal"]),
            *candidate["case_ids"],
        )
        if query_folded and not any(
            query_folded in value.casefold() for value in searchable
        ):
            continue
        items.append(
            {
                "evidence_id": candidate["evidence_id"],
                "case_ids": candidate["case_ids"],
                "status": current["status"],
                "ordinal": candidate["run_ordinal"],
                "document_name": candidate["document_name"],
                "classification": candidate["classification"],
                "locator": candidate["locators"][0],
                "location_count": len(candidate["locators"]),
                "active_entity_count": current["active_entity_count"],
                "active_relationship_count": current[
                    "active_relationship_count"
                ],
                "ready_for_relationship_registration": current[
                    "ready_for_relationship_registration"
                ],
            }
        )
    total = len(items)
    returned = items[offset : offset + limit]
    return {
        "pack_id": pack["pack_id"],
        "source_labeling_session_id": pack[
            "source_labeling_session"
        ]["session_id"],
        "summary": summary,
        "total": total,
        "limit": limit,
        "offset": offset,
        "has_previous": offset > 0,
        "has_next": offset + len(returned) < total,
        "filters": {
            "status": normalized_status,
            "query": normalized_query or None,
        },
        "items": returned,
    }


def inspect_graph_pilot_pack(
    database: Database,
    paths: WorkspacePaths,
    pack_path: Path,
) -> dict:
    pack_path, _, pack = resolve_graph_pilot_pack(
        database, paths, pack_path
    )
    items = []
    with database.connect() as connection:
        current_locators = _location_map(
            connection,
            [candidate["evidence_id"] for candidate in pack["candidates"]],
        )
        for candidate in pack["candidates"]:
            evidence_id = candidate["evidence_id"]
            row = connection.execute(
                """
                SELECT e.id, e.excerpt, e.status, e.run_ordinal,
                       pr.id AS processing_run_id, pr.is_current,
                       dv.id AS document_version_id,
                       d.id AS document_id, d.original_name,
                       d.classification,
                       CASE WHEN d.current_version_id = dv.id
                            THEN 1 ELSE 0 END AS version_is_current,
                       (
                           SELECT COUNT(DISTINCT eem.entity_id)
                           FROM evidence_entity_mentions eem
                           JOIN canonical_entities ce
                             ON ce.id = eem.entity_id
                            AND ce.status = 'active'
                           WHERE eem.evidence_id = e.id
                       ) AS active_entity_count,
                       (
                           SELECT COUNT(DISTINCT ere.relationship_id)
                           FROM entity_relationship_evidence ere
                           JOIN entity_relationships er
                             ON er.id = ere.relationship_id
                            AND er.status = 'active'
                           WHERE ere.evidence_id = e.id
                       ) AS active_relationship_count
                FROM evidence e
                JOIN processing_runs pr ON pr.id = e.processing_run_id
                JOIN document_versions dv ON dv.id = e.document_version_id
                JOIN documents d ON d.id = dv.document_id
                WHERE e.id = ?
                """,
                (evidence_id,),
            ).fetchone()
            if not row:
                items.append(
                    {
                        "evidence_id": evidence_id,
                        "snapshot_valid": False,
                        "status": "missing",
                        "active_entity_count": 0,
                        "active_relationship_count": 0,
                        "ready_for_relationship_registration": False,
                    }
                )
                continue
            snapshot_valid = (
                bool(row["is_current"])
                and bool(row["version_is_current"])
                and row["excerpt"] == candidate["excerpt"]
                and row["run_ordinal"] == candidate["run_ordinal"]
                and row["processing_run_id"]
                == candidate["processing_run_id"]
                and row["document_version_id"]
                == candidate["document_version_id"]
                and row["document_id"] == candidate["document_id"]
                and row["original_name"] == candidate["document_name"]
                and row["classification"] == candidate["classification"]
                and current_locators[evidence_id] == candidate["locators"]
            )
            items.append(
                {
                    "evidence_id": evidence_id,
                    "snapshot_valid": snapshot_valid,
                    "status": row["status"],
                    "classification": row["classification"],
                    "active_entity_count": row["active_entity_count"],
                    "active_relationship_count": row[
                        "active_relationship_count"
                    ],
                    "ready_for_relationship_registration": (
                        snapshot_valid
                        and row["status"] == "verified"
                        and row["classification"] != "restricted"
                        and row["active_entity_count"] >= 2
                    ),
                }
            )
    total = len(items)
    valid_items = [item for item in items if item["snapshot_valid"]]
    verified_items = [
        item
        for item in valid_items
        if item["status"] == "verified"
        and item.get("classification") != "restricted"
    ]
    linked_items = [
        item for item in verified_items if item["active_entity_count"] >= 1
    ]
    dual_linked_items = [
        item for item in verified_items if item["active_entity_count"] >= 2
    ]
    related_items = [
        item
        for item in verified_items
        if item["active_relationship_count"] >= 1
    ]
    status_counts = dict(
        sorted(Counter(item["status"] for item in items).items())
    )
    invalid_count = total - len(valid_items)
    restricted_leak_count = sum(
        item.get("classification") == "restricted" for item in items
    )
    return {
        "schema_version": "1.0",
        "kind": "graph-pilot-status",
        "pack_id": pack["pack_id"],
        "source_labeling_session_id": pack[
            "source_labeling_session"
        ]["session_id"],
        "checked_at": utc_now(),
        "summary": {
            "candidate_count": total,
            "snapshot_valid_count": len(valid_items),
            "snapshot_invalid_count": invalid_count,
            "restricted_leak_count": restricted_leak_count,
            "status_counts": status_counts,
            "verified_evidence_count": len(verified_items),
            "verified_evidence_coverage": _coverage(
                len(verified_items), total
            ),
            "verified_with_entity_count": len(linked_items),
            "verified_entity_link_coverage": _coverage(
                len(linked_items), len(verified_items)
            ),
            "verified_with_two_entities_count": len(dual_linked_items),
            "verified_dual_entity_coverage": _coverage(
                len(dual_linked_items), len(verified_items)
            ),
            "relationship_ready_evidence_count": sum(
                item["ready_for_relationship_registration"]
                for item in items
            ),
            "verified_with_active_relationship_count": len(related_items),
            "active_relationship_coverage": _coverage(
                len(related_items), len(verified_items)
            ),
            "source_snapshot_passed": invalid_count == 0,
            "classification_boundary_passed": restricted_leak_count == 0,
            "evidence_review_complete": (
                invalid_count == 0
                and all(
                    item["status"] not in {"draft", "reviewing"}
                    for item in items
                )
            ),
            "graph_gold_prerequisites_met": (
                invalid_count == 0
                and restricted_leak_count == 0
                and bool(related_items)
            ),
        },
        "items": items,
    }


def resolve_graph_pilot_pack(
    database: Database,
    paths: WorkspacePaths,
    pack_path: Path,
) -> tuple[Path, str, dict]:
    pack_path, content, pack = _read_pack(paths, pack_path)
    _validate_pack_identity(pack)
    _validate_pack_audit(database, paths, pack_path, content, pack)
    return pack_path, content, pack


def build_graph_pilot_pack(
    database: Database,
    paths: WorkspacePaths,
    session_id: str,
    output: Path,
    *,
    actor: str,
) -> dict:
    actor = _required_actor(actor)
    output = _validated_output(paths, output)
    with database.connect() as connection:
        session = connection.execute(
            """
            SELECT id, name, status, created_by, approved_by
            FROM labeling_sessions WHERE id = ?
            """,
            (session_id,),
        ).fetchone()
        if not session:
            raise KnowledgeWorkbenchError("黄金标注会话不存在")
        if session["status"] != "approved" or not session["approved_by"]:
            raise KnowledgeWorkbenchError(
                "只有 approved 黄金标注会话可以生成图谱试点证据包"
            )
        rows = connection.execute(
            """
            SELECT lc.case_id, lc.classification AS case_classification,
                   lcr.decision AS review_decision,
                   lcr.reviewer AS case_reviewer,
                   e.id AS evidence_id, e.excerpt, e.status, e.run_ordinal,
                   e.locator_json, pr.id AS processing_run_id,
                   pr.is_current AS run_is_current,
                   dv.id AS document_version_id,
                   d.id AS document_id, d.original_name,
                   d.classification,
                   CASE WHEN d.current_version_id = dv.id
                        THEN 1 ELSE 0 END AS version_is_current
            FROM labeling_cases lc
            JOIN labeling_expected_evidence lee
              ON lee.case_row_id = lc.id
            JOIN labeling_case_reviews lcr
              ON lcr.case_row_id = lc.id
            JOIN evidence e ON e.id = lee.evidence_id
            JOIN processing_runs pr ON pr.id = e.processing_run_id
            JOIN document_versions dv ON dv.id = e.document_version_id
            JOIN documents d ON d.id = dv.document_id
            WHERE lc.session_id = ?
            ORDER BY e.id, lc.case_id
            """,
            (session_id,),
        ).fetchall()
        if not rows:
            raise KnowledgeWorkbenchError(
                "approved 黄金标注会话没有已复核必要证据"
            )
        case_count = connection.execute(
            "SELECT COUNT(*) FROM labeling_cases WHERE session_id = ?",
            (session_id,),
        ).fetchone()[0]
        _validate_source_rows(rows)
        locators = _location_map(
            connection, sorted({row["evidence_id"] for row in rows})
        )

    grouped: dict[str, dict] = {}
    restricted_ids: set[str] = set()
    for row in rows:
        evidence_id = row["evidence_id"]
        if row["classification"] == "restricted":
            restricted_ids.add(evidence_id)
            continue
        candidate = grouped.setdefault(
            evidence_id,
            {
                "evidence_id": evidence_id,
                "case_ids": [],
                "status": row["status"],
                "run_ordinal": row["run_ordinal"],
                "document_id": row["document_id"],
                "document_version_id": row["document_version_id"],
                "processing_run_id": row["processing_run_id"],
                "document_name": row["original_name"],
                "classification": row["classification"],
                "excerpt": row["excerpt"],
                "locators": locators[evidence_id],
            },
        )
        candidate["case_ids"].append(row["case_id"])
    candidates = [grouped[item] for item in sorted(grouped)]
    if not candidates:
        raise KnowledgeWorkbenchError(
            "黄金标注会话的必要证据全部为 restricted，不能生成试点包"
        )
    for candidate in candidates:
        candidate["case_ids"] = sorted(set(candidate["case_ids"]))
    selected_count = len(grouped) + len(restricted_ids)
    pack = {
        "schema_version": "1.0",
        "kind": "graph-pilot-evidence-pack",
        "pack_id": "pending",
        "generated_at": utc_now(),
        "generated_by": actor,
        "source_labeling_session": {
            "session_id": session["id"],
            "name": session["name"],
            "created_by": session["created_by"],
            "approved_by": session["approved_by"],
            "case_count": case_count,
        },
        "workflow": {
            "purpose": "manual-evidence-review-and-entity-graph-pilot",
            "automatic_status_changes": False,
            "automatic_entity_creation": False,
            "automatic_relationship_creation": False,
        },
        "statistics": {
            "selected_evidence_count": selected_count,
            "exported_evidence_count": len(candidates),
            "restricted_evidence_excluded": len(restricted_ids),
            "status_counts": dict(
                sorted(Counter(
                    candidate["status"] for candidate in candidates
                ).items())
            ),
        },
        "candidates": candidates,
    }
    pack["pack_id"] = _pack_id(pack)
    validate_graph_pilot_pack(pack)
    content = json.dumps(
        pack, ensure_ascii=False, indent=2, sort_keys=True
    ) + "\n"
    write_text_atomic(output, content)
    try:
        with database.transaction() as connection:
            record_event(
                connection,
                "graph_pilot_pack_created",
                "graph_pilot_pack",
                pack["pack_id"],
                actor=actor,
                details={
                    "source_labeling_session_id": session_id,
                    "output": output.relative_to(paths.root.resolve()).as_posix(),
                    "content_sha256": sha256_text(content),
                    "selected_evidence_count": selected_count,
                    "exported_evidence_count": len(candidates),
                    "restricted_evidence_excluded": len(restricted_ids),
                },
            )
    except Exception:
        output.unlink(missing_ok=True)
        raise
    return pack


def _validate_source_rows(rows: list) -> None:
    for row in rows:
        if row["review_decision"] != "approved":
            raise KnowledgeWorkbenchError(
                f"黄金标注用例尚未复核通过：{row['case_id']}"
            )
        if not row["run_is_current"] or not row["version_is_current"]:
            raise KnowledgeWorkbenchError(
                f"黄金标注证据来源已过期：{row['evidence_id']}"
            )
        if row["case_classification"] != row["classification"]:
            raise KnowledgeWorkbenchError(
                f"黄金标注证据密级已漂移：{row['evidence_id']}"
            )
        if row["status"] in {"deprecated", "archived"}:
            raise KnowledgeWorkbenchError(
                f"黄金标注证据已退出当前试点资格：{row['evidence_id']}"
            )


def _location_map(
    connection, evidence_ids: list[str]
) -> dict[str, list[dict]]:
    output = {evidence_id: [] for evidence_id in evidence_ids}
    for start in range(0, len(evidence_ids), 500):
        batch = evidence_ids[start : start + 500]
        placeholders = ",".join("?" for _ in batch)
        rows = connection.execute(
            f"""
            SELECT evidence_id, locator_json
            FROM evidence_locations
            WHERE evidence_id IN ({placeholders})
            ORDER BY evidence_id, location_ordinal
            """,
            batch,
        ).fetchall()
        for row in rows:
            output[row["evidence_id"]].append(
                json.loads(row["locator_json"])
            )
    missing = [
        evidence_id
        for evidence_id, locations in output.items()
        if not locations
    ]
    if missing:
        raise KnowledgeWorkbenchError(
            "图谱试点证据缺少来源定位：" + ", ".join(missing)
        )
    return output


def _validated_output(paths: WorkspacePaths, output: Path) -> Path:
    output = output.expanduser().resolve()
    if output.suffix.lower() != ".json":
        raise KnowledgeWorkbenchError("图谱试点证据包必须使用 .json 文件")
    try:
        output.relative_to(paths.evaluations.resolve())
    except ValueError as exc:
        raise KnowledgeWorkbenchError(
            "图谱试点证据包只能保存到当前 workspace/evaluations 内"
        ) from exc
    if output.exists():
        raise KnowledgeWorkbenchError(
            f"图谱试点证据包已存在，不允许静默覆盖：{output}"
        )
    return output


def _read_pack(
    paths: WorkspacePaths, pack_path: Path
) -> tuple[Path, str, dict]:
    pack_path = pack_path.expanduser().resolve()
    try:
        pack_path.relative_to(paths.evaluations.resolve())
    except ValueError as exc:
        raise KnowledgeWorkbenchError(
            "图谱试点证据包只能从当前 workspace/evaluations 内读取"
        ) from exc
    try:
        content = pack_path.read_text(encoding="utf-8")
        pack = json.loads(content)
    except FileNotFoundError as exc:
        raise KnowledgeWorkbenchError(
            f"图谱试点证据包不存在：{pack_path}"
        ) from exc
    except json.JSONDecodeError as exc:
        raise KnowledgeWorkbenchError(
            f"图谱试点证据包不是有效 JSON：{exc}"
        ) from exc
    validate_graph_pilot_pack(pack)
    return pack_path, content, pack


def _find_pack_path(paths: WorkspacePaths, pack_id: str) -> Path:
    pack_id = pack_id.strip()
    if not pack_id:
        raise KnowledgeWorkbenchError("图谱试点证据包 ID 不能为空")
    matched = []
    for pack_path in sorted(paths.evaluations.glob("*.json")):
        try:
            raw = json.loads(pack_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if (
            isinstance(raw, dict)
            and raw.get("kind") == "graph-pilot-evidence-pack"
            and raw.get("pack_id") == pack_id
        ):
            matched.append(pack_path)
    if not matched:
        raise KnowledgeWorkbenchError("图谱试点证据包不存在")
    if len(matched) > 1:
        raise KnowledgeWorkbenchError("图谱试点证据包 ID 重复，无法安全读取")
    return matched[0]


def _pack_id(pack: dict) -> str:
    identity = {
        key: value for key, value in pack.items() if key != "pack_id"
    }
    canonical = json.dumps(
        identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return f"graphpilot_{sha256_text(canonical)[:32]}"


def _validate_pack_identity(pack: dict) -> None:
    if pack["pack_id"] != _pack_id(pack):
        raise KnowledgeWorkbenchError(
            "图谱试点证据包身份校验失败；内容或来源元数据已被修改"
        )


def _validate_pack_audit(
    database: Database,
    paths: WorkspacePaths,
    pack_path: Path,
    content: str,
    pack: dict,
) -> None:
    relative_path = pack_path.relative_to(paths.root.resolve()).as_posix()
    with database.connect() as connection:
        rows = connection.execute(
            """
            SELECT details_json FROM audit_log
            WHERE event_type = 'graph_pilot_pack_created'
              AND entity_type = 'graph_pilot_pack'
              AND entity_id = ?
            """,
            (pack["pack_id"],),
        ).fetchall()
    content_sha256 = sha256_text(content)
    if not any(
        (
            details := json.loads(row["details_json"])
        ).get("content_sha256")
        == content_sha256
        and details.get("output") == relative_path
        for row in rows
    ):
        raise KnowledgeWorkbenchError(
            "图谱试点证据包缺少匹配的系统生成审计记录"
        )


def _coverage(numerator: int, denominator: int) -> float:
    return round(0.0 if denominator == 0 else numerator / denominator, 6)


def _pagination(limit: int, offset: int) -> tuple[int, int]:
    if limit <= 0 or limit > 50:
        raise ValueError("图谱试点分页 limit 必须在 1 到 50 之间")
    if offset < 0:
        raise ValueError("图谱试点分页 offset 不能为负数")
    return limit, offset


def _required_actor(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise KnowledgeWorkbenchError("actor 不能为空")
    actor = value.strip()
    if len(actor) > 80:
        raise KnowledgeWorkbenchError("actor 不能超过 80 个字符")
    return actor
