from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .models import EvidenceCandidate


@dataclass(frozen=True, slots=True)
class RenderedEvidence:
    id: str
    candidate: EvidenceCandidate


def render_draft(
    *,
    title: str,
    page_id: str,
    revision_id: str,
    revision_number: int,
    document_version_id: str,
    source_name: str,
    source_sha256: str,
    classification: str,
    generated_at: str,
    evidence: tuple[RenderedEvidence, ...],
) -> str:
    frontmatter = [
        "---",
        f"id: {json.dumps(page_id, ensure_ascii=False)}",
        f"revision_id: {json.dumps(revision_id, ensure_ascii=False)}",
        f"revision: {revision_number}",
        "status: draft",
        f"classification: {classification}",
        f"source_version: {json.dumps(document_version_id)}",
        f"source_sha256: {json.dumps(source_sha256)}",
        'generator: "faithful-draft-v2-multilocator"',
        f"generated_at: {json.dumps(generated_at)}",
        "---",
        "",
    ]
    body = [
        f"# {title}",
        "",
        "> 当前页面是证据保真草稿，尚未经过人工审核。内容仅整理原文摘录，不代表正式结论。",
        "",
        "## 来源",
        "",
        f"- 文件：`{source_name}`",
        f"- SHA-256：`{source_sha256}`",
        f"- 文件版本：`{document_version_id}`",
        "",
        "## 原子证据",
        "",
    ]
    for item in evidence:
        locators = json.dumps(
            list(item.candidate.locators), ensure_ascii=False, sort_keys=True
        )
        locator_label = (
            "定位" if len(item.candidate.locators) == 1 else f"定位（{len(item.candidate.locators)} 处）"
        )
        quote = item.candidate.excerpt.replace("\n", "\n> ")
        body.extend(
            [
                f"### {item.id}",
                "",
                f"{locator_label}：`{locators}`",
                "",
                f"> {quote}",
                "",
            ]
        )
    body.extend(
        [
            "## 待审核事项",
            "",
            "- [ ] 确认原文摘录和定位正确",
            "- [ ] 提炼可发布的知识结论",
            "- [ ] 检查与现有正式知识是否冲突",
            "- [ ] 确认资料密级和适用时间",
            "",
        ]
    )
    return "\n".join(frontmatter + body)


def write_text_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8", newline="\n")
    temporary.replace(path)
