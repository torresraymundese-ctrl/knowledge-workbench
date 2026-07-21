from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from .database import Database
from .citation_support import assess_generation_citations
from .errors import KnowledgeWorkbenchError
from .models import Classification
from .parsers import parse_document
from .parsers.legacy_word import LegacyDocConverter
from .pipeline import faithful_analysis, faithful_wiki_generation
from .schema_validation import validate_evaluation_dataset
from .utils import sha256_file, utc_now


@dataclass(frozen=True, slots=True)
class CaseMetrics:
    case_id: str
    passed: bool
    evidence_count: int
    required_evidence_count: int
    required_evidence_found: int
    evidence_coverage: float
    traceability_rate: float
    duplicate_rate: float
    forbidden_hits: tuple[str, ...]
    missing_required_evidence: tuple[str, ...]
    conclusion_count: int
    minimum_citation_support: float | None
    average_citation_support: float | None
    low_support_conclusion_count: int
    signal_conflict_conclusion_count: int


def evaluate_dataset(
    dataset_path: Path,
    *,
    allow_legacy_word_conversion: bool = False,
    legacy_doc_converter: LegacyDocConverter | None = None,
) -> dict:
    dataset_path = dataset_path.expanduser().resolve()
    try:
        dataset = json.loads(dataset_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise KnowledgeWorkbenchError(f"评测数据集不存在：{dataset_path}") from exc
    except json.JSONDecodeError as exc:
        raise KnowledgeWorkbenchError(f"评测数据集不是有效 JSON：{exc}") from exc
    validate_evaluation_dataset(dataset, require_ready=True)

    results: list[CaseMetrics] = []
    for case in dataset["cases"]:
        source = (dataset_path.parent / case["source_path"]).resolve()
        if not source.is_file():
            raise KnowledgeWorkbenchError(
                f"评测用例 {case['case_id']} 的文件不存在：{source}"
            )
        parsed = parse_document(
            source,
            allow_legacy_word_conversion=allow_legacy_word_conversion,
            legacy_doc_converter=legacy_doc_converter,
        )
        digest = sha256_file(source)
        analysis = faithful_analysis(
            parsed,
            document_version_id=f"ver_{digest[:32]}",
            source_sha256=digest,
            classification=Classification(case["classification"]),
        )
        generation = faithful_wiki_generation(analysis, title=source.stem)
        citation_assessment = assess_generation_citations(analysis, generation)
        excerpts = [item["excerpt"] for item in analysis["evidence"]]
        required = [
            item["text"] for item in case["expected_evidence"] if item["required"]
        ]
        missing = [
            expected
            for expected in required
            if not any(expected in excerpt for excerpt in excerpts)
        ]
        traceable = sum(
            1
            for excerpt in excerpts
            if any(excerpt in unit.text for unit in parsed.units)
        )
        normalized = [" ".join(excerpt.split()).casefold() for excerpt in excerpts]
        duplicate_rate = (
            0.0
            if not normalized
            else 1.0 - (len(set(normalized)) / len(normalized))
        )
        output_text = json.dumps(
            {"analysis": analysis, "generation": generation},
            ensure_ascii=False,
            sort_keys=True,
        )
        forbidden_hits = tuple(
            value for value in case["forbidden_substrings"] if value in output_text
        )
        coverage = 1.0 if not required else (len(required) - len(missing)) / len(required)
        traceability_rate = 1.0 if not excerpts else traceable / len(excerpts)
        passed = (
            coverage == 1.0
            and traceability_rate == 1.0
            and duplicate_rate <= case["max_duplicate_rate"]
            and not forbidden_hits
        )
        results.append(
            CaseMetrics(
                case_id=case["case_id"],
                passed=passed,
                evidence_count=len(excerpts),
                required_evidence_count=len(required),
                required_evidence_found=len(required) - len(missing),
                evidence_coverage=round(coverage, 6),
                traceability_rate=round(traceability_rate, 6),
                duplicate_rate=round(duplicate_rate, 6),
                forbidden_hits=forbidden_hits,
                missing_required_evidence=tuple(missing),
                conclusion_count=citation_assessment["conclusion_count"],
                minimum_citation_support=citation_assessment["minimum_support_score"],
                average_citation_support=citation_assessment["average_support_score"],
                low_support_conclusion_count=citation_assessment[
                    "low_support_conclusion_count"
                ],
                signal_conflict_conclusion_count=citation_assessment[
                    "signal_conflict_conclusion_count"
                ],
            )
        )

    total_required = sum(item.required_evidence_count for item in results)
    total_found = sum(item.required_evidence_found for item in results)
    total_evidence = sum(item.evidence_count for item in results)
    report = {
        "schema_version": "1.0",
        "dataset_name": dataset["name"],
        "dataset_path": str(dataset_path),
        "evaluated_at": utc_now(),
        "mode": "faithful",
        "aggregate": {
            "case_count": len(results),
            "passed_cases": sum(item.passed for item in results),
            "pass_rate": round(sum(item.passed for item in results) / len(results), 6),
            "required_evidence_coverage": round(
                1.0 if total_required == 0 else total_found / total_required, 6
            ),
            "evidence_count": total_evidence,
            "all_evidence_traceable": all(
                item.traceability_rate == 1.0 for item in results
            ),
            "conclusion_count": sum(item.conclusion_count for item in results),
            "low_support_conclusion_count": sum(
                item.low_support_conclusion_count for item in results
            ),
            "signal_conflict_conclusion_count": sum(
                item.signal_conflict_conclusion_count for item in results
            ),
        },
        "cases": [asdict(item) for item in results],
    }
    return report


def build_labeling_candidate_pack(
    database: Database,
    dataset_path: Path,
    *,
    candidates_per_case: int = 20,
) -> dict:
    if candidates_per_case < 1:
        raise KnowledgeWorkbenchError("每个评测用例的候选证据数量必须至少为 1")
    dataset_path = dataset_path.expanduser().resolve()
    try:
        dataset = json.loads(dataset_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise KnowledgeWorkbenchError(f"评测数据集不存在：{dataset_path}") from exc
    except json.JSONDecodeError as exc:
        raise KnowledgeWorkbenchError(f"评测数据集不是有效 JSON：{exc}") from exc
    validate_evaluation_dataset(dataset)

    cases: list[dict] = []
    with database.connect() as connection:
        for case in dataset["cases"]:
            source = (dataset_path.parent / case["source_path"]).resolve()
            if not source.is_file():
                raise KnowledgeWorkbenchError(
                    f"标注用例 {case['case_id']} 的文件不存在：{source}"
                )
            digest = sha256_file(source)
            rows = connection.execute(
                """
                SELECT e.id, e.run_ordinal, e.excerpt, e.locator_json, e.status,
                       pr.id AS processing_run_id, pr.parser_name, pr.parser_version,
                       dv.id AS document_version_id, d.classification
                FROM document_versions dv
                JOIN documents d
                  ON d.id = dv.document_id AND d.current_version_id = dv.id
                JOIN processing_runs pr
                  ON pr.document_version_id = dv.id AND pr.is_current = 1
                JOIN evidence e ON e.processing_run_id = pr.id
                WHERE dv.sha256 = ?
                ORDER BY e.run_ordinal
                """,
                (digest,),
            ).fetchall()
            if not rows:
                raise KnowledgeWorkbenchError(
                    f"标注用例 {case['case_id']} 没有已导入的当前证据；请先导入该文件"
                )
            if rows[0]["classification"] != case["classification"]:
                raise KnowledgeWorkbenchError(
                    f"标注用例 {case['case_id']} 的密级与数据库不一致："
                    f"{case['classification']} != {rows[0]['classification']}"
                )
            selected = _evenly_spaced(rows, candidates_per_case)
            cases.append(
                {
                    "case_id": case["case_id"],
                    "source_path": case["source_path"],
                    "source_sha256": digest,
                    "classification": case["classification"],
                    "document_version_id": rows[0]["document_version_id"],
                    "processing_run_id": rows[0]["processing_run_id"],
                    "parser_name": rows[0]["parser_name"],
                    "parser_version": rows[0]["parser_version"],
                    "total_current_evidence": len(rows),
                    "candidate_evidence": [
                        {
                            "evidence_id": row["id"],
                            "run_ordinal": row["run_ordinal"],
                            "status": row["status"],
                            "excerpt": row["excerpt"],
                            "locator": json.loads(row["locator_json"]),
                        }
                        for row in selected
                    ],
                }
            )
    return {
        "schema_version": "1.0",
        "kind": "labeling-candidate-pack",
        "dataset_name": dataset["name"],
        "generated_at": utc_now(),
        "selection_method": "evenly-spaced-current-document-version-and-processing-run",
        "warning": "候选证据仅用于人工定位，不代表正确答案，不能直接作为黄金标注。",
        "candidates_per_case": candidates_per_case,
        "cases": cases,
    }


def _evenly_spaced(rows, limit: int):
    if len(rows) <= limit:
        return list(rows)
    if limit == 1:
        return [rows[0]]
    indexes = {
        round(position * (len(rows) - 1) / (limit - 1))
        for position in range(limit)
    }
    return [rows[index] for index in sorted(indexes)]
