from __future__ import annotations

import json
from collections import defaultdict
from difflib import SequenceMatcher
from itertools import combinations
from pathlib import Path
from typing import Any

from .audit import record_event
from .config import WorkspacePaths
from .conflicts import _classify_conflict, _comparison_signature, _normalize
from .database import Database
from .errors import KnowledgeWorkbenchError
from .schema_validation import (
    validate_conflict_candidate_pack,
    validate_conflict_evaluation_dataset,
)
from .utils import sha256_text, utc_now
from .wiki import write_text_atomic


def create_cross_document_candidate_pack(
    database: Database,
    paths: WorkspacePaths,
    output: Path,
    *,
    actor: str,
    limit: int = 200,
    minimum_similarity: float = 0.55,
) -> dict[str, Any]:
    actor = _required_actor(actor)
    if limit < 1 or limit > 5000:
        raise KnowledgeWorkbenchError("候选数量必须在 1 到 5000 之间")
    if not 0 <= minimum_similarity <= 1:
        raise KnowledgeWorkbenchError("minimum_similarity 必须在 0 到 1 之间")
    output = _validated_output(paths, output)

    with database.connect() as connection:
        rows = connection.execute(
            """
            SELECT e.id, e.excerpt, e.run_ordinal,
                   d.id AS document_id, d.original_name, d.classification,
                   dv.id AS document_version_id, dv.sha256,
                   pr.id AS processing_run_id
            FROM evidence e
            JOIN processing_runs pr
              ON pr.id = e.processing_run_id AND pr.is_current = 1
            JOIN document_versions dv ON dv.id = e.document_version_id
            JOIN documents d
              ON d.id = dv.document_id AND d.current_version_id = dv.id
            WHERE e.status NOT IN ('deprecated', 'archived')
            ORDER BY d.id, e.run_ordinal, e.id
            """
        ).fetchall()
        restricted_count = sum(row["classification"] == "restricted" for row in rows)
        eligible = [row for row in rows if row["classification"] != "restricted"]
        locators = _location_map(connection, [row["id"] for row in eligible])

    candidates = _cross_document_candidates(
        eligible,
        locators,
        minimum_similarity=minimum_similarity,
    )
    total_candidate_count = len(candidates)
    selected = candidates[:limit]
    pack = {
        "schema_version": "1.0",
        "kind": "cross-document-conflict-candidate-pack",
        "pack_id": "pending",
        "generated_at": utc_now(),
        "generated_by": actor,
        "minimum_similarity": minimum_similarity,
        "statistics": {
            "eligible_evidence_count": len(eligible),
            "restricted_evidence_excluded": restricted_count,
            "total_candidate_count": total_candidate_count,
            "exported_candidate_count": len(selected),
            "truncated": total_candidate_count > len(selected),
        },
        "candidates": selected,
    }
    pack["pack_id"] = _pack_id(pack)
    validate_conflict_candidate_pack(pack)
    content = json.dumps(pack, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    write_text_atomic(output, content)
    try:
        with database.transaction() as connection:
            record_event(
                connection,
                "conflict_candidate_pack_created",
                "conflict_candidate_pack",
                pack["pack_id"],
                actor=actor,
                details={
                    "output": output.relative_to(paths.root).as_posix(),
                    "eligible_evidence_count": len(eligible),
                    "restricted_evidence_excluded": restricted_count,
                    "total_candidate_count": total_candidate_count,
                    "exported_candidate_count": len(selected),
                    "minimum_similarity": minimum_similarity,
                },
            )
    except Exception:
        output.unlink(missing_ok=True)
        raise
    return pack


def submit_cross_document_candidate_annotations(
    database: Database,
    paths: WorkspacePaths,
    pack_path: Path,
    *,
    actor: str,
) -> dict[str, Any]:
    actor = _required_actor(actor)
    pack = _read_candidate_pack(paths, pack_path)
    _validate_pack_identity(pack)
    _validate_pack_provenance(database, pack)
    if pack["statistics"]["truncated"]:
        raise KnowledgeWorkbenchError("候选包已截断，不能提交为完整质量基线")
    _validated_labels(pack)
    for candidate in pack["candidates"]:
        review = candidate["review"]
        if review["decision"] is not None or (review["note"] or "").strip():
            raise KnowledgeWorkbenchError(
                f"候选 {candidate['candidate_id']} 在标注提交前不能填写复核结果"
            )
    annotation_sha256 = _annotation_sha256(pack)
    with database.transaction() as connection:
        record_event(
            connection,
            "conflict_candidate_annotations_submitted",
            "conflict_candidate_pack",
            pack["pack_id"],
            actor=actor,
            details={
                "annotation_sha256": annotation_sha256,
                "candidate_count": len(pack["candidates"]),
            },
        )
    return {
        "pack_id": pack["pack_id"],
        "annotation_sha256": annotation_sha256,
        "candidate_count": len(pack["candidates"]),
        "annotator": actor,
    }


def finalize_cross_document_candidate_pack(
    database: Database,
    paths: WorkspacePaths,
    pack_path: Path,
    output: Path,
    *,
    name: str,
    reviewer: str,
) -> dict[str, Any]:
    reviewer = _required_actor(reviewer)
    dataset_name = name.strip()
    if not dataset_name:
        raise KnowledgeWorkbenchError("数据集名称不能为空")
    pack = _read_candidate_pack(paths, pack_path)
    _validate_pack_identity(pack)
    _validate_pack_provenance(database, pack)
    if pack["statistics"]["truncated"]:
        raise KnowledgeWorkbenchError("候选包已截断，不能固化为完整质量基线")

    cases = _validated_labels(pack)
    annotation_sha256 = _annotation_sha256(pack)
    annotator = _submitted_annotator(
        database,
        pack_id=pack["pack_id"],
        annotation_sha256=annotation_sha256,
    )
    if annotator is None:
        raise KnowledgeWorkbenchError("当前候选标签尚未通过 submit-pack 提交审计")
    if annotator == reviewer:
        raise KnowledgeWorkbenchError("标注人与复核人必须不同")
    for candidate in pack["candidates"]:
        review = candidate["review"]
        if review["decision"] != "approved":
            raise KnowledgeWorkbenchError(
                f"候选 {candidate['candidate_id']} 尚未复核通过"
            )
    output = _validated_output(paths, output)
    dataset = {"schema_version": "1.0", "name": dataset_name, "cases": cases}
    validate_conflict_evaluation_dataset(dataset)
    content = json.dumps(dataset, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    write_text_atomic(output, content)
    try:
        with database.transaction() as connection:
            record_event(
                connection,
                "conflict_dataset_finalized",
                "conflict_evaluation_dataset",
                sha256_text(content),
                actor=reviewer,
                details={
                    "output": output.relative_to(paths.root).as_posix(),
                    "pack_sha256": sha256_text(
                        json.dumps(pack, ensure_ascii=False, sort_keys=True)
                    ),
                    "annotation_sha256": annotation_sha256,
                    "case_count": len(cases),
                    "annotator": annotator,
                    "reviewer": reviewer,
                },
            )
    except Exception:
        output.unlink(missing_ok=True)
        raise
    return dataset


def _validated_labels(pack: dict[str, Any]) -> list[dict[str, Any]]:
    cases = []
    for candidate in pack["candidates"]:
        label = candidate["label"]
        expected_conflict = label["expected_conflict"]
        expected_type = label["expected_type"]
        if expected_conflict is None:
            raise KnowledgeWorkbenchError(
                f"候选 {candidate['candidate_id']} 尚未填写冲突标注"
            )
        if expected_conflict and expected_type is None:
            raise KnowledgeWorkbenchError(
                f"候选 {candidate['candidate_id']} 标记为冲突时必须填写类型"
            )
        if not expected_conflict and expected_type is not None:
            raise KnowledgeWorkbenchError(
                f"候选 {candidate['candidate_id']} 标记为非冲突时类型必须为 null"
            )
        cases.append(
            {
                "case_id": candidate["candidate_id"],
                "older": candidate["left"]["excerpt"],
                "newer": candidate["right"]["excerpt"],
                "expected_conflict": expected_conflict,
                "expected_type": expected_type,
            }
        )
    if not cases:
        raise KnowledgeWorkbenchError("候选包不包含可固化的用例")
    return cases


def _read_candidate_pack(paths: WorkspacePaths, pack_path: Path) -> dict[str, Any]:
    pack_path = pack_path.expanduser().resolve()
    try:
        pack_path.relative_to(paths.evaluations.resolve())
    except ValueError as exc:
        raise KnowledgeWorkbenchError(
            "冲突候选包只能从当前 workspace/evaluations 内读取"
        ) from exc
    try:
        pack = json.loads(pack_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise KnowledgeWorkbenchError(f"冲突候选包不存在：{pack_path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise KnowledgeWorkbenchError(f"冲突候选包不是有效 JSON：{exc}") from exc
    validate_conflict_candidate_pack(pack)
    return pack


def _pack_id(pack: dict[str, Any]) -> str:
    identity = {
        key: value
        for key, value in pack.items()
        if key not in {"pack_id", "candidates"}
    }
    identity["candidates"] = [
        {
            key: value
            for key, value in candidate.items()
            if key not in {"label", "review"}
        }
        for candidate in pack["candidates"]
    ]
    canonical = json.dumps(
        identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return "cpack_" + sha256_text(canonical)[:24]


def _validate_pack_identity(pack: dict[str, Any]) -> None:
    if pack["pack_id"] != _pack_id(pack):
        raise KnowledgeWorkbenchError("候选包身份校验失败；候选内容或来源元数据已被修改")


def _validate_pack_provenance(database: Database, pack: dict[str, Any]) -> None:
    expected: dict[str, dict[str, Any]] = {}
    for candidate in pack["candidates"]:
        for side in (candidate["left"], candidate["right"]):
            evidence_id = side["evidence_id"]
            existing = expected.get(evidence_id)
            if existing is not None and existing != side:
                raise KnowledgeWorkbenchError(
                    f"候选包中的证据 {evidence_id} 出现不一致来源投影"
                )
            expected[evidence_id] = side
    with database.connect() as connection:
        created = connection.execute(
            """
            SELECT 1 FROM audit_log
            WHERE event_type = 'conflict_candidate_pack_created'
              AND entity_type = 'conflict_candidate_pack'
              AND entity_id = ?
            LIMIT 1
            """,
            (pack["pack_id"],),
        ).fetchone()
        if not created:
            raise KnowledgeWorkbenchError("候选包缺少生成审计，不能提交或固化")
        current_rows = []
        evidence_ids = sorted(expected)
        for start in range(0, len(evidence_ids), 500):
            batch = evidence_ids[start : start + 500]
            placeholders = ",".join("?" for _ in batch)
            current_rows.extend(
                connection.execute(
                    f"""
                    SELECT e.id, e.excerpt, e.run_ordinal,
                           d.id AS document_id, d.original_name, d.classification,
                           dv.id AS document_version_id,
                           pr.id AS processing_run_id
                    FROM evidence e
                    JOIN processing_runs pr
                      ON pr.id = e.processing_run_id AND pr.is_current = 1
                    JOIN document_versions dv ON dv.id = e.document_version_id
                    JOIN documents d
                      ON d.id = dv.document_id AND d.current_version_id = dv.id
                    WHERE e.id IN ({placeholders})
                      AND e.status NOT IN ('deprecated', 'archived')
                      AND d.classification != 'restricted'
                    """,
                    tuple(batch),
                ).fetchall()
            )
        locators = _location_map(connection, evidence_ids)
    current = {
        row["id"]: _evidence_projection(row, locators) for row in current_rows
    }
    for evidence_id, expected_projection in expected.items():
        if current.get(evidence_id) != expected_projection:
            raise KnowledgeWorkbenchError(
                f"候选证据 {evidence_id} 已过期、受限或来源内容发生变化"
            )


def _annotation_sha256(pack: dict[str, Any]) -> str:
    annotation = [
        {"candidate_id": item["candidate_id"], "label": item["label"]}
        for item in pack["candidates"]
    ]
    canonical = json.dumps(
        annotation, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return sha256_text(canonical)


def _submitted_annotator(
    database: Database,
    *,
    pack_id: str,
    annotation_sha256: str,
) -> str | None:
    with database.connect() as connection:
        rows = connection.execute(
            """
            SELECT actor, details_json FROM audit_log
            WHERE event_type = 'conflict_candidate_annotations_submitted'
              AND entity_type = 'conflict_candidate_pack'
              AND entity_id = ?
            ORDER BY id DESC
            """,
            (pack_id,),
        ).fetchall()
    for row in rows:
        try:
            details = json.loads(row["details_json"])
        except json.JSONDecodeError:
            continue
        if details.get("annotation_sha256") == annotation_sha256:
            return row["actor"]
    return None


def _cross_document_candidates(rows, locators, *, minimum_similarity: float) -> list[dict]:
    blocked_pairs: set[tuple[int, int]] = set()
    blocks: dict[str, list[int]] = defaultdict(list)
    for index, row in enumerate(rows):
        signature = _comparison_signature(row["excerpt"])
        if len(signature) >= 4:
            blocks[f"signature:{signature}"].append(index)
        for shingle in _shingles(_normalize(row["excerpt"])):
            blocks[f"shingle:{shingle}"].append(index)
    for indexes in blocks.values():
        if len(indexes) > 200:
            continue
        for left_index, right_index in combinations(indexes, 2):
            if rows[left_index]["document_id"] == rows[right_index]["document_id"]:
                continue
            blocked_pairs.add((left_index, right_index))

    output = []
    for left_index, right_index in blocked_pairs:
        left = rows[left_index]
        right = rows[right_index]
        left_normalized = _normalize(left["excerpt"])
        right_normalized = _normalize(right["excerpt"])
        similarity = SequenceMatcher(None, left_normalized, right_normalized).ratio()
        if similarity < minimum_similarity:
            continue
        classified = _classify_conflict(left["excerpt"], right["excerpt"])
        evidence_ids = sorted((left["id"], right["id"]))
        candidate_id = "xdoc_" + sha256_text(":".join(evidence_ids))[:20]
        output.append(
            {
                "candidate_id": candidate_id,
                "left": _evidence_projection(left, locators),
                "right": _evidence_projection(right, locators),
                "predicted_conflict": classified is not None,
                "predicted_type": classified[0] if classified else None,
                "similarity": round(similarity, 6),
                "reason": classified[2] if classified else None,
                "label": {
                    "expected_conflict": None,
                    "expected_type": None,
                    "note": None,
                },
                "review": {"decision": None, "note": None},
            }
        )
    output.sort(
        key=lambda item: (
            not item["predicted_conflict"],
            -item["similarity"],
            item["candidate_id"],
        )
    )
    return output


def _evidence_projection(row, locators) -> dict[str, Any]:
    return {
        "evidence_id": row["id"],
        "document_id": row["document_id"],
        "document_name": row["original_name"],
        "document_version_id": row["document_version_id"],
        "processing_run_id": row["processing_run_id"],
        "classification": row["classification"],
        "excerpt": row["excerpt"],
        "locators": locators.get(row["id"], []),
    }


def _location_map(connection, evidence_ids: list[str]) -> dict[str, list[dict]]:
    if not evidence_ids:
        return {}
    output: dict[str, list[dict]] = defaultdict(list)
    for start in range(0, len(evidence_ids), 500):
        batch = evidence_ids[start : start + 500]
        placeholders = ",".join("?" for _ in batch)
        rows = connection.execute(
            f"""
            SELECT evidence_id, locator_json FROM evidence_locations
            WHERE evidence_id IN ({placeholders})
            ORDER BY evidence_id, location_ordinal
            """,
            tuple(batch),
        ).fetchall()
        for row in rows:
            output[row["evidence_id"]].append(json.loads(row["locator_json"]))
    return dict(output)


def _shingles(value: str) -> set[str]:
    if len(value) < 3:
        return {value} if value else set()
    return {value[index : index + 3] for index in range(len(value) - 2)}


def _validated_output(paths: WorkspacePaths, output: Path) -> Path:
    output = output.expanduser().resolve()
    try:
        output.relative_to(paths.raw.resolve())
    except ValueError:
        pass
    else:
        raise KnowledgeWorkbenchError("不能向只读原始资料目录写入冲突标注产物")
    try:
        output.relative_to(paths.evaluations.resolve())
    except ValueError as exc:
        raise KnowledgeWorkbenchError(
            "冲突候选包和评测数据集只能保存到当前 workspace/evaluations 内"
        ) from exc
    if output.exists():
        raise KnowledgeWorkbenchError(f"输出文件已存在，不会覆盖：{output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    return output


def _required_actor(value: str) -> str:
    actor = value.strip()
    if not actor:
        raise KnowledgeWorkbenchError("actor 不能为空")
    return actor
