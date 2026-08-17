# Embedded Agent UI 设计约束

## Design DNA

- Color mode：light。允许宿主通过 `--eap-css` variables 覆盖。
- Primary：#2563eb；background：#ffffff / #f8fafc
- Text：#0f172a；secondary：#475569；border：#cbd5e1
- Font：继承宿主 font-family；base size 14px
- Radius：8px；spacing unit：4px
- 组件不得依赖 Triage、Ant Design 或宿主私有主题。

## 组件与状态

- AgentPanel：Run 标题、状态、进度区域和持久化 A2UI Surface；窄容器单列布局。
- ProgressCard：可扫描的状态与说明；使用文本而非仅颜色表达状态。
- EvidenceSummary：只渲染服务器 Catalog 允许的数据引用和文本项。
- ArtifactLink：只发起已授权的短期下载对象，不接收 object key 或原始 S3 URL。
- ApprovalCard：仅在服务器 Surface 带有稳定 approval_id 时可执行，并显示服务器给出的 canonical target 与 request digest；approval_id 不进入浏览器 Action 命令，批准/拒绝操作可键盘访问，提交期间禁用。
- SafeFallback：未知组件显示克制的不可执行提示，不动态加载代码。

覆盖 loading、empty、error、unknown component、submitting、disabled、stale surface 和 narrow layout。禁止 dangerouslySetInnerHTML、任意组件名动态导入和浏览器直连 Sandbox。
