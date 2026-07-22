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
        sentences = [
            part.strip()
            for part in re.split(r"(?<=[。！？.!?；;])\s*", value)
            if part.strip()
        ]
        chunks: list[str] = []
        current = ""
        for sentence in sentences:
            if len(sentence) > self.max_chars:
                if current:
                    chunks.append(current)
                    current = ""
                chunks.extend(
                    sentence[index : index + self.max_chars]
                    for index in range(0, len(sentence), self.max_chars)
                )
            elif current and len(current) + len(sentence) > self.max_chars:
                chunks.append(current)
                current = sentence
            else:
                current += sentence
        if current:
            chunks.append(current)
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
