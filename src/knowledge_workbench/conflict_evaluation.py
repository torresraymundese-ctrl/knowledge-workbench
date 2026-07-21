from __future__ import annotations

import json
from pathlib import Path

from .conflicts import _classify_conflict
from .errors import KnowledgeWorkbenchError
from .schema_validation import validate_conflict_evaluation_dataset
from .utils import utc_now


def evaluate_conflict_dataset(dataset_path: Path) -> dict:
    dataset_path = dataset_path.expanduser().resolve()
    try:
        dataset = json.loads(dataset_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise KnowledgeWorkbenchError(f"冲突评测数据集不存在：{dataset_path}") from exc
    except json.JSONDecodeError as exc:
        raise KnowledgeWorkbenchError(f"冲突评测数据集不是有效 JSON：{exc}") from exc
    validate_conflict_evaluation_dataset(dataset)

    results = []
    true_positive = false_positive = true_negative = false_negative = 0
    correct_type = 0
    for case in dataset["cases"]:
        classified = _classify_conflict(case["older"], case["newer"])
        predicted_conflict = classified is not None
        predicted_type = classified[0] if classified else None
        expected_conflict = case["expected_conflict"]
        if expected_conflict and predicted_conflict:
            true_positive += 1
            correct_type += predicted_type == case["expected_type"]
        elif expected_conflict:
            false_negative += 1
        elif predicted_conflict:
            false_positive += 1
        else:
            true_negative += 1
        passed = (
            predicted_conflict == expected_conflict
            and predicted_type == case["expected_type"]
        )
        results.append(
            {
                "case_id": case["case_id"],
                "passed": passed,
                "expected_conflict": expected_conflict,
                "expected_type": case["expected_type"],
                "predicted_conflict": predicted_conflict,
                "predicted_type": predicted_type,
                "similarity": classified[1] if classified else None,
                "reason": classified[2] if classified else None,
            }
        )

    actual_positive = true_positive + false_negative
    predicted_positive = true_positive + false_positive
    case_count = len(results)
    return {
        "schema_version": "1.0",
        "dataset_name": dataset["name"],
        "dataset_path": str(dataset_path),
        "evaluated_at": utc_now(),
        "aggregate": {
            "case_count": case_count,
            "passed_cases": sum(result["passed"] for result in results),
            "pass_rate": round(sum(result["passed"] for result in results) / case_count, 6),
            "true_positive": true_positive,
            "false_positive": false_positive,
            "true_negative": true_negative,
            "false_negative": false_negative,
            "precision": round(
                1.0 if predicted_positive == 0 else true_positive / predicted_positive,
                6,
            ),
            "recall": round(
                1.0 if actual_positive == 0 else true_positive / actual_positive,
                6,
            ),
            "type_accuracy": round(
                1.0 if true_positive == 0 else correct_type / true_positive,
                6,
            ),
        },
        "cases": results,
    }
