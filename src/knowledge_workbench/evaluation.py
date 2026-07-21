from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from .errors import KnowledgeWorkbenchError
from .models import Classification
from .parsers import parse_document
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


def evaluate_dataset(dataset_path: Path) -> dict:
    dataset_path = dataset_path.expanduser().resolve()
    try:
        dataset = json.loads(dataset_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise KnowledgeWorkbenchError(f"评测数据集不存在：{dataset_path}") from exc
    except json.JSONDecodeError as exc:
        raise KnowledgeWorkbenchError(f"评测数据集不是有效 JSON：{exc}") from exc
    validate_evaluation_dataset(dataset)

    results: list[CaseMetrics] = []
    for case in dataset["cases"]:
        source = (dataset_path.parent / case["source_path"]).resolve()
        if not source.is_file():
            raise KnowledgeWorkbenchError(
                f"评测用例 {case['case_id']} 的文件不存在：{source}"
            )
        parsed = parse_document(source)
        digest = sha256_file(source)
        analysis = faithful_analysis(
            parsed,
            document_version_id=f"ver_{digest[:32]}",
            source_sha256=digest,
            classification=Classification(case["classification"]),
        )
        generation = faithful_wiki_generation(analysis, title=source.stem)
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
        },
        "cases": [asdict(item) for item in results],
    }
    return report

