# 企业本地知识工作台

一个 Wiki 优先、证据兜底、智能体导航的本地知识系统。项目借鉴 LLM Wiki 的持续维护理念，但采用独立实现，不复制 GPL 项目代码。

系统先帮助使用者看懂“有哪些资料、它们大致讲什么、彼此是什么关系”，确认范围和权威版本后才进入知识编译。当前正确顺序是：

```text
指定资料目录
  -> 本地只读预读
  -> 全库资料地图与文档说明卡
  -> 人工确认资料范围、用途和权威版本
  -> 批准导入，系统自动校验原文、哈希和定位
  -> 按业务主题合并生成少量逐字摘录式 Wiki 草稿
  -> 只复核主题适用性、冲突、重要结论和 Wiki 发布内容
  -> 智能体先查 Wiki，必要时回到证据和原文件
```

> 当前研学平台试点只把 `D:\研学平台数据\平台数据` 作为业务资料入口。`D:\研学平台数据\开发数据` 仅供理解系统实现，统一标记为 `development_fixture / out_of_scope`，不出现在默认资料列表、主题 Wiki 或问答中。历史处理记录仍保留用于审计与工程回归；`verified` 在界面中只解释为“技术校验通过”，仍不代表业务真实性、时效性或权威性已经获得批准。

## 使用者真正需要审核什么

- 系统自动负责：文件能否读取、哈希和版本、原文能否回溯、定位是否完整、解析产物是否一致。
- 资料负责人负责：哪些文件属于本次知识范围、文件用途、哪个版本更权威、哪些资料应排除。
- 业务专家负责：重要 Wiki 结论、跨文件真冲突、例外规则和正式发布内容。
- 普通原子证据不再作为默认人工审核清单；一次性批量勾选上千条证据既不能证明内容正确，也不构成企业知识批准。

## 核心约束

- 原始资料永不原地修改，以 SHA-256 标识内容版本。
- 每条知识必须能回到原子证据；原子证据必须保存原文片段。
- `verified` 内容通过新修订更新，旧版本在新版本审核通过前继续有效。
- `conflicted` 只表示证据互相矛盾，不表示一般修改。
- 云模型调用必须先通过代码级密级策略。
- 正文中的高置信口令、密钥或令牌会被隔离，系统不会把具体值写入界面或审计。
- 普通业务问答默认只使用业务资料与外部政策；技术设计、模板、示例和过程记录按问题意图显式加入。
- Obsidian 是可选的 Markdown 浏览与编辑端，核心流程不依赖插件。

## 本地运行

当前版本只依赖 Python 3.11+。TXT、Markdown、SQL 和 CSV 可零依赖解析；PDF、DOCX、XLSX、PPTX 需要安装可选依赖。旧版二进制 Word `.doc` 仅在 Windows 已安装 Microsoft Word、并显式增加 `--allow-legacy-word-conversion` 时受控导入；默认拒绝转换。

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
# 第一步：只读预读指定目录，生成资料地图和说明卡。
# 此命令不复制原文件、不创建原子证据、不生成 Wiki、不调用模型。
.\.venv\Scripts\knowledge.exe corpus scan "D:\待整理资料" --project "项目A" --actor scanner-01
.\.venv\Scripts\knowledge.exe corpus list
.\.venv\Scripts\knowledge.exe corpus show corpusfile_复制实际文件ID
# 阅读说明卡后再确认是否属于范围，以及该文件的权威性。
.\.venv\Scripts\knowledge.exe corpus decide corpusfile_复制实际文件ID --scope in_scope --authority authoritative --reason "当前有效的正式制度" --actor owner-01
# 被排除的资料仍保留元数据决定，不进入知识编译。
.\.venv\Scripts\knowledge.exe corpus decide corpusfile_复制实际文件ID --scope out_of_scope --authority unknown --reason "临时备份，不属于本次范围" --actor owner-01

# 只有范围和权威性确认完成后，才用单独的明确命令导入。
# 导入成功后，系统自动完成依据的技术校验；不需要逐条人工勾选证据。
.\.venv\Scripts\knowledge.exe corpus import corpusfile_复制实际文件ID --classification internal --actor importer-01

# 直接 ingest 仍保留给开发和兼容流程；未经过资料地图确认时只会成为候选资料。
.\.venv\Scripts\knowledge.exe ingest .\samples\example.md --classification internal
.\.venv\Scripts\knowledge.exe ingest .\samples\example.md --classification internal --reprocess
.\.venv\Scripts\knowledge.exe ingest .\samples\legacy.doc --classification internal --allow-legacy-word-conversion
# NAS 扫描只登记元数据和 SHA-256；不会复制、解析或调用模型
.\.venv\Scripts\knowledge.exe nas scan "\\nas\共享资料" --project "项目A" --actor scanner-01
.\.venv\Scripts\knowledge.exe nas list --status discovered
.\.venv\Scripts\knowledge.exe nas decide nas_复制实际发现ID admit --classification internal --reason "属于项目正式资料" --actor approver-01
.\.venv\Scripts\knowledge.exe nas import nas_复制实际发现ID --actor importer-01
# 不纳入的资料必须明确登记 ignore
.\.venv\Scripts\knowledge.exe nas decide nas_复制实际发现ID ignore --reason "临时或归档资料" --actor approver-01
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
.\.venv\Scripts\knowledge.exe relation type-add responsible_for "负责" --inverse-label "由其负责" --actor curator-01
.\.venv\Scripts\knowledge.exe relation add responsible_for entity_源实体ID entity_目标实体ID --evidence-id ev_证据ID --note "回源确认关系方向" --actor curator-01
.\.venv\Scripts\knowledge.exe relation list --entity-id entity_源实体ID
.\.venv\Scripts\knowledge.exe graph business
# 默认只沿有向关系正向查找，最多 4 跳；每一跳保留证据 ID
.\.venv\Scripts\knowledge.exe graph paths entity_起点ID --target entity_目标ID --max-depth 3
# 只有显式授权时才允许反向遍历有向关系
.\.venv\Scripts\knowledge.exe graph paths entity_起点ID --target entity_目标ID --include-inverse
.\.venv\Scripts\knowledge.exe graph pilot-pack labels_已批准黄金会话ID --actor pilot-builder-01
.\.venv\Scripts\knowledge.exe graph pilot-status .\workspace\evaluations\graph-pilot-labels_已批准黄金会话ID.json
# 初审人逐条核对后提交复核；未通过项选择“暂缓”并填写原因
.\.venv\Scripts\knowledge.exe graph pilot-triage-export .\workspace\evaluations\graph-pilot-labels_已批准黄金会话ID.json .\workspace\evaluations\graph-pilot-triage.md --actor curator-01
.\.venv\Scripts\knowledge.exe graph pilot-review-status .\workspace\evaluations\graph-pilot-triage.md
.\.venv\Scripts\knowledge.exe graph pilot-triage-apply .\workspace\evaluations\graph-pilot-triage.md --actor curator-01
# 默认由不同 actor 对 reviewing 证据验证通过或退回草稿
.\.venv\Scripts\knowledge.exe graph pilot-verification-export .\workspace\evaluations\graph-pilot-labels_已批准黄金会话ID.json .\workspace\evaluations\graph-pilot-verification.md --actor reviewer-01
.\.venv\Scripts\knowledge.exe graph pilot-review-status .\workspace\evaluations\graph-pilot-verification.md
.\.venv\Scripts\knowledge.exe graph pilot-verification-apply .\workspace\evaluations\graph-pilot-verification.md --actor reviewer-01
# 单人开发时可显式记录“同一责任人二次核对”；审计保证级别仍是非独立复核
.\.venv\Scripts\knowledge.exe graph pilot-verification-export .\workspace\evaluations\graph-pilot-labels_已批准黄金会话ID.json .\workspace\evaluations\graph-pilot-verification-solo.md --actor curator-01 --review-mode solo-attested
.\.venv\Scripts\knowledge.exe graph pilot-verification-apply .\workspace\evaluations\graph-pilot-verification-solo.md --actor curator-01 --solo-attestation "我确认本次由同一责任人完成二次核对，非独立复核"
# verified 证据进入实体裁决；每条必须明确登记实体或说明当前无实体
.\.venv\Scripts\knowledge.exe graph pilot-entity-export .\workspace\evaluations\graph-pilot-labels_已批准黄金会话ID.json .\workspace\evaluations\graph-pilot-entity-curation.md --actor entity-curator-01
.\.venv\Scripts\knowledge.exe graph pilot-entity-apply .\workspace\evaluations\graph-pilot-entity-curation.md --actor entity-curator-01
# 已登记关系类型后，只导出同条 verified 证据中至少有两个 active 实体的项
.\.venv\Scripts\knowledge.exe graph pilot-relationship-export .\workspace\evaluations\graph-pilot-labels_已批准黄金会话ID.json .\workspace\evaluations\graph-pilot-relationship-curation.md --actor relation-curator-01
.\.venv\Scripts\knowledge.exe graph pilot-relationship-apply .\workspace\evaluations\graph-pilot-relationship-curation.md --actor relation-curator-01
# 标注关系正负例和路径用例，应用后保存不可变 reviewing 候选包
.\.venv\Scripts\knowledge.exe graph pilot-gold-annotation-export .\workspace\evaluations\graph-pilot-labels_已批准黄金会话ID.json .\workspace\evaluations\graph-gold-annotation.md --actor graph-annotator-01
.\.venv\Scripts\knowledge.exe graph pilot-gold-work-pack-status .\workspace\evaluations\graph-gold-annotation.md
.\.venv\Scripts\knowledge.exe graph pilot-gold-annotation-apply .\workspace\evaluations\graph-gold-annotation.md .\workspace\evaluations\graph-gold-candidate.json --actor graph-annotator-01
# 默认由不同 actor 逐案复核；单人原型必须显式记录 solo_attested
.\.venv\Scripts\knowledge.exe graph pilot-gold-review-export .\workspace\evaluations\graph-gold-candidate.json .\workspace\evaluations\graph-gold-review.md --actor graph-reviewer-01
.\.venv\Scripts\knowledge.exe graph pilot-gold-work-pack-status .\workspace\evaluations\graph-gold-review.md
.\.venv\Scripts\knowledge.exe graph pilot-gold-review-apply .\workspace\evaluations\graph-gold-review.md .\workspace\evaluations\graph-gold-v1.json --actor graph-reviewer-01
.\.venv\Scripts\knowledge.exe graph-evaluate .\workspace\evaluations\graph-gold-v1.json
.\.venv\Scripts\knowledge.exe relation retract entityrel_关系ID --note "适用期结束" --actor curator-02
.\.venv\Scripts\knowledge.exe page list
# 先查看少量权威资料确认建议和九类业务主题
.\.venv\Scripts\knowledge.exe page topic-status
# 从当前合格资料生成主题草稿；不会自动提交审核或发布
.\.venv\Scripts\knowledge.exe page topic-build --actor wiki-organizer-01
.\.venv\Scripts\knowledge.exe page request-review rev_复制实际修订ID --actor author-01
.\.venv\Scripts\knowledge.exe page publish rev_复制实际修订ID --actor reviewer-01
.\.venv\Scripts\knowledge.exe page reject rev_复制实际修订ID --note "复核意见" --actor reviewer-01
.\.venv\Scripts\knowledge.exe index build --model bge-m3
.\.venv\Scripts\knowledge.exe semantic-search "哪些资料不能发送到云端"
# 直接从当前 verified、非 restricted 证据生成带逐条引用的本地回答
.\.venv\Scripts\knowledge.exe ask "项目团队人员不少于多少人？" --actor qa-user
# 可选：仅本次问题授权 DeepSeek 综合已筛选的 internal 依据；失败时回退本地回答
.\.venv\Scripts\knowledge.exe ask "项目团队人员不少于多少人？" --actor qa-user --deepseek --allow-internal-cloud-once
# 从有限问题集导出人工核对包；填写后先检查，再固化并评测
.\.venv\Scripts\knowledge.exe qa gold-export .\workspace\evaluations\qa-questions.json .\workspace\evaluations\qa-gold-annotation.md --actor qa-curator-01
.\.venv\Scripts\knowledge.exe qa gold-status .\workspace\evaluations\qa-gold-annotation.md
.\.venv\Scripts\knowledge.exe qa gold-apply .\workspace\evaluations\qa-gold-annotation.md .\workspace\evaluations\qa-gold-v1.json --actor qa-curator-01 --review-mode solo-attested --solo-attestation "我确认本次由同一责任人完成二次核对，非独立复核"
.\.venv\Scripts\knowledge.exe qa evaluate .\workspace\evaluations\qa-gold-v1.json
.\.venv\Scripts\knowledge.exe benchmark --mode fts --iterations 50
.\.venv\Scripts\knowledge.exe benchmark --mode vector --synthetic-count 500
.\.venv\Scripts\knowledge.exe pipeline .\samples\example.md --mode faithful
.\.venv\Scripts\knowledge.exe task list
.\.venv\Scripts\knowledge.exe conflict list --status pending
.\.venv\Scripts\knowledge.exe conflict candidate-pack --actor pack-builder
# 把完整候选清单分层为抽样框；清单覆盖全部候选，但不要求全部人工标注
.\.venv\Scripts\knowledge.exe conflict batch-plan .\workspace\evaluations\cross-document-conflict-candidates-实际时间.json --actor coordinator-01 --batch-size 60 --seed real-conflict-v1

# 可选：把尚未标注且相似度最高的少量 value_change 候选集中到第一批，
# 用于主动补充已知真冲突，不要求再审核一个普通大批次。
.\.venv\Scripts\knowledge.exe conflict batch-plan .\workspace\evaluations\cross-document-conflict-candidates-实际时间.json --output .\workspace\evaluations\cross-document-conflict-focused-plan.json --actor coordinator-01 --batch-size 20 --seed known-conflict-v1 --focus-predicted-type value_change --focus-limit 20
.\.venv\Scripts\knowledge.exe conflict batch-status .\workspace\evaluations\cross-document-conflict-labeling-plan-实际时间.json
# 按质量配额逐批导出工作包；单批完整填写后即可独立复核，不必等待候选全集
.\.venv\Scripts\knowledge.exe conflict batch-annotation-export cplan_计划ID batch_001 .\workspace\evaluations\batch_001-annotation.md --actor annotator-01
.\.venv\Scripts\knowledge.exe conflict batch-work-pack-status .\workspace\evaluations\batch_001-annotation.md
.\.venv\Scripts\knowledge.exe conflict batch-annotation-apply .\workspace\evaluations\batch_001-annotation.md --actor annotator-01
# 完成全部批次后提交整包，标签随即锁定
.\.venv\Scripts\knowledge.exe conflict submit-pack .\workspace\evaluations\cross-document-conflict-candidates-实际时间.json --actor annotator-01
# 默认由另一人逐批复核；全部批次完成后固化
.\.venv\Scripts\knowledge.exe conflict batch-review-export cplan_计划ID batch_001 .\workspace\evaluations\batch_001-review.md --actor reviewer-01
.\.venv\Scripts\knowledge.exe conflict batch-work-pack-status .\workspace\evaluations\batch_001-review.md
.\.venv\Scripts\knowledge.exe conflict batch-review-apply .\workspace\evaluations\batch_001-review.md --actor reviewer-01
.\.venv\Scripts\knowledge.exe conflict finalize-pack .\workspace\evaluations\cross-document-conflict-candidates-实际时间.json .\workspace\evaluations\cross-document-conflict-v1.json --name "真实跨文档冲突基线" --reviewer reviewer-01
# 单人原型只能显式记为非独立复核；导出、应用和固化均绑定同一 actor
$SoloAttestation = "我确认本次由同一责任人完成二次核对，非独立复核"
.\.venv\Scripts\knowledge.exe conflict batch-review-export cplan_计划ID batch_001 .\workspace\evaluations\batch_001-solo-review.md --actor annotator-01 --review-mode solo-attested
.\.venv\Scripts\knowledge.exe conflict batch-review-apply .\workspace\evaluations\batch_001-solo-review.md --actor annotator-01 --solo-attestation $SoloAttestation
.\.venv\Scripts\knowledge.exe conflict finalize-pack .\workspace\evaluations\cross-document-conflict-candidates-实际时间.json .\workspace\evaluations\cross-document-conflict-solo-v1.json --name "单人确认跨文档冲突基线" --reviewer annotator-01 --review-mode solo-attested --solo-attestation $SoloAttestation
.\.venv\Scripts\knowledge.exe conflict-evaluate .\evaluation\conflict-sample.json
.\.venv\Scripts\knowledge.exe citation-evaluate .\evaluation\citation-support-sample.json
.\.venv\Scripts\knowledge.exe lint --output .\workspace\evaluations\workspace-lint.json
# 只读汇总10个质量硬门槛；20份仅为覆盖建议，冲突默认抽样100条并加入5条真冲突
.\.venv\Scripts\knowledge.exe quality status
# 将同一状态快照保存并写入脱敏审计
.\.venv\Scripts\knowledge.exe quality status --recommended-gold-documents 20 --conflict-sample-size 100 --minimum-known-conflicts 5 --output .\workspace\evaluations\quality-closure-status.json --actor quality-owner-01
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

DeepSeek 是可选增强。只有配置 `DEEPSEEK_API_KEY` 后才能显式运行 `--mode deepseek` 或问答命令的 `--deepseek`；`internal` 资料还必须增加 `--allow-internal-cloud-once`。`confidential` 和 `restricted` 资料始终禁止云调用。Web 问答使用逐问、默认不勾选的单次授权。除问候、实时外部能力和工作区实时统计等确定性分流外，每个获授权的企业知识问题都进入统一 `wiki-reasoning-agent-v2`：模型根据当前问题、工作区范围统计、已发布 Wiki 的受控主题概览和本地命中的直接依据，在一次调用中判断 `fact / advice / clarify / out_of_scope`，不依赖预置咨询关键词，也不因没有命中原句就提前拒答。发送内容不含数据库 ID、文件路径或定位。建议必须把资料现状与分析意见分开，不能因资料未提到某项能力就断言平台没有该能力；完全没有资料上下文时只能明确标为一般建议。模型返回后，本地重验 JSON、回答模式、引用编号、逐段引用、方向词和关键数值；事实答案还要求文本支撑，任一失败、超时或策略拒绝都回退本地回答。阶段一 `analysis-v3-source-anchored` 由本地 faithful 提取器先固定完整、有序的证据 ID、逐字正文和全部定位；模型只能补充语义字段，返回后本地按 ID 强制覆盖正文与定位，遗漏、重排、重复或未知 ID 均阻断。阶段二使用 `wiki-generation-v2-extractive` 抽取式提示词，结论必须逐字等于一条所引证据，禁止改写、多证据合成及原文范围外推断。无法逐字表达的整理需求进入 `human_tasks`，现有方向词、关键数字和文本支撑校验继续作为硬防线。真实短 Markdown 与34单元表格 DOCX 已完成调用验证，但不能代表企业资料整体质量。项目禁止使用 `qwen2.5:7b-instruct`。

在本机 PowerShell 中可用不可见输入绑定密钥；密钥写入当前 Windows 用户环境，不写入仓库、SQLite 或审计。以下入口会在绑定后直接启动 Web 工作台：

```powershell
Set-Location "D:\全新知识库"
powershell -ExecutionPolicy Bypass -File .\tools\start_web_with_deepseek.ps1
```

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

SQLite Schema v12 增加人工业务关系类型、业务关系及其证据支撑。`relation type-add` 只登记人工定义的有向或无向关系类型；`relation add` 必须显式选择两个 active 实体和至少一条当前 `verified` 证据，并逐条确认该证据已有两个端点的人工逐字提及关联。系统不会把共现、名称相似度或模型候选自动提升为业务关系。无向关系会按实体 ID 规范化方向，重复 active 关系被唯一约束阻断；关系可 `active → retracted`，撤销保留端点、证据和操作者历史，说明正文只以 SHA-256 写入数据库与审计。被任何业务关系历史引用的实体提及不可解除；源实体一旦存在业务关系历史，实体合并也会整体阻断，避免迁移提及时静默改写过去的关系语义。

SQLite Schema v13 增加 NAS 资料准入记录。`nas scan` 只读遍历普通文件，先按受支持扩展名、路径 glob、修改时间、临时/备份/归档规则过滤，再计算 SHA-256 和登记元数据；扫描阶段不复制、不解析、不调用模型。发现项只能由人工明确转为 `admitted` 或 `ignored`，只有带密级的 `admitted` 项能通过 `nas import` 进入既有 SHA-256 导入链路。批准后的源文件若变化，导入会阻断并要求重新扫描、重新准入；同内容重复文件不会重复建立发现项，后续新内容只形成新的 `discovered` 增量。

SQLite Schema v14 为 NAS 发现项增加路径元数据风险标记。扫描不会读取文件正文来猜测密级，但会对“账号密码”、`account…password`、`password list`、`credentials` 等高度疑似凭据清单的文件名登记 `credential_material`。该标记在 `decide` 和 `import` 两层都强制阻断吸收，即使选择 `restricted` 也不能绕过，只能明确 `ignore` 并转交专用凭据管理系统。普通密码修改说明不会仅因出现“密码”二字被阻断。

> 当前阶段决策：备份、NAS 同步、自动调度和恢复演练全部暂缓，优先完成并验证对话式知识问答。以下命令仅保留为已实现能力说明，当前不要求配置或执行。

`backup` 为整个运行工作区生成版本化恢复包。默认备份仓库是工作区同级的 `本地知识库备份/`，可直接作为 Synology Drive 的本地同步目录；运行中的 SQLite 仍保留在本机，不在网络文件系统上直接读写。`backup create` 复制全部工作区文件，SQLite 使用在线备份 API 生成一致副本，跳过 `-wal/-shm/-journal` 瞬时文件，再为每个载荷文件生成 SHA-256 清单并执行数据库完整性和外键检查。快照先写入 `.staging/`，全部校验通过后才发布到 `snapshots/` 并更新 `latest.json`。备份目标不能位于运行工作区内部，符号链接和备份期间发生变化的文件会阻断快照。

```powershell
# 默认目标：D:\全新知识库\本地知识库备份
.\.venv\Scripts\knowledge.exe backup create --actor "backup-operator"
.\.venv\Scripts\knowledge.exe backup list
.\.venv\Scripts\knowledge.exe backup verify

# 默认只演练，不写入文件
.\.venv\Scripts\knowledge.exe backup restore "D:\全新知识库\workspace-restored"

# 演练通过后，使用 list 返回的完整快照 ID 恢复到尚不存在的新目录
.\.venv\Scripts\knowledge.exe backup restore "D:\全新知识库\workspace-restored" `
  --snapshot "snapshot-实际ID" --apply --confirm "snapshot-实际ID"
```

恢复绝不直接覆盖当前运行工作区，也不会写入已存在的目标目录。应用恢复前会再次校验完整清单和数据库；写入暂存目录并逐文件复验后才发布为新工作区。应先以 `--workspace "D:\全新知识库\workspace-restored" status`、`lint` 验证，再人工决定是否切换。`本地知识库备份/` 只同步到 NAS 用户私有目录，不提交 Git；NAS 账号、密码和模型密钥不进入快照清单。

`graph business` 只读投影 active 人工业务关系中仍有“当前文件版本＋当前处理运行＋verified＋非 restricted”支撑的边。旧版本、状态退回或仅 restricted 支撑会使关系退出投影，但不会删除历史记录；Lint 会把失去当前 verified 支撑的 active 关系标为待复核警告。输出保留关系类型、方向和支撑证据 ID，不返回证据原文，也不建立第二套图数据库。

`graph paths` 在同一安全投影上执行确定性、只读的受限深度简单路径查询。默认只沿有向关系的登记方向遍历，无向关系可双向遍历；`--include-inverse` 必须显式给出才允许逆向走有向边。深度限制为1到4跳、结果限制为1到100条，并有20,000次边扩展硬上限；输出会报告候选边、结果或扩展是否截断。每一跳都保留原关系方向、实际遍历方向、关系标签、当前 verified 非 restricted 支撑证据 ID 和密级；路径级证据并集与各跳共同证据分别输出。路径只表示人工关系的逐跳连通性，不能表述为新的业务关系、因果或其他事实结论。

`graph-evaluate` 读取通过受控人工复核的图谱黄金集，评测关系精确率/召回率、方向准确率、关系证据覆盖率、路径精确率/召回率、路径路线准确率、路径证据覆盖率和 restricted 支撑泄漏数。黄金集只保存实体、关系与证据 ID，不复制证据原文；默认 `independent` 要求标注人与复核人不同，单人原型只能显式保存为 `solo_attested` 和 `independent_review=false`。评测启动前会重验全部实体仍为 active，全部黄金证据仍属于当前文件版本与当前处理运行、状态为 verified 且不是 restricted；数据过期会整体阻断，不能被计成算法失败。任何候选或路径查询截断也不能作为正式通过。关系、路径或安全指标未全部通过时命令默认返回失败，但仍保存报告；只有诊断时可显式使用 `--allow-failures`。

`graph pilot-pack` 从既有 `approved` 黄金标注会话中提取双人确认过的必要证据，作为人工证据审核、实体建档和关系登记的优先试点池。生成器重验每个用例已批准、来源版本和处理运行仍为当前、密级未漂移，并排除 restricted；包只能写入当前 `workspace/evaluations/`，包含证据原文与全部定位，禁止覆盖。它只生成候选快照，不改变证据状态、不创建实体或关系；审计仅保存包哈希、相对路径和计数，不保存原文。

`graph pilot-status` 只读取系统生成且内容哈希、包身份、保存路径均与审计匹配的试点包，然后逐条重查数据库快照。输出不回显原文，只报告来源失效、密级泄漏、证据状态、verified 覆盖、active 实体提及、双实体关系资格和 active 关系覆盖，并明确给出证据审核是否完成、是否已经具备图谱黄金集前置条件。复制或修改包文件不能通过状态检查。

`graph pilot-triage-export/apply` 将仍为 `draft` 或 `conflicted` 的试点证据导出为本地 Markdown 初审包。初审人必须逐条选择“提交复核”或“暂缓”，暂缓原因必填；提交项只进入 `reviewing`，不会直接验证、归档、创建实体或关系。`pilot-verification-export/apply` 默认使用 `independent` 模式，只导出当前复核人未亲自提交的 `reviewing` 证据；单人开发可显式选择 `solo-attested`，但只能处理当前 actor 自己提交的证据，应用时还必须提供精确确认短语。两种模式都要求逐条选择“验证通过”或“退回草稿”，退回意见必填；状态、工作包和审计输出分别记录 `review_mode` 与 `independent_review`，单人二次核对不会伪装成独立复核。两阶段工作包均绑定原试点包身份和哈希、actor、导出路径、证据范围与导出状态；只有勾选框和对应 JSON 意见是可编辑字段，修改展示的原文、定位、提交人或其他受保护内容也会被模板哈希阻断。复制、删改范围、重复字段、状态或来源漂移同样使整批零写入。`pilot-review-status` 是不写数据库也不新增审计的预检入口，同时支持两类工作包；它报告决定进度、缺失意见、来源与状态漂移、复核模式、actor 策略和最终 `apply_ready`，但绝不规范化勾选符号或代填人工决定。状态变更和批次结果在同一 SQLite 事务内完成，审计保存证据 ID、决定、计数、保证级别、确认短语/意见/工作包哈希，不保存正文、确认短语或意见明文。

Web“图谱试点”区只发现通过 Schema、包身份、内容哈希、审计路径和当前来源快照校验的试点包。列表按试点包内的证据 ID、资料名、序号和黄金用例 ID 搜索并独立分页，只返回定位摘要、状态与实体/关系进度，不批量返回原文；用户必须显式打开单条证据才能查看内容。证据审核继续复用既有 CSRF、actor、密级边界及 `draft → reviewing → verified` 核心状态机，已验证证据仍保留在试点进度中。被篡改、复制、来源过期或密级越界的包不会进入可操作视图。

跨文档冲突质量基线使用独立候选包，不会自动创建冲突或修改证据状态。候选只来自“当前文件版本＋当前处理运行”，排除 `restricted` 和已弃用/归档证据；原文候选包及固化数据集只能保存在当前 `workspace/evaluations/`，且不会覆盖已有文件。标注人先填写 `label` 并运行 `conflict submit-pack` 写入标签摘要审计，再逐批完成受控 `review` 并运行 `conflict finalize-pack`。默认 `independent` 强制标注人与复核人不同；单人原型必须显式使用 `solo-attested`，由同一 actor 完成二次回源核对，并在应用和固化时输入精确确认短语。审计只保存短语哈希，质量报告分别统计独立、单人和无归因复核，单人结果不会冒充独立。提交和固化都会重新验证候选仍是数据库当前证据，且原文、密级、文档元数据与全部定位未变化；固化还会校验候选来源身份、标签摘要、完整性、未截断状态和复核 actor 策略，生成的数据集可直接交给 `conflict-evaluate`。

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

第一条命令行知识链路、证据约束的本地知识问答、全文检索、可选 BGE-M3 语义检索、审核状态机、任务工作器、双向链接、质量评测、人工实体规范化、模型实体候选裁决、只读证据共现图、人工业务关系和受限深度路径查询已经可运行。详细边界见 [第一阶段架构](docs/ARCHITECTURE.md)、[性能基线](docs/PERFORMANCE_BASELINE.md)、[质量评测](docs/QUALITY_EVALUATION.md) 和 [下一里程碑](docs/NEXT_MILESTONE.md)。

## 本地 Web 工作台

### 资料库与问答的操作边界

- 资料库中的“打开”只会交给本机应用打开当前 `workspace/raw/` 内容寻址只读副本；界面不会暴露本地路径，也不会打开最初扫描目录中的原始文件。
- 已绑定 DeepSeek 时，问答页默认勾选“本次使用 DeepSeek”，以减少重复操作；但每一次问答 `POST` 仍会携带单次字段 `allow_deepseek_once`，并由服务端按资料密级策略重新裁决。`internal` 仍须逐次授权，`confidential` 和 `restricted` 始终不能发送到云端。
- 资料地图顶部的五张统计卡由服务端对完整扫描结果执行对应筛选后返回，不是当前分页或浏览器已加载条目的前端计数；点选卡片会请求相应的完整筛选视图。
- 文档说明卡里的正文节选只在本地扫描时生成。旧扫描没有节选或需要补全时，必须重新扫描，不能由浏览器或云端模型回填。
- 资料地图的“系统关系”只提示内容完全重复或可能属于同一版本链的文件，供人工确认范围和权威版本；它不表示完整的业务、组织或因果关系。

当前首屏是“全库资料地图”。用户先指定资料目录，阅读系统生成的文件夹地图和文档说明卡，再确认资料范围与权威版本。明确导入后，系统会把全部当前依据的哈希、原文、定位和处理运行技术校验结果一次性写入审计，并自动完成兼容状态；这些依据随即可以作为问答兜底，不再形成逐条人工审核任务。人工界面默认只展示冲突和待发布的 Wiki 结论。当前研学平台试点只扫描 `平台数据`，`开发数据` 作为隐藏的工程背景保留，不进入当前资料统计、主题 Wiki 或问答结果。

本节后续长段落保留既有高级功能与兼容状态机的技术细节，其中 `verified` 是数据库内部枚举。面向普通用户时一律按“技术校验通过”解释；旧问答工作包与质量报告属于开发回归材料，不再要求用户继续填写。

“知识问答助手”先用 `conversation-router-v2` 区分问候、实时外部信息、工作区状态和企业知识问题，再对获授权的企业知识问题进入统一 `wiki-reasoning-agent-v2`。问候直接自然回应；未接入实时来源的天气等问题明确说明能力边界；“数据库目前有哪些数据”等平台自检问题直接读取 SQLite 实时统计，不会误搜业务 PDF。企业知识上下文只使用 `production + in_scope` 且未被新版本替代的资料；开发样例、范围未确认资料和 `restricted` 内容不会进入回答。智能体同时看到正式 Wiki 主题、人工确认的业务关系和必要的原始依据，再判断应当回答事实、给出建议、提出澄清还是说明范围边界。“怎么扩大受众群体”这类没有文件名、没有原文同句的开放式问题不再被本地检索提前拒绝。回答会展示中文分析路径、实际引用的知识页、业务关系、原文依据和可继续追问的建议；内部证据 ID 与 JSON 定位不作为主界面文字。未勾选 DeepSeek 时仍由本机确定性流程生成摘录回答；单次授权提交后自动复位。问答正文不持久化。

启动本机受控审核工作台：

```powershell
.\.venv\Scripts\knowledge.exe --workspace workspace web
```

然后打开 `http://127.0.0.1:8765/`。当前页面提供工作区总览、当前资料、审核队列、图谱试点执行区、实体候选审核、实体合并复核、人工业务关系、跨文档冲突标注、质量评测、脱敏审计动态，以及受控的证据审核、冲突处理和 Wiki 草稿提交复核。审核队列支持按密级和安全元数据搜索，证据、冲突和 Wiki 修订分别使用自身有效的状态筛选与独立分页；后端会拒绝队列类型与状态不匹配的请求，不再静默返回空结果。`restricted` 资料不能通过原文件名或页面标题搜索。实体候选区只投影非 `restricted` 候选元数据，支持 `pending`、`accepted`、`rejected` 独立分页和安全搜索；人工接受时必须显式选择已存在的 active 规范实体，非逐字候选、过期来源和别名冲突仍由核心状态机阻断，Web 不自动创建实体或直接写实体表。驳回意见必填，接受与驳回都是不可重复裁决的终态。实体合并区只允许从 Web 可见、同类型 active 实体中人工选择有方向的“源 → 目标”，说明与复核意见只以哈希进入审计；请求提交后必须由不同 actor 批准或驳回。批准还必须输入绑定请求 ID 的确认短语，列表过滤和写接口都会重新计算两个实体的全部历史证据最高密级，手工构造 restricted、unclassified 或 archived 实体 ID 会被阻断；最终合并仍由核心事务原子迁移别名、证据提及与已接受候选并归档源实体。人工业务关系区只使用 CLI 已登记的 active 关系类型；用户选择两个 Web 可见实体后，页面仅列出同时关联两端的当前技术校验通过且非受限的证据元数据，不批量返回原文。登记要求至少一条证据、必填说明和绑定关系类型及两个实体 ID 的确认短语；撤销要求必填说明和绑定关系 ID 的确认短语。列表和写接口都会重算实体可见性与证据资格，伪造受限、过期或未通过技术校验的证据 ID 会被阻断，最终写入继续委托 Schema v12 核心服务。非受限 Wiki 修订可在显式打开后查看元数据、引用状态和受限长度的业务可读纯文本；后端会从预览中移除 YAML、内部对象 ID、哈希和章节机器标记。只有内容哈希与数据库一致、来源仍有效且逐章节声明依据的当前草稿才能提交审核。待审核修订可填写必需复核意见后驳回，旧正式修订继续生效；也可在来源全部合格、预览覆盖全文时，输入绑定修订 ID 的确认短语并再次确认后正式发布。发布事务失败不会遗留未登记的正式文件。已驳回修订另在只读历史中显示复核意见、操作者、时间和来源是否仍为当前版本，多资料主题页同样适用；受限历史标题和意见继续脱敏。预览被截断的长修订只能回到 Obsidian 核对全文并通过 CLI 发布。服务只允许绑定 `127.0.0.1` 或 `localhost`，不暴露来源路径、评测集路径或通用审计详情；列表接口不批量返回证据原文，只有明确打开非受限证据时才返回单条详情，受限资料名称、定位、原文和 Web 写操作始终被阻断。

页面“知识问答助手”采用 Wiki 优先的受控编排：先装载已经发布且仍有效的 Wiki 主题，再沿人工确认的实体关系扩展，必要时回到正式业务资料中的原始依据。原始依据必须同时满足当前文件版本、当前处理运行、兼容状态为“技术校验通过”、独立技术校验记录为 `passed`，并且不是 `restricted`。界面使用资料名称、可读位置和原文说明，不以证据 ID 或 JSON 作为主要表达。未授权云调用时保持本地证据摘录和谨慎拒答；已绑定且用户勾选本次授权时，除冲突和数值歧义外，每个企业知识问题都会由 DeepSeek 判断事实、建议、澄清或范围边界，不再要求先命中某个咨询关键词。事实不能越过来源，建议必须显式标明建议属性并引用所用主题；完全没有知识来源时只能给出清楚标记的一般建议。模型超时、策略拒绝或输出未通过本地校验时不采用模型答案。短追问只使用当前页面最近六条消息，刷新或清空即丢弃；审计不保存问题、回答或依据正文。当前版本尚未加入 BGE-M3/Reranker 混排和持久化会话。

`qa gold-export/status/apply/evaluate` 把真实问答基线限制为一个小型、受审计的人工流程。问题集和工作包只能位于当前 `workspace/evaluations/`；导出时固定问题集哈希、系统候选引用、证据原文与定位快照，并在审计中只保存 ID、路径、哈希和计数。人工只编辑每个用例的期望答案类型、必要引用复选框、确认项和单行说明；`[x]`、`[ x]`、`[x ]` 均视为勾选。应用前会重验问题集、工作包显示内容、导出审计以及全部候选证据仍为当前 `verified` 非 `restricted`，随后按独立复核或显式 `solo_attested` 保证级别固化不含证据原文的 JSON 数据集。评测覆盖答案类型准确率、引用精确率/召回率、逐用例通过率和 restricted 泄漏；评测本身不新增问答审计。

“跨文档冲突标注”按候选包安全发现并提供状态筛选、文本搜索和每页10条的证据对比。经审计的完整分层计划会作为可选抽样框出现；计划列表不返回候选 ID 或原文，选择批次后后端重新校验计划身份、审计路径、来源候选包不可变身份、当前证据来源和全量覆盖，再只投影该批候选。标注保存前会重查当前文件版本、当前处理运行、密级、原文和全部定位，并要求客户端提交所见文件的 SHA-256；文件被其他页面或人工编辑后会拒绝覆盖。批次页面不保存标签，所有决定仍写回原候选包。CLI 受控批次工作包在单批标签完整并原子应用后即可独立进入人工复核，不再等待候选全集；Web 的整包提交与逐项复核入口仍保留兼容。页面不自动采用规则预测、不创建业务冲突，也不开放数据集固化。批次切换使用请求序号门禁，较慢的旧请求不能覆盖最后一次选择。

`conflict batch-plan` 本身是覆盖候选总体的分层抽样框，不复制证据原文，也不建立第二套标签文件。它只接受未截断且来源仍有效的完整候选包，按“预测类型 × 高/中/低相似度”将每个候选 ID 确定性分配到一个且仅一个小批次；相同 seed 得到相同分配。完整覆盖用于证明抽样总体没有被静默截断，不表示所有候选都必须人工处理。需要主动寻找已知真冲突时，可用 `--focus-predicted-type` 和 `--focus-limit` 将尚未标注、相似度最高的指定预测类型集中到第一批，其余候选仍无重复覆盖。计划只能新增到 `workspace/evaluations/`，内容哈希、路径、来源包不可变身份、seed 哈希和聚焦选择摘要写入审计。`batch-status` 重验计划身份、生成审计、来源当前性和全量覆盖，再从原候选包动态统计每批标注与人工复核进度，因此人工决定仍只有原候选包这一套真相源。

`batch-annotation-export/apply` 和 `batch-review-export/apply` 把一个经审计批次导出为本地 Obsidian 兼容 Markdown，内含左右证据原文、定位、规则预测以及必须人工填写的决定。工作包只能位于当前 `workspace/evaluations/`，禁止覆盖，并绑定计划、批次、actor、来源身份和受保护模板；修改原文、定位、人工标签、规则预测或其他说明会整体阻断。标注批次的原子应用审计同时充当该批次的提交凭据，因此该批可立即导出复核包；已进入批次复核的标签被锁定，其他批次仍可继续标注。`batch-work-pack-status` 只读检查完成数、意见、来源阶段、职责分离、`already_applied` 及 `apply_ready`。应用仍以单次原子替换写回原候选包，任一候选无效时本批零写入，审计不保存原文或意见正文。来源 SHA-256 变化后旧工作包必须重新导出。

`quality status` 每次从 SQLite、经审计的冲突抽样框、图谱试点包和本地黄金数据集重新计算真实进度。当前检查工作区完整性、10份黄金基线、冲突抽样框、冲突分层样本标注、样本人工复核、图谱试点快照、证据审核、实体提及、业务关系和图谱黄金评测10项。默认冲突样本为100条，按“预测类型 × 相似度区间”分配配额，并额外要求至少5条人工确认真冲突；1515等候选总数只定义抽样总体，未抽样候选不形成质量欠账。20份黄金资料通过 `--recommended-gold-documents` 作为非阻塞覆盖建议，现有10份已满足硬基线。报告另从 Schema v13 实时统计“NAS 已发现、NAS 已准入、已导入、黄金样本”四段流量，不再从已导入资料反推 NAS 状态。使用 `--output` 时必须提供 actor，报告与审计均不包含证据原文或资料路径。

`graph pilot-entity-export/apply` 把当前试点包中的 verified 证据导出为本地实体裁决工作包。每条证据必须明确选择“登记实体”或“当前无实体”；后者要求说明，前者必须给出逐字 mention，并显式选择已有实体 ID，或填写新实体的规范名与类型。同类型同规范名只在本包内复用，数据库已有实体不会靠名称自动匹配。应用前会重验来源包、原文、全部定位、密级、证据状态、actor、路径、精确范围和受保护模板哈希；新实体、别名、证据提及和批次审计在同一事务内写入，任一别名冲突、非逐字提及或来源漂移都会整批回滚。工作包不可重放，审计只保存 ID、计数及名称、提及、意见和文件哈希，不复制正文或人工说明。

`graph pilot-relationship-export/apply` 只处理试点包中当前 verified、非 restricted，且同条证据已人工绑定至少两个不同 active 实体的项。导出前必须先用 CLI 登记 active 关系类型；工作包逐条要求“登记关系”或带说明的“当前无关系”，不会把实体共现当成关系。每个关系使用本包内 `relationship_ref`，同一 ref 可跨证据累积支持证据，但类型、方向、端点和登记说明必须一致；端点必须来自该证据的受保护实体清单。应用会重验关系类型快照、证据原文与定位、密级、状态、实体提及范围、actor、路径和模板哈希，并通过 `entity_relationships.py` 的同一事务规则创建关系。任一提及漂移、重复 active 关系、无效方向或来源变化会整批回滚；审计不保存实体名称、原文或人工说明。

`graph pilot-gold-annotation-export/apply` 与 `pilot-gold-review-export/apply` 将图谱黄金数据生产收敛为受审计人工流程。标注包绑定当前试点来源、verified 证据、人工实体范围和未截断安全业务图快照；标注人自行填写关系正/负例、方向、黄金证据及1–4跳路径，当前图只作候选参考，不能自动成为答案。应用会通过独立 Schema、最终评测 Schema、当前实体/证据及试点范围校验，保存不可重放的 `reviewing` 候选包。复核默认要求不同 actor；单人原型必须显式记录 `solo_attested`，并逐案批准或带意见驳回。任一驳回不生成数据集，全部批准才写入最终 JSON、立即运行 `graph-evaluate` 并记录聚合结果。`pilot-gold-work-pack-status` 可在应用前只读预检标注和复核两类 Markdown，报告名称/用例完整性、逐案决定、必填意见、actor 策略、来源与业务图快照、导出审计、受保护模板、是否已应用及 `apply_ready`；它不写 SQLite、不补答案，也不记录审计。质量闭环只承认路径和内容哈希与 `graph_gold_dataset_finalized` 审计匹配的文件；手写或复制一个自报 provenance 的 JSON 会计入 `unaudited`，不能通过图谱黄金门槛。

Web 写操作和知识问答请求必须填写 `actor`，使用进程级 CSRF 令牌、JSON Content-Type、同源与 Host 校验。状态变更继续调用既有证据、冲突、Wiki 修订、实体候选、实体合并、人工业务关系或冲突候选业务服务写入审计日志；问答只保存脱敏检索摘要，不保存问题、回答或证据摘录正文。Web 层不直接执行状态 SQL；正式发布、实体合并和业务关系登记/撤销仍由核心状态机执行原子更新与审计。关系类型管理、冲突候选固化及其他高影响写操作尚未开放。
