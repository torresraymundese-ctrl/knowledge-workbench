# 第三版知识空间界面 Design QA

## 对照目标

- source visual truth path: `C:\Users\zz\.codex\generated_images\019fa134-f9a6-7c52-9b9d-d4aecab16fe7\call_94vmpXwCB57ypF1Rzsg5ZX8N.png`
- implementation URL: `http://127.0.0.1:8765/#qa`
- implementation screenshot path: `C:\Users\zz\AppData\Local\Temp\knowledge-workbench-third-answered-v2.png`
- full-view comparison evidence: `C:\Users\zz\AppData\Local\Temp\knowledge-workbench-design-comparison.png`
- state: 桌面端、知识问答已返回答案、右侧来源抽屉已填充、管理功能收起
- viewport: 浏览器覆盖为 `1488 × 1058` CSS px；页面可用宽度 `1473` CSS px；`devicePixelRatio ≈ 1`
- source pixels: `1487 × 1058`
- implementation capture pixels: `1473 × 1048`
- normalization: 对照图中将来源稿和实现截图分别归一到 `1488 × 1058` 后左右并排，未改变内容裁切或状态

## Findings

- 没有剩余的 P0、P1 或 P2 问题。
- [P3] 动态回答内容的结构化程度取决于问答后端。本次本地回答是连续段落，而视觉稿示例包含“现有资料显示 / 建议”等分段。这不是界面结构缺失；前端已经保留可承载分段、关系与冲突内容的回答卡。
- [P3] 为保留现有密级和云调用授权边界，实现稿在输入框下方继续显示“一问一授权”的 DeepSeek 复选项，视觉稿没有这一行。该差异属于必要的产品安全约束。

## Required Fidelity Surfaces

- Fonts and typography: 使用系统中文字体栈与 Segoe UI，标题、正文、辅助文字层级和视觉稿一致；标题略重但没有造成换行或密度问题。
- Spacing and layout rhythm: 左侧导航、中央问答、右侧来源三栏比例一致；输入区、消息气泡、来源卡和底部追问保持稳定节奏。回答态不再残留空状态。
- Colors and visual tokens: 白色背景、深青文字、薄荷绿选中态和低对比边框与视觉稿一致；没有新增装饰渐变。
- Image quality and asset fidelity: 目标界面没有照片或插画资产；所有可见图标来自同一套 Segoe Fluent Icons 字体，没有使用 CSS 绘图、手工 SVG、emoji 或占位图形。
- Copy and content: 用户可见文字均为自然中文；主任务围绕“不必记住文件名、直接提问、右侧核对来源”展开，管理术语不再占据主界面。
- Responsiveness and accessibility: 已在 `768 × 900` 验证窄屏；导航转为顶部图标栏，回答与来源顺序堆叠，无水平溢出。输入框、按钮、复选框和导航均保留可访问名称。

## Focused Region Comparison

未另做裁切对照。归一化后的并排图保留了原始桌面像素尺寸，输入区、问答卡、来源卡、导航图标和底部追问文字均可直接辨认；没有出现需要放大才能判断的关键细节。

## Comparison History

1. 初次回答态对照发现一个 P2：首次提问后，“不需要记住文件名”的空状态仍留在消息列表中，使答案被推到首屏下方，明显改变视觉稿的信息密度。
2. 修复：在 `appendQaMessage()` 首次追加消息时移除 `.qa-empty-conversation`。
3. 修复后证据：`knowledge-workbench-third-answered-v2.png`。答案现在紧接输入区出现，右侧同时展示 6 个真实来源卡；与来源稿的核心构图、层级和首屏密度一致。

## Primary Interactions Tested

- 知识首页、资料库、知识主题、知识问答四个主导航均可切换。
- 资料库加载 12 行当前资料；知识主题加载 9 个业务主题。
- 自然语言提问可生成回答；有依据的问题会填充右侧来源抽屉。
- 管理与维护可展开，并可进入资料整理与导入页面。
- 操作者身份会作为本机偏好保存，刷新后无需重复填写。
- 浏览器控制台未发现 error 或 warning。
- 资料库成功用本地应用打开《旅行社安全规范.pdf》的当前只读副本；成功提示不暴露路径，直接桌面关联打开也已复核。
- DeepSeek 复选框在已配置时初始为选中；手工取消后为未选中，提交一次本地问题后恢复选中。该问题未调用占位 DeepSeek。
- 资料地图五个统计视图均可点击，且结果由服务端全量筛选；重复关系跳转会更新目标 `file_id` 和目录，不会重新打开旧详情弹窗。
- 在 `768 × 900` 视口复核资料卡弹窗、关闭控制和三段正文节选；页面无水平溢出。

## Implementation Checklist

- [x] 对话优先的三栏桌面布局
- [x] 右侧真实来源抽屉
- [x] 管理功能默认收起
- [x] 资料库与知识主题独立入口
- [x] 空状态、回答状态和窄屏状态
- [x] 核心交互与控制台错误检查
- [x] 资料库本机只读副本打开与桌面关联打开
- [x] DeepSeek 单次授权复位与本地问题不触发占位调用
- [x] 资料地图服务端统计筛选、说明卡与重复关系跳转
- [x] `768 × 900` 窄屏资料访问验收

## 本轮资料访问与复核验收

- 验收服务：临时 QA 服务的 `/api/v1/bootstrap` 显示 `configured=true` 的 DeepSeek 配置，且 `document-open-local` 可用；验收期间未授权任何外部调用。
- 资料库打开：点击《旅行社安全规范.pdf》返回“已用本地应用打开只读副本：旅行社安全规范.pdf”，并已另外确认直接桌面关联同样可用。
- 单次授权：DeepSeek 复选框初始值为 `true`，手动取消后为 `false`；提交一次本地问题后恢复为 `true`，该问题没有调用占位 DeepSeek。
- 服务端统计卡：五个视图均可点选并返回全量结果：`all` 36、`readable` 18、`duplicates` 4 个文件（2 组）、`versions` 12 个文件（1 组）、`attention` 0。
- 最新只读扫描：`corpus_a892be41b43245edb2f1bc0489850800`，根目录为 `D:\研学平台数据\平台数据`；36 个文件、18 个目录、18 个正文可读、18 个仅元数据、2 个重复组、1 个版本组、0 个关注项。
- 富说明卡：已检查《研学通平台角色资料提交开发指导手册 20260727》；显示 3 段代表性正文节选、109 个内容单元、约 5011 字符、日期 `2026-07-27`、版本 `V1.0`，并提供范围与权威性的说明和可见表单。
- 关系跳转：从根目录 `corpusfile_0177356...` 的重复关系跳到另一目录 `corpusfile_0748f34...`；目标 `file_id` 与目录均更新，旧详情弹窗未重新打开。
- 窄屏与控制台：在 `768 × 900` 时，`document.scrollWidth = document.clientWidth = 753`；弹窗宽约 714.8 px，内部宽度 700/700，关闭按钮及 3 段正文均可见。浏览器 `error=0`、`warning=0`。
- 截图证据：`D:\全新知识库\.superpowers\sdd\2026-07-29-document-access-corpus-review\qa-rich-document-card-desktop.png` 与 `D:\全新知识库\.superpowers\sdd\2026-07-29-document-access-corpus-review\qa-rich-document-card-768x900.png`。

## Follow-up Polish

- 后续若恢复问答策略优化，可让综合回答层输出稳定的分段结构和来源标签，以进一步贴近视觉稿中的回答组织方式。

final result: passed
