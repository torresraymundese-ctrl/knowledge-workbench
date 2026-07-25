# 输出质量评测

评测数据集使用 `evaluation-dataset-v1.json` Schema。每个用例绑定一个真实来源文件，并声明：

- 必须被提取的证据片段；
- 输出中禁止出现的内容；
- 可接受的最大重复率；
- 资料密级。

运行命令：

```powershell
.\.venv\Scripts\knowledge.exe evaluate .\evaluation\sample-dataset.json
```

评测集包含旧版 `.doc` 时，仍需为本次命令显式授权本机 Word 转换：

```powershell
.\.venv\Scripts\knowledge.exe evaluate .\workspace\evaluations\nas-pilot-v1.json --allow-legacy-word-conversion
```

不增加该参数时，评测器会拒绝 `.doc`，不会静默启动 Word。

人工标注前可从数据库中的“当前文档版本 + 当前处理运行”生成均匀分布的定位候选：

```powershell
.\.venv\Scripts\knowledge.exe labeling-pack .\workspace\evaluations\nas-pilot-v1.template.json --candidates-per-case 20
```

候选包包含证据 ID、原文、定位、解析器和来源哈希，但不自动认定任何候选为黄金答案。审核人仍需回到原文件选择关键证据，并由第二人复核。

正式评测会在解析文档前检查数据集是否可用：每个用例至少包含一条必要证据，不允许保留 `待人工填写`、`TODO` 等占位文本，也不允许重复的必要证据。结构合法但尚未标注完成的模板仍可用于 `labeling-pack`，不能用于 `evaluate`。

评测报告从 `schema_version: 1.1` 起会为重复内容输出匿名诊断：重复文本只保存 SHA-256 短指纹，不把原文复制进诊断字段；同时记录重复组数量、最大组大小、同一定位重复次数和不同定位原文重复次数。`duplicate_rate` 及数据集中的 `max_duplicate_rate` 仍是质量门槛，诊断信息不会自动放宽阈值。`failure_reasons` 会明确列出用例未通过的原因。

真实资料确认存在合理的重复条款时，策略负责人可以审计式调整单个用例阈值。命令要求记录操作者和原因，已批准会话也会保留变更审计；之后必须导出新的评测集文件，不覆盖旧版本：

```powershell
.\.venv\Scripts\knowledge.exe --workspace workspace label set-duplicate-threshold $SessionId nas-pilot-007 0.10 --actor policy-owner-01 --reason "真实招标资料存在经确认的重复条款"
```

工作区产物一致性基线使用：

```powershell
.\.venv\Scripts\knowledge.exe lint --output .\workspace\evaluations\workspace-lint.json
```

Lint 检查 SQLite 外键、只读原始副本及 SHA-256、当前处理运行的两阶段 JSON Schema、证据 JSONL、数据库证据和 Wiki Markdown 哈希。迁移前尚未生成的Schema产物作为警告显示，不会伪造为现代产物。

## 双人黄金标注流程

候选包只用于定位。正式黄金集通过数据库状态机建立：

```powershell
# 标注人创建空会话；默认每个用例至少选择3条必要证据
.\.venv\Scripts\knowledge.exe label create .\workspace\evaluations\nas-pilot-v1.template.json --actor "标注人姓名"

# 将创建命令输出的ID复制到这里；不要输入PowerShell保留的尖括号占位符
$SessionId = "labels_复制实际会话ID"
$CaseId = "nas-pilot-001"
$Annotator = "annotator-01"
$Reviewer = "reviewer-01"

# 可选：先生成只能保存在 workspace/evaluations 内的 Obsidian 兼容本地标注工作包
.\.venv\Scripts\knowledge.exe label annotation-pack $SessionId .\workspace\evaluations\nas-pilot-v1.annotation.md --actor $Annotator
# 在 Obsidian 中核对并勾选后保存，再增量应用所有已勾选项
.\.venv\Scripts\knowledge.exe label apply-annotation-pack .\workspace\evaluations\nas-pilot-v1.annotation.md --actor $Annotator

# 查看候选并记录列表中显示的 #编号
.\.venv\Scripts\knowledge.exe label candidates $SessionId $CaseId --limit 20
$Ordinals = @() # 核对原文后填写，例如 @(22, 35, 48)；空数组不会写入
if ($Ordinals.Count -lt 3) { throw "请先核对原文并填写至少3个候选编号" }
.\.venv\Scripts\knowledge.exe label add-ordinals $SessionId $CaseId $Ordinals --actor $Annotator

# 查看进度并提交
.\.venv\Scripts\knowledge.exe label show $SessionId
.\.venv\Scripts\knowledge.exe label check $SessionId --strict
.\.venv\Scripts\knowledge.exe label submit $SessionId --actor $Annotator

# 必须由另一人批准；也可以使用 reject --note 驳回
.\.venv\Scripts\knowledge.exe label review-pack $SessionId .\workspace\evaluations\nas-pilot-v1.review.md --actor $Reviewer
# 在 Obsidian 中逐用例勾选批准或驳回；驳回必须填写原因
.\.venv\Scripts\knowledge.exe label apply-review-pack .\workspace\evaluations\nas-pilot-v1.review.md --actor $Reviewer
.\.venv\Scripts\knowledge.exe label candidates $SessionId $CaseId --only-selected --full
.\.venv\Scripts\knowledge.exe label review-case $SessionId $CaseId approved --actor $Reviewer
.\.venv\Scripts\knowledge.exe label approve $SessionId --actor $Reviewer

# 批准后导出到 workspace 内，不允许覆盖已有文件
.\.venv\Scripts\knowledge.exe label export $SessionId .\workspace\evaluations\nas-pilot-v1.json --actor $Reviewer
```

`add-ordinals` 只负责把当前处理运行中的短编号安全解析为正式 evidence ID，不会自动判断证据是否重要。编号不存在、资料已更新、处理运行已过期或操作者不是会话创建人时，整批选择都会失败且不会留下部分写入。仍可使用 `add-evidence` 直接提交实际 evidence ID。

`annotation-pack` 默认包含各用例的全部当前候选、原文片段和定位，可直接用 Obsidian 打开。勾选本身不会修改 SQLite；保存后必须显式运行 `apply-annotation-pack`。应用操作是增量且幂等的：只添加已勾选项，不会因为取消勾选而删除已有选择；任一编号、证据 ID、来源版本或处理运行不匹配时整包回滚。包中包含非公开原文，只允许保存在当前 `workspace/evaluations/`，不能写入只读的 `workspace/raw/`，且不会覆盖同名文件。资料量很大时可增加 `--limit-per-case 200`，但被截断的工作包不能替代回到原文件进行完整核对。

`review-pack` 在提交后由另一位人员生成，每个用例必须且只能勾选“批准”或“驳回”，驳回还必须填写原因。`apply-review-pack` 会先验证所有用例、证据与导出审计记录，再在一个事务中写入逐项决定；它不会自动执行会话最终批准。全部用例通过后，复核人仍须显式运行 `label approve`。

标注会话支持 `list`、`remove-evidence`、`add-forbidden` 和 `remove-forbidden`。冲突、弃用或归档证据不能成为黄金证据；来源文件内容、密级、当前文件版本或当前处理运行变化后，旧会话不能提交、批准或导出。

## 冲突检测评测

冲突评测数据集为独立的成对文本Schema，每个用例声明旧证据、新证据、是否冲突及预期类型：

```powershell
.\.venv\Scripts\knowledge.exe conflict-evaluate .\evaluation\conflict-sample.json --output .\workspace\evaluations\conflict-baseline.json
```

报告包含混淆矩阵、精确率、召回率和类型准确率，默认任何用例失败都会返回非零退出码。报告不复制证据原文，只记录用例ID和预测结果；真实公司冲突对数据集必须放在 `workspace/`，不得提交Git。

报告默认写入 `workspace/evaluations/`。任一用例不通过时命令返回失败状态，适合后续加入 CI；调试数据集时可增加 `--allow-failures` 只生成报告。

### 跨文档真实候选标注

系统可以从当前文件版本和当前处理运行召回跨文档相似证据对，但候选不等于真实冲突，也不会进入业务冲突队列：

```powershell
.\.venv\Scripts\knowledge.exe conflict candidate-pack --actor pack-builder --limit 500
# 标注人填写每项 label.expected_conflict、label.expected_type 和说明；review 保持为空
.\.venv\Scripts\knowledge.exe conflict submit-pack .\workspace\evaluations\cross-document-conflict-candidates-实际时间.json --actor annotator-01
# 另一位复核人逐项填写 review.decision=approved 或 rejected；驳回必须填写原因
.\.venv\Scripts\knowledge.exe conflict finalize-pack .\workspace\evaluations\cross-document-conflict-candidates-实际时间.json .\workspace\evaluations\cross-document-conflict-v1.json --name "真实跨文档冲突基线" --reviewer reviewer-01
.\.venv\Scripts\knowledge.exe conflict-evaluate .\workspace\evaluations\cross-document-conflict-v1.json
```

候选生成排除 `restricted` 以及已弃用、归档证据，只输出证据 ID、文档安全元数据、原文和定位到当前 `workspace/evaluations/`，不允许覆盖文件。`submit-pack` 要求标签完整且复核字段为空，并将标签摘要和标注人写入审计；此后改动标签会使摘要失配。提交与固化都会重查候选仍属于数据库当前文件版本和当前处理运行，原文、密级、文档元数据及全部定位必须一致。`finalize-pack` 还要求所有复核决定通过、复核人与已审计标注人不同、候选来源身份未变化且候选包未被 `--limit` 截断。固化后的数据集沿用现有冲突评测 Schema，报告仍不复制原文。

本机 Web 工作台的“跨文档冲突标注”可替代直接编辑候选包中的 `label` 和 `review`：支持候选包选择、状态筛选、文本搜索和每页10条对照标注。Web 不提供“采用预测”快捷操作，人工必须明确选择标签；每次保存均提交所见候选包文件的 SHA-256，后端在进程锁内重读文件、重验候选身份及当前来源，避免并发覆盖。整包提交后标签不可再改；Web 逐项复核仍要求 actor 不同于已审计标注人，驳回意见必填。单人原型改走本地批次工作包并显式选择 `solo-attested`，应用与最终 CLI 固化都要求精确确认短语；审计只保存模式、独立性布尔值、意见和确认短语 SHA-256，不复制原文或明文。质量状态分别统计 `independent`、`solo_attested` 和无归因决定，单人二次核对不会冒充独立复核。最终数据集仍通过 CLI `finalize-pack` 固化。

## 当前指标

- 必要证据覆盖率：黄金证据片段中成功找到的比例。
- 原文可追溯率：输出证据能够逐字回到解析单元的比例。
- 重复率：规范化后重复证据占比。
- 禁用内容命中：输出中是否出现数据集禁止的字符串。
- 用例通过率：同时满足以上门槛的用例比例。
- 结论引用文本支持：结论与所引原文的保守字符关联分数、最低分和低支持结论数。

文本支持分数不是语义正确率，只用于阻断“拿真实证据ID包装完全无关结论”的明显错误。低于0.15的结论被硬阻断；0.15至0.25属于弱支撑灰区，必须人工审核；达到0.25只表示文本关联足以通过第一道自动筛查，并不代表事实或推理正确。高相似文本还要经过方向词和关键数字一致性检查，“允许/禁止”或“30/45”不一致时不能作为支撑；同一结论混引支持与矛盾证据也会被阻断。无模型保真模式不会自动生成结论，因此报告中的 `conclusion_count` 为0，而不是把不存在的结论记为100%正确。

### DeepSeek 真实小样例校准

2026-07-22 对 `samples/example.md` 进行了两轮真实 `deepseek-chat` 两阶段调用。首轮的阶段一输出通过，阶段二 JSON Schema 和引用 ID 也通过，但 `page[1].conclusion[2]` 与引用证据出现方向或关键数值不一致，最终产物被硬阻断。阶段二随后改为 `wiki-generation-v2-extractive`：结论文本必须逐字等于一条所引证据，单条结论只允许一个匹配 ID，禁止改写、多证据合成及原文范围外推断，不能逐字表达的需求进入 `human_tasks`。

第二轮仍使用 internal 单次云授权并产生两条成功审计。阶段一提取4条、共93字符证据，与 faithful 覆盖一致；阶段二生成4条逐字结论，引用覆盖4/4，最小和平均支撑分数均为1.0，低支撑及方向/数值冲突均为0。相比之下，faithful 生成0条自动结论和1个人工综合任务，更保守但没有语义提炼。DeepSeek 还补充了 evidence type、concepts 和 project，但其定位少于 faithful 的 segment、unit 和多定位字段。该结果仅验证一个短 Markdown 样例，尚不能代表长文、表格、跨段或真实企业资料的稳定性。

随后使用 internal 单次授权验证表格型 `总体进度计划.docx`。该文件解析为34个单元、共1600字符，其中33个来自表格。DeepSeek 阶段一输出34条互不重复证据，与 faithful 的证据正文集合完全一致，字符覆盖1600/1600；阶段二生成34条结论，每条只引用一条逐字匹配证据，引用覆盖34/34，最低和平均支撑分数均为1.0，低支撑和方向/数值冲突均为0。分析模型把27条标为 fact、6条标为 requirement、1条标为 definition，并保留全部33个表格定位。

本轮仍暴露定位和语义元数据缺口：DeepSeek 输出没有 faithful 的 `segment`、`unit` 和 `locators[]` 多定位字段，concepts 仅7/34非空，projects 仅1/34非空；faithful 则完整保留这些定位但不产生概念、项目或证据类型语义。阶段一最初升级为按逐字 excerpt 匹配本地定位，但真实复验中模型轻微改写 `E0001`，被原文追溯校验正确阻断，阶段二未调用。由于模型响应正文按安全规则不落审计库，该失败响应没有被复用。

随后升级为 `analysis-v3-source-anchored`：本地 `FaithfulEvidenceExtractor` 先固定34个有序证据 ID、逐字正文和全部定位，模型只做语义标注，返回后再按 ID 强制覆盖正文和定位，未知 ID 直接阻断。2026-07-22 经新的 internal 单次授权完成阶段一和阶段二两次真实调用，两条调用审计均成功。阶段一34条、1600字符证据与 faithful 的 ID 和正文逐项同序一致，恢复33/33表格定位以及34/34 `segment`、`unit` 和 `locators[]`；阶段二生成34条单证据逐字结论，引用覆盖34/34，最低和平均支撑分数均为1.0，方向/关键数字冲突为0。当前抽取式阶段二已在短 Markdown 和中等表格 DOCX 上稳定通过，但尚未覆盖超长文档、跨页表格和跨段冲突。

引用支撑算法本身使用独立的人工标签数据集评测：

```powershell
.\.venv\Scripts\knowledge.exe citation-evaluate .\evaluation\citation-support-sample.json --output .\workspace\evaluations\citation-support-baseline.json
```

报告包含混淆矩阵、精确率和召回率，但不复制结论或证据原文。内置样例只用于算法回归；公司真实引用对必须保存在 `workspace/` 并由人工标注是否确有支撑。

## 尚需真实资料验证

内置样例只验证评测框架本身，不能代表企业资料质量。进入 Web 开发前需要建立 10–20 份人工标注资料，进一步加入：

- 结论级引用正确率；
- 重要事实遗漏率；
- 跨文档冲突召回率与误报率；
- 时间范围和适用范围准确率；
- 模型辅助模式与证据保真模式的差异对比。
