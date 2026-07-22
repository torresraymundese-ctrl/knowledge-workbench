# 企业本地知识工作台

一个证据优先、可审核、可追溯的本地知识编译系统。项目借鉴 LLM Wiki 的持续维护理念，但采用独立实现，不复制 GPL 项目代码。

当前里程碑只实现命令行闭环：

```text
原始文件（只读副本）
  -> 文档解析
  -> 原子证据
  -> Wiki 草稿
  -> 人工审核
  -> 正式知识（后续里程碑）
```

## 核心约束

- 原始资料永不原地修改，以 SHA-256 标识内容版本。
- 每条知识必须能回到原子证据；原子证据必须保存原文片段。
- `verified` 内容通过新修订更新，旧版本在新版本审核通过前继续有效。
- `conflicted` 只表示证据互相矛盾，不表示一般修改。
- 云模型调用必须先通过代码级密级策略。
- Obsidian 是可选的 Markdown 浏览与编辑端，核心流程不依赖插件。

## 本地运行

当前版本只依赖 Python 3.11+。TXT、Markdown 和 CSV 可零依赖解析；PDF、DOCX、XLSX、PPTX 需要安装可选依赖。旧版二进制 Word `.doc` 仅在 Windows 已安装 Microsoft Word、并显式增加 `--allow-legacy-word-conversion` 时受控导入；默认拒绝转换。

```powershell
Set-Location "D:\全新知识库"
python -m venv .venv --system-site-packages
.\.venv\Scripts\python.exe -m pip install -e ".[documents,dev]"
.\.venv\Scripts\knowledge.exe init
.\.venv\Scripts\knowledge.exe ingest .\samples\example.md --classification internal
```

本机开发环境已经创建 `.venv` 时，可直接使用：

```powershell
Set-Location "D:\全新知识库"
.\.venv\Scripts\knowledge.exe init
.\.venv\Scripts\knowledge.exe ingest .\samples\example.md --classification internal
.\.venv\Scripts\knowledge.exe ingest .\samples\example.md --classification internal --reprocess
.\.venv\Scripts\knowledge.exe ingest .\samples\legacy.doc --classification internal --allow-legacy-word-conversion
.\.venv\Scripts\knowledge.exe evidence list
.\.venv\Scripts\knowledge.exe page list
.\.venv\Scripts\knowledge.exe index build --model bge-m3
.\.venv\Scripts\knowledge.exe semantic-search "哪些资料不能发送到云端"
.\.venv\Scripts\knowledge.exe benchmark --synthetic-count 500
.\.venv\Scripts\knowledge.exe pipeline .\samples\example.md --mode faithful
.\.venv\Scripts\knowledge.exe task list
.\.venv\Scripts\knowledge.exe conflict list --status pending
.\.venv\Scripts\knowledge.exe conflict-evaluate .\evaluation\conflict-sample.json
.\.venv\Scripts\knowledge.exe citation-evaluate .\evaluation\citation-support-sample.json
.\.venv\Scripts\knowledge.exe lint --output .\workspace\evaluations\workspace-lint.json
.\.venv\Scripts\knowledge.exe evaluate .\evaluation\sample-dataset.json
.\.venv\Scripts\knowledge.exe labeling-pack .\workspace\evaluations\nas-pilot-v1.template.json --candidates-per-case 20
.\.venv\Scripts\knowledge.exe label --help
$SessionId = "labels_复制实际会话ID"
$CaseId = "nas-pilot-001"
$Reviewer = "reviewer-01"
.\.venv\Scripts\knowledge.exe label candidates $SessionId $CaseId --limit 20
.\.venv\Scripts\knowledge.exe label check $SessionId --strict
.\.venv\Scripts\knowledge.exe label review-pack $SessionId .\workspace\evaluations\review.md --actor $Reviewer
.\.venv\Scripts\knowledge.exe label review-case $SessionId $CaseId approved --actor $Reviewer
.\.venv\Scripts\knowledge.exe task enqueue faithful_pipeline --payload-file .\samples\faithful-task.json
.\.venv\Scripts\knowledge.exe worker run-once --worker local-worker-1
```

DeepSeek 是可选增强。只有配置 `DEEPSEEK_API_KEY` 后才能显式运行 `--mode deepseek`；`internal` 资料还必须增加 `--allow-internal-cloud-once`。`confidential` 和 `restricted` 资料始终禁止云调用。项目禁止使用 `qwen2.5:7b-instruct`。

不安装项目也可以运行：

```powershell
python .\knowledge.py init
python .\knowledge.py ingest .\samples\example.md --classification internal
```

运行数据默认写入项目下的 `workspace/`，该目录不会提交到 Git。

解析器或证据提取器升级后，使用 `ingest --reprocess` 为同一文件版本创建新的派生运行。该操作不会复制 SHA-256 文件版本，也不会删除旧证据或旧 Wiki 修订；相同解析器版本和提取方法的重复重处理会被跳过。
重处理会改变当前证据集合；已有向量索引会被识别为过期，必须重新运行 `index build` 后才能继续语义检索。全文检索不受影响。

旧版 `.doc` 转换完全在本机完成：Word 以隐藏、只读、禁用宏的方式打开源文件，在临时目录生成 DOCX，解析完成后删除临时文件。原始 `.doc` 的 SHA-256 和只读副本仍是来源真相；每条证据额外保存转换工具、工具版本和临时 DOCX 的 SHA-256。该开关只授权单次命令，不会改变全局默认策略。

黄金标注使用 SQLite Schema v6 状态机，不直接手改正式 JSON：创建者选择当前证据并提交，另一位审核人逐用例记录复核决定，全部通过后才能批准和导出。来源文件、密级、当前版本或当前处理运行发生变化时，提交、复核、批准和导出都会被阻断。非公开评测集只能导出到当前 `workspace/` 内，且不会覆盖已有文件。

## 资料密级

| 密级 | 云端模型策略 |
|---|---|
| `public` | 允许 |
| `internal` | 默认禁止，必须单次显式授权 |
| `confidential` | 禁止 |
| `restricted` | 禁止，并限制导出 |

## 项目状态

第一条命令行知识链路、全文检索、可选 BGE-M3 语义检索、审核状态机、任务工作器、双向链接和质量评测已经可运行。详细边界见 [第一阶段架构](docs/ARCHITECTURE.md)、[性能基线](docs/PERFORMANCE_BASELINE.md)、[质量评测](docs/QUALITY_EVALUATION.md) 和 [下一里程碑](docs/NEXT_MILESTONE.md)。
