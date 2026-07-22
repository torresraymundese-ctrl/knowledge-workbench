from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class Classification(StrEnum):
    PUBLIC = "public"
    INTERNAL = "internal"
    CONFIDENTIAL = "confidential"
    RESTRICTED = "restricted"


class EvidenceStatus(StrEnum):
    DRAFT = "draft"
    REVIEWING = "reviewing"
    VERIFIED = "verified"
    CONFLICTED = "conflicted"
    DEPRECATED = "deprecated"
    ARCHIVED = "archived"


class RevisionStatus(StrEnum):
    DRAFT = "draft"
    REVIEWING = "reviewing"
    VERIFIED = "verified"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"
    ARCHIVED = "archived"


class TaskStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    RETRYING = "retrying"
    DONE = "done"
    FAILED = "failed"


class ConflictStatus(StrEnum):
    PENDING = "pending"
    REVIEWING = "reviewing"
    RESOLVED = "resolved"
    DISMISSED = "dismissed"


@dataclass(frozen=True, slots=True)
class ParsedUnit:
    text: str
    locator: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ParseResult:
    parser_name: str
    parser_version: str
    units: tuple[ParsedUnit, ...]


@dataclass(frozen=True, slots=True)
class EvidenceCandidate:
    excerpt: str
    locator: dict[str, Any]
    extraction_method: str
    extraction_model: str | None = None
    locators: tuple[dict[str, Any], ...] = ()

    def __post_init__(self) -> None:
        locators = self.locators or (self.locator,)
        if locators[0] != self.locator:
            raise ValueError("locator must equal the first item in locators")
        object.__setattr__(self, "locators", tuple(dict(item) for item in locators))
        object.__setattr__(self, "locator", dict(self.locator))


@dataclass(frozen=True, slots=True)
class IngestResult:
    document_id: str
    version_id: str
    sha256: str
    duplicate: bool
    evidence_count: int
    page_id: str | None
    revision_id: str | None
    conflict_count: int = 0
    processing_run_id: str | None = None
    reprocessed: bool = False


@dataclass(frozen=True, slots=True)
class ClaimedTask:
    id: str
    task_type: str
    payload: dict[str, Any]
    attempts: int
    max_attempts: int
