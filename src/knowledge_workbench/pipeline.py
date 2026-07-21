from __future__ import annotations

from .extraction import FaithfulEvidenceExtractor
from .models import Classification, ParseResult
from .schema_validation import validate_analysis, validate_wiki_generation
from .utils import slugify


def faithful_analysis(
    parsed: ParseResult,
    *,
    document_version_id: str,
    source_sha256: str,
    classification: Classification,
) -> dict:
    candidates = FaithfulEvidenceExtractor().extract(parsed)
    payload = {
        "schema_version": "1.0",
        "source": {
            "document_version_id": document_version_id,
            "sha256": source_sha256,
            "classification": classification.value,
        },
        "provenance": {
            "mode": "faithful",
            "provider": "deterministic",
            "model": None,
            "prompt_version": "faithful-v1",
        },
        "evidence": [
            {
                "candidate_id": f"E{index:04d}",
                "excerpt": candidate.excerpt,
                "locator": candidate.locator,
                "evidence_type": "other",
                "entities": [],
                "concepts": [],
                "projects": [],
                "applicability": {
                    "scope": None,
                    "valid_from": None,
                    "valid_to": None,
                },
                "potential_conflicts": [],
            }
            for index, candidate in enumerate(candidates, start=1)
        ],
    }
    validate_analysis(payload, parsed.units)
    return payload


def faithful_wiki_generation(analysis: dict, *, title: str) -> dict:
    evidence_ids = [item["candidate_id"] for item in analysis["evidence"]]
    payload = {
        "schema_version": "1.0",
        "analysis_schema_version": analysis["schema_version"],
        "pages": [
            {
                "title": title,
                "slug_suggestion": slugify(title),
                "summary": None,
                "conclusions": [],
                "evidence_sections": [
                    {"heading": "待审核原子证据", "evidence_ids": evidence_ids}
                ],
                "links": [],
            }
        ],
        "human_tasks": [
            {
                "task_type": "synthesize",
                "description": "根据已验证证据提炼知识结论；不得加入无证据事实。",
                "evidence_ids": evidence_ids,
            }
        ],
    }
    validate_wiki_generation(payload, analysis)
    return payload

