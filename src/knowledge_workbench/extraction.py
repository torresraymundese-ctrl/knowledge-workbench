from __future__ import annotations

import json
import re

from .models import EvidenceCandidate, ParseResult, ParsedUnit


class FaithfulEvidenceExtractor:
    """Creates reviewable evidence without paraphrasing or model inference."""

    name = "faithful-segmenter-v1"

    def __init__(self, max_chars: int = 1200):
        self.max_chars = max_chars

    def extract(self, parsed: ParseResult) -> tuple[EvidenceCandidate, ...]:
        candidates: list[EvidenceCandidate] = []
        for unit_index, unit in enumerate(parsed.units, start=1):
            for segment_index, excerpt in enumerate(self._segments(unit), start=1):
                locator = dict(unit.locator)
                locator.update(
                    {"unit": unit_index, "segment": segment_index}
                )
                candidates.append(
                    EvidenceCandidate(
                        excerpt=excerpt,
                        locator=locator,
                        extraction_method=self.name,
                    )
                )
        return _merge_duplicate_candidates(candidates)

    def _segments(self, unit: ParsedUnit) -> tuple[str, ...]:
        paragraphs = [
            value.strip()
            for value in re.split(r"\n\s*\n", unit.text)
            if value.strip()
        ]
        output: list[str] = []
        for paragraph in paragraphs:
            if len(paragraph) <= self.max_chars:
                output.append(paragraph)
                continue
            output.extend(self._split_long(paragraph))
        return tuple(output)

    def _split_long(self, value: str) -> list[str]:
        chunks: list[str] = []
        start = 0
        minimum_boundary = max(self.max_chars // 2, 1)
        while len(value) - start > self.max_chars:
            window = value[start : start + self.max_chars]
            boundary = max(
                (
                    window.rfind(character) + 1
                    for character in "。！？.!?；;\n"
                ),
                default=0,
            )
            if boundary < minimum_boundary:
                whitespace = max(window.rfind(" "), window.rfind("\t"))
                boundary = whitespace + 1 if whitespace >= minimum_boundary else 0
            if boundary < minimum_boundary:
                boundary = self.max_chars
            end = start + boundary
            excerpt = value[start:end].strip()
            if excerpt:
                chunks.append(excerpt)
            start = end
            while start < len(value) and value[start].isspace():
                start += 1
        remainder = value[start:].strip()
        if remainder:
            chunks.append(remainder)
        return chunks


def _merge_duplicate_candidates(
    candidates: list[EvidenceCandidate],
) -> tuple[EvidenceCandidate, ...]:
    merged: list[EvidenceCandidate] = []
    indexes: dict[str, int] = {}
    locator_keys: list[set[str]] = []
    for candidate in candidates:
        # Merge only exactly equal extracted text. A locator may be attached to
        # an evidence row only when the stored excerpt can be found there verbatim.
        key = candidate.excerpt
        existing_index = indexes.get(key)
        if existing_index is None:
            indexes[key] = len(merged)
            merged.append(candidate)
            locator_keys.append({_locator_key(item) for item in candidate.locators})
            continue
        existing = merged[existing_index]
        unique_locators = list(existing.locators)
        for locator in candidate.locators:
            locator_key = _locator_key(locator)
            if locator_key not in locator_keys[existing_index]:
                locator_keys[existing_index].add(locator_key)
                unique_locators.append(locator)
        merged[existing_index] = EvidenceCandidate(
            excerpt=existing.excerpt,
            locator=existing.locator,
            extraction_method=existing.extraction_method,
            extraction_model=existing.extraction_model,
            locators=tuple(unique_locators),
        )
    return tuple(merged)


def _locator_key(locator: dict) -> str:
    return json.dumps(locator, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
