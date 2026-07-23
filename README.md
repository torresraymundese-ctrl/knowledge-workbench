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
.\.venv\Scripts\knowledge.exe entity create "产权交易平台" --type project --actor curator-01
.\.venv\Scripts\knowledge.exe entity add-alias entity_复制实际实体ID "交易平台升级" --actor curator-01
.\.venv\Scripts\knowledge.exe entity link-evidence entity_复制实际实体ID ev_复制实际证据ID --mention "交易平台升级" --actor curator-01
.\.venv\Scripts\knowledge.exe entity list --type project
.\.venv\Scripts\knowledge.exe entity import-candidates .\workspace\evaluations\实际.analysis.json --actor curator-01
.\.venv\Scripts\knowledge.exe entity candidate-list --status pending
.\.venv\Scripts\knowledge.exe entity accept-candidate entitycand_复制实际候选ID entity_复制实际实体ID --actor reviewer-01
.\.venv\Scripts\knowledge.exe entity reject-candidate entitycand_复制实际候选ID --note "非同一实体" --actor reviewer-01
.\.venv\Scripts\knowledge.exe entity merge-candidates --type project --minimum-similarity 0.65 --limit 100
.\.venv\Scripts\knowledge.exe entity visibility-list --web-visible-only
.\.venv\Scripts\knowledge.exe entity merge-propose entity_源实体ID entity_目标实体ID --note "人工核对为同一实体" --actor curator-01
.\.venv\Scripts\knowledge.exe entity merge-list --status reviewing
# 必须由不同于提议人的操作者复核
.\.venv\Scripts\knowledge.exe entity merge-review entitymerge_复制实际请求ID --decision approve --note "已回源确认" --actor reviewer-02
.\.venv\Scripts\knowledge.exe graph project
.\.venv\Scripts\knowledge.exe graph project --entity-id entity_复制实际实体ID
# 仅用于审核中的探索视图；显式加入 draft/reviewing/conflicted，仍排除 restricted
.\.venv\Scripts\knowledge.exe graph project --include-unverified
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

DeepSeek 是可选增强。只有配置 `DEEPSEEK_API_KEY` 后才能显式运行 `--mode deepseek`；`internal` 资料还必须增加 `--allow-internal-cloud-once`。`confidential` 和 `restricted` 资料始终禁止云调用。阶段一 `analysis-v3-source-anchored` 由本地 faithful 提取器先固定完整、有序的证据 ID、逐字正文和全部定位；模型只能补充语义字段，返回后本地按 ID 强制覆盖正文与定位，遗漏、重排、重复或未知 ID 均阻断。阶段二使用 `wiki-generation-v2-extractive` 抽取式提示词，结论必须逐字等于一条所引证据，禁止改写、多证据合成及原文范围外推断。无法逐字表达的整理需求进入 `human_tasks`，现有方向词、关键数字和文本支撑校验继续作为硬防线。真实短 Markdown 与34单元表格 DOCX 已完成调用验证，但不能代表企业资料整体质量。项目禁止使用 `qwen2.5:7b-instruct`。

不安装项目也可以运行：

```powershell
python .\knowledge.py init
python .\knowledge.py ingest .\samples\example.md --classification internal
```

运行数据默认写入项目下的 `workspace/`，该目录不会提交到 Git。

解析器或证据提取器升级后，使用 `ingest --reprocess` 为同一文件版本创建新的派生运行。该操作不会复制 SHA-256 文件版本，也不会删除旧证据或旧 Wiki 修订；相同解析器版本和提取方法的重复重处理会被跳过。
重处理会改变当前证据集合；已有向量索引会被识别为过期，必须重新运行 `index build` 后才能继续语义检索。全文检索不受影响。

SQLite Schema v8 支持一条证据正文对应多个来源定位。`faithful-schema-v2-multilocator` 只合并同一文件版本、同一处理运行内逐字相同的正文；跨文档、跨版本或仅大小写/空白近似的内容不会合并。首定位继续保存在兼容字段 `evidence.locator_json`，全部有序定位保存在 `evidence_locations`，并同步写入分析 JSON、JSONL 镜像、Wiki 草稿、标注候选和 Web 详情。历史证据 ID、审核状态和引用不会由迁移改写；需要使用新策略时显式运行 `ingest --reprocess`，旧处理运行仍完整保留。

SQLite Schema v9 增加人工确认的实体规范化基础层。规范实体创建时自动登记规范名称别名；同一实体类型中的规范化别名只能指向一个实体，不能自动一名多指。证据关联只接受当前文件版本、当前处理运行中的证据，并要求 `--mention` 逐字存在于证据原文且已登记为该实体别名。创建、别名增删和证据关联/解除均要求 actor 并写审计，审计只保存名称或提及哈希；DeepSeek 输出不会自动创建、合并或关联规范实体。

SQLite Schema v10 增加模型实体候选队列。`entity import-candidates` 只读取已存在的 `model_assisted` 阶段一 JSON，不触发模型调用；导入前重验 JSON Schema、来源文件版本、SHA-256、密级和当前处理运行，并按逐字 excerpt 映射数据库证据。候选保持 `pending`，只有人工用 `accept-candidate` 明确选择现有规范实体后，系统才会原子登记无歧义别名并创建证据关联；非逐字候选、过期来源和别名冲突不能接受。`rejected` 和 `accepted` 都是不可重复裁决的终态，复核意见只以 SHA-256 进入数据库与审计。

SQLite Schema v11 增加人工实体合并复核。`merge-propose` 只登记人工选择的同类型源实体和目标实体，不运行相似度模型、不自动合并，并要求提议说明；请求进入 `reviewing` 后只能由不同 actor 用 `merge-review` 批准或驳回。批准时系统在单一事务中把源实体的全部别名、证据逐字提及和已接受模型候选指向迁移到目标实体，再归档源实体；源实体原规范名称成为不可删除、可继续随链式合并迁移的 merge anchor。旧实体 ID、源目标方向、提议人、复核人和哈希意见永久保留在合并请求与审计中。类型变化、实体已归档、并发重复请求、同人自审或任何别名/外键完整性错误都会阻断并整体回滚。合并不删除证据、不修改原文，也不自动建立业务关系。

`entity merge-candidates` 是不落库的确定性候选投影，不会自动调用 `merge-propose`。它只比较 active、同类型规范实体及其人工别名，先按规范化名称的三字符片段阻塞，再计算最佳别名编辑相似度；超过50个实体的高频块跳过，单次最多比较50,000对，并在结果中显式报告跳块、比较上限和截断状态。候选包含稳定 ID、相似度、最佳匹配别名、当前证据计数及已有合并请求状态，不产生审计事件，也不修改实体、别名或合并请求。

实体自身不建立第二套人工密级字段；`entity visibility-list` 从该实体全部历史证据提及实时推导最高密级，优先级为 `restricted > confidential > internal > public`，没有证据支持时为 `unclassified`。该策略不会因文件版本更新或证据退出当前运行而降低历史敏感性。Web 只列出具有至少一条非 restricted 证据且最高密级不是 restricted 的 active 实体；`unclassified`、restricted 支持实体及已归档实体保持 CLI 边界。接受实体候选时后端会按目标实体 ID 再次计算可见性，不能通过手工构造请求绕过列表过滤。

`graph project` 是不落库的确定性实体共现投影。节点只来自 active 规范实体，边只表示两个实体逐字出现在同一条当前原子证据中，不代表因果、隶属、依赖或其他业务关系。默认只消费 `verified` 证据；`--include-unverified` 仅用于审核探索，会额外加入 draft、reviewing 和 conflicted，但 deprecated、archived 以及所有 `restricted` 支持始终排除。输出只包含实体、状态/密级集合和支持证据 ID，不包含证据原文，也不提供文件导出。

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

第一条命令行知识链路、全文检索、可选 BGE-M3 语义检索、审核状态机、任务工作器、双向链接、质量评测、人工实体规范化、模型实体候选裁决和只读证据共现图已经可运行。详细边界见 [第一阶段架构](docs/ARCHITECTURE.md)、[性能基线](docs/PERFORMANCE_BASELINE.md)、[质量评测](docs/QUALITY_EVALUATION.md) 和 [下一里程碑](docs/NEXT_MILESTONE.md)。

## 本地 Web 工作台

启动本机受控审核工作台：

```powershell
.\.venv\Scripts\knowledge.exe --workspace workspace web
```

然后打开 `http://127.0.0.1:8765/`。当前页面提供工作区总览、当前资料、审核队列、实体候选审核、实体合并复核、跨文档冲突标注、质量评测、脱敏审计动态，以及受控的证据审核、冲突处理和 Wiki 草稿提交复核。审核队列支持按密级和安全元数据搜索，证据、冲突和 Wiki 修订分别使用自身有效的状态筛选与独立分页；后端会拒绝队列类型与状态不匹配的请求，不再静默返回空结果。`restricted` 资料不能通过原文件名或页面标题搜索。实体候选区只投影非 `restricted` 候选元数据，支持 `pending`、`accepted`、`rejected` 独立分页和安全搜索；人工接受时必须显式选择已存在的 active 规范实体，非逐字候选、过期来源和别名冲突仍由核心状态机阻断，Web 不自动创建实体或直接写实体表。驳回意见必填，接受与驳回都是不可重复裁决的终态。实体合并区只允许从 Web 可见、同类型 active 实体中人工选择有方向的“源 → 目标”，说明与复核意见只以哈希进入审计；请求提交后必须由不同 actor 批准或驳回。批准还必须输入绑定请求 ID 的确认短语，列表过滤和写接口都会重新计算两个实体的全部历史证据最高密级，手工构造 restricted、unclassified 或 archived 实体 ID 会被阻断；最终合并仍由核心事务原子迁移别名、证据提及与已接受候选并归档源实体。非受限 Wiki 修订可在显式打开后查看元数据、引用证据状态和受限长度的 Markdown 预览；只有内容哈希与数据库一致的当前 `draft` 修订才能提交为 `reviewing`。`reviewing` 修订可填写必需复核意见后驳回为不可变的 `rejected`，旧正式修订继续生效；也可在至少引用一条证据、全部引用证据为 `verified`、内容哈希一致且 Web 已展示全文时，输入绑定修订 ID 的确认短语并再次确认后正式发布。已驳回修订另在只读历史中显示复核意见、操作者、时间和来源是否仍为当前版本，不重新占用审核队列或开放状态流转；`restricted` 历史标题和意见继续脱敏。预览被截断的长修订只能回到 Obsidian 核对全文并通过 CLI 发布。服务只允许绑定 `127.0.0.1` 或 `localhost`，不暴露来源路径、评测集路径或通用审计详情；列表接口不批量返回证据原文，只有明确打开非 `restricted` 证据时才返回单条详情，`restricted` 名称、定位、原文和 Web 写操作始终被阻断。

“跨文档冲突标注”按候选包安全发现并提供状态筛选、文本搜索和每页10条的证据对比。标注保存前会重查当前文件版本、当前处理运行、密级、原文和全部定位，并要求客户端提交所见文件的 SHA-256；文件被其他页面或人工编辑后会拒绝覆盖。整包标签完整且未截断时才可提交，提交审计固定标签摘要和标注人，此后 Web 锁定标签；只有不同操作者可以逐项复核，驳回必须填写意见。页面不自动采用规则预测、不创建业务冲突，也不开放数据集固化，最终 `finalize-pack` 继续由 CLI 执行。

Web 写操作必须填写 `actor`，使用进程级 CSRF 令牌、同源与 Host 校验，并继续调用既有证据、冲突、Wiki 修订、实体候选、实体合并或冲突候选业务服务写入审计日志。Web 层不直接执行状态 SQL；正式发布和实体合并仍由核心状态机执行原子更新与审计。冲突候选固化及其他高影响写操作尚未开放。
