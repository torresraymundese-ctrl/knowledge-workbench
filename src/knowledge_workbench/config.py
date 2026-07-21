from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class WorkspacePaths:
    root: Path

    @property
    def database(self) -> Path:
        return self.root / "knowledge.sqlite3"

    @property
    def raw(self) -> Path:
        return self.root / "raw"

    @property
    def evidence(self) -> Path:
        return self.root / "evidence"

    @property
    def analysis(self) -> Path:
        return self.root / "analysis"

    @property
    def wiki(self) -> Path:
        return self.root / "wiki"

    @property
    def wiki_drafts(self) -> Path:
        return self.wiki / "drafts"

    @property
    def wiki_verified(self) -> Path:
        return self.wiki / "verified"

    @property
    def index(self) -> Path:
        return self.root / "index"

    @property
    def logs(self) -> Path:
        return self.root / "logs"

    @property
    def evaluations(self) -> Path:
        return self.root / "evaluations"

    def create(self) -> None:
        for directory in (
            self.root,
            self.raw,
            self.evidence,
            self.analysis,
            self.wiki_drafts,
            self.wiki_verified,
            self.index,
            self.logs,
            self.evaluations,
        ):
            directory.mkdir(parents=True, exist_ok=True)


def resolve_workspace(value: str | Path | None = None) -> WorkspacePaths:
    configured = value or os.getenv("KNOWLEDGE_WORKSPACE") or "workspace"
    return WorkspacePaths(Path(configured).expanduser().resolve())
