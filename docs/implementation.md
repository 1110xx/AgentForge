    实现说明与证据边界
    #1.阅读方法
    本文件描述当前目录中已经存在的实现,不把目标架构、部署模板或测试替身写成生产事实,判断一个能力时应同时区分四件事:
    1.是否存在领域模型或接口:
    2、是否存在可扶行参考实现:
    3.是否存在生产适配入口:
    4.是否在相应环境完成验证。
    例如,S3adapter、Hemvalues和NetworkPolicy已存在,表示接口和清单可审查;只有真实对像存储、集群和故障演练通过后,才表示生产能力得到验证。
    2、目录结构
        text
    enterprise-agent-platform/
        backend/ Python 包、Alembic和测试
        frontend/ 版本化JSoN Schema、OpenAPI和golden fixtures
        deploy! TypeScriptprotocol/client/catalog/ReactSDk与嵌入示例 Compose、镜像、Helm、Kind、可观测性和 runbooks
        scripts/ docs/ 架构、安全、接入、迁移与交付证据 合同生成、可移植性检查和L1/L2/L3验证入口
    整个旨录是交付单元。backend不通过相对路径读取外部业务源码,前端workspaces不依赖宿主私有组件库,镜像context和脚本也限制在本目录。
    3.Backend块
        】当前职爽|当前实现状态|
        domain contracts/ 1严格Pydantic command、event、view、A2uI、Artifact、error 合同与 schema export |已实现并有 deterministic golden tests|
                Run/Step/ExecutionUnit/Attempt/Lease/Checkpoint/Approval/Effect等不可变记录与纯FSM|已实现:不依赖FastAPI/SQLAlchemy/Kubernetes|
                Run create/cancel/rerun、Attempt reserve/activate/heartbeat、Checkpoint、审批、Lease/FAILED Effect 恢复、查询投影、fair scheduler|已实现共享 services;单Agent admission 是当前策路
        controy
        persistence/
        integration/
        security/ Auth、Resource、HostContext、Policy 四个可信Host Ports和 canonical authorization snapshotProtocol 与验证边界已实现:具体企业系统adapter 由接入方单独实现|
        execution/ Pod bootstrap、TokenReview adapter、Capability issuer/verifier、revocation、generation fence 和 scope intersection|领域与 Kubernetes adapter 已实现:生产key/issuer/revocation backend 需配置 Agent Runtimeloop、KubernetesJob spec与asyncorchestrator adapter|已实现;通用生产worker/factory 不内置|
            Toolregistry/grant、READ gateway、Credential/Connector Ports、ActionProposal/Effect、executor Lease/epoch 与 durable effect execution |已实现共享边界:真实业务Connector 与 Broker 由接入方提供|
        tooLs/
        artifacts/
        /densey. platfom/ Iclesed catalog.Surface validation/persistence、stale action binding 和 approval action handler 已实现,公开 catalog 为四个组件| 可挂pubLicrouter/app、SSE、稳定错误合同、内部Runtime/Effect API|已实现;public 与 internal app 必须在网络和身份层隔离|
        reference/ synthetic可执行纵切与显式API-onlyLocalstack|仅用于测试、学习和copy-and-run:不是生产adapter NATS/Inbox、Outboxpublisher、OTeL/Prometheus、进程factory entrypoint|adapter 与进程入口已实现:不保存领域真相|
    #4.稳定公开Python 边界
    嵌入/适配应用优先以包根导入,追免依赖内部目录结构:
        python
    from enterprise_agent_platform import(
        AgentPlatfornContainer,
        ApprovalDecisionService,
        AuthContextProvider,
        Connector,
        CredentiaLBroker,
        DurableEffectExecutor,
        EffectCapabilityAuthorizer,
        EffectPayloadResolver,
        EffectReconciliationAuthorizer,
        FailedEffectRecoveryService,
        HostContextVerifier,
        PlatformStore,
        PolicyContextProvider,
        ResourceResolver,
        create_app,
        create_in_memory_container,
        create_router,
                                        Doyouw
    重要边界: extensio
7A
                                        Ln1,Col1Spoces4 UTS
    #实现说明与证据边界
    ##4.稳定公开Python 边界
        create_router(container):把公共/v1router 挂到适配应用:
        create_app(container):创建独立publicFastAPI app:
        create_in_memory_containeri...):仅测试和演示,四个可信 Host Ports 仍为必填,不提供 permissive auth;
        AgentPlatformContainer:组合 Store、Control service、Host Ports、ssEnotifier、可选 UI Action 和 Artifact download authorizer: ApprovalDecisionService:消费持久化Approval,一次性写入决定和可选Effect
        DurableEtfectExecutor
    reconciliation 另行使用reconciler identity、evidence 与状态fence; internal route 把已验证 effect-worker subject 作为 executor identity 传入;service 再校验 effect capability、payload digest、审批和 connector binding.并用该 identity/epoch/lease ownership fence 执行和收尾 Effect:UNKNON
        FailedEffectRecveryService:在保留旧FAILEDEffect 不变的提下、从持久化Checkpoint 进入新Attempt 的董新规划流程。
    PlatforStore和PtatformTransaction是生产持久化adapter的公开Protocol.调用方不应导入persistence.tables或修改内部 sQLAlchemy model
    #5.PubLicHTT API
    当前publicrouter 提供:
        Method|Path丨说明|
        'POST GET' v1/runs|校验Idempotency-Key、Host authority 并创建Run|
        GET' GET /v1/runs/{run_id}/events|按after_event_seq分页重放事件|
        POST' /v1/runs/{run_id}/events/stream|SSE增量流,支持Last-Event-ID /v1/runs/{run_id}/cancel|需要If-Match²,提交取消意图
        'POST'
        'POST" /v1/runs/{run_id}/effects/{effect_id}/recover|要effects:recover
            /vi/runs/{run_id}/actions|处理Surface-boundUIAction;需显式组装handler| If-Match和Idempotency-Key保留旧FAILEDEffect 并请求新Attempt重新规划|
        GET I/vi/runs/{run_id}/artifacts/{artifact_id}/versions/{version}/download-authorization|签发短期下载授权:需显式组装policy/signer
lee 公共误练一为api-error/v1,且trace ID、retryable 和稳定code 可被 SDK解析。身份从 Authorization adapter 派生,客户端自报tenant header 会被拒绝。
    #5.1InternalAPI不是publicAPI
1e5 fastapi/internal.py提供Runtime bootstrap/restore/heartbeat、READ tool、Artifact、ActionProposal、final Checkpoint、failure、Surface publish 和 Effect execution。它要求 Runtime capability 或 service identity,必须只在受控内部网络露
    Effect执行路径是:
        text
    PoST/internal/v1/tenants/{tenant_id}/effects/{effect_id}/execute
    Authorization:Bearer <effect-worker service identity>
    X-Effect-Capability:<tenant/effect/approval/request/tool/target-bound token>
    浏策菱和普通业务后端不得调用该路由tenant_id出现在内部 path 不代表 caller 可以选择 tenant:Effect worker identity、capability claims和 PostgreSQL Effect facts必须一致。UNKNOwN 对账使用分离的 internalreconcile route 与reconciler identity;对账为FAILED时还 要通过executor inactive和stable observation fence.
    6.持久化实现
    ##6.1Postgre5QL 事实
    SOLAlchemyStore 与Alembic盖Run、authorization snapshot、Step、ExecutionUnit、Attempt、Lease、Checkpoint、WorkspaceSnapshot、Artifact/Version、ActionProposal、Approval、EffectLedger、UiSurface/Revision、RunEvent、Idempotency、Outbox、Inbox 和 Audit 等 事实。
    正确性依赖:
        tenant-qualified key 和查询;
        database time:
        versionCAS;
        每Run 严格选增event sequence;
        active Attempt/Lease-约束
        generation fence:
        mutation、Audit.Outbox/Inbox同事%:
        Effecteffect_key唯-性;
    Effect 领取时的已验证workerexecutorid进增execution_epoch和 database-time execution lease shape/CAS,以及 finish/watchdog 对expected owner/epoch的强制匹配: Checkpoint只引用READYimiutableobjects,
    ##6.2进程内 Store
    InMemoryPlatformStore用于L1、单元测试、referenceharness和LocalAPI demo它验证相同领域协议,但不能证明: Do youw
        多进程/多副本并发; extension
        数据库故障和连接池行为:
                                        Ln1,Col1 Spaces:4
docs>
    实现说明与证据边界
    #6.持久化实现
    ##6.2进程内Store
        多进程/多副本井发;
        数据库故障和连接池行为:
    -migration rollback:
        持久化跨进程重启:
    -PostgreSOL isolation/locking 的真实表现
    ##7.Checkpeint、Snapshot 与恢复实现
    -compteted Step IDs与active Step context;
        input/output Artifact versions;
        WorkspeceSnapshot ID:
        resolved Tool calt IDs:
        Effect states与consumed budget;
        model context summary ref;
        runtine image digest,
    ControlPlaneR在所有referencedArtifact/Snapshot READY、owmership/generation/checksum/image digest 合法时推进current_checkpoint_id
16e 审批pause在一个事务内提交Checkpoint 并释放Lease。Lease 过期recovery 使用Inbox 去重并新建 successor Attempt/Lease;Approval 拒绝或 Effect 成功后也从持久化 Checkpoint 重新 admission。原 Pod 不是恢复目标。
    对已知FAILED
    'RECOVERING" Step为ACTIVE,scheduler 创建新Attempt;successor Agent 必须重新规划、提案和审批,而不是重放l日 Effect Effect,FailedEffectRecoveryService要求effects:recover、Run version CAS 和幂等键,且只接受与NEEDs_ATTENTIONRun/ExecutionUnit/Step 和现有COMMITTEDCheckpoint 绑定的 Effect。成功后原 Effect 仍为FAILED,Run/ExecutionUnit为
    Runtine. 当丽reference vertica的 terminal completion 最后一步仍由ReferenceTerminaLAdapter模拟,因为共享 public command service 尚未暴露通用successor Runtime 提交 terminal success"入口该限制记录在reference/README.md,不能把reference harness 描述为完整生产
    8.Tool.Approval与Effect 实现
    8.IREAD路径
        ToolGateway
    factsB入 durable store/object store, 实现五层 scope intersection、runtime fence、ToolGrant、ToolSpec、resource prefix、schema/size/timeout、call ID 幂等、Credential Broker 和 Connector 调用。当前 invocation/result repository 还包含进程内实现:生产组装应把需要恢复的 invocation/result
    ###8.2WRITE路径
    共享生产语义由两项公开 service 构成
        ApprpvalDecisionService:审批 CAS.idempotency、Event、Audit.Outbox 和PREPAREDEffect;
    为executor_id传入,执行领取持久化该executor_id、递增execution_epoch和租约截止点:每个 Connector finish 都必须命中领取时的 expected owner/epoch,Audit actor/details记录真实worker subject/epoch.watchdog 同样必须命中它所观测的expected owner/epoch,然后才 EffectPREPARED -EXECUTING- SUCCEEDED/FAILED/UNKNOwN:authority snapshot 固化 tool spec、connector、 scopes、target 与 payload digest:internal execute route 验证effect-worker service identity.并把实际 worker subject 作
    能在程约到期后把速EXECUTING变为UNKNOwN:owner/epochmismatch 以EFFECT_EXECUTOR_FENCE_MISMATCH拒绝,租约内以EFFECT_EXECUTOR_STILL_ACTIVE拒绝UNKNOwNreconciliation 需要专用capability 和evidence digest,对账为FAILED还需要executor
    Effecttransport是at-Least-once,Connector必须在外部系统兑现稳定effect_key的幂等语义;因此只能把整体结果描述为effectively-once,而不能宣称跨系统exactly-once.
        tools/effects.py 还保留较小的in-memory/referenceprimitives,用于局部协议测试:新生产适配应优先组装根包公开的 shared services,而不是创建第二套approval/effect 真相。
    #9.Artifact 与UI 实现
    ### 9.1 Artifact
        LocaLobjectStore防路径速选和覆盖,适合开发:
        S30bjectStore把步SDk离在线程中,使用immutableput语义;
        ArtifactService/workspaceSnapshotService 校验generation.checksum.scan 和 READY;
        ArtifactDown LoadService 重新做policy check,生成最长15 分钟的授权 URL,并持久化Audit。
    生产仍需提供真实malware/buP scanner.S3client,download signer.classification/retention policy 和 Lifecycle 配置。
    ###9.2A2UT
    后端catalog 当前固定为:
        "ProgressCard"
        EvidenceSummary
        ApprovalCard
        ArtifactCard noo
                                        extension
    SurfaceValidator 做协议、catalog、字段、深度、ite、string.unsate content 和 canonical checksum 校验。Surface revision 在 PostgresQL 不可变保存、并与 source Attempt/generation/event seq 端定.ApprovalCard 由控制面基于持久化 Approval/ActionProposal 构造:
                                        Ln1,Co1
    实现说明与证据边界
    ##9.Artifact 与UI 实现
    ###9.2A2UI
    SurfaceValidator 做物议、catalog、字段、深度、item、string、unsafe content 和canonical checksum 校验。Surface revision 在 PostgresQL 不可变保存,并与source Attempt/generation/event seq 绑定.ApprovalCard 由控制面基于持久化Approval/ActionProposal 构造: Runtime不在释放Lease 后伪造审批内容。
    ##10.Frontend packages
        Package丨职责1
        @platform/agent-ui-client'
                                |Bearer API client、Run/Event 请求、recoverFailedEffect、严格 SSE parser、Artifact authorization、projection store
        @platform/agent-ui-react' 1Provider、projection hook、AgentPanel和 Host Bridge 连接
        @platform/embeoded-host-example1与任何业务系统无关的React/Vite披入示例
    SDk不特有业雾权辰getAccessToken、navigation和authorizeddownload由宿主Bridge 注入:Surface只能触发稳定action/artifact intent。
    #11.NessageBus与可观测性
        OutboxPubLisher从PostgreSQL 选择未发布消息,发布到MessageBus 后标记published:
22e essageEnvelope只允许稳定ID、schema/version、causation 和 w3C trace context;
        DiagnosticTelemetry只创建有限操作span,不保持数小时Run trace
        correctnesssignal与普通SLOmetric分离,返免高基数ID 进入 Prometheuslabel
    NATS和telemetry不能用于恢复Run;恢复始终读取PostgreSQL
    #12.进程与部著入口
    platform/entrypoint.py提供两个模式:
        api:从 AGENT_PLATFORM_CONTAINER_FACTORY=module:caLlable构造FastAPI 或AgentPlatformContainer
        worker 从AGENT_PLATFORM_WORKER_FACTORY=module:calLable 构造awaitableworkerloop.
    未配置或返回误类型时进程直接失败,不回退为假认证或假worker.Compose的runtimeprofile才启动API/worker:L2testprofile只启动依赖和精确集成测试。
    Heln/Kind/Compose 的详细职责见deploy/DOCS.md生产Chart 不安装企业托管PostgreSQL、NATS、S3、oIDC、External Secrets Operator、KEDA或telemetry backend,而是通过endpoint/Secret contracts 接入。
    #13.显式Localstack
        enterprise_agent t_platform.reference.local_stack:create_app是唯一用于copy-and-run的简单 API factory
        token定为本地演示token;
        tenant和 actor 国定为 synthetic identity:
    -只接受synthetic-case: 使用InMemoryPLatformStore; /synthetic-dataset: resource refs. reference -context: colilext 和synthetic-analysisworkflowi
        每次进程重启数塞消失:
        create/read/cancel Run;
        不含scheduler.worker.NATS、S3.真实Connector 或durableEffect; create后Run停留在OUEUED
    任何module都不会式导入Local stack,productionentrypoint也不会选择它,若把该factory 填入生产配置,是接入方的显式错误配置,不是平台fallback。
    ##14.Contracts 与生成物
    scripts/generate-contracts.py从Pydantic和publicFastAPI app 生成contracts/schemas/ contracts/openapi.json
                                        scripts/check-generated、sh在临时目录重生成并做byte-leveldiff,防止代码合同和提交生成物漂移。
    Golden fixtures羞event,RunView、approval、Artifact authorization、capability、Tool invocation、AzuI Surface 等跨语言样本。前端 Zod schema 与后端 Pydantic schema 都应对这些版本保持一致:破坏性变更必须发布新schema version,而不是原地放宽解析。 #15,验证等级
    统一入口:
        bash
    -/scripts/verify.shl1
    ./scripts/verify.sh12
    ./scripts/verify.sh13
                                        Do you wa
                                        extension
                                        Ln1,Col1 Spaces:4 UTF-
    实现说明与证据边界
    #15.验证等级
    脚本请求的工具或环境不可用时返回非零并打印INVERIFIED,不会把skip写成pass
    Level丨证明内容|不证明内容|
        -|--|---1
    1L1|frozen Python/npm dependencies、backend unit/contracts/E2E、Ruff、generated parity,clean wheel install、全目录 copy portability、前端 test/typecheck/lint/build、secret/path/import/symlink 边界|真实数据库、消息队列、对象存储、Kubernetes、强化 runtinel
    |L3|disposableKind 的真实镇像/Job/Pod、digest、projected token、Sandbox 零RBAC、无 Secret volume、DNS 和 API egress 阻断|生产CNI、gVisor、跨节点HA/PITR、真实企业依赖
    IL4|生产等价隔离、HA/failover/PITR、NATS R3、对象版本恢复、OIDC/Secret/KMS、credentialrotation、真实Connectorreconciliation、攻击与容量成本演练只可由目标环境的证据声明;仓库内没有自动把L4 标为通过的命令|
    每次交付的实际运行结果应写入带日期的taskreport:长期文档只描述gatecontract,不固化易过期的通过数量。
    #16.当前已知缺口
        没有通用生产Hostadapter:这是有意的独立边界,需要在接入项目实现。
        没有内置生产worker loop:entrypoint要求显式 factory.
        local stack不执行 queued Run。
        reterencevertical最终terminal success仍有reference-only adapter.
        Artifact/WorkspaceSnapshot 的 generation-fenced publish service 当前附带进程内 repository: SQLAlchemy Store 已有 durable metadata 表与操作,但生产组装仍需提供把 publish service 接到 durable Store 的repository adapter,不能沿用referencerepository. 多Agent dependencygraph、跨execution-unit admission 和 Run 聚合终态尚未实现:当前只有单 Agent 策略。
        强化RuntimeClass、企业网络、OIDC、KMS、Secretmanager、HA/PITR和真实Connector 需要L4环境验证。
        生产数据retention、right-to-delete、DLP、legalhold和成本模型需要接入组织定义
    这些缺口不能通过添加宽松fallback掩盖;应保持进程failclosed,并在[migration-guide.md](migration-guide.md)中作为接人工作包管理。
                                        Doyou wa
                                        extension
                                        Ln1,Col1Spac4 UTE-
