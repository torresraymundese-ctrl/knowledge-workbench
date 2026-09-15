from __future__ import annotations

from pathlib import Path


KNOWLEDGE_DOMAINS = (
    "business",
    "policy",
    "technical",
    "template",
    "example",
    "process",
)


def infer_document_purpose(source: str | Path) -> str:
    """Keep development material available for engineering context, not business use."""

    normalized = str(source).replace("\\", "/").casefold()
    parts = {part for part in normalized.split("/") if part}
    if "开发数据" in parts:
        return "development_fixture"
    return "production"


_POLICY_PATH_MARKERS = ("/政策文件/",)
_POLICY_NAME_MARKERS = (
    "教育部",
    "文旅部",
    "文化和旅游部",
    "教育局",
    "服务要求",
    "安全规范",
    "示范合同",
)
_TEMPLATE_NAME_MARKERS = ("模板", "待确认", "核对清单")
_EXAMPLE_NAME_MARKERS = ("演示", "示范研学路线")
_BUSINESS_NAMES = frozenset({"product.md"})
_BUSINESS_NAME_PREFIXES = ("prd",)

_PROCESS_PATH_MARKERS = (
    "/archive/",
    "/archives/",
    "/backup/",
    "/backups/",
    "/docs/qa/",
    "/tests/",
    "/工作计划/",
    "/归档/",
    "/备份/",
)
_PROCESS_NAMES = frozenset(
    {
        "agents.md",
        "claude.md",
        "design-qa.md",
        "frontend-ui-v1.0.md",
        "readme.md",
        "readme-运行说明.md",
    }
)
_PROCESS_NAME_MARKERS = (
    "工作计划",
    "交付清单",
    "交付说明",
    "验收报告",
    "里程碑报告",
    "完成度评审",
)
_TECHNICAL_PATH_MARKERS = (
    "/开发数据/",
    "/ddl/",
    "/详设/",
    "/bpmn/",
)
_TECHNICAL_NAME_MARKERS = (
    "api",
    "ddl",
    "sql",
    "接口设计",
    "技术架构",
    "技术选型",
    "数据字典",
    "时序图",
    "状态机",
    "后端对接",
)


def infer_knowledge_domain(
    source: str | Path,
    *,
    file_name: str | None = None,
) -> str:
    """Classify current platform material without treating all dev files as fixtures."""

    raw = str(source).replace("\\", "/")
    normalized = f"/{raw.strip('/').casefold()}/"
    name = (file_name or Path(raw).name).casefold()
    if any(marker in normalized for marker in _POLICY_PATH_MARKERS) or any(
        marker.casefold() in name for marker in _POLICY_NAME_MARKERS
    ):
        return "policy"
    if any(marker.casefold() in name for marker in _EXAMPLE_NAME_MARKERS):
        return "example"
    if any(marker.casefold() in name for marker in _TEMPLATE_NAME_MARKERS):
        return "template"
    if name in _BUSINESS_NAMES or name.startswith(_BUSINESS_NAME_PREFIXES):
        return "business"
    if (
        name in _PROCESS_NAMES
        or any(marker in normalized for marker in _PROCESS_PATH_MARKERS)
        or any(marker.casefold() in name for marker in _PROCESS_NAME_MARKERS)
    ):
        return "process"
    if (
        Path(name).suffix == ".sql"
        or any(marker in normalized for marker in _TECHNICAL_PATH_MARKERS)
        or any(marker.casefold() in name for marker in _TECHNICAL_NAME_MARKERS)
    ):
        return "technical"
    return "business"
