# Embedded Agent UI 设计约束

> 设计 token 来源：`DESIGN-SYSTEM.md`（Pi Web Access curator 设计系统，dark-first 双主题）。
> 组件 token 默认值已按该设计系统落地（EAP_THEME / followup-panel `--agent-*`）；宿主可用 `--eap-*` / `--agent-*` CSS 变量整体切换主题（含浅色变体）。

## Design DNA

- Color mode：dark-first（`--bg #18181e` / `--bg-card #1e1e24` / `--bg-elevated #252530`）；宿主可整体切换浅色变体。
- Primary（accent）：teal `#8abeb7`，其上前景深色 `#18181e`；默认值见 `DESIGN-SYSTEM.md` §1.1。
- Text：#e0e0e0 / muted #909098 / dim #606068；border：#2a2a34（subtle #353540）
- Font：继承宿主 font-family（宿主可引入 Outfit）；hero 显示体建议 Instrument Serif italic；不强制
- Radius：10px（卡片）/ 6px（小控件）/ 999px（pill）；spacing unit：4px
- 组件不得依赖 Triage、Ant Design 或宿主私有主题。

## 组件与状态

- AgentPanel：Run 标题、状态、进度区域和持久化 A2UI Surface；窄容器单列布局。
- ProgressCard：可扫描的状态与说明；使用文本而非仅颜色表达状态。
- EvidenceSummary：只渲染服务器 Catalog 允许的数据引用和文本项。
- ArtifactLink：只发起已授权的短期下载对象，不接收 object key 或原始 S3 URL。
- ApprovalCard：仅在服务器 Surface 带有稳定 approval_id 时可执行，并显示服务器给出的 canonical target 与 request digest；approval_id 不进入浏览器 Action 命令，批准/拒绝操作可键盘访问，提交期间禁用。
- SafeFallback：未知组件显示克制的不可执行提示，不动态加载代码。

覆盖 loading、empty、error、unknown component、submitting、disabled、stale surface 和 narrow layout。禁止 dangerouslySetInnerHTML、任意组件名动态导入和浏览器直连 Sandbox。

## AgentLauncher（Phase 3.6 前端入口）

- 宿主级自由对话浮窗：右下角折叠 pill ↔ 展开面板（消息列表 + 输入框 + Send），不经 server catalog 渲染（与 A2UI Surface 文档无关）。
- 每条消息经 `POST /v1/chat` 创建 Run（SDK `chat()`）；消息原文保留为 Run intent；`onRunCreated` 回传宿主绑定 AgentPanel。
- 样式沿用 EAP_THEME tokens（`position: fixed`），不引入 UI 框架；空白消息不发；每条消息独立 Idempotency-Key。
