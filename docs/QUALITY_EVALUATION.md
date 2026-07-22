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

# 使用候选包中的 evidence_id 逐条选择
.\.venv\Scripts\knowledge.exe label candidates <session_id> nas-pilot-001 --limit 20
.\.venv\Scripts\knowledge.exe label add-evidence <session_id> nas-pilot-001 <evidence_id-1> <evidence_id-2> <evidence_id-3> --actor "标注人姓名"

# 查看进度并提交
.\.venv\Scripts\knowledge.exe label show <session_id>
.\.venv\Scripts\knowledge.exe label check <session_id> --strict
.\.venv\Scripts\knowledge.exe label submit <session_id> --actor "标注人姓名"

# 必须由另一人批准；也可以使用 reject --note 驳回
.\.venv\Scripts\knowledge.exe label review-pack <session_id> .\workspace\evaluations\nas-pilot-v1.review.md --actor "审核人姓名"
.\.venv\Scripts\knowledge.exe label approve <session_id> --actor "审核人姓名"

# 批准后导出到 workspace 内，不允许覆盖已有文件
.\.venv\Scripts\knowledge.exe label export <session_id> .\workspace\evaluations\nas-pilot-v1.json --actor "审核人姓名"
```

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
