# Document Access and Corpus Review Enhancements Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让使用者可以安全打开正式资料只读副本、默认使用已绑定的 DeepSeek、通过资料地图统计卡筛选全库文件，并在范围判断前阅读代表性正文和关联文件。

**Architecture:** SQLite 迁移 18 为资料地图增加受控正文预览；扫描阶段只在本机生成预览，读取接口通过固定 `view` 枚举筛选。正式资料通过受控 POST 路由打开 `workspace/raw/` 当前只读副本，前端只提交稳定 ID，不接触文件路径。

**Tech Stack:** Python 3.11、SQLite、`unittest`/pytest、原生 HTML/CSS/JavaScript、Windows `os.startfile`

## Global Constraints

- 开始每个任务前阅读并遵守 `KNOWLEDGE_RULES.md`。
- 所有生产代码修改必须先写失败测试并确认失败原因，再写最小实现。
- 原始资料和 `workspace/raw/` 副本不得被本系统改写。
- 浏览器不得接收绝对路径、`stored_path`、凭据正文或 `restricted` 正文。
- DeepSeek 默认勾选不得绕过后端密级、一次请求授权、模型输出校验和审计规则。
- 资料扫描预览只使用本机解析器，不调用云模型。
- 数据库变更必须通过可重复执行的迁移完成。
- 不重构与本目标无关的现有模块，不拆分大型 `app.js`。
- 不提交 `workspace/`、密钥、客户资料或本地模型输出。

---

### Task 1: SQLite Migration 18 for Corpus Content Previews

**Files:**
- Modify: `src/knowledge_workbench/database.py:9`
- Modify: `src/knowledge_workbench/database.py:906-1005`
- Test: `tests/test_database_migrations.py:16-330`

**Interfaces:**
- Consumes: existing `corpus_files` table from migration 15.
- Produces: `SCHEMA_VERSION = 18`, `MIGRATION_18`, and a non-null `corpus_files.content_preview_json` array column.

- [ ] **Step 1: Write the failing migration assertions**

In the existing comprehensive initialization test, add:

```python
corpus_columns = {
    row["name"]
    for row in connection.execute(
        "PRAGMA table_info(corpus_files)"
    ).fetchall()
}
self.assertIn("content_preview_json", corpus_columns)
self.assertEqual(
    connection.execute(
        "SELECT COUNT(*) FROM schema_migrations WHERE version = 18"
    ).fetchone()[0],
    1,
)
```

After the repeated `database.initialize(...)`, also assert that version 18 still appears exactly once.

- [ ] **Step 2: Run the migration test and verify RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_database_migrations.py -q
```

Expected: FAIL because `content_preview_json` and schema migration 18 do not exist.

- [ ] **Step 3: Add the minimal migration**

In `database.py`:

```python
SCHEMA_VERSION = 18

MIGRATION_18 = """
ALTER TABLE corpus_files
    ADD COLUMN content_preview_json TEXT NOT NULL DEFAULT '[]'
    CHECK (
        json_valid(content_preview_json)
        AND json_type(content_preview_json) = 'array'
    );
"""
```

Append `(18, MIGRATION_18)` to the migration tuple in `Database.initialize()`.

- [ ] **Step 4: Run the migration test and verify GREEN**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_database_migrations.py -q
```

Expected: PASS, including the repeated-initialization assertion.

- [ ] **Step 5: Commit the migration**

```powershell
git add src/knowledge_workbench/database.py tests/test_database_migrations.py
git commit -m "feat: add corpus content preview migration"
```

---

### Task 2: Generate and Project Safe Representative Content Previews

**Files:**
- Modify: `src/knowledge_workbench/corpus_map.py:104-395`
- Modify: `src/knowledge_workbench/corpus_map.py:900-990`
- Modify: `src/knowledge_workbench/corpus_map.py:1090-1140`
- Test: `tests/test_corpus_map.py:21-560`

**Interfaces:**
- Consumes: parsed units exposing `.text` and `.locator`.
- Produces:
  - `_content_preview(units) -> list[dict[str, str]]`
  - `_preview_label(locator: dict[str, Any], ordinal: int) -> str`
  - `content_preview` in detailed corpus card projections.

- [ ] **Step 1: Write failing preview-generation tests**

Add a test that scans a structured Markdown document and verifies bounded, readable previews:

```python
def test_scan_builds_bounded_representative_content_preview(self):
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        source = root / "资料"
        source.mkdir()
        (source / "制度.md").write_text(
            "# 适用范围\n本制度适用于研学机构。\n\n"
            "# 安全要求\n活动前必须完成风险评估和应急预案。\n\n"
            "# 费用与退款\n退款应按合同约定处理。\n",
            encoding="utf-8",
        )
        paths = WorkspacePaths(root / "workspace")
        database = initialize_workspace(paths)

        scan_corpus_source(database, source, actor="mapper")
        file_id = latest_corpus_map(database)["items"][0]["file_id"]
        detail = corpus_file_card(database, file_id)

        self.assertGreaterEqual(len(detail["content_preview"]), 2)
        self.assertLessEqual(len(detail["content_preview"]), 8)
        self.assertTrue(
            any("安全要求" in item["label"] for item in detail["content_preview"])
        )
        self.assertTrue(
            any("风险评估" in item["text"] for item in detail["content_preview"])
        )
        self.assertLessEqual(
            sum(len(item["text"]) for item in detail["content_preview"]),
            2400,
        )
```

Extend `test_content_credential_risk_quarantines_an_already_imported_document`:

```python
self.assertEqual(
    corpus_file_card(database, card["file_id"])["content_preview"],
    [],
)
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest `
  tests\test_corpus_map.py::CorpusMapTests::test_scan_builds_bounded_representative_content_preview `
  tests\test_corpus_map.py::CorpusMapTests::test_content_credential_risk_quarantines_an_already_imported_document -q
```

Expected: FAIL because detailed cards do not expose `content_preview`.

- [ ] **Step 3: Implement bounded local preview selection**

Add constants:

```python
MAX_CONTENT_PREVIEW_ITEMS = 8
MAX_CONTENT_PREVIEW_ITEM_CHARS = 360
MAX_CONTENT_PREVIEW_TOTAL_CHARS = 2400
```

Add helpers:

```python
def _preview_label(locator: dict[str, Any], ordinal: int) -> str:
    heading_path = locator.get("heading_path")
    if isinstance(heading_path, list):
        headings = [
            _clean_text(str(value))
            for value in heading_path
            if _clean_text(str(value))
        ]
        if headings:
            return " / ".join(headings[-2:])
    if isinstance(locator.get("page"), int):
        return f"第 {locator['page']} 页"
    if locator.get("sheet"):
        cell_range = locator.get("cell_range")
        suffix = f" · {cell_range}" if cell_range else ""
        return f"工作表“{locator['sheet']}”{suffix}"
    if isinstance(locator.get("slide"), int):
        return f"第 {locator['slide']} 张幻灯片"
    return f"正文片段 {ordinal}"


def _content_preview(units) -> list[dict[str, str]]:
    prepared: list[dict[str, str | bool]] = []
    for ordinal, unit in enumerate(units, start=1):
        text = _clip(_clean_text(unit.text), MAX_CONTENT_PREVIEW_ITEM_CHARS)
        if not text:
            continue
        label = _preview_label(unit.locator, ordinal)
        prepared.append(
            {
                "label": label,
                "text": text,
                "structured": not label.startswith("正文片段 "),
            }
        )
    structured_items: list[dict[str, str | bool]] = []
    seen_labels: set[str] = set()
    for item in prepared:
        label = str(item["label"])
        if not item["structured"] or label in seen_labels:
            continue
        structured_items.append(item)
        seen_labels.add(label)
    if structured_items:
        selected = structured_items[:MAX_CONTENT_PREVIEW_ITEMS]
    elif prepared:
        positions = sorted({0, len(prepared) // 2, len(prepared) - 1})
        selected = [prepared[index] for index in positions]
    else:
        selected = []
    result: list[dict[str, str]] = []
    total = 0
    for item in selected:
        remaining = MAX_CONTENT_PREVIEW_TOTAL_CHARS - total
        if remaining <= 0:
            break
        text = str(item["text"])[:remaining]
        result.append({"label": str(item["label"]), "text": text})
        total += len(text)
    return result
```

- [ ] **Step 4: Persist and project the preview**

Return `"content_preview": _content_preview(parsed.units)` from `_build_plain_card()`.

Extend the `INSERT INTO corpus_files(...)` column list and values with:

```python
json.dumps(item["content_preview"], ensure_ascii=False)
```

Ensure metadata-only, blocked and unreadable observations initialize `"content_preview": []`.

In `_file_projection(..., detail=True)` add:

```python
"content_preview": (
    []
    if credential_risk
    else [
        {
            "label": str(item.get("label", ""))[:200],
            "text": str(item.get("text", ""))[:MAX_CONTENT_PREVIEW_ITEM_CHARS],
        }
        for item in json.loads(row["content_preview_json"])[:MAX_CONTENT_PREVIEW_ITEMS]
        if isinstance(item, dict) and str(item.get("text", "")).strip()
    ]
),
```

Do not include `content_preview` in the paginated list projection.

- [ ] **Step 5: Run corpus-map tests and verify GREEN**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_corpus_map.py -q
```

Expected: PASS and no credential content appears in cards or audit details.

- [ ] **Step 6: Commit preview generation**

```powershell
git add src/knowledge_workbench/corpus_map.py tests/test_corpus_map.py
git commit -m "feat: add safe corpus content previews"
```

---

### Task 3: Add Server-Side Corpus Summary Filters

**Files:**
- Modify: `src/knowledge_workbench/corpus_map.py:410-510`
- Modify: `src/knowledge_workbench/web_service.py:82-114`
- Modify: `src/knowledge_workbench/webapp.py:104-126`
- Test: `tests/test_corpus_map.py:424-560`
- Test: `tests/test_webapp.py:65-228`

**Interfaces:**
- Consumes: query value `view`.
- Produces:
  - `CORPUS_VIEWS = {"all", "readable", "duplicates", "versions", "attention"}`
  - `latest_corpus_map(..., view: str = "all")`
  - `WorkbenchReadService.corpus_map(..., view: str = "all")`
  - `GET /api/v1/corpus-map?view=<fixed-value>`

- [ ] **Step 1: Write failing domain filter tests**

Create files that produce readable, duplicate and attention states, then assert:

```python
self.assertEqual(latest_corpus_map(database, view="all")["total"], 5)
self.assertEqual(latest_corpus_map(database, view="readable")["total"], 4)
self.assertEqual(latest_corpus_map(database, view="duplicates")["total"], 2)
self.assertEqual(latest_corpus_map(database, view="versions")["total"], 2)
self.assertEqual(latest_corpus_map(database, view="attention")["total"], 1)
with self.assertRaisesRegex(KnowledgeWorkbenchError, "资料地图视图"):
    latest_corpus_map(database, view="sql-fragment")
```

Use two identical Markdown files named `副本甲.md` and `副本乙.md` for duplicates, two different files named `流程-v1.md` and `流程-v2.md` for versions, and one corrupt `.pdf` fixture for `unreadable`.

- [ ] **Step 2: Run the domain test and verify RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_corpus_map.py -q
```

Expected: FAIL because `latest_corpus_map()` does not accept `view`.

- [ ] **Step 3: Implement fixed server-side predicates**

Add:

```python
CORPUS_VIEWS = frozenset(
    {"all", "readable", "duplicates", "versions", "attention"}
)
```

Extend the signature:

```python
def latest_corpus_map(
    database: Database,
    *,
    limit: int = 100,
    offset: int = 0,
    folder: str | None = None,
    scope_status: str | None = None,
    query: str | None = None,
    view: str = "all",
) -> dict[str, Any]:
```

Validate and add only fixed predicates:

```python
if view not in CORPUS_VIEWS:
    raise KnowledgeWorkbenchError(f"不支持的资料地图视图：{view}")
if view == "readable":
    where.append("cf.map_status = 'readable'")
elif view == "duplicates":
    where.append("cf.duplicate_group IS NOT NULL")
elif view == "versions":
    where.append("cf.version_group IS NOT NULL")
elif view == "attention":
    where.append("cf.map_status IN ('blocked', 'unreadable')")
```

Return the normalized `view` in the page projection.

- [ ] **Step 4: Add failing Web API assertions**

In `test_corpus_map_web_flow_uses_plain_language_and_does_not_import_files`, request:

```python
filtered = application.handle(
    "GET", "/api/v1/corpus-map?view=readable"
)
self.assertEqual(filtered.status, 200)
self.assertEqual(
    json.loads(filtered.body.decode("utf-8"))["view"],
    "readable",
)
invalid = application.handle(
    "GET", "/api/v1/corpus-map?view=not-supported"
)
self.assertEqual(invalid.status, 404)
```

Expected before implementation: first response ignores the filter and has no `view`.

- [ ] **Step 5: Thread `view` through the read service and route**

Update `WorkbenchReadService.corpus_map()` and the `/api/v1/corpus-map` handler:

```python
view=_string_query(query, "view") or "all",
```

Pass the value unchanged into `latest_corpus_map()`.

- [ ] **Step 6: Run focused tests and verify GREEN**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest `
  tests\test_corpus_map.py `
  tests\test_webapp.py::WorkbenchWebTests::test_corpus_map_web_flow_uses_plain_language_and_does_not_import_files -q
```

Expected: PASS.

- [ ] **Step 7: Commit corpus filters**

```powershell
git add src/knowledge_workbench/corpus_map.py src/knowledge_workbench/web_service.py src/knowledge_workbench/webapp.py tests/test_corpus_map.py tests/test_webapp.py
git commit -m "feat: filter corpus map by summary category"
```

---

### Task 4: Open Current Read-Only Document Copies with the Native Application

**Files:**
- Modify: `src/knowledge_workbench/web_service.py:1623-1750`
- Modify: `src/knowledge_workbench/webapp.py:45-120`
- Modify: `src/knowledge_workbench/webapp.py:270-620`
- Test: `tests/test_webapp.py:2155-2235`

**Interfaces:**
- Consumes:
  - `document_id: str`
  - `actor: str`
  - `document_versions.stored_path`
- Produces:
  - `WorkbenchActionService.open_document(document_id: str, *, actor: str) -> dict[str, Any]`
  - `POST /api/v1/documents/{document_id}/open`
  - audit event `document_opened_locally`
- Constructor extension:
  - `file_opener: Callable[[Path], None] | None = None`

- [ ] **Step 1: Write failing service tests for allowed and denied opens**

Create an imported internal document and inject an opener:

```python
opened: list[Path] = []
actions = WorkbenchActionService(
    database,
    paths,
    file_opener=opened.append,
)
result = actions.open_document(imported.document_id, actor="reader-01")
self.assertTrue(result["opened"])
self.assertEqual(result["document_id"], imported.document_id)
self.assertEqual(len(opened), 1)
self.assertTrue(opened[0].is_relative_to(paths.raw.resolve()))
```

Add separate assertions for:

```python
restricted_source = root / "受限资料.md"
restricted_source.write_text("受限正文。", encoding="utf-8")
restricted = ingest_file(
    restricted_source,
    paths,
    Classification.RESTRICTED,
)
with self.assertRaisesRegex(PermissionError, "受限资料"):
    actions.open_document(restricted.document_id, actor="reader-01")

tampered_source = root / "越界资料.md"
tampered_source.write_text("普通正文。", encoding="utf-8")
tampered = ingest_file(
    tampered_source,
    paths,
    Classification.INTERNAL,
)
with database.transaction() as connection:
    connection.execute(
        """
        UPDATE document_versions
        SET stored_path = '../outside.md'
        WHERE id = ?
        """,
        (tampered.version_id,),
    )
with self.assertRaisesRegex(KnowledgeWorkbenchError, "只读资料目录"):
    actions.open_document(tampered.document_id, actor="reader-01")
```

After a successful open, inspect the audit row:

```python
self.assertEqual(event["event_type"], "document_opened_locally")
self.assertNotIn(str(paths.root), event["details_json"])
self.assertNotIn("stored_path", event["details_json"])
```

- [ ] **Step 2: Run the service test and verify RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_webapp.py -q
```

Expected: FAIL because the constructor has no `file_opener` and no `open_document`.

- [ ] **Step 3: Implement path-constrained native opening**

Add:

```python
import os


def _open_with_default_application(path: Path) -> None:
    if os.name != "nt":
        raise KnowledgeWorkbenchError("当前系统不支持用本机应用打开资料")
    try:
        os.startfile(path)  # type: ignore[attr-defined]
    except OSError as exc:
        raise KnowledgeWorkbenchError("本机应用无法打开这份资料") from exc
```

Extend the action-service constructor:

```python
self.file_opener = file_opener or _open_with_default_application
```

Add `open_document()`:

```python
def open_document(self, document_id: str, *, actor: str) -> dict[str, Any]:
    actor = _required_actor(actor)
    with self.database.connect() as connection:
        row = connection.execute(
            """
            SELECT d.id, d.original_name, d.classification,
                   dv.id AS version_id, dv.stored_path
            FROM documents d
            JOIN document_versions dv ON dv.id = d.current_version_id
            WHERE d.id = ?
            """,
            (document_id,),
        ).fetchone()
    if row is None:
        raise KnowledgeWorkbenchError("资料不存在或没有当前版本")
    if row["classification"] == "restricted":
        raise PermissionError("受限资料不能从 Web 打开")
    raw_root = self.paths.raw.resolve()
    candidate = (self.paths.root / row["stored_path"]).resolve()
    try:
        candidate.relative_to(raw_root)
    except ValueError as exc:
        raise KnowledgeWorkbenchError("资料不在受控只读资料目录中") from exc
    if not candidate.is_file():
        raise KnowledgeWorkbenchError("只读资料副本不存在")
    self.file_opener(candidate)
    with self.database.transaction() as connection:
        connection.execute(
            """
            INSERT INTO audit_log(
                event_type, entity_type, entity_id, actor,
                details_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                "document_opened_locally",
                "document",
                row["id"],
                actor,
                json.dumps(
                    {"version_id": row["version_id"]},
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                utc_now(),
            ),
        )
    return {
        "document_id": row["id"],
        "display_name": row["original_name"],
        "opened": True,
    }
```

- [ ] **Step 4: Write failing POST route tests**

Create an application with the injected action service and request:

```python
opened_response = application.handle(
    "POST",
    f"/api/v1/documents/{imported.document_id}/open",
    body=json.dumps({"actor": "reader-01"}).encode("utf-8"),
    headers={
        "Content-Type": "application/json",
        "X-Workbench-CSRF": "csrf",
        "Origin": "http://127.0.0.1:8765",
        "Host": "127.0.0.1:8765",
    },
)
self.assertEqual(opened_response.status, 200)
self.assertTrue(
    json.loads(opened_response.body.decode("utf-8"))["result"]["opened"]
)
```

Also assert missing CSRF is `403`, `GET` is not an open action, and bootstrap contains `"document-open-local"`.

- [ ] **Step 5: Implement the POST route**

In `_handle_post()` resolve:

```python
document_open_id = _route_entity_id(
    path, entity="documents", action="open"
)
```

Include it in the supported-operation guard and dispatch:

```python
elif document_open_id is not None:
    result = self.action_service.open_document(
        document_open_id,
        actor=actor,
    )
```

Add `"document-open-local"` to bootstrap `write_capabilities`.

- [ ] **Step 6: Run focused tests and verify GREEN**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_webapp.py -q
```

Expected: PASS; the injected opener records the validated `workspace/raw/` path without launching an application.

- [ ] **Step 7: Commit native file opening**

```powershell
git add src/knowledge_workbench/web_service.py src/knowledge_workbench/webapp.py tests/test_webapp.py
git commit -m "feat: open current documents with local applications"
```

---

### Task 5: Make Documents Clickable and DeepSeek Default-On

**Files:**
- Modify: `src/knowledge_workbench/web_assets/index.html:145-160`
- Modify: `src/knowledge_workbench/web_assets/app.js:256-320`
- Modify: `src/knowledge_workbench/web_assets/app.js:1107-1170`
- Modify: `src/knowledge_workbench/web_assets/app.js:2995-3012`
- Modify: `src/knowledge_workbench/web_assets/app.css`
- Test: `tests/test_webapp.py:65-228`

**Interfaces:**
- Consumes:
  - `item.document_id`
  - `qaState.deepseekConfigured`
  - `POST /api/v1/documents/{document_id}/open`
- Produces:
  - `openDocument(documentId: string) -> Promise<void>`
  - visible `.document-open` control
  - configured DeepSeek checkbox defaults back to checked after every submission.

- [ ] **Step 1: Write failing static-asset assertions**

In the Web static-page test, fetch HTML and JavaScript:

```python
html = application.handle("GET", "/").body.decode("utf-8")
javascript = application.handle(
    "GET", "/assets/app.js"
).body.decode("utf-8")
self.assertIn('id="qa-use-deepseek" type="checkbox" checked disabled', html)
self.assertIn("已绑定；默认用于每次提问", javascript)
self.assertIn("async function openDocument(documentId)", javascript)
self.assertIn("document-open", javascript)
self.assertIn("deepseek.checked = qaState.deepseekConfigured", javascript)
```

- [ ] **Step 2: Run the static test and verify RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest `
  tests\test_webapp.py::WorkbenchWebTests::test_corpus_map_web_flow_uses_plain_language_and_does_not_import_files -q
```

Expected: FAIL because the checkbox is not checked by default and document controls are not clickable.

- [ ] **Step 3: Implement the local-open control**

Add:

```javascript
async function openDocument(documentId) {
  let actor;
  try { actor = actorValue(); } catch { return; }
  try {
    const result = await postTransition(
      `/api/v1/documents/${encodeURIComponent(documentId)}/open`,
      { actor },
    );
    showBanner(`已用本机应用打开只读副本：${result.display_name}`, true);
  } catch (cause) {
    showBanner(cause instanceof Error ? cause.message : "无法打开资料");
  }
}
```

In `renderDocuments()`, replace the plain document-name element with:

```javascript
const openControl = button(
  item.display_name,
  "document-open",
  () => openDocument(item.document_id),
);
openControl.setAttribute(
  "aria-label",
  `用本机应用打开只读副本：${item.display_name}`,
);
```

Keep the document-type icon and explanatory secondary text.

- [ ] **Step 4: Implement DeepSeek default-on semantics**

Set the initial HTML control to:

```html
<input id="qa-use-deepseek" type="checkbox" checked disabled>
```

Update configuration:

```javascript
checkbox.checked = configured;
checkbox.disabled = !configured;
status.textContent = configured
  ? "已绑定；默认用于每次提问，可在发送前取消。本次提交会记录云调用审计。"
  : "尚未绑定 DEEPSEEK_API_KEY；当前只能使用本地摘录回答";
```

Update conversation reset:

```javascript
document.querySelector("#qa-use-deepseek").checked =
  qaState.deepseekConfigured;
```

Update form submission:

```javascript
const allowDeepSeekOnce =
  qaState.deepseekConfigured && deepseek.checked;
deepseek.checked = qaState.deepseekConfigured;
```

The request payload must continue using `allow_deepseek_once`; do not add a server-side permanent consent field.

- [ ] **Step 5: Add focused styles**

Style `.document-open` as text-forward, left-aligned, keyboard-focusable control. Reuse existing colors and radii; do not add decorative gradients or custom icons.

- [ ] **Step 6: Run static and backend tests and verify GREEN**

Run:

```powershell
node --check src\knowledge_workbench\web_assets\app.js
.\.venv\Scripts\python.exe -m pytest tests\test_webapp.py -q
```

Expected: both commands PASS.

- [ ] **Step 7: Commit clickable documents and default-on DeepSeek**

```powershell
git add src/knowledge_workbench/web_assets/index.html src/knowledge_workbench/web_assets/app.js src/knowledge_workbench/web_assets/app.css tests/test_webapp.py
git commit -m "feat: open documents and default DeepSeek on"
```

---

### Task 6: Add Clickable Corpus Summary Cards and Rich Comparison Details

**Files:**
- Modify: `src/knowledge_workbench/web_assets/index.html:535-573`
- Modify: `src/knowledge_workbench/web_assets/app.js:300-745`
- Modify: `src/knowledge_workbench/web_assets/app.css`
- Test: `tests/test_webapp.py:65-228`

**Interfaces:**
- Consumes:
  - `data.scan.*_count`
  - `data.view`
  - `detail.content_preview`
  - `detail.relations[].other_file_id`
- Produces:
  - `corpusState.view`
  - `selectCorpusView(view: string) -> Promise<void>`
  - summary buttons with `data-corpus-view`
  - `#corpus-dialog-preview`
  - relation buttons that call `openCorpusCard(other_file_id)`.

- [ ] **Step 1: Write failing static contract assertions**

Add:

```python
self.assertIn('id="corpus-dialog-preview"', html)
self.assertIn("data-corpus-view", javascript)
self.assertIn("selectCorpusView", javascript)
self.assertIn("查看对方说明", javascript)
self.assertIn("代表性正文节选", javascript)
self.assertIn("范围表示是否属于当前知识库", html)
```

- [ ] **Step 2: Run the static test and verify RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest `
  tests\test_webapp.py::WorkbenchWebTests::test_corpus_map_web_flow_uses_plain_language_and_does_not_import_files -q
```

Expected: FAIL because the rich preview container and interactive summary controls are absent.

- [ ] **Step 3: Add the stable corpus view state and query**

Extend state:

```javascript
const corpusState = {
  limit: 60,
  offset: 0,
  folder: "",
  scope: "",
  query: "",
  view: "all",
  data: null,
};
```

Add:

```javascript
async function selectCorpusView(view) {
  const allowed = new Set(
    ["all", "readable", "duplicates", "versions", "attention"],
  );
  if (!allowed.has(view)) return;
  corpusState.view = view;
  corpusState.offset = 0;
  await refreshCorpusMap();
}
```

In `refreshCorpusMap()` always add:

```javascript
parameters.set("view", corpusState.view);
```

Reset `corpusState.view = "all"` in the existing clear-filters action.

- [ ] **Step 4: Render summary cards as accessible buttons**

Replace `corpusSummaryCard()` with:

```javascript
function corpusSummaryCard(view, value, label, note, active) {
  const card = button(label, "corpus-summary-card", () => {
    selectCorpusView(view).catch((cause) => {
      showBanner(cause instanceof Error ? cause.message : "资料筛选失败");
    });
  });
  card.dataset.corpusView = view;
  card.setAttribute("aria-pressed", String(active));
  card.replaceChildren(
    element("strong", "", formatNumber(value)),
    element("span", "", label),
    element("small", "", note),
  );
  return card;
}
```

Call it with `all`, `readable`, `duplicates`, `versions` and `attention`; compare each value with `data.view`.

- [ ] **Step 5: Add the rich preview and decision guidance containers**

In the corpus dialog, add before `#corpus-dialog-outline`:

```html
<section id="corpus-dialog-preview" class="corpus-dialog-section"></section>
```

Immediately before the scope-decision fields add:

```html
<p class="corpus-decision-guidance">
  范围表示这份资料是否属于当前知识库；权威性表示它能否作为现行主要依据，两者需要分别判断。
</p>
```

- [ ] **Step 6: Render content preview and linked relation cards**

In `openCorpusCard()`:

```javascript
const preview = document.querySelector("#corpus-dialog-preview");
preview.replaceChildren(element("h3", "", "代表性正文节选"));
if (detail.content_preview?.length) {
  detail.content_preview.forEach((item) => {
    const block = element("article", "corpus-preview-item");
    block.append(
      element("strong", "", item.label || "正文片段"),
      element("p", "", item.text),
    );
    preview.append(block);
  });
} else {
  preview.append(
    element(
      "p",
      "muted",
      "当前扫描没有保存正文节选；重新生成资料地图后即可查看。",
    ),
  );
}
```

Replace each plain relation list item with:

```javascript
const relationCard = element("article", "corpus-relation-card");
relationCard.append(
  element("strong", "", relation.other_title),
  element("p", "", relation.plain_reason),
  button(
    "查看对方说明",
    "quiet-button",
    () => openCorpusCard(relation.other_file_id),
  ),
);
relations.append(relationCard);
```

When there are no relations, explain that only exact duplicates and filename-based version candidates have been checked.

- [ ] **Step 7: Add selected, preview and relation styles**

Add styles for:

- `.corpus-summary-card[aria-pressed="true"]`
- `.corpus-preview-item`
- `.corpus-relation-card`
- `.corpus-decision-guidance`

Use existing design tokens, visible keyboard focus, and responsive stacking below `820px`.

- [ ] **Step 8: Run syntax and Web tests and verify GREEN**

Run:

```powershell
node --check src\knowledge_workbench\web_assets\app.js
.\.venv\Scripts\python.exe -m pytest tests\test_webapp.py tests\test_corpus_map.py -q
```

Expected: PASS.

- [ ] **Step 9: Commit corpus review UI**

```powershell
git add src/knowledge_workbench/web_assets/index.html src/knowledge_workbench/web_assets/app.js src/knowledge_workbench/web_assets/app.css tests/test_webapp.py
git commit -m "feat: make corpus review understandable"
```

---

### Task 7: Documentation, Full Verification, and Browser Acceptance

**Files:**
- Modify: `README.md`
- Modify: `docs/ARCHITECTURE.md`
- Modify: `docs/NEXT_MILESTONE.md`
- Create or update: `design-qa.md`

**Interfaces:**
- Consumes: all behavior from Tasks 1-6.
- Produces: user-facing operating notes, architecture record, clean full test evidence, and browser acceptance evidence.

- [ ] **Step 1: Update documentation**

Document these exact behaviors:

- 资料库打开的是 `workspace/raw/` 当前只读副本。
- DeepSeek 已绑定时默认勾选，但每个 POST 仍携带单次授权字段并受密级策略约束。
- 资料地图统计卡是服务端全量筛选，不是当前页前端过滤。
- 正文节选在本机扫描阶段生成，旧扫描需要重新扫描才能补齐。
- 系统候选关系只表示完全重复或疑似版本，不代表完整业务关系。

- [ ] **Step 2: Run the complete automated verification**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\knowledge.exe --workspace .\workspace lint
node --check src\knowledge_workbench\web_assets\app.js
git diff --check
```

Expected:

- all tests PASS;
- lint reports `error_count: 0` and `warning_count: 0`;
- JavaScript syntax check exits 0;
- `git diff --check` exits 0.

- [ ] **Step 3: Restart the local service**

Resolve the exact listener PID for `127.0.0.1:8765`, stop only that process, then start:

```powershell
powershell -ExecutionPolicy Bypass -File .\tools\start_web_with_deepseek.ps1
```

Verify `/api/v1/bootstrap` reports DeepSeek configured and includes `document-open-local`.

- [ ] **Step 4: Run browser acceptance**

Using the in-app Browser:

1. Open `http://127.0.0.1:8765/#documents`.
2. Click a non-restricted Word or PDF item and confirm a success toast; do not use a restricted item.
3. Open `#qa` and confirm DeepSeek is checked.
4. Uncheck it, submit one question, and confirm the next-question state is checked again.
5. Open the corpus map and click all five summary cards; verify each selected state and result set.
6. Open an authority-confirmation document and confirm summary, representative excerpts, structure, relation cards and decision guidance are visible.
7. Click “查看对方说明” when a relation exists and verify the dialog updates.
8. Check a `768 × 900` viewport for overflow or unusable controls.
9. Confirm browser console has no errors or warnings.

- [ ] **Step 5: Update design QA evidence**

Record:

- desktop and narrow-screen screenshots;
- the four primary flows tested;
- native-open success without exposed paths;
- console-error check;
- `final result: passed` only when no P0/P1/P2 issue remains.

- [ ] **Step 6: Commit documentation and QA report**

```powershell
git add README.md docs/ARCHITECTURE.md docs/NEXT_MILESTONE.md design-qa.md
git commit -m "docs: record document review enhancements"
```
