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


def _pack_id(pack: dict) -> str:
    identity = {
        key: value for key, value in pack.items() if key != "pack_id"
    }
    canonical = json.dumps(
        identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return f"graphpilot_{sha256_text(canonical)[:32]}"


def _required_actor(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise KnowledgeWorkbenchError("actor 不能为空")
    actor = value.strip()
    if len(actor) > 80:
        raise KnowledgeWorkbenchError("actor 不能超过 80 个字符")
    return actor
