# 第一阶段架构

## 目标

当前版本建立一个安全、可验证的知识编译内核。它不是 Obsidian 插件，也不依赖 Web 界面；未来的 Web 工作台和 Obsidian 都只通过同一业务层读取或修改状态。

## 数据流

```text
用户文件
  -> SHA-256 去重与版本识别
  -> workspace/raw/ 内容寻址只读副本
  -> 格式解析器（保留页码/标题/单元格/幻灯片/行号）
  -> 原文保真分段器
  -> SQLite 原子证据 + JSONL 可检查镜像
  -> Obsidian 兼容 Markdown Wiki 草稿
  -> 人工证据审核
  -> 页面修订审核与发布
  -> SQLite FTS5 + BGE-M3 精确余弦检索
```

## 代码边界

| 模块 | 职责 |
|---|---|
| `parsers/` | 文件格式到带定位信息的解析单元 |
| `extraction.py` | 原文保真原子证据；当前不做模型推断 |
| `schema_validation.py` | 两阶段 Draft 2020-12 JSON Schema 与跨阶段证据引用校验 |
| `pipeline.py` | 无生成模型的两阶段证据保真输出 |
| `model_pipeline.py` | 可选模型辅助的分析与 Wiki 生成契约 |
| `ingest.py` | 事务化导入、版本、证据、草稿和审计 |
| `review.py` | 证据与页面修订状态机 |
| `tasks.py` | 持久化任务、工作租约、指数退避、幂等和宕机恢复 |
| `conflicts.py` | 原子证据级潜在冲突检测和人工处理队列 |
| `policy.py` | 密级和本地/云端模型调用硬策略 |
| `providers.py` | 模型提供方的统一策略包装 |
| `search.py` | SQLite 全文搜索与中文子串回退 |
| `embeddings.py` | Ollama 嵌入客户端 |
| `vector_store.py` | 可替换向量接口和 NumPy 精确索引 |
| `wiki.py` | Obsidian 兼容 Markdown 修订渲染 |
| `wiki_links.py` | 证据约束的 Obsidian 出链、SQLite 反链与草稿修改保护 |
| `evaluation.py` | 黄金资料集评测、覆盖率、可追溯率和重复率报告 |
| `worker.py` | 从持久化队列领取并执行可信任务处理器 |
| `database.py` | SQLite 数据结构与连接生命周期 |

## 当前安全性质

- 同一 SHA-256 不会重复创建文件版本、证据或 Wiki 页面。
- 导入事务失败时会回滚数据库并清理本次生成的文件。
- 原文片段为空时数据库约束和业务逻辑都会阻断。
- `draft` 证据不能跳过 `reviewing` 直接变成 `verified`。
- 页面发布前，所有引用证据必须是 `verified`。
- 已发布页面的新资料生成新修订，不覆盖当前已验证修订。
- `internal` 云调用需要单次授权；更高密级无条件禁止云调用。
- 模型调用审计只保存模型、密级、耗时和输入输出哈希，不保存提示词明文。
- 新旧版本出现允许/禁止方向或关键数值变化时按证据对进入冲突队列。
- 任务工作者失联后，过期租约会回到重试队列；达到最大次数才进入 `failed`。

## 存储真相

- SQLite 是业务状态、审核状态和审计状态的唯一真相。
- `workspace/raw/` 是内容寻址的来源副本。
- `workspace/evidence/*.jsonl` 是便于人工检查和迁移的镜像，不负责状态流转。
- `workspace/wiki/` 是 Obsidian 可读产物；手动编辑需要在后续里程碑加入回写和差异保护。
- `workspace/index/` 是可重建派生数据，可随时由 SQLite 证据重新生成。
- `workspace/evaluations/` 保存可重复比较的质量评测报告。

## 尚未实现

- DeepSeek 真实样例调用与输出质量校准（当前未配置密钥）；
- 更广泛的语义冲突识别、实体规范化、知识图谱和下游引用重验证；
- 后台工作者进程（任务状态机、租约和恢复机制已经具备）；
- Reranker 和全文/向量/图谱联合排序；
- OCR、NAS 增量同步、权限主体、审批流和 Web 工作台；
- Obsidian 手动编辑回写和并发差异检测。
- 10–20 份真实黄金资料集及跨文档冲突质量基线。

这些能力必须沿用现有证据、密级、修订和审计边界，不得绕过业务层直接写 Markdown。
