from __future__ import annotations

from pathlib import Path

from knowledge_workbench.errors import UnsupportedFormatError
from knowledge_workbench.models import ParseResult

from .office import DocxParser, PdfParser, PptxParser, XlsxParser
from .legacy_word import LegacyDocConverter, LegacyDocParser
from .plain import CsvParser, PlainTextParser


_PARSERS = (
    PlainTextParser(),
    CsvParser(),
    PdfParser(),
    DocxParser(),
    XlsxParser(),
    PptxParser(),
)

_LEGACY_DOC_EXTENSION = ".doc"


def supported_extensions() -> tuple[str, ...]:
    return tuple(
        sorted(
            {
                _LEGACY_DOC_EXTENSION,
                *(extension for parser in _PARSERS for extension in parser.extensions),
            }
        )
    )


def parse_document(
    path: Path,
    *,
    allow_legacy_word_conversion: bool = False,
    legacy_doc_converter: LegacyDocConverter | None = None,
) -> ParseResult:
    suffix = path.suffix.lower()
    if suffix == _LEGACY_DOC_EXTENSION:
        if not allow_legacy_word_conversion:
            raise UnsupportedFormatError(
                "旧版 .doc 需要显式启用本地 Microsoft Word 转换；"
                "请使用 --allow-legacy-word-conversion"
            )
        result = LegacyDocParser(legacy_doc_converter).parse(path)
        if not result.units:
            raise UnsupportedFormatError(f"文件 {path.name} 转换后没有提取到可用文本")
        return result
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
