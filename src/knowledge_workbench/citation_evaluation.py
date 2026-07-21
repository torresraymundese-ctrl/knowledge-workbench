from __future__ import annotations

import json
from pathlib import Path

from .citation_support import (
    LOW_CITATION_SUPPORT,
    MINIMUM_CITATION_SUPPORT,
    assess_citation_texts,
)
from .errors import KnowledgeWorkbenchError
from .schema_validation import validate_citation_evaluation_dataset
from .utils import utc_now


def evaluate_citation_dataset(dataset_path: Path) -> dict:
    dataset_path = dataset_path.expanduser().resolve()
    try:
        dataset = json.loads(dataset_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise KnowledgeWorkbenchError(
            f"引用支撑评测数据集不存在：{dataset_path}"
        ) from exc
    except json.JSONDecodeError as exc:
        raise KnowledgeWorkbenchError(
            f"引用支撑评测数据集不是有效 JSON：{exc}"
        ) from exc
    validate_citation_evaluation_dataset(dataset)

    results = []
    true_positive = false_positive = true_negative = false_negative = 0
    for case in dataset["cases"]:
        text_assessment = assess_citation_texts(
            case["conclusion"], case["cited_excerpts"]
        )
        score = text_assessment["support_score"]
        predicted_supported = (
            score >= LOW_CITATION_SUPPORT
            and text_assessment["signal_conflict_count"] == 0
        )
        expected_supported = case["expected_supported"]
        if expected_supported and predicted_supported:
            true_positive += 1
        elif expected_supported:
            false_negative += 1
        elif predicted_supported:
            false_positive += 1
        else:
            true_negative += 1
        results.append(
            {
                "case_id": case["case_id"],
                "passed": predicted_supported == expected_supported,
                "expected_supported": expected_supported,
                "predicted_supported": predicted_supported,
                "support_score": round(score, 6),
                "low_support": score < LOW_CITATION_SUPPORT,
                "cited_excerpt_count": len(case["cited_excerpts"]),
                "signal_conflict_count": text_assessment[
                    "signal_conflict_count"
                ],
            }
        )

    actual_positive = true_positive + false_negative
    predicted_positive = true_positive + false_positive
    case_count = len(results)
    passed_cases = sum(result["passed"] for result in results)
    return {
        "schema_version": "1.0",
        "dataset_name": dataset["name"],
        "dataset_path": str(dataset_path),
        "evaluated_at": utc_now(),
        "algorithm": {
            "name": "deterministic-textual-citation-support",
            "hard_rejection_threshold": MINIMUM_CITATION_SUPPORT,
            "supported_prediction_threshold": LOW_CITATION_SUPPORT,
        },
        "aggregate": {
            "case_count": case_count,
            "passed_cases": passed_cases,
            "pass_rate": round(passed_cases / case_count, 6),
            "true_positive": true_positive,
            "false_positive": false_positive,
            "true_negative": true_negative,
            "false_negative": false_negative,
            "signal_conflict_cases": sum(
                result["signal_conflict_count"] > 0 for result in results
            ),
            "precision": round(
                1.0 if predicted_positive == 0 else true_positive / predicted_positive,
                6,
            ),
            "recall": round(
                1.0 if actual_positive == 0 else true_positive / actual_positive,
                6,
            ),
        },
        "cases": results,
    }
