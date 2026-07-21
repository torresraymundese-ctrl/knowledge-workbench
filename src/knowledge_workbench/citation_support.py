from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher


MINIMUM_CITATION_SUPPORT = 0.15
LOW_CITATION_SUPPORT = 0.25

_NEGATIVE_POLARITY = (
    "禁止",
    "严禁",
    "不得",
    "不允许",
    "不应",
    "无需",
    "不需要",
    "不必",
    "不是",
    "禁用",
)
_POSITIVE_POLARITY = ("允许", "可以", "应当", "应该", "必须", "需要", "启用")
_NUMBER_PATTERN = re.compile(r"\d+(?:\.\d+)?")


def assess_generation_citations(analysis: dict, generation: dict) -> dict:
    evidence = {
        item["candidate_id"]: item["excerpt"] for item in analysis["evidence"]
    }
    conclusions = []
    for page_index, page in enumerate(generation["pages"], start=1):
        for conclusion_index, conclusion in enumerate(page["conclusions"], start=1):
            cited = [
                evidence[evidence_id]
                for evidence_id in conclusion["evidence_ids"]
                if evidence_id in evidence
            ]
            text_assessment = assess_citation_texts(conclusion["text"], cited)
            conclusions.append(
                {
                    "page_index": page_index,
                    "conclusion_index": conclusion_index,
                    "evidence_ids": conclusion["evidence_ids"],
                    "support_score": text_assessment["support_score"],
                    "low_support": (
                        text_assessment["support_score"] < LOW_CITATION_SUPPORT
                    ),
                    "signal_conflict_count": text_assessment[
                        "signal_conflict_count"
                    ],
                }
            )
    scores = [item["support_score"] for item in conclusions]
    return {
        "conclusion_count": len(conclusions),
        "minimum_support_score": min(scores) if scores else None,
        "average_support_score": (
            round(sum(scores) / len(scores), 6) if scores else None
        ),
        "low_support_conclusion_count": sum(
            item["low_support"] for item in conclusions
        ),
        "signal_conflict_conclusion_count": sum(
            item["signal_conflict_count"] > 0 for item in conclusions
        ),
        "conclusions": conclusions,
    }


def citation_support_score(conclusion: str, cited_excerpts: list[str]) -> float:
    return assess_citation_texts(conclusion, cited_excerpts)["support_score"]


def assess_citation_texts(conclusion: str, cited_excerpts: list[str]) -> dict:
    normalized_conclusion = _normalize(conclusion)
    if not normalized_conclusion or not cited_excerpts:
        return {
            "support_score": 0.0,
            "signal_conflict_count": 0,
            "signal_conflicts": [],
        }
    pairs = []
    for excerpt_index, excerpt in enumerate(cited_excerpts, start=1):
        normalized_excerpt = _normalize(excerpt)
        raw_score = _text_similarity(normalized_conclusion, normalized_excerpt)
        conflicts = (
            _claim_signal_conflicts(conclusion, excerpt)
            if raw_score >= MINIMUM_CITATION_SUPPORT
            else []
        )
        pairs.append(
            {
                "excerpt_index": excerpt_index,
                "raw_support_score": round(raw_score, 6),
                "support_score": 0.0 if conflicts else round(raw_score, 6),
                "signal_conflicts": conflicts,
            }
        )
    signal_conflicts = [pair for pair in pairs if pair["signal_conflicts"]]
    return {
        "support_score": (
            0.0
            if signal_conflicts
            else max(pair["support_score"] for pair in pairs)
        ),
        "signal_conflict_count": len(signal_conflicts),
        "signal_conflicts": signal_conflicts,
    }


def _text_similarity(left: str, right: str) -> float:
    if not right:
        return 0.0
    if left in right:
        return 1.0
    if len(right) < 4:
        return 0.0
    if right in left and len(right) >= max(8, len(left) // 2):
        return 1.0
    sequence = SequenceMatcher(None, left, right).ratio()
    left_bigrams = _ngrams(left, 2)
    right_bigrams = _ngrams(right, 2)
    union = left_bigrams | right_bigrams
    jaccard = 0.0 if not union else len(left_bigrams & right_bigrams) / len(union)
    return max(sequence, jaccard)


def _normalize(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).casefold()
    return re.sub(r"[\W_]+", "", value, flags=re.UNICODE)


def _claim_signal_conflicts(conclusion: str, excerpt: str) -> list[str]:
    if _normalize(conclusion) in _normalize(excerpt):
        return []
    conflicts = []
    conclusion_polarity = _polarity(_normalize(conclusion))
    excerpt_polarity = _polarity(_normalize(excerpt))
    if (
        conclusion_polarity
        and excerpt_polarity
        and conclusion_polarity != excerpt_polarity
    ):
        conflicts.append("polarity_mismatch")
    conclusion_numbers = _numbers(conclusion)
    excerpt_numbers = _numbers(excerpt)
    if conclusion_numbers and not conclusion_numbers.issubset(excerpt_numbers):
        conflicts.append("numeric_mismatch")
    return conflicts


def _polarity(value: str) -> str | None:
    if any(token in value for token in _NEGATIVE_POLARITY):
        return "negative"
    if any(token in value for token in _POSITIVE_POLARITY):
        return "positive"
    return None


def _numbers(value: str) -> set[str]:
    normalized = unicodedata.normalize("NFKC", value)
    return set(_NUMBER_PATTERN.findall(normalized))


def _ngrams(value: str, size: int) -> set[str]:
    if len(value) < size:
        return {value} if value else set()
    return {value[index : index + size] for index in range(len(value) - size + 1)}
