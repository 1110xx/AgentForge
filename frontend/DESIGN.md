Embedded Agent UI 设计约束
##Design DNA
Color mode:light.允宿主通过 -eap-cSsvariables覆盖
Primary:#2563ebbackground:#ffffff#fBfafc
Text:#f172a:secondary:#475569:border:#cbd5e1
Font:承宿主font-family:base size14px
Radius:Spx:spacing unit:4px
组件不得依赖 Triage、AntDesi.gn 或宿主私有主题。
#组件与状态
AgentPanel:Run标题、状态、进度区域和持久化AzUI Surface:窄容器单列布局
ProgressCard:可扫描的状态与说明:使用文本而非仅颜色表达状态。
EvidenceSummary:只染服务器Catalog允许的数据引用和文本项。
ArtifactLink:只敷发已授权的短期下载对象,不接objectkey或原始S3URL。 ApprovalCard:仅在服务器 Surface 带有稳定approval_id时可执行,并显示服务器给出的canonical target 与request digest;approval_id不进入浏览器 Action 命令,批准/拒绝操作可键盘访问,提交期间禁用,
SafeFallback:未知组件显示克制的不可执行提示,不动态加载代码。
盖oading、empty、error、unknown component、submitting、disabled、stale surface 和narrow layout.禁止dangerous lySetInnerHTML、任意组件名动态导入和浏览器直连 Sandbox。
                                        QLn1,Col1Space4uTF-8
