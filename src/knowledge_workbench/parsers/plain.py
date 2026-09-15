from __future__ import annotations

import csv
import io
from pathlib import Path

from knowledge_workbench.models import ParsedUnit, ParseResult


def _read_text(path: Path) -> str:
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


class PlainTextParser:
    name = "plain-text"
    version = "1"
    extensions = frozenset({".txt", ".md", ".markdown", ".sql"})

    def parse(self, path: Path) -> ParseResult:
        text = _read_text(path)
        units: list[ParsedUnit] = []
        headings: list[str] = []
        paragraph: list[str] = []
        paragraph_start = 1

        def flush(end_line: int) -> None:
            nonlocal paragraph, paragraph_start
            value = "\n".join(paragraph).strip()
            if value:
                units.append(
                    ParsedUnit(
                        text=value,
                        locator={
                            "line_start": paragraph_start,
                            "line_end": end_line,
                            "heading_path": headings.copy(),
                        },
                    )
                )
            paragraph = []

        lines = text.splitlines()
        for line_number, line in enumerate(lines, start=1):
            stripped = line.strip()
            if path.suffix.lower() in {".md", ".markdown"} and stripped.startswith("#"):
                flush(line_number - 1)
                level = len(stripped) - len(stripped.lstrip("#"))
                title = stripped[level:].strip()
                if title:
                    headings[:] = headings[: level - 1]
                    headings.append(title)
                paragraph_start = line_number + 1
            elif not stripped:
                flush(line_number - 1)
                paragraph_start = line_number + 1
            else:
                if not paragraph:
                    paragraph_start = line_number
                paragraph.append(line)
        flush(len(lines))

        return ParseResult(self.name, self.version, tuple(units))


class CsvParser:
    name = "csv"
    version = "1"
    extensions = frozenset({".csv"})

    def parse(self, path: Path) -> ParseResult:
        text = _read_text(path)
        sample = text[:4096]
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
        except csv.Error:
            dialect = csv.excel
        rows = list(csv.reader(io.StringIO(text), dialect))
        units: list[ParsedUnit] = []
        for row_number, row in enumerate(rows, start=1):
            values = [value.strip() for value in row]
            if not any(values):
                continue
            rendered = " | ".join(
                f"{_column_name(index + 1)}={value}"
                for index, value in enumerate(values)
                if value
            )
            units.append(
                ParsedUnit(
                    rendered,
                    {"row": row_number, "cell_range": f"A{row_number}:{_column_name(len(row))}{row_number}"},
                )
            )
        return ParseResult(self.name, self.version, tuple(units))


def _column_name(number: int) -> str:
    result = ""
    while number:
        number, remainder = divmod(number - 1, 26)
        result = chr(65 + remainder) + result
    return result or "A"
