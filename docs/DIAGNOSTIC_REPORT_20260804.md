# 知识工作台诊断报告(2026-08-04)

> 诊断执行: Hermes(独立第三方)
> 诊断方式: 219 项测试实跑、Lint、`quality status`、CLI 问答复盘、SQLite 直接核查、Git 历史核对、文档声称与实际逐条比对
> 诊断对象: `D:\全新知识库`(knowledge-workbench 0.1.0,Python 3.13 venv)
> 重要说明: 测试环境需清除 `PYTHONPATH` 中的 `D:\Hermes Agent\venv\Lib\site-packages` 后执行,否则 rpds 二进制崩溃导致全部测试无法收集(见 §5.3)。

---

## 0. 执行摘要

系统框架质量高(219 项测试全过、Lint 0 错误、证据溯源设计严谨),但存在**一个致命问题、一类严重问题、多个中等与轻微问题**:

- **致命**: 全部评测资产(黄金问答集、冲突样本、图谱试点包、质量关闭报告)在 `workspace/evaluations/` 中不存在,而文档宣称"已完成"。`quality status` 自我判定 11 个门槛仅 2 个通过,`complete: false`。
- **严重**: 文档声称与实际状态系统性脱节;真实检索质量差(问 A 答 B);7 个已"发布"主题页仍带"未经人工审核"免责声明;7月29日后 8215 行改动未提交 Git。
- **中等**: 无任何备份;向量索引未构建;`corpus_files` 表累积 473 条失控记录;79 个单文件 Wiki 草稿与正式主题混存。
- **轻微**: venv 依赖外部环境、权限问题、个别页面元数据不一致。

**一句话结论**: 这是"机制演示已完备、真实业务闭环未完成、文档把机制可运行写成了业务已验收"的状态。测试与 Lint 没有撒谎,但它们只证明代码能跑。

---

## 1. 致命问题: 评测资产全部缺失

### 1.1 现象

`workspace/evaluations/` 目录中除本次诊断生成的两个文件外**为空**:

```
workspace/evaluations/
├── hermes-lint.json              (654B,  本次诊断生成)
└── hermes-quality-status.json    (24,733B, 本次诊断生成)
```

全盘搜索下列文档声称存在的关键资产,**0 命中**:

| 资产类型 | 文档声称 | 实际 |
|---|---|---|
| qa-gold-annotation.md / qa-gold-v1.json | "12条真实问题的受保护人工工作包已生成" | 不存在 |
| cross-document-conflict-candidates-*.json | "共1515对候选,已完成178条人工标注,100条分层样本+7条真冲突独立复核" | 不存在 |
| graph-pilot-labels_*.json | "46条证据试点包" | 不存在 |
| graph-gold-v1.json | "5个关系用例+1个路径用例,6/6通过" | 不存在 |
| nas-pilot-v1.*.json | "NAS 试点标注" | 不存在 |
| quality-closure-status-*.json | "统一质量状态10/10通过" | 不存在 |
| batch_001-annotation.md / review.md 等 | "26批计划全部应用" | 不存在 |

`git log --all -- workspace/evaluations` 确认:**该目录从未被提交过任何内容**。

### 1.2 数据库侧对应状态(实测)

| 表 | 行数 | 文档声称 |
|---|---|---|
| `conflicts` | **0** | "100条分层样本+7条真冲突" |
| `labeling_sessions` | **0** | "双人黄金标注会话" |
| `canonical_entities` | **0** | "17个规范实体" |
| `entity_relationships` | **0** | "4条有效业务关系" |
| `labeling_cases` / `labeling_case_reviews` | **0** | "逐用例独立复核" |

### 1.3 系统自我判定

```
quality status 实测:
  summary: complete = false
  gate_count: 11, passed: 2, pending: 9
  human_action_required: true
  未通过门槛: development_regression_baseline / conflict_labeling_plan /
    conflict_annotation / conflict_review / graph_pilot_snapshot /
    graph_evidence_review / graph_entity_mentions / graph_business_relationship /
    graph_gold_evaluation
  advisory: 20 份推荐黄金资料,已批准 0 份
```

### 1.4 影响与建议

**影响**: 若据此对外宣称"知识库已验收",属于虚假验收;任何关于回答准确率、冲突检出率、图谱质量的数字都无法复现。

**建议**:
1. 立即在 README/NEXT_MILESTONE/QUALITY_EVALUATION 中把"已完成"改为"机制已实现、业务闭环未完成",或将相应条目移入"历史开发回归"区并显著标注。
2. 若要重建: 按流程重新生成黄金问答集(`qa gold-export`)、冲突候选包(`conflict candidate-pack` + batch-plan)、图谱试点(`graph pilot-pack`),每步产出落盘后再更新文档。
3. 将 `quality status` 输出纳入 CI/验收门槛,杜绝"文档说完成但系统说不完成"。

---

## 2. 严重问题: 检索质量差(实测问 A 答 B)

### 2.1 实测复现

使用 CLI 问答复盘(本地模式,无云模型)提问:

| 问题 | 回答摘要 | 判定 |
|---|---|---|
| "研学活动带队老师与学生的人数比例是多少?" | 返回《旅行社安全规范》5.2 招徕安全要求逐字摘录(含"报名和组团时应做出明示"等),**未给出任何比例数字** | ❌ 答非所问 |
| "平台如何保障学生的个人信息安全?" | 返回《研学通平台角色资料提交开发指导手册》表格一行"C1=通用安全出行保障制度…",与"个人信息安全"不相关 | ❌ 答非所问 |
| "研学活动退款流程是怎样的?" | 返回《文旅部研学旅游规范》合同条款摘录(退款时限、违约金比例),无流程性回答 | ⚠️ 部分相关但未回答流程 |

每次回答均标注 `answer_type: evidence`、`mode: wiki-first-agent-v1`、`model_response_mode: None`。

### 2.2 根因

1. **向量索引从未构建**: `workspace/index/` 目录 **0 条目**。README 明确写"已有向量索引会被识别为过期,必须重新运行 `index build` 后才能继续语义检索",而当前从未建立过索引。检索只有 SQLite FTS5 词法匹配。
2. **词法匹配对同义/口语化提问失效**: "带队比例"≠"招徕安全要求";"个人信息安全"≠"通用安全出行保障制度"。
3. **wiki-first 策略把"最相关片段摘录"当作答案**: 命中主题页后直接输出原文摘录,没有"理解问题→组织答案"环节。`wiki_section_count: 40` 说明主题页已存在,但匹配后没有精炼。
4. 文档声称"首轮9问基线 7/7 命中正确主题"——与本次实测表现不符(可能当时的9问是精心挑选的字面匹配问题)。

### 2.3 建议(按成本排序)

1. 低成本: 构建 BGE-M3 向量索引(`knowledge.exe index build --model bge-m3`),验证混合检索(词法+语义)是否显著改善召回。本机已有 `bge-m3` 相关依赖迹象(见 §5.6)。
2. 中成本: 为回答层增加"问题类型识别→答案组织"逻辑(现已有 `fact / advice / clarify / out_of_scope` 路由框架,但本地模式未启用)。
3. 高成本(推荐路线): 对真实业务问题建 20~50 条黄金问答集(这正是缺失的资产),用其驱动检索与回答评测,不再靠感觉判断"7/7"。
4. 引入 Reranker(README 提及但未配置)。

---

## 3. 严重问题: 文档与实现自相矛盾(逐条)

### 3.1 已发布主题页仍带"未经人工审核"免责声明(实测)

7 个 `verified` 主题页(位于 `workspace/wiki/verified/`)**全部**仍包含:

> "这是按主题整理的逐字摘录草稿,**尚未经过人工业务审核**,不代表平台已经确认这些内容的适用范围或优先级。"

而 NEXT_MILESTONE.md 声称"业务负责人已逐页审核并正式发布7个主题"。两种可能,均为问题:
- 审核确实没做 → 页面不应为 verified;
- 审核做了 → 页面模板/文案未更新,业务读者会误以为内容未审。

**建议**: 核查审计日志中 7 个页面的 publish 事件(`audit_log` 共 6697 行,应有记录);若已发布,更新页面免责声明文案为"已通过人工业务审核"。

### 3.2 双向链接声称与事实不符(实测)

- 文档声称:"Wiki 出链和反链写入 SQLite,并生成 Obsidian `[[链接]]`;已发布修订不可静默修改。"
- 实测: `wiki_links` 表 **0 行**;7 个 verified 页面正则扫描 `[[...]]` 链接 **全部为空**。

即"双向链接"功能要么从未实际写入,要么仅在旧开发样例中存在(文档亦自称"旧样例已清理")。建议在文档中明确当前状态为"链接机制已实现,当前业务页面尚未启用"。

### 3.3 Git 状态与"已发布修订不可静默修改"的张力(实测)

- 最后提交: `a885e7a` 2026-07-29 14:40。
- 之后未提交改动: **35 个文件,8215 insertions / 604 deletions**,含全部核心模块(`cli.py` +587、`web_service.py` +935、`database.py` +479、`quality_closure.py` +491、`review.py` +569、`webapp.py`、`app.js` +1309、`app.css` +716 等)以及 13 个全新未跟踪模块(`backups.py`、`corpus_map.py`、`governance.py`、`nas_admission.py`、`qa_gold_workpacks.py`、`question_answering.py`、`topic_wiki.py` 等)与 13 个新测试文件。
- 含义: 当前代码库**没有任何可恢复的历史版本**(除工作区 SQLite 外),一旦误改/误删即不可回滚;同时大量新功能(备份、资料地图、NAS 准入、问答)虽有测试,但从未进入版本管理。

**建议**: 立即 `git add` + `git commit`(或至少建立工作分支),并确认 `.gitignore` 中 `workspace/` 与 `本地知识库备份/` 的排除是否符合预期(当前被排除,意味着 SQLite 与全部原始资料不进 Git——设计如此,但必须依赖 §4.1 的备份策略兜底)。

### 3.4 其他文档/实际不一致

| 文档声称 | 实际 |
|---|---|
| "面向用户的研学平台范围为16份资料、922条技术校验依据" | 数据库 79 文档/3034 证据全为 `verified`(技术校验通过);governance 表 16 份 production in_scope(1 authoritative + 15 reference)+ 63 份 development_fixture out_of_scope。922 条面向用户的口径无法从现有表直接复现(需按 governance 过滤后统计,建议文档补充统计口径) |
| "统一质量状态 v3 … 新增真实生产资料范围与权威版本门槛" 且通过 | `business_corpus_scope` 门槛确实通过(16 in_scope/0 unreviewed/1 authoritative)——这条一致 ✓ |
| "Lint 为0错误、0警告" | 实测一致 ✓ |
| "当前工程回归为219项测试全部通过" | 实测一致(清理环境变量后)✓ |

---

## 4. 中等问题

### 4.1 从未备份(实测)

- README 提供 `backup create`(默认目标 `D:\全新知识库\本地知识库备份`),并注明"当前阶段决策:备份暂缓"。
- 实测 `D:\本地知识库备份` **不存在**;`workspace/knowledge.sqlite3`(14 MB)+ `raw/` 79 份原始资料 + 全部评测/审计数据无任何备份。
- NEXT_MILESTONE 明确写"待功能测试完全稳定且出现重要数据后再恢复此工作项"。当前已出现真实业务数据(7 个正式主题),建议尽快执行一次 `backup create` 并验证 `backup verify`。

### 4.2 向量索引缺失(见 §2.2.1)

`workspace/index/` 0 条目。除影响检索外,`ingest --reprocess` 的文档也要求重建索引,当前状态属于"功能未启用"。

### 4.3 `corpus_files` 表数据失控(实测)

- `corpus_scans`: 8 次扫描;`corpus_files`: **473 行**(含根目录 36 文件那次扫描、NAS 扫描、多次增量),而当前生产范围仅 16 份。
- 大量 out_of_scope / development_fixture / 历史版本候选长期累积,无清理归档机制。建议: 区分"活跃地图"与"历史扫描存档",或定期归档过期扫描。

### 4.4 Wiki 草稿与正式知识混存(实测)

- `wiki_pages`: 88 行(79 个单文件 draft 页 + 7 个 verified 主题 + 2 个 archived 主题)。
- 79 个 draft 页中 63 个来自 out_of_scope 开发数据,仍保留在默认列表/检索路径(README 声称"不出现在默认资料列表、主题 Wiki 或问答中"——需验证检索是否真正排除,建议为 draft 单文件页增加明确的排除断言测试)。

### 4.5 未验证的"数据口径"

README 多处以"922条面向用户可见"作为对外数字,但该数字没有对应的 SQLite 视图/表,属于派生统计。建议: 建立只读视图或校验查询,避免文档数字与数据库漂移。

---

## 5. 轻微问题与环境注意事项

### 5.1 免责声明文案(见 §3.1)

### 5.2 权限问题

- `.pytest_cache` 写入被拒(`Errno 13 / WinError 5 拒绝访问`),pytest 只能以 `-p no:cacheprovider` 运行。原因待查(目录 ACL 或杀软锁定),建议修复或接受并文档化。

### 5.3 venv 环境脆弱性(实测,重要)

- `.venv` 用 `python -m venv --system-site-packages` 创建(home = `C:\Python`,Python 3.13)。
- 当环境中存在 `PYTHONPATH=D:\Hermes Agent\venv\Lib\site-packages`(Hermes 运行时注入)时,Python 3.13 的 venv 会加载为 Python 3.11 编译的 `rpds`/`jsonschema`,导致 `ModuleNotFoundError: No module named 'rpds.rpds'`,**40 个测试文件全部无法收集**(并非代码缺陷)。
- 复现: 在注入环境下 `pytest` 全部 ERROR;`env -u PYTHONPATH pytest` 219 项全过。
- 建议: (a) 在 `pyproject.toml` 或测试文档中写明此环境要求;(b) 或改用非 `--system-site-packages` 的干净 venv。

### 5.4 测试耗时与输出

- 219 项测试约 63 秒(无缓存),可接受;但 `test_webapp.py`(107KB)等超大测试文件建议拆分(纯维护性建议)。

### 5.5 已发布页面 frontmatter 一致性

- 7 个 verified 页 frontmatter 中 `status: verified` 与正文免责声明冲突(见 §3.1);建议发布流程在模板中自动替换免责声明文案。

### 5.6 依赖现状(实测)

- venv 已安装: pypdf 6.14.2、python-docx 1.2.0、openpyxl 3.1.5、python-pptx 1.0.2、jsonschema 4.26.0、pytest 9.1.1(文档可选依赖齐全)。
- 未安装/未配置: BGE-M3、Reranker、DeepSeek API Key(`.env` 不存在;README 说"密钥写入当前 Windows 用户环境,不写入仓库")。

---

## 6. 真实数据盘点(实测,供修复优先级参考)

| 项目 | 数值 |
|---|---|
| 文档/文件版本 | 79 / 79(SHA-256 内容寻址) |
| 当前处理运行 | 79 |
| 当前证据 | 3034 条,全部 `verified`(技术校验) |
| 证据定位 | 3777 条(`evidence_locations`) |
| 技术校验记录 | 3034/3034(`evidence_technical_validation`) |
| 治理记录 | 79(`document_governance`):16 production in_scope(1 权威+15 参考)、63 development_fixture out_of_scope |
| Wiki 页面 | 88:7 verified 主题、2 archived、79 draft 单文件 |
| Wiki 修订 | 111:7 verified、25 superseded、79 draft |
| 修订-证据绑定 | 3208 行(`revision_evidence`) |
| 审计日志 | 6697 行 |
| 实体/关系/冲突/标注 | 全部 0 |
| FTS 索引 | 3034 条(可用) |
| 向量索引 | 0 条(未构建) |
| 备份 | 无 |
| 测试 | 219 项全过(需清理 PYTHONPATH) |
| Lint | 0 错误 0 警告 |

---

## 7. 建议修复顺序(供你自行排期)

1. **P0 数据安全**: 提交 Git(35 文件/8215 行);执行 `backup create` + `backup verify`。
2. **P0 真实性**: 修正文档"已完成"口径;以 `quality status` 输出为唯一验收事实源。
3. **P1 检索**: 构建 BGE-M3 向量索引,验证混合检索;建立 20~50 条真实业务黄金问答集。
4. **P1 一致性**: 核对 7 个主题页发布审计,更新免责声明文案;验证 draft 页是否真的被排除出检索。
5. **P2 数据卫生**: corpus_files 归档机制;建立"面向用户 922 条"的只读统计视图。
6. **P3 环境**: 修复 .pytest_cache 权限;文档化 PYTHONPATH 要求。

---

## 附录 A: 诊断命令备忘

```powershell
# 清理环境变量后运行(重要)
$env:PYTHONPATH = ""
.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider
.\.venv\Scripts\python.exe -m knowledge_workbench.cli lint --output workspace\evaluations\lint.json
.\.venv\Scripts\python.exe -m knowledge_workbench.cli quality status --recommended-gold-documents 20 --conflict-sample-size 100 --minimum-known-conflicts 5 --output workspace\evaluations\quality.json --actor diagnostic-01
.\.venv\Scripts\python.exe -m knowledge_workbench.cli ask "研学活动带队老师与学生的人数比例是多少?" --actor diagnostic-01
```

## 附录 B: 证据文件

- 本次诊断生成: `workspace/evaluations/hermes-lint.json`、`workspace/evaluations/hermes-quality-status.json`
- 数据库: `workspace/knowledge.sqlite3`(只读核查,未做任何修改)
- 本次诊断未修改任何源代码、数据库、文档或评测文件。
