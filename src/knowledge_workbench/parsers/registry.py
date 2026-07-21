from __future__ import annotations

from pathlib import Path

from knowledge_workbench.errors import UnsupportedFormatError
from knowledge_workbench.models import ParseResult

from .office import DocxParser, PdfParser, PptxParser, XlsxParser
from .plain import CsvParser, PlainTextParser


_PARSERS = (
    PlainTextParser(),
    CsvParser(),
    PdfParser(),
    DocxParser(),
    XlsxParser(),
    PptxParser(),
)


def supported_extensions() -> tuple[str, ...]:
    return tuple(sorted(extension for parser in _PARSERS for extension in parser.extensions))


def parse_document(path: Path) -> ParseResult:
    suffix = path.suffix.lower()
    for parser in _PARSERS:
        if suffix in parser.extensions:
            result = parser.parse(path)
            if not result.units:
                raise UnsupportedFormatError(
                    f"文件 {path.name} 没有提取到可用文本；扫描件需等待 OCR 阶段"
                )
            return result
    extensions = ", ".join(supported_extensions())
    raise UnsupportedFormatError(f"暂不支持 {suffix or '无扩展名'}；已支持：{extensions}")

