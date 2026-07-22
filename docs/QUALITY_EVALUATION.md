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
.\.venv\Scripts\knowledge.exe label candidates $SessionId $CaseId --only-selected --full
.\.venv\Scripts\knowledge.exe label review-case $SessionId $CaseId approved --actor $Reviewer
.\.venv\Scripts\knowledge.exe label approve $SessionId --actor $Reviewer

# 批准后导出到 workspace 内，不允许覆盖已有文件
.\.venv\Scripts\knowledge.exe label export $SessionId .\workspace\evaluations\nas-pilot-v1.json --actor $Reviewer
```

`add-ordinals` 只负责把当前处理运行中的短编号安全解析为正式 evidence ID，不会自动判断证据是否重要。编号不存在、资料已更新、处理运行已过期或操作者不是会话创建人时，整批选择都会失败且不会留下部分写入。仍可使用 `add-evidence` 直接提交实际 evidence ID。

`annotation-pack` 默认包含各用例的全部当前候选、原文片段和定位，可直接用 Obsidian 打开。勾选本身不会修改 SQLite；保存后必须显式运行 `apply-annotation-pack`。应用操作是增量且幂等的：只添加已勾选项，不会因为取消勾选而删除已有选择；任一编号、证据 ID、来源版本或处理运行不匹配时整包回滚。包中包含非公开原文，只允许保存在当前 `workspace/evaluations/`，不能写入只读的 `workspace/raw/`，且不会覆盖同名文件。资料量很大时可增加 `--limit-per-case 200`，但被截断的工作包不能替代回到原文件进行完整核对。

标注会话支持 `list`、`remove-evidence`、`add-forbidden` 和 `remove-forbidden`。冲突、弃用或归档证据不能成为黄金证据；来源文件内容、密级、当前文件版本或当前处理运行变化后，旧会话不能提交、批准或导出。

## 冲突检测评测

冲突评测数据集为独立的成对文本Schema，每个用例声明旧证据、新证据、是否冲突及预期类型：

```powershell
.\.venv\Scripts\knowledge.exe conflict-evaluate .\evaluation\conflict-sample.json --output .\workspace\evaluations\conflict-baseline.json
```

报告包含混淆矩阵、精确率、召回率和类型准确率，默认任何用例失败都会返回非零退出码。报告不复制证据原文，只记录用例ID和预测结果；真实公司冲突对数据集必须放在 `workspace/`，不得提交Git。

报告默认写入 `workspace/evaluations/`。任一用例不通过时命令返回失败状态，适合后续加入 CI；调试数据集时可增加 `--allow-failures` 只生成报告。

## 当前指标

- 必要证据覆盖率：黄金证据片段中成功找到的比例。
- 原文可追溯率：输出证据能够逐字回到解析单元的比例。
- 重复率：规范化后重复证据占比。
- 禁用内容命中：输出中是否出现数据集禁止的字符串。
- 用例通过率：同时满足以上门槛的用例比例。
- 结论引用文本支持：结论与所引原文的保守字符关联分数、最低分和低支持结论数。

文本支持分数不是语义正确率，只用于阻断“拿真实证据ID包装完全无关结论”的明显错误。低于0.15的结论被硬阻断；0.15至0.25属于弱支撑灰区，必须人工审核；达到0.25只表示文本关联足以通过第一道自动筛查，并不代表事实或推理正确。高相似文本还要经过方向词和关键数字一致性检查，“允许/禁止”或“30/45”不一致时不能作为支撑；同一结论混引支持与矛盾证据也会被阻断。无模型保真模式不会自动生成结论，因此报告中的 `conclusion_count` 为0，而不是把不存在的结论记为100%正确。

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
