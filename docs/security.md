# 安全模型与 Sandbox 边界

## 1. 安全结论

Sandbox Pod 内的 workspace 是否持久化，不决定 Agent 是否安全。真正的边界是：不可信 Agent 代码能够看到什么身份、调用哪些网络目标、执行哪些工具、读取哪些文件、获得多少资源，以及控制面能否在旧 Pod 失效后拒绝它继续写入。

本平台采用"Pod 可失陷、业务事实不可伪造"的设计：Sandbox 是一次 Attempt 的临时隔离环境；所有重要写入都要经过短时 capability、tenant/Attempt/generation/Lease fence、服务端策略和持久化事务。即便某个 Runtime 进程被模型输出、工具依赖或恶意文件利用，攻击者也不应自动得到 Kubernetes API、长期业务凭据、其它租户数据或任意 WRITE 权限。

这不是"容器绝对不会逃逸"的保证，普通 container hardening 只能降低风险：对 hostile code 的强隔离仍需要 gVisor、Kata、microVM 或等价运行时，并且必须在真实生产节点做 L4 验证。

## 2. 威胁模型

### 2.1 需要防护的攻击者

- 恶意或被 prompt injection 操纵的模型输出；
- Run 输入中的恶意仓库、压缩包、文档、脚本或二进制；
- 有漏洞的 Runtime、解释器、浏览器工具或第三方 Tool；
- 被盗的用户 token、Runtime capability、Effect capability 或下载授权；
- 试图跨租户访问的合法用户；
- 重放旧 Attempt、旧 generation、旧 Surface 或旧审批请求的调用方；
- 失陷的 Sandbox Pod 试图访问 Kubernetes API、metadata service、内部数据库或凭据服务；
- WRITE 请求已发出但结果未知时，自动重试造成重复外部操作；
- Effect worker 暂时不可达时，watchdog 过早把仍在执行的 WRITE 判为遗留，与迟到成功产生竞态。

### 2.2 主要资产

- 租户身份、用户身份、策略快照和权限范围；
- Run、Step、Attempt、Checkpoint、审批、Effect 和 Audit 事实；
- Artifact、WorkspaceSnapshot、Tool result 和业务输入；
- Credential Broker 管理的业务系统凭据；
- capability signing key、OIDC key、对象存储签名能力；
- Kubernetes Control Plane、PostgreSQL、NATS 和对象存储。

### 2.3 信任区域

| 区域 | 信任级别 | 可以持有的能力 |
| --- | --- | --- |
| Public Control API | 可信控制面 | 验证用户身份、解析业务资源与策略、提交领域事务 |
| Scheduler/Orchestrator | 有限平台权限 | 领取 Attempt，创建/观察/删除 Sandbox Job；不应拥有业务连接器凭据 |
| Sandbox Runtime | 按已失陷设计 | 仅当前 Attempt/generation 的短时 Runtime capability、临时文件和获批输入 |
| PostgreSQL | 业务事实源 | 持久化所有裁决、恢复、幂等、审计和消息去重事实 |
| Tool Gateway/Effect Worker | 受控执行面 | 在服务器端校验 scopes/capability，通过 Broker 临时取得目标凭据 |
| Object Store | 不可变字节存储 | Artifact/Snapshot bytes；不能单独决定 READY 或授权 |

## 3. Sandbox Pod 防逃逸基线

### 3.1 Pod 规格中已实现的控制

每个 Attempt Job 的确定性规格包含：

- image 必须使用 sha256 digest，避免 tag 漂移；
- backoffLimit=0，业务重试必须由控制面创建新 Attempt；
- runAsNonRoot=true，容器 UID/GID 为 65532；
- allowPrivilegeEscalation=false；
- drop ALL Linux capabilities；
- readOnlyRootFilesystem=true；
- seccompProfile=RuntimeDefault；
- automountServiceAccountToken=false；
- enableServiceLinks=false；
- /workspace、/tmp 和 inputs 使用有限额 volume；
- CPU、内存、workspace、tmp 和 active deadline 均有显式限制；
- 生产默认要求 agent-platform-gvisor RuntimeClass；
- Job 完成后可由 TTL 清理，Pod 不成为长期工作站。

这些控制阻止常见的 root filesystem 写入、Linux capability 滥用、默认 SA token 泄露和无限资源占用，但不能消除内核、容器运行时、设备插件或宿主挂载漏洞。

### 3.2 文件系统原则

- /workspace 是当前 Attempt 的临时工作目录，不是稳定业务事实，也不应直接绑定用户 home 或控制面文件系统。
- /inputs 只读；输入通过受控 staging 和校验进入 Sandbox。
- 不把 Docker socket、container runtime socket、宿主根目录、开发者 home、SSH agent、云配置或 Secret volume 挂入 Pod。
- 需要恢复的字节必须先形成不可变 WorkspaceSnapshot/Artifact，计算 checksum，完成扫描和 READY 状态，再由 Checkpoint 引用。
- 恢复到新 Pod 时校验 snapshot checksum、runtime image digest、tenant、Run、ExecutionUnit、source Attempt 和 generation。
- 本地 filesystem ObjectStore 只用于测试和开发：生产使用有 versioning、retention、访问审计和加密的 S3-compatible service。

### 3.3 强隔离要求

生产处理不可信代码时，至少选择并验证一种强化运行时：

- gVisor：缩小可见 Linux syscall/内核攻击面；
- Kata Containers 或 microVM：提高内核与硬件级隔离；
- 独立节点池：把 Sandbox 与控制面、数据库、密钥服务分开；
- 高风险工作负载独立集群：进一步缩小 blast radius。

本工程 Helm 默认表达 gVisor contract；Kind profile 显式不设置 RuntimeClass，因为普通 Kind 不能证明 gVisor。只有在真实节点验证 runtime handler、逃逸测试、性能和故障行为后，才能声称 hostile-code isolation 通过。

## 4. Kubernetes 身份与 RBAC

### 4.1 职责分离

- Control Plane ServiceAccount 仅获得 create tokenreviews 用于验证 projected token。
- Orchestrator ServiceAccount 仅能在 Sandbox namespace 创建、观察、删除 Job，并只读观察 Pod。
- Sandbox ServiceAccount 没有 RoleBinding，不拥有 Kubernetes API 业务权限。
- Control Plane 和 Orchestrator 使用各自 audience-bound、短时 projected token，不共享默认 SA token。

### 4.2 一次性 Runtime bootstrap

1. Kubernetes TokenReview 认证 token；
2. audience 和过期时间正确；
3. namespace、ServiceAccount 和 Pod UID 与持久化 binding 一致；
4. Attempt ID、generation、Run 和 ExecutionUnit 与 binding 一致；
5. bootstrap claim 尚未被消费。

成功后签发新的短时 Runtime capability。projected token 不再用于 Tool 调用；同一 Pod/Attempt/generation 的 claim 不能重复消费。

### 4.3 generation 与 Lease fence

Runtime capability 绑定 tenant_id、run_id、execution_unit_id、attempt_id、generation、audience、scopes、jti 和过期时间。每次受保护操作还要检查数据库中当前 Attempt、generation、active Lease、Lease owner、version 与 expiration。

因此，即使旧 Pod 在网络分区后重新连通，它持有的 token 未到期，也会被 live fence 以 STALE_GENERATION、LEASE_EXPIRED 或 owner/version mismatch 拒绝。销毁 Pod 只是清理动作，数据库 fence 才是正确性边界。

## 5. 网络隔离

生产 NetworkPolicy 采用两个 namespace 默认 deny：

- Sandbox 仅允许 DNS 和明确的 Control API、model proxy、Tool Gateway、Artifact proxy、OTel exporter 等目的地；
- Sandbox 不允许 Kubernetes API Server egress；
- Sandbox 不直接访问 PostgreSQL、NATS、对象存储管理端或 Credential Broker；
- 只有指定 Control Plane Pod 访问 API Server TokenReview；
- ingress 只开放确有业务需要的 service-to-service path；
- 对企业网络、防火墙、service mesh 和云 security group 做同方向限制，避免仅依赖可绕过或未启用的 CNI policy。

NetworkPolicy 不能防 DNS rebinding、被允许代理自身的 SSRF 或应用层越权。代理仍需校验固定 upstream、禁止 caller-supplied URL、限制协议/端口、请求大小、响应大小和超时。

## 6. 多租户身份与宿主授权

Public API 的租户和用户来自 AuthContextProvider，不接受客户端自报 tenant header。`AuthContextProvider` 是**唯一接头**，生产路径为 OIDC（`security/oidc.py`）：

- 启用：`AGENT_PLATFORM_AUTH_PROVIDER=oidc` + `AGENT_PLATFORM_OIDC_ISSUER`（必填）+ `AGENT_PLATFORM_OIDC_AUDIENCE`（必填）；可选 `AGENT_PLATFORM_OIDC_JWKS_URI`（跳过 discovery）、`AGENT_PLATFORM_OIDC_TENANT_CLAIM`（默认 `tenant_id`）、`AGENT_PLATFORM_OIDC_ACTOR_CLAIM`（默认 `sub`）、`AGENT_PLATFORM_OIDC_SCOPE_CLAIM`（默认 `scope,scopes`）；K8s API 工厂 `reference/k8s_container.create_container` 经 `create_auth_provider_from_env` 选择；参考静态 bearer（`ReferenceLocalAuth`）为 dev/kind 默认回退。
- 校验链：Bearer RS256 JWT → discovery/JWKS（TTL 缓存）→ kid 选钥 → 签名验签 → iss/aud/exp/nbf（±30s leeway）→ claims 映射 tenant_id/actor_id/scopes。**契约**：IdP 的 `scope`（或自定义 scopes claim）直接命名平台 scopes（`runs:create` …）；tenant 与 actor 仅来自 IdP 签发 claims，客户端不可自报。
- 内部 Runtime capability 已 HMAC 签名化（`security/runtime_tokens.py`，`rt.v1.*`，密钥 `AGENT_PLATFORM_CAPABILITY_KEY`），不再有明文确定性派生 token（详见 docs/phase-4.5-security-decisions.md §2.3）。

创建 Run 时：

- resource_ref 和 host_context_ref 必须是 opaque reference，不能包含 URL、路径、userinfo 或 header；
- ResourceResolver 返回 canonical ID、owner、classification、version 和 digest，并且 tenant 必须与 RequestContext 一致；
- HostContextVerifier 把 context 绑定同一 tenant 和 actor；
- 所有权威事实都 canonicalize 并形成 immutable authorization snapshot digest；
- PolicyContextProvider 返回 version、digest、scopes 和 budget，budget 禁止承载 credential、token、secret、endpoint 或 URL；
- 对不存在和跨租户资源统一返回 NOT_FOUND 或安全错误，避免枚举；
- 所有存储查询、唯一约束、缓存 key、Outbox/Inbox、Artifact 和 capability 都必须包含 tenant boundary。

## 7. Tool 与 Credential 安全

### 7.1 READ/LOCAL Tool

Tool Gateway 只有在以下集合交集包含 ToolSpec 所需 scope 时才执行：

```text
principal scopes ∩ Run policy scopes ∩ ToolGrant scopes ∩ ToolSpec scopes ∩ Runtime capability scopes
```

此外还检查 Tool name/version、grant active 状态、Run/Attempt 绑定、resource prefix、input/output 字段、结果大小、超时、call ID 幂等和 generation。模型不能通过参数选择 Connector endpoint 或任意 URL。

Credential Broker 根据服务端已经确认的 tenant、connector 和 canonical resource 获取短时凭据。CredentialMaterial 只存在于 Gateway/Effect worker 内存中，不返回 Runtime，不写入 Checkpoint、事件、Artifact 或 trace。

### 7.2 WRITE Tool 与审批

Runtime 对 WRITE 请求只可创建 ActionProposal，proposal 固化：

- tool name/version/spec digest；
- connector name；
- required scopes；
- canonical payload digest 和 payload ref；
- canonical target；
- request digest、risk class、Attempt、generation 和过期时间。

审批先提交 Checkpoint 并释放 Lease。ApprovalDecisionService 是公开可组装的持久化服务：它验证审批权限、tenant、绑定、摘要、版本、过期时间和幂等键，然后在一个事务内写 Approval、ActionProposal、EffectLedger、Event、Audit 与 Outbox。

批准不等于浏览器执行 Tool。真正外部写操作只通过内部 effect worker route 和 DurableEffectExecutor 发生，调用内部 route 需要 effect-worker service identity，同时在 `X-Effect-Capability` 中携带 tenant/effect/approval/request/tool/spec-digest/connector/target/scopes-bound capability。普通 public router 不暴露 Effect execute 或 reconcile。

领取 Effect 前，internal execute route 先验证 effect-worker service identity，把验证后的 worker subject 作为 executor_id 传给 Executor。Executor 在同一持久化状态迁移中写入该 executor_id、递增的 execution_epoch 和按数据库时间计算的 executor_lease_expires_at。PREPARED 不允许伪造 owner/epoch/Lease，EXECUTING 必须同时存在三者，终态或 UNKNOWN 则清除 active lease。这不是仅记录 worker 和 epoch 的可观测字段：每个 Connector finish 都必须同时命中自己领取时的 expected executor_id。

Effect 领取与 Connector finish 的 Audit actor 是实际验证通过的 worker subject，details 记录对应 execution_epoch；因此不会用一个泛化 `service:effect-executor` 身份盖住真正执行者。

### 7.3 Effect 结果未知

一旦 Connector dispatch 开始，超时、进程崩溃或未分类异常可能表示外部系统已成功但响应丢失，此时 Effect 必须进入 UNKNOWN：

- 不自动重新 dispatch；
- 对失联 executor 的 watchdog 必须提交 expected executor_id + execution_epoch；
- 优先按 remote_operation_id 或 effect_key 查询外部系统；
- owner/epoch 命中后，watchdog 仍只能在 execution lease 已按数据库时间到期后执行 EXECUTING→UNKNOWN：提前调用会以 EFFECT_EXECUTOR_STILL_ACTIVE 拒绝，Audit 记录 watchdog subject 以及被裁决的 worker/epoch；
- Run、ExecutionUnit 与 Step 显式进入 NEEDS_ATTENTION，不再伪装成仍待审批；
- reconciliation 必须由 effect-reconciler service identity 调用内部 route，并携带独立的 reconciliation capability；
- 对账为 FAILED 还必须由 authorizer 确认 executor inactive 和 observation stable，并确认账本不再存在 active executor lease；任一 fence 缺失则以 RECONCILIATION_FENCE_REQUIRED 拒绝；
- 由授权 reconciliation 把状态变为 SUCCEEDED 或 FAILED；迟到的 Live Connector 成功也可把 UNKNOWN 收敛为 SUCCEEDED，不能丢弃已发生的外部事实；
- 无法判定时进入人工处置和告警。

真实语义是 PostgreSQL/NATS 的 at-least-once 运输，加上持久化 Ledger、稳定 effect_key、Connector 对 idempotency key 的承诺以及 UNKNOWN reconciliation，得到 effectively-once 的外部结果。平台不宣称数据库、消息系统和外部业务系统存在理论上的 exactly-once。

当 Effect 已可靠收敛为 FAILED 时，恢复也不是重放旧 Effect。public `POST /v1/runs/{run_id}/effects/{effect_id}/recover` 需要 `effects:recover` scope、If-Match 和 Idempotency-Key：事务保留原 FAILED ledger 不变，仅把 Run/ExecutionUnit/Step 从 NEEDS_ATTENTION 转入可恢复状态并发出调度通知，新 Attempt 必须从 COMMITTED Checkpoint 重新规划、创建新 ActionProposal 和获取新审批，才能发生下一次 WRITE。

## 8. 浏览器、A2UI 与嵌入安全

浏览器只传用户意图和稳定 ID，不是授权决策点：

- 不接受浏览器选择 tenant、role、scope、Tool、Connector、credential、canonical target、payload ref 或 object key；
- approval_id 是服务端 ApprovalCard 的持久化事实，但浏览器 Action 不回传或选择它；服务端从 Surface revision 反查；
- displayed_digest 用于检测用户看到的内容与待执行请求是否一致；
- stale Surface、摘要 mismatch 或重复 action fail closed。

A2UI 只允许固定 catalog：ProgressCard、EvidenceSummary、ApprovalCard、ArtifactCard。服务端拒绝 HTML、script、style、event handler、URL、dynamic import、危险 scheme、未知 prop、超大文档和深层结构；前端对未知 component 使用不可执行 fallback，不使用 dangerouslySetInnerHTML，不动态加载模型指定模块。

嵌入宿主应通过同源反向代理或严格 allowlist CORS 提供 API：token 获取函数由宿主实现，SDK 不从 localStorage 猜测权限。导航和下载通过显式 Host Bridge 回调，不能让 Surface 直接改变顶层窗口或发起任意网络请求。

## 9. Artifact 与下载授权

- 对象 key 不进入 public Surface 或普通业务响应。
- ArtifactVersion 必须与 tenant、Run、source Attempt/generation、checksum、size、media type 和 lineage 绑定。
- 下载前重询 READY Artifact、校验 Run ownership，并调用宿主 ArtifactAccessPolicy。
- 签发的 URL 默认短时，最长不超过 15 分钟：只允许显式 scheme，禁止 userinfo 和 fragment。
- 授权记录写入 Audit；前端还校验返回的 artifact ID/version 与请求一致。
- 对敏感类型增加内容扫描、DLP、retention、legal hold、watermark 和一次性代理下载应由生产适配实现。

## 10. 数据、日志与可观测性

- PostgreSQL、NATS、对象存储和备份应启用传输与静态加密：密钥由外部 KMS/Secret manager 管理，当前代码提供适配边界，不证明某个部署已经启用这些企业能力。
- Event/Outbox 只运输稳定 ID 和必要版本；NATS envelope 拒绝 URL 和原始业务 payload。
- AuditEvent 是独立合规事实，与受保护 mutation 同事务写入：应用日志和 trace 不能替代它。
- trace attribute 不放 credential、原始 prompt、Tool 参数或 Artifact 内容；Run/Attempt/Artifact/user/Pod ID 不作为 Prometheus 高基数标签。
- 日志输出前做 token、header、querystring、对象 URL 和业务字段 redaction：导出失败不能回滚或改变业务事务。

## 11. 正确性告警与应急

以下情况不是普通 SLO 错误，而是零容忍正确性事故：

- 未授权 WRITE；
- 审计绕过；
- stale generation/fence write 被接受；
- 跨租户访问；
- 同一 ExecutionUnit 存在多个活跃 Attempt；
- 已提交 Checkpoint 丢失；
- Artifact/Snapshot checksum 不匹配。

检测到事故时优先停止 admission 和对应 Effect worker，撤销 capability/credential，保留 PostgreSQL 与对象版本证据，再根据 `deploy/runbooks/` 处理。不要为了"恢复服务"删除 Run、Checkpoint、Lease、Effect 或 Audit 历史。

## 12. 剩余风险与上线前证据

即使所有静态清单和单元测试通过，仍存在以下剩余风险：

- 容器运行时、内核、CNI、CSI、GPU/设备插件逃逸漏洞；
- 被允许的 model/tool proxy 发生 SSRF 或 credential confused deputy；
- Connector 对 effect_key 等实现不正确；
- 对象存储 versioning/retention、数据库 PITR 或 NATS R3 配置与声明不一致；
- OIDC、KMS、Secret manager、镜像供应链和节点基线被攻破；
- 高负载时租户公平性、成本和队列退避不符合预期；
- 人工审批用户被社会工程或看到不完整上下文。

因此发布证据必须分层：

- L1：本地 contracts、状态机、权限、恢复、portability、wheel 和前端安全测试；
- L2：真实 PostgreSQL/NATS/MinIO 的 Compose 集成；
- L3：真实 Kind Job、RBAC、projected identity 和 NetworkPolicy；
- L4：生产等价 gVisor/隔离节点、HA/failover/PITR、对象版本恢复、凭据轮换、外部 Connector 幂等、攻击演练和容量成本。

某一级没有运行或环境不具备时必须标为 UNVERIFIED，不能用 YAML 存在、unit test 或下一级的 mock 结果代替。
