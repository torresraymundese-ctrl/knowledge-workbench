from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from knowledge_workbench.errors import KnowledgeWorkbenchError, MissingDependencyError
from knowledge_workbench.models import ParsedUnit, ParseResult
from knowledge_workbench.utils import sha256_file

from .office import DocxParser


@dataclass(frozen=True, slots=True)
class ConversionMetadata:
    tool_name: str
    tool_version: str


class LegacyDocConverter(Protocol):
    def convert(self, source: Path, output: Path) -> ConversionMetadata: ...


class WordComDocConverter:
    """Converts a legacy Word file locally with an isolated hidden Word instance."""

    tool_name = "microsoft-word-com"

    def __init__(self, *, timeout_seconds: int = 90):
        self.timeout_seconds = timeout_seconds

    def convert(self, source: Path, output: Path) -> ConversionMetadata:
        if os.name != "nt":
            raise MissingDependencyError("旧版 .doc 转换仅在安装 Microsoft Word 的 Windows 上可用")
        powershell = shutil.which("powershell") or shutil.which("powershell.exe")
        word_path = find_word_executable()
        if powershell is None or word_path is None:
            raise MissingDependencyError(
                "未找到 Microsoft Word 或 Windows PowerShell，无法转换旧版 .doc"
            )

        output.parent.mkdir(parents=True, exist_ok=True)
        environment = os.environ.copy()
        environment.update(
            {
                "KW_DOC_SOURCE": str(source),
                "KW_DOC_OUTPUT": str(output),
                "KW_WORD_PATH": str(word_path),
            }
        )
        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            completed = subprocess.run(
                [
                    powershell,
                    "-NoLogo",
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    _WORD_CONVERSION_SCRIPT,
                ],
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=environment,
                timeout=self.timeout_seconds,
                creationflags=creation_flags,
            )
        except subprocess.TimeoutExpired as exc:
            raise KnowledgeWorkbenchError(
                f"Microsoft Word 转换超过 {self.timeout_seconds} 秒，已终止等待"
            ) from exc
        if completed.returncode != 0 or not output.is_file():
            detail = (completed.stderr or completed.stdout).strip()
            if len(detail) > 500:
                detail = detail[:497] + "..."
            raise KnowledgeWorkbenchError(
                f"Microsoft Word 转换 .doc 失败：{detail or '未生成 DOCX 文件'}"
            )
        version = completed.stdout.strip().splitlines()[-1] if completed.stdout.strip() else "unknown"
        return ConversionMetadata(self.tool_name, version)


class LegacyDocParser:
    name = "legacy-doc-conversion"
    version = "1"
    extensions = frozenset({".doc"})

    def __init__(self, converter: LegacyDocConverter | None = None):
        self.converter = converter or WordComDocConverter()

    def parse(self, path: Path) -> ParseResult:
        with tempfile.TemporaryDirectory(prefix="knowledge-legacy-doc-") as temporary:
            converted = Path(temporary) / f"{path.stem}.docx"
            metadata = self.converter.convert(path, converted)
            parsed = DocxParser().parse(converted)
            converted_sha256 = sha256_file(converted)
            units = tuple(
                ParsedUnit(
                    unit.text,
                    {
                        **unit.locator,
                        "source_format": "doc",
                        "converted_format": "docx",
                        "conversion_tool": metadata.tool_name,
                        "conversion_tool_version": metadata.tool_version,
                        "converted_sha256": converted_sha256,
                    },
                )
                for unit in parsed.units
            )
        return ParseResult(
            f"{metadata.tool_name}+{parsed.parser_name}",
            f"{metadata.tool_version}/docx-{parsed.parser_version}",
            units,
        )


def find_word_executable() -> Path | None:
    configured = os.getenv("KNOWLEDGE_WORD_PATH")
    candidates = [
        Path(configured).expanduser() if configured else None,
        Path(r"C:\Program Files\Microsoft Office\root\Office16\WINWORD.EXE"),
        Path(r"C:\Program Files (x86)\Microsoft Office\root\Office16\WINWORD.EXE"),
    ]
    return next((candidate.resolve() for candidate in candidates if candidate and candidate.is_file()), None)


_WORD_CONVERSION_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$OutputEncoding = [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$source = [Environment]::GetEnvironmentVariable('KW_DOC_SOURCE')
$output = [Environment]::GetEnvironmentVariable('KW_DOC_OUTPUT')
$wordPath = [Environment]::GetEnvironmentVariable('KW_WORD_PATH')
$word = $null
$document = $null
try {
    $word = New-Object -ComObject Word.Application
    $word.Visible = $false
    $word.DisplayAlerts = 0
    $word.AutomationSecurity = 3
    # This optional COM property is not exposed by every Word version.
    $sendPersonalInformation = $word.Options.PSObject.Properties['SendPersonalInformation']
    if ($null -ne $sendPersonalInformation) {
        $word.Options.SendPersonalInformation = $false
    }
    $document = $word.Documents.Open($source, $false, $true, $false)
    $document.SaveAs2($output, 16)
    $version = (Get-Item -LiteralPath $wordPath).VersionInfo.FileVersion
    [Console]::Out.WriteLine($version)
}
finally {
    if ($null -ne $document) {
        $saveChanges = [ref]0
        $document.Close($saveChanges)
        [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($document)
    }
    if ($null -ne $word) {
        $saveChanges = [ref]0
        $word.Quit($saveChanges)
        [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($word)
    }
    [GC]::Collect()
    [GC]::WaitForPendingFinalizers()
}
"""
