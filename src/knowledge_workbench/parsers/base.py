from __future__ import annotations

from pathlib import Path
from typing import Protocol

from knowledge_workbench.models import ParseResult


class DocumentParser(Protocol):
    name: str
    version: str
    extensions: frozenset[str]

    def parse(self, path: Path) -> ParseResult: ...

