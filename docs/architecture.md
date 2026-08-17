docs>
企业Agent平台总体架构
##1.目标与边界
本工程是一套可整体复制、独立构建、独立部署的企业Agent平台。它解决的不是"如何调用一次模型",而是如何把长时间、可中断、可能产生外部副作用的Agent工作,放进一个多租户、可审批、可恢复、可审计的分布式执行系统。
平台代码不导入入它的业务系统。业务系统通过明确的HostPorts提供身份、资源解析、策略和业务连接器;平台通过稳定HTTP、事件、Artifact与前端SDK返回状态和结果。把整个目录复制到新的仓库后,核心包、Contracts、前端SDK、部署资产和验证脚本仍然具有完整含义。
当前版本有以下明确边界:
第一版按单Agent编排:一个Run在任一时刻最多有一个活跃执行单元。
-单Agent是当前admission、scheduler和并发策略,不是持久化模型的一对一约束。
reference/loca_stack.py只是显式启用的API-only、进程内、create-only演示。它能创建并查询QUEUEDRun,但没有durableworker,不能把该Run执行到完成,也不是生产falLback。 生产进程必须注入认证、资源、策略、持久化、调度、worker、对象存储和连接器适配:平台不会在缺少配置时退回"内置管理员"。
#2.统一执行型
2.1稳定业务实体
实体含义生命周期与持久化原则|
Run1用户发起的一次业务任务及其整体生命周期|是用户可查询、取消、重跑和审计的根实体;不绑定浏览器Ses5ion、Runner或Pod|
    "Step' Run内可持久化、可审批、可恢复的业务步骤丨保存步骤状态、策略快照和失败原因;一个Run可以有多个Step|
    Executionlinit|编排器可独立调度的Agent 执行单元|当前每个Run 创建一个primary单元;未来可在同—Run下增加planner、analyst、reviewer等单元|
    Attempt|某个执行单元针对某一步或任务的一次具体执行|每次重试、Lease丢失或恢复都产生新Attempt和更高generation;历史Attempt不被覆盖|
    Checkpoint|可恢复的业务游标和已验证引用集合|CoMMITTED后成为ExecutionUnit 的恢复游标,是PostgreSQL事实;不等于Pod文件目录|
    WorkspaceSnapshot|某个Attempt工作区不可变字节快照|对象内容在对象存储,元数据、checksum、runtime image digest 和 READY状态持久化;可选地被Checkpoint引用|
    Approva|对持久化ActionProposal的人工决定丨审批对象、请求摘要、决定人、版本和审计事实都持久化;不依赖打开审批页面的前端Session|
    EffectLedger|已批准外部写操作的唯一执行账本|用稳定effect_key去重,记录PREPARED/EXECUTING/UNKNOwN/SUCCEEDED/FAILED、executor_id、递增execution_epoch和执行租约;外部结果不确定时止盲目重试| Artifact|Run产生或消费的受控结果|元数据与lineage持久化,字节在对象存储;浏览器通过短期授权下载|
    RunEvent|Run内按event_seq严格递增的业务事件|用于重放、投影和UI增量更新;NATS通知丢失不影响事件事实|
Agent Runtime/Runner是真正运行Agent 逻辑的进程或程序;Sandbox Pod是承载一次Attempt 的临时隔离环境。它们都不是业务事实源。Pod 可以销毁并由新 Pod承载新 Attempt,Runner 进程也可以升级或迁移,而Run、Step、Checkpoint、审批、Artifact 和事件仍然存在。
*2.2关系而不是永久一对一
    mermaid
flowchart TD
    R{Run]->S1[Step 1]
    R->SN[Step N]
    R >U[ExecutionUnit primary]
    R future.->UN[ExecutionUnitreviewer/specialist]
    U1 ->A1[Attempt generation 1]
    U1 —A2[Attempt generation 2]
    >Pi[ephemeral Sandbox Pod]
    A2 >P2[newephemeral Sandbox Pod]
    U1 >C1[COMMITTED Checkpoint]
    U1 >C2[newCOMMITTED Checkpoint]
    C2 -.optionalreference.->W[READYworkspaceSnapshot]
    S1->AP[Approval]
    AP->E[EffectLedger]
数据库约束和generetionfence保证同一Executionunit 不会同时接受两个活跃Attempt的写入,未来多Agent 仍使用同一套结构:一个 Run下增加多个 ExecutionUnit,每个单元拥有自己的Attempt、Lease、Checkpoint、SandboxPod、权限范围和恢复状态。
#3.分层架构
    mermaid
flowchart LR
    Host[业务系统]->丨OIoCtoken/opaquerefs|API[Public Control API]
    Host ->|React componentj uI[Embedded Agent UI]
    UI->|REST+SSE+stableaction IDs|API
    API->CP[ControlPlaneServices]
    CP->DB[(PostgreSQLfacts)]
    CP->OB[Transactional Outbox]
    OB->MQ[NATS JetStream notifications]
    MQ->SCH[Scheduler/Worker]
    SCH->ORCH[Kubernetes Orchestrator]
    ORCH-->POD[Sandbox Pod per Attempt] Do you
    POD -->|one-shot bootstrap|INT[Internal Runtime API] extensio
    POD -->|short runtime capability| TG[Tool Gateway]
        -rRirrerentisl Rrnker]
                                        n1,Col1 Space MTE
#企业Agent平台总体架构
#3.分层架构
    POD-->|shortruntime capability|TG[Tool Gateway] TG >CB[Credential Broker]
SL TG >CoN[Business Connectors]
    CP->OBJ POD ->OBJ[(S3-compatibteArtifact/Snapshot bytes)]
    CP >TEL[OTel/Prometheus]
    DB->UI
#3.1PubticControlPlane
公共API负责:
    认证用户#从可信HostPort 得到tenant_id actor id和scopes
    把opaqueresource_ref解析为 canonicalresource facts;
    化Run创建时的策版本、scope、budget 和摘要;
一创建、查询、取消、重跑Run:
    提供事件分页、SSE和持久化RunView:
    接收与不可变Surfacerevision绑定的 UIAction:
    生成Artifact短期下载授权。
公共请求不能通过X-Tenant-Id自报租户,也不能提交连接器地址、对象存储key、Pod地址、凭据或任意回调URL。
3.2DurableControl Services
独立运配应用组装的公开compositionservices/ports,不要求调用方导入内部数据库表。 ControlPlaneService、Checkpoint/恢复函数、ApprovalDecisionService DurableEffectExecutor Surface/Artifact 服务围绕稳定记录工作。重要写入使用PostgreSQL事务、CASversion、幂等键、Audit与Outbox。ApprovalDecisionService 和DurableEffectExecutor是可由
le1 3.3Scheduler、0rchestrator 与Runtime
Scheduler从PostgreSQL查询可调度工作,按tenantround-robin做admission,并通过持久化约束领取-个 Attempt/LeaseOrchestrator 把 Attempt 转成Kubernetes Job:Job 只承载这一次Attempt, 录内静默重。 backoffLimit=o, 重试由控制面新建Attempt,而不是让Kubernetes在同一业务执行记
failure 操作。 Checkpoint 恢复,心跳续 Lease,通过内部 Runtime API 请求 READ tool、Artifact、ActionProposal、Checkpoint 或
#3.4ToolGateway与EffectExecutor
wRITEtool不在Runtime 内直接执行.Runtime 只能持久化ActionProposal 并暂停等待审批:批准后生成 EffectLedger再由独立 Effect Executor 通过 tenant-bound、effect-bound capability 执行。/internal/vl/tenants/(tenant_id}/effects/{effect_id}/execute先验证
effect-orker serviceidentity,再把实际worker subject 作为executor_id传给 Executor.领取事务持久化该owner、新的execution_epoch和以数据库时间计算的租约载止点:Connector finish与watchdog之后都必须命中相同owner/epoch对,Audit 也记录实际subject和 epoch,这不是仅供观测的标签,而是防止失效worker收尾或watchdog覆盖新ownership的持久化fence.该 internal route 不是浏览器或普通业务客户端API
3.5A2UI与嵌入式前端
Runtine 或控制画交的是声明式Surface 文档,不是JavaScript、HML 或组件模块。服务端只接受固定catalog,持久化不可变revision;浏览器 SDk 再次校验协议并只澄染 alLowlisted React 组件。用户动作只携带 Run、Surface、revision、action_ref、客户端幂等 ID 和显示搁要,服务端从已持久 化Surface反查approval_id canonical target 和request digest。
4.三条不可爱淆的数挑路径
|路径|用途丢失后的含义
IPostgreSOL领域事实丨Run、Step、ExecutionUnit、Attempt、Lease、Checkpoint、审批、Effect、Artifact 元数据、事件、Outbox/Inbox、Audit不允许无恢复策略地丢失:这是裁决和恢复的唯一事实源 NATSJetStrea|运输"有新工作/新事件"的通知」可以从PostgreSQLOutbox补发:消费者由Inbox去重:不能据此重建业务真相
OTel/Prometheus|trace、etric.告警和容量诊断|允许采样或短时丢失:不能替代Audit、EffectLedger 或Checkpoint
S3-compatibleobject store保存Artifact 和WorkspaceSnapshot 的不可变字节。其 READY 元数据、版本,checksum 和lineage 仍由 PostgreSQL 裁决。对象存在但数据库未提交 READY,不能作为可恢复输入:数据库引用的对象checksum 不匹配则是正确性事故。
##5,端到端状态与数据流
###5.1创建Run
1.浏览器或业务后端向POST /vl/runs发送workfLow_type,intent,opaque resource refs、业务参数和可选host context ref。
2.AuthPort 从token 派生租户、用户和scopes;Resource/Context/PolicyPorts在超时和 fail-closed 边界内解析权威事实。
3.ControlPlane在一个事务内写入Run,当前单个primaryExecutionUnit,初始coMMITTEDCheckpoint,RunEvent、Outbox、幂等结果以及有授权快照时的Audit
4.返回2o1、Location、ETag和RunViewSnapshot,此时QUEuED只表示持久化成功,不表示已有Runner或Pod noa
##5.2调度和启动Attempt extensior
                                        Ln1.Col 1 Spaces:4 CTF
#企业Agent平台总体架构
##5.端到端状态与数据流
#5.2调度和启动Attempt
1.Outbox 通知scheduler有工作,但scheduler仍从 PostgreSQL读取事实。
2.Scheduler 领取工作时创建新Attempt与RESERVEDLease,并递增ExecutionUnit generation。
3.Orchestrater 创建一个digest-pinned Kubernetes Job:控制面记录Pod与Attempt binding
4.Pod 使用projected token 和 downward API Pod UID 做一次bootstrap:Control Plane TokenReview 验证audience、namespace、ServiceAccount、Pod UID、Attempt 和generation
5.3Checkpoint与普通继续执行
Runtime先把Artifact/WorkspaceSnapshot 字节写到对象存储并完成 READY 校验,再提交 Checkpoint metadata.Control Plane 检查active Lease、owner、version、generation、来源 Checkpoint、READY Artifact/Snapshot、runtime image digest 和checksum.随后在一个事务内:
    插人新的CoMITTEDCheckpoint:
把ExecutionUnit 的current_checkpoint_id推进到新Checkpoint;
    更新Attenpt/Run version:
追加事件、Audit 和Outbox。
只有事务提交后的Checkpoint才能用于恢复。Pod本地/workspace不是进度事实。
##5.4审批、外部Effect与恢复
1.Runtime 生成canonical ActionProposal:request_digest固化 action ref、Tool/version/spec digest、Connector、required scopes、canonical target、payload digest 与risk class.随后提交包含恢复游标的 Checkpoint,并请求 approval pause。 —个事务Checkpoint、Approval、Step/Unit/Run=WAITING_APPROVAL、Attempt=CHECKPOINTED_FOR_APPROVAL、Lease=RELEASED事件、Audit 和Outbox一起持久化,
16e
    SurfaceBoundActionHandler先校验immutable Surface revision 与action binding;ApprovaLDecisionService再校验 tenant、scope、Approval/ActionProposal binding、displayed digest、版本、过期时间和幂等键: APPROVE:原子持久化Approval=APPROVED、ActionProposal=CONSUMED和唯-PREPAREDEffect;
    -RE3ECT:持久化拒绝、并让Run/Unit 从已提交Checkpoint 进入RECOVERING,不创建Effect。
5.Internalroute校验effect-worker serviceidentity,把验证后的worker subject 作为executor_id传入Effect Executor:Executor 再校验与持久化Effect 完全一致的 tenant/effect/approval/request/tool/spec/connector/target/scopes-boundcapability.领取时写入 该executor_id、递增的execution_epoch和执行租约,从 Broker 获取凭据,使用effect_key作为连接器幂等键执行。每个 Connector finish只能用自己领取到的owner/epoch 对收尾,并把该 subject/epoch 写入Audit。
6.Effect成功或拒绝恢复后,scheduler从等待前的COMMITTEDCheckpoint 创建更高generation 的新Attempt、新Lease 和新Pod:原Attempt与原Pod 不恢复为活跃状态。
7.Effectworker 失联时,watchdog 必须提交它所观测的expectedexecutor_id+execution_epoch,只有该对仍是当前ownership 且持久化执行租约已按数据库时间到期,才能执行EXEcuTING-UNKNOwN;owner/epoch mismatch或租约未到期都必须fail closed,watchdog Audit 保留它裁决 的workersubject/epoch。迟到的Connector 成功仍须命中原owner/epoch,才可把UNKNOwN更新为SUCCEEDED保留已发生的外部事实,
约。
9.已知失数后,操作老通过publicfailed-Effectrecovery command 提交If-Match、幂等键和effects:recover权限。该事务保留原FAILEDEffect 不变,把Run/ExecutionUnit 转为RECOVERING、Step 转为ACTIVE,并从已提交Checkpoint 调度新Attempt:successor Agent 必须重新规划、创建新ActionProposal并获得新审批,不得重新dispatch 旧Effect。
1e.外部成功返回期间若Run被并发取消,Effect=SUCCEEDED仍作为外部事实提交,Run保持CANCEL_REQUESTED:Pod/Run状态不能反向覆盖外部结果。
##5.5失数、Lease 过期与重试
Runtime明确失败、进程溃、Pod丢失和 Lease过期都不能覆盖原Attempt。Leaserecovery 使用Inbox去重,在同一事务中:
    标记EAttenpt=LOSTBLease=EXPIRED:
    确认当能恢复源为CoMITTEDCheckpoint;
新建generation+1的Attempt 与RESERVEDLease;
    Run/ExecutionUnit 进入RECOVERING
    写入事件、Audit.Outbox与Inbox processedmarker;
    求推旧capability、删除旧 Sandbox、调度 successor Attempt。
因此"恢复"意妹着基于业多事实创建新Attempt,不是复活原Runner 进程。
按EffectID尾,也不能用Pod已除或进程不可达替代owner/epoch匹配与Effect租约到期证据。
#6.当前单Agent与未来多Agent
当前admission规则限制同一个Run同时只有一个活跃ExecutionUnit,创建Run 时只创建role=primary"的 ExecutionUnit,这一规则减少第一版审批、并发、资源配额和UI 汇总复杂度。
扩展到多Agent时不修改Run的基本语义,而是
1.在一个Run下创建多个具备role和dependency的ExecutionUnit;
2.对每个ExecutionUnit 独立维护generationAttempt、Lease、Checkpoint和capability;
3.Scheduler 根据依赖、tenant quota 和Run budget 决定并发;
4.Artifactlineage 和事件causation连接多个单元的输入输出;
5.审批绑定具体Step/ActionProposal/ExecutionUnit,但仍汇总在稳定Run下;
6.Runterminal状态由编排策略聚合,不由任意一个Pod决定,
数据库模型已经道免Run,Runtime、Pod永久一对一;多Agent的主要新增工作是依赖图,并发admiss1on、跨单元消息和聚合状态策略 Do youw
##7.高可用与资源调度 extension
                                        Ln1.Col1
##6.当前单 Agent 与未来多 Agent
5.审批期定具体Step/ActionProposal/ExecutionUnit,但仍汇总在稳定 Run下:
6.Runterminal状态由编排策略聚合,不由任意一个Pod决定。
数据库型已经造免Run、Runtime、Pod永久一对一:多Agent 的主要新增工作是依赖图、并发admission、跨单元消息和聚合状态策略
##7.高可用与资源调度
    PostgreSOL通过事务、唯一约束、CAS、数据库时间和fencing保证正确性:生产HA/PITR由托管数据库或等价方案提供。 API可无状态多副本:SSE客户端用持久化cUrsor重连。
    Outboxpubtisher、Inboxconsumer 和scheduler 可通过数据库互斥与幂等横向扩展:严格tenant round-robin 需要单活 scheduler 或显式分布式选主。
NATSconsumerlag 可驱动KEDA:API延迟可驱动 HPA:ResourceQuota、PriorityClass 和每Attempt CPU/内存/时限避免 Sandbox 抢占控制面。 Artifact/Snapshot使用版本化对象存储、retention与checksum:对象存储不可被当作数据库锁服务。
8、可移性约束
平台长期遵守以下规则:
核心包不导入宿主业务楼块:宿主适配器位于平台目录之外,通过公开Protocol和factory注入。
Contracts、OpenAPI、JSON Schema 和前端runtimevalidators 版本化并可确定性生成。
    配置只保存endpoint/Secret引用等契约,不提交真实凭据。
    文档、测试、镁像构建上下文和脚本不依赖某台开发机的绝对路径。
9.延件阅读
    [security.md](security.md):Sandbox、身份、网络、工具、A2UI 和剩余风险。
    [mplementation.md](implementation.md):当前代码模块、公开边界、部著和验证证据。
    [embedding-guide.md](embedding-guide.md):如何由现有业务系统通过 Ports、HTTP 和前端 SDK 接入。
    [migration-guide,md](migration-guide,md):从宿主内嵌原型迁移为独立平台的分阶段方案。
                                        nooo
                                        extenskon
                                        an1,col1 Spaces:4 UTS
