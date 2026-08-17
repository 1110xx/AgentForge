docs>
    安全模型与 Sandbox 边界
    ##1.安全结论
    Sandbox Pod内的workspace是否持久化,不决定Agent 是否安全。真正的边界是:不可信Agent 代码能够看到什么身份、调用哪些网络目标、执行哪些工具、读取娜些文件、获得多少资源,以及控制面能否在旧 Pod 失效后拒绝它继续写入。
    本平台采用"Pod可失陷、业务事实不可伪造的设计:Sandbox 是一次Attempt 的临时限离环境:所有重要写入都要经过短时 capability、tenant/Attempt/generation/Lease fence、服务端策略和持久化事务。即便某个 Runtime 进程被模型输出、工具依赖或恶意文件利用,攻击者也不应自动得到Kubernetes API、长期业务凭据、其它租户数据或任意WRITE 权限,
    这不是"容器绝对不会遵途"的保证,普通container hardening 只能降低风险:对hostile code 的强隔离仍需要gVisor、Kata、microVM或等价运行时,并且必须在真实生产节点做 L4 验证。
    2.胁横型
    单#2.1靠要防护的攻击者
    意或被prompt injection 操纵的模型输出:
    Run辅入中的恶意仓库、压缩包、文档、脚本或二进制:
    有的Runtime、解释器、浏览器工具或第三方Tool:
    被盗的用户token、Runtimecapability、Effectcapability 或下载授权: 试图租户访问的合法用户:
    重放 Attept、I generation、I旧 Surface 或旧审批请求的调用方;
    失陷的Sandbox Pod 试图访问 Kubernetes API、metadata service、内部数据库或凭据服务: ITE请求已发达但结果未知时,自动重试造成重复外部操作。
    -Eftectworker暂时不可达时,watchdog 过早把仍在执行的wRITE判为选留,与迟到成功产生竞态。
    2.2主要资产
    租户份、角户身份、集略快照和权限范围:
6Z -Run、Step、Attempt、Checkpoint、审批、Effect 和 Audit 事实: Artifact.WorkspaceSnapshot、Tool result 和业务输入;
    CredentialBroker管理的业务系统凭据:
    capability signing key、OIDCkey、对象存储签名能力:
    KubernetesControl Plane、PostgreSQL、NATS 和对象存储。
    2.3信任区域
    区域信任级别|可以持有的能力|
    PublicControlAPI丨可信控制面丨验证用户身份、解析业务资源与策略、提交领域事务|
    Scheauler/0rchestrator|有限平台权限|领取Attempt,创建/观察/删除SandboxJob:不应拥有业务连接器凭据
    SandboxRuntime|按已失陷设计|仅当前Attempt/generation的短时Runtime capability、临时文件和获批输入
    1PostgreSOL1业务事实源丨持久化所有裁决、恢复、幂等、审计和消息去重事实| ToolGatewey/EffectWorker|受控执行面|在服务器端校验 scopes/capability,通过 Broker 临时取得目标凭据
    ObjectStore|不可变字节存储|Artifact/Snapshotbytes;不能单独决定READY或授权|
    ##3.SandboxPod防选选基线
    3.1Pod格中已实现的控制
5e 每个Attenpt Job 的确定性规格包含
    -image 必须使用she256digest,避免tag 漂移;
        backoffLiit=,业务重试必须由控制面创建新Attempt; runAsNonRootetrue" 容UID/GID为65532
        allowPrivilegeEscalation=false';
    dropALLLinux capabilities:
        readonLyRootFilesysteestrue
        secconpProfile=RuntimeDefault;
        automountServiceAccountToken=false
        enableServiceLinksefalse
        /workspace" /tmp和 inputs使用有限额volume;
    CPu、内存、workspace、tmp 和active deadline 均有显式限制:
    生产默认要求agent-platform-gvisorRuntimeClass;
    Job 完成后可由 TTL 清理,Pod 不成为长期工作站。
    这些控制阳止常见的rootfilesystem写入、Linuxcapability滋用,默认 SAtoken泄露和无限资源占用,但不能消除内核、容器运行时、设备插件或宿主挂载漏洞。
    ###3.2文件系统原则
        /workspace是当前Attempt 的临时工作目录,不是稳定业务事实,也不应直接绑定用户home或控制面文件系统 /inputs只读;输入通过受控staging和校验进入Sandbox
刀 不把Docker socket、container runtime socket、宿主根目录,开发者home,SSH agent,云配置或 Secret volume 挂入 Pod 至核有的宝劳总生形成不可恋Wnrkcn nareSnanshnt/artsfart 社her成扫 #Fny 由 Fherknin
                                        Ln1,Col1
    #安全模型与 Sandbox 边界
    #3.SandboxPod 防逃速基线
    #3.1Pod规格中已实现的控制
    这些控制阻止常见的rootfilesystem写入、LinuxcapabiLity 用、默认 SAtoken泄露和无限资源占用,但不能消除内核、容器运行时、设备插件或宿主挂载漏洞。
    ##3.2文件系统原则
        workspace是当前Attempt 的临时工作目录,不是稳定业务事实,也不应直接绑定用户home 或控制面文件系统。 /inputs只读:输入通过爱控staging和校验进入 Sandbox。
    不把Docker socket、container runtime socket、宿主根目录、开发者home、SSH agent、云配置或 Secret volume 挂入Pod。
    需要恢复的字节必须先形成不可变WorkspaceSnapshot/Artifact,计算checksum,完成扫描和 READY状态,再由Checkpoint 引用。
    恢复到新Pod 时校snapshot checksum、runtime image digest、tenant、Run、ExecutionUnit、source Attempt 和 generation。
    本地filesysteObjectstore 只用于测试和开发:生产使用有versioning、retention、访问审计和加密的 S3-compatible service。
    3.3强离要求
    生产处理不可信代码时,至少选择并验证一种强化运行时
    gvisor:小可见Linuxsyscall/内核攻击面:
E8 KataContainers或microvM:提高内核与硬件级隔离; 立节点池:把Sandbox与控制面、数据库、密钥服务分开;
    高风险工作负载立集群:进一步缩小blastradius.
    本工程He默认表达gVisor contract;Kind profile 显式不设置 RuntimeClass,因为普通Kind 不能证明gVisor。只有在真实节点验证 runtime handler、逃逸测试、性能和故障行为后,才能声称 hostile-code isolation 通过。
    4.Kubernetes身份与RBAC
9e 4.1职责分真
    ControlPlane ServiceAccount 仅获得create tokenreviews用于验证projected token。
    Orchestrator ServiceAccount 仅能在 Sandbox namespace 创建、观察、删除 Job,并只读观察 Pod。
    SandboxServiceAccount 没有RoleBinding,不拥有Kubernetes API 业务权限。
    ControlPlane 和 Orchestrator 使用各自audience-bound、短时projected token,不共享默认 SA token。
    ##4.2-次性Runtimebootstrap
    1.Kubermetes TokenReview B认证 token;
    2.audience 和过期时间正确:
    3.mamespace.ServiceAccount 和 Pod UID 与持久化 binding 一致;
1e5 4.Attempt ID.generation、Run 和 ExecutionUnit 与binding 一致; 5.bootstrap claim 尚未被消费。
    成功后发多新的短时Runtimecapability。projected token 不再用于Tool 调用;同一Pod/Attempt/generation的claim不能重复消费。
    ###4.3generation 与Lease fence
    Runtine capability 定tenant_id
                                        execution_unit_id、attempt_id、generation、audience、scopes、jti和过期时间。每次受保护操作还要检查数据库中当前 Attempt、generation、active Lease、Lease owner、version与expiration
                                run_id"
    因此,即使旧Po在网终分区后重新连通,它持有的token未到期,也会被LIvefence 以STALE_GENERATION、LEASE_EXPIRED或owner/version mismatch 拒绝。销毁Pod 只是清理动作,数据库fence才是正确性边界。
    #5.网络离
    生产NetworkPolicy 采用两个namespace 默认deny:
    Sandbox仅允许DNS和明确的 Control API.model proxy、Tool Gateway,Artifact proxy、OTel exporter 等目的地; Sandbox 不允许Kubernetes API Server egress:
    Sandbox 不直接访PostgreSOL,NATS,对象存储管理端或Credential Broker;
    只有指定 Control Plane Pod访同 API Server TokenReview:
    ingress 只开放确有业务常要的 service-to-service path;
    一对企业网络、防火墙,serviceesh 和云security group 做同方向限制,避免仅依赖可绕过或未启用的 CNI policy
    NetworkPolicy 不能防DNSrebinding.被允许代理自身的SSRF或应用层越权。代理仍需校验固定upstream、禁止caller-Supplied URL、限制协议/端口、请求大小、响应大小和超时。
    #6.多租户身份与宿主授权
    PubLic API 的租户和用户来自AuthContextProvi.der,不接受客户端自报tenant header,创建Run 时:
        resource_ref和host_context_ref必须是opaque referece,不能包含uRL.路径,userinfo 或header
        ResourceResolver返回 canonical ID.owner.classification,version 和 digest,并目 tenant 必须与 RequestContext一致; HostContextVerifier 把 context 绑定同-tenant 和 actor:
                                        n1.Col1 Spces4u
    安全模型与Sandbox 边界
    #6.多租户身份与宿主授权
    PublicAPI 的租户和用户来自AuthContextProvider不接受客户端自报tenant header。创建 Run 时:
        resource_ref和host_context_ref必须是opaque reference,不能包含URL、路径、userinfo或header;
        ResourceResolver返回canonical ID、owner、classification、version 和 digest,井且 tenant 必须与 RequestContext 一致; HostContextVerifier把context 绑定同- tenant 和 actori
        所有权威事实核canonicalize 并形成 immutable authorization snapshot digest. PolicyContextProvider返回version、digest、scopes 和budget,budget 禁止承载credential、token、secret、endpoint 或URL;
    对不存在和聘租户源应统一返回NOT_FOuND或安全错误,避免枚举。所有存储查询、唯一约束、缓存key、Outbox/Inbox、Artifact 和 capability 都必须包含tenant boundary。
    #7、Tool与Credential安全
    #7.1READ/LOCALTo0l
    ToolGateway只有在以下集合交集包含ToolSpec所需scope时才执行:
    principal scopes n Run policy scopes n ToolGrant scopes n ToolSpec scopes n Runtime capability scopes
    此外还检童Tooname/version、grant active 状态、Run/Attempt 绑定、resource prefix、input/output 字段、结果大小、超时、call ID 幂等和 generation,模型不能通过参数选择Connector endpoint 或任意URL
    CredentialBroker 根据服务端已经确认的 tenant、connector 和canonicalresource 获取短时凭据。CredentialMaterial只存在于 Gateway/Effect worker内存中,不返回Runtime,不写入Checkpoint,事件、Artifact 或 trace。 ##7.2WRITETo0与审批
    Runtine对wRITE请求只可创建 ActionProposal,proposal 固化:
    toolname/version/specdigest;
        connector name;
        required scopes;
16e canonicalpayload digest 和payload ref: canonical target;
    request digest、riskclass、Attempt、generation 和过期时间。
    审批先提交Checkpoint 并释放 Lease。ApprovaLDecisionService是公开可组装的持久化服务:它验证审批权限、tenant、绑定、摘要、版本、过期时阁和幂等键,然后在一个事务内写 Approval、ActionPraposal、EffectLedger、Event、Audit 与 Outbox。
    批准不等于浏美器执行Tool真正外部写操作只通过内部 effect worker route 和DurableEffectExecutor发生,调用内部route 需要effect-worker service identity,同时在x-Effect-Capability中携带 tenant/effect/approval/request/too l/spec-digest/connector/target/ scopes-boundcapability.普通publicrouter 不暴露Effect execute 或reconcile.
        PREPARED
    executor_lease_expires_at" 领取Effect 前,internalexecuteroute 先验证effect-workerservice identity,把验证后的worker subject 作为executar_id传给 Executor.Executor在同一持久化状态迁移中写入该executor_id、递增的execution_epoch和按数据库时间计算的
                            PREPARED不允许伪造owner/epoch/Lease,EXECUTING必须同时存在三者,终态或UNKNowN则清除 active lease,这不是仅记录worker 和epoch 的可观测字段:每个 Connector finish都必须同时命中自己领取时的expectedexecutor_id
    Effect领数与Connectorfinish的Audit actor是实际验证通过的worker subject,details记录对应execution_epoch:因此不会用一个泛化service:effect-executor
    ##7.3Effect纯未知 身份盖真正执行者。
    一旦Connectordispatch开始,超时、进程前溃或未分类异常可能表示外部系统已成功但响应丢失,此时Effect必须进入UNkNOwN
    不自动新dispatch;
    对失联executor的watchdog必须提交expectedexecutor_id+execution_epoch,
    优先按renote_operation_id或effect_key查询外部系统; mer/epoch会中后,watchoog仍只能在executionlease已按数据库时间到期后执行EXECUTING-UNKNOwN:提前调用会以EFFECT EXEcUToR_STILL_ACTIVE拒绝,Audit 记录watchdog subject 以及被裁决的worker/epoch:
    Run、ExecutionUnit与Step显式进入NEEDS_ATTENTION,不再伪装成仍待审批:
    reconciliation 必须由effect-reconcilerservice identity 调用内部route,并携带独立的reconciliation capability
    对账为FAILED还必须由authorizer确认executor inactive 和observation stable,并确认账本不再存在active executar leasei任一fence 缺失则以
    由授权reconciliation把分态变为SUCCEEDED或FAILED;迟到的LiveConnector成功也可把UNKNOwN收敛为SUCCEEDED,不能丢弃已发生的外部事实: 无法判定时进入人工处置和告量。 RECONCILIATION_FENCE_REQUIRED拒绝:
    真实语义是PostgreSQL/NATs的at-Least-once 运输,加上持久化Ledger、稳定effect_keyConnector 对 idempotency key 的承诺以及uNkNowNreconciliation,得到effectively-ance 的外部结果。平台不宣称数据库、消息系统和外部业务系统存在理论上的exactly-once。
    当Effect 已可靠收效为FAILED时,恢复也不是重放旧Effect,publicPoST/vi/runs/{run_id)/effects/{effect_id}/recover需要effects:recoverscope、
    NEEDS_ATTENTION转入可恢复状态并发出调度通知,新Attenpt必须从COMMITTEDCheckpoint 重新规划、创建新ActionProposal和获取新审批,才能发生下一次wRITE。 If-Match和Idempotency-Key:事务保留原FAILEDledger 不变,仅把Run/ExecutionUnit/Step 从
    #B.浏览器、A2UI与嵌入安全
    浏览器只传用户意图和稳定1D,不是授权决策点:
                                        Ln1,Col1Space4 UTE
    安全模型与 Sandbox 边界
    ##7.Tool与Credential安全
    ##7.3Effect结果末知
    #B.浏览器、A2UI与嵌入安全
    浏宽器只传用户意图和稳定ID、不是授权决策点:
    不接浏宽选择tenant、role、scope、Tool、Connector、credential、canonical target、payload ref 或object key:
        approvalid是服务端ApprovalCard 的持久化事实,但浏览器Action不回传或选择它:服务端从 Surface revision 反查; displayed_d1gest用于检测用户看到的内容与待执行请求是否一致:
    staleSurface、要mismatch或重复action fail closed
    AzUI R允许固定catalog:ProgressCard、EvidenceSummary dangerouslySetInnerh7ML,不动态加载模型指定模块 ApprovalCard
                                        ArtifactCard服务端拒绝HTML、script、style、event handler、URL、dynamicimport、危险 scheme、未知 prop、超大文档和深层结构;前端对未知 component 使用不可执行fallback,不使用
    嵌入宿主应通过同派反向代理或严格allowlist CoRS 提供API:token 获取函数由宿主实现,SDK 不从local storage 猜测权限。导航和下载通过显式Host Bridge 回调,不能让 Surface直接改变顶层窗口或发起任意网络请求。
    9、Artifact与下授权
    -对象key不选入publicSurface 或普通业务响应。 ArtifactVersion必须与 tenant、Run、source Attempt/generation、checksum、size、media type 和lineage 绑定。
        下载前新询 READY Artifact、校验Run owmership,井调用宿主ArtifactAccessPolicy.
    签发的uRL默认短时,最长不超过15分钟:只允许显式 scheme,禁止userinfo和fragment。
    授权记录写入Audit;前端还校验返回的artifactID/version与请求一致。
    对敏感类型增加内容扫、DLP、retention、legalhold、watermark和一次性代理下载应由生产适配实现。
    10.数据、日志与可观测性
    PostgreSQL、NATS、对象存储和备份应启用传输与静态加密:密钥由外部 KMS/Secret manager 管理,当前代码提供适配边界,不证明某个部署已经启用这些企业能力。 Event/Outbox只运输稳定ID和必要版本;NATSenvelope拒绝URL和原始业务payload。
    -AuditEvent是独立合规事实,与受保护mutation同事务写入:应用日志和trace 不能替代它。 trace attribute不放credential、原始prompt、Tool参数或 Artifact 内容;Run/Attempt/Artifact/user/Pod ID 不作为Prometheus 高基数标签。
    日志输出前做token、header、querystring、对象URL和业务字段redaction:导出失败不能回滚或改变业务事务。
    11.正确性告警与应急
    以下情况不是管通SLO错误,而是零容忍正确性事故:
    未授权WRITE:
    审计统过:
    stale generation/fence write 被接受; 跨租户访问:
    同一Executionunit 存在多个活跃Attempt;
    已提交Checkpoint丢失;
    Artifact/Snapshot checksum不匹配
    检测到事签时优先著amission和对应 Efect worker,撤销capability/credential,保留PostgresQL 与对象版本证据,再根据deploy/runbooks/处理。不要为了"恢复服务"删除Run、Checkpoint、Lease、Effect或Audit 历史。
    #12.剩余风险与上线前证据
    即使所有静态清单和单元测试通过,仍存在以下剩余风险:
    -容器运行时、内核、ONI、CSI、GPU/设备插件逃选漏洞;
    被允许的modeL/toolproxy 发生SSRF 或credential confused deputy; Connector对effect_key等实现不正确;
    对象存储versioning/retention.数据库PITR或NATSR3配置与声明不一致:
    OIDC、KMS、Secretmanager,镜售供应锁和节点基线被攻破:
    高负载时租户公平性、成本和队列退避不符合预期:
    人工审批用户被社会工程或看到不完整上下文。
    因此发布证据必须分层:
    L1:本地contracts、状态机、权限、恢复.portability、wheel和前端安全测试: L2:真实PostgreSQL/NATS/MinIO的Compose集成:
    -L3:真实Kind Job、RBAC.projected identity 和 NetworkPolicyi
    一L4:生产等价gVisor/隔离节点、HA/failover/PITR,对象版本恢复,凭据轮换、外部Connector幂等、攻击演练和容量成本
    某一级没有运行或环境不具备时必须标为UNVERIFIED,不能用YAML有在,unittest或下一级的mock结果代替。
