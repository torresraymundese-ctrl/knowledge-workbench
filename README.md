# 企业本地知识工作台

一个证据优先、可审核、可追溯的本地知识编译系统。项目借鉴 LLM Wiki 的持续维护理念，但采用独立实现，不复制 GPL 项目代码。

当前里程碑已完成命令行闭环，并提供仅限本机访问的受控审核 Web 工作台：

```text
原始文件（只读副本）
  -> 文档解析
  -> 原子证据
  -> Wiki 草稿
  -> 人工审核
  -> 正式知识
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
.\.venv\Scripts\knowledge.exe page request-review rev_复制实际修订ID --actor author-01
.\.venv\Scripts\knowledge.exe page publish rev_复制实际修订ID --actor reviewer-01
.\.venv\Scripts\knowledge.exe page reject rev_复制实际修订ID --note "复核意见" --actor reviewer-01
.\.venv\Scripts\knowledge.exe index build --model bge-m3
.\.venv\Scripts\knowledge.exe semantic-search "哪些资料不能发送到云端"
.\.venv\Scripts\knowledge.exe benchmark --mode fts --iterations 50
.\.venv\Scripts\knowledge.exe benchmark --mode vector --synthetic-count 500
.\.venv\Scripts\knowledge.exe pipeline .\samples\example.md --mode faithful
.\.venv\Scripts\knowledge.exe task list
.\.venv\Scripts\knowledge.exe conflict list --status pending
.\.venv\Scripts\knowledge.exe conflict candidate-pack --actor pack-builder
# 在 workspace/evaluations 中填写每个候选的 label，且暂不填写 review
.\.venv\Scripts\knowledge.exe conflict submit-pack .\workspace\evaluations\cross-document-conflict-candidates-实际时间.json --actor annotator-01
# 由另一人填写 review 后固化；标注人与复核人必须不同
.\.venv\Scripts\knowledge.exe conflict finalize-pack .\workspace\evaluations\cross-document-conflict-candidates-实际时间.json .\workspace\evaluations\cross-document-conflict-v1.json --name "真实跨文档冲突基线" --reviewer reviewer-01
.\.venv\Scripts\knowledge.exe conflict-evaluate .\evaluation\conflict-sample.json
.\.venv\Scripts\knowledge.exe citation-evaluate .\evaluation\citation-support-sample.json
.\.venv\Scripts\knowledge.exe lint --output .\workspace\evaluations\workspace-lint.json
.\.venv\Scripts\knowledge.exe evaluate .\evaluation\sample-dataset.json
.\.venv\Scripts\knowledge.exe labeling-pack .\workspace\evaluations\nas-pilot-v1.template.json --candidates-per-case 20
.\.venv\Scripts\knowledge.exe label --help
$SessionId = "labels_复制实际会话ID"
$CaseId = "nas-pilot-001"
$Reviewer = "reviewer-01"
.\.venv\Scripts\knowledge.exe label annotation-pack $SessionId .\workspace\evaluations\annotation.md --actor annotator-01
.\.venv\Scripts\knowledge.exe label apply-annotation-pack .\workspace\evaluations\annotation.md --actor annotator-01
.\.venv\Scripts\knowledge.exe label candidates $SessionId $CaseId --limit 20
.\.venv\Scripts\knowledge.exe label add-ordinals $SessionId $CaseId 22 35 48 --actor annotator-01
.\.venv\Scripts\knowledge.exe label check $SessionId --strict
.\.venv\Scripts\knowledge.exe label review-pack $SessionId .\workspace\evaluations\review.md --actor $Reviewer
.\.venv\Scripts\knowledge.exe label apply-review-pack .\workspace\evaluations\review.md --actor $Reviewer
.\.venv\Scripts\knowledge.exe label review-case $SessionId $CaseId approved --actor $Reviewer
.\.venv\Scripts\knowledge.exe task enqueue faithful_pipeline --payload-file .\samples\faithful-task.json
.\.venv\Scripts\knowledge.exe worker run-once --worker local-worker-1
.\.venv\Scripts\knowledge.exe worker run --worker local-worker-1
.\.venv\Scripts\knowledge.exe worker run --worker local-worker-1 --stop-when-idle
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

SQLite Schema v8 支持一条证据正文对应多个来源定位。`faithful-schema-v2-multilocator` 只合并同一文件版本、同一处理运行内逐字相同的正文；跨文档、跨版本或仅大小写/空白近似的内容不会合并。首定位继续保存在兼容字段 `evidence.locator_json`，全部有序定位保存在 `evidence_locations`，并同步写入分析 JSON、JSONL 镜像、Wiki 草稿、标注候选和 Web 详情。历史证据 ID、审核状态和引用不会由迁移改写；需要使用新策略时显式运行 `ingest --reprocess`，旧处理运行仍完整保留。

跨文档冲突质量基线使用独立候选包，不会自动创建冲突或修改证据状态。候选只来自“当前文件版本＋当前处理运行”，排除 `restricted` 和已弃用/归档证据；原文候选包及固化数据集只能保存在当前 `workspace/evaluations/`，且不会覆盖已有文件。标注人先填写 `label` 并运行 `conflict submit-pack` 写入标签摘要审计，复核人再填写 `review` 并运行 `conflict finalize-pack`。提交和固化都会重新验证候选仍是数据库当前证据，且原文、密级、文档元数据与全部定位未变化；固化还会校验候选来源身份、标签摘要、完整性、未截断状态和双人分离，生成的数据集可直接交给 `conflict-evaluate`。

旧版 `.doc` 转换完全在本机完成：Word 以隐藏、只读、禁用宏的方式打开源文件，在临时目录生成 DOCX，解析完成后删除临时文件。原始 `.doc` 的 SHA-256 和只读副本仍是来源真相；每条证据额外保存转换工具、工具版本和临时 DOCX 的 SHA-256。该开关只授权单次命令，不会改变全局默认策略。

黄金标注使用 SQLite Schema v6 状态机，不直接手改正式 JSON：创建者选择当前证据并提交，另一位审核人逐用例记录复核决定，全部通过后才能批准和导出。来源文件、密级、当前版本或当前处理运行发生变化时，提交、复核、批准和导出都会被阻断。非公开评测集只能导出到当前 `workspace/` 内，且不会覆盖已有文件。

持续后台工作器使用 SQLite Schema v7 租约所有权：领取任务时写入 `lease_owner`，只有该工作器能续租、完成或报告失败；长任务由独立心跳延长租期，租约过期后原工作器不能再提交结果。`worker run` 在队列为空时从 `--poll-seconds` 指数退避到 `--max-poll-seconds`，收到 `Ctrl+C` 或终止信号后等待当前任务结束再退出，并把启动、停止原因和处理统计写入审计。`--stop-when-idle` 用于排空当前队列后退出，`--max-tasks` 可限制单次处理数量。当前可信处理器仍只有 `faithful_pipeline`，未知任务类型会直接进入 `failed`，不会浪费重试次数。

## 资料密级

| 密级 | 云端模型策略 |
|---|---|
| `public` | 允许 |
| `internal` | 默认禁止，必须单次显式授权 |
| `confidential` | 禁止 |
| `restricted` | 禁止，并限制导出 |

## 项目状态

第一条命令行知识链路、全文检索、可选 BGE-M3 语义检索、审核状态机、任务工作器、双向链接和质量评测已经可运行。详细边界见 [第一阶段架构](docs/ARCHITECTURE.md)、[性能基线](docs/PERFORMANCE_BASELINE.md)、[质量评测](docs/QUALITY_EVALUATION.md) 和 [下一里程碑](docs/NEXT_MILESTONE.md)。

## 本地 Web 工作台

启动本机受控审核工作台：

```powershell
.\.venv\Scripts\knowledge.exe --workspace workspace web
```

然后打开 `http://127.0.0.1:8765/`。当前页面提供工作区总览、当前资料、审核队列、跨文档冲突标注、质量评测、脱敏审计动态，以及受控的证据审核、冲突处理和 Wiki 草稿提交复核。审核队列支持按密级和安全元数据搜索，证据、冲突和 Wiki 修订分别使用自身有效的状态筛选与独立分页；后端会拒绝队列类型与状态不匹配的请求，不再静默返回空结果。`restricted` 资料不能通过原文件名或页面标题搜索。非受限 Wiki 修订可在显式打开后查看元数据、引用证据状态和受限长度的 Markdown 预览；只有内容哈希与数据库一致的当前 `draft` 修订才能提交为 `reviewing`。`reviewing` 修订可填写必需复核意见后驳回为不可变的 `rejected`，旧正式修订继续生效；也可在至少引用一条证据、全部引用证据为 `verified`、内容哈希一致且 Web 已展示全文时，输入绑定修订 ID 的确认短语并再次确认后正式发布。已驳回修订另在只读历史中显示复核意见、操作者、时间和来源是否仍为当前版本，不重新占用审核队列或开放状态流转；`restricted` 历史标题和意见继续脱敏。预览被截断的长修订只能回到 Obsidian 核对全文并通过 CLI 发布。服务只允许绑定 `127.0.0.1` 或 `localhost`，不暴露来源路径、评测集路径或通用审计详情；列表接口不批量返回证据原文，只有明确打开非 `restricted` 证据时才返回单条详情，`restricted` 名称、定位、原文和 Web 写操作始终被阻断。

“跨文档冲突标注”按候选包安全发现并提供状态筛选、文本搜索和每页10条的证据对比。标注保存前会重查当前文件版本、当前处理运行、密级、原文和全部定位，并要求客户端提交所见文件的 SHA-256；文件被其他页面或人工编辑后会拒绝覆盖。整包标签完整且未截断时才可提交，提交审计固定标签摘要和标注人，此后 Web 锁定标签；只有不同操作者可以逐项复核，驳回必须填写意见。页面不自动采用规则预测、不创建业务冲突，也不开放数据集固化，最终 `finalize-pack` 继续由 CLI 执行。

Web 写操作必须填写 `actor`，使用进程级 CSRF 令牌、同源与 Host 校验，并继续调用既有证据、冲突、Wiki 修订或冲突候选业务服务写入审计日志。Web 层不直接执行状态 SQL；正式发布仍由核心状态机原子更新 Markdown、当前正式修订和审计事件。冲突候选固化及其他高影响写操作尚未开放。
