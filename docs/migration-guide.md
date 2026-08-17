    #1.迁移目标
    迁移完成后,Agent 平台应当是可单独复制、构建、发布和回滚的产品单元,原业务系统保留业务事实、用户入口和策略权威:独立平台负责 Run 生命周期、远端执行、Checkpoint、审批、Effect、Artifact、事件和A2UI。
    目标依赖方向必须是单向的:
        text
    业务系统/adapter enterprise-agent-platform enterprise-agent-platform public contracts -X->业务系统源码、ORM、路由、部着目录
    不要把"代码抓到新目录"当作迁移完成,只要平台仍导入宿主model、读取宿主绝对路径、复用宿主数据库Session、把用户 Session 当Run、或依赖某个固定 Pod/home,它就仍是内实现。
    2、推养迁移策略
    默认推荐"新Run 进入新平台、I旧Run 留在原系统只读完成/归档",而不是搬迁正在运行的 Runtime/Pod原因是活动 Run 可能已经绑定l旧审批、IBTool权限、未完成外部WRITE 和不可验证的本地文件状态。
    只有同时满足以下条件,才考虑迁移活动Run:
    旧系统能导出完整、版本化且checksum可验证的Checkpoint;
    所有已完成Step、Artifact lineage、Tool call 和 Effect state 可枚举:
    不存在继果未知的外部wRITE,或可在新平台重建同一effectkey并reconciliation:
    -可冻结旧scheduler/worker,井以 generation fence 阻止日 Runtime 继续写:
    迁移后使用新Attempt、新Lease、新 Sandbox,不复活l旧 Pod。
    3.购段0:现状盘点与冻结边界
3e 先建立迁移清单,不立即改执行代码。
    3.1业务入口
    盘点所有创建Agent任务的入口:页面按钮、REST、定时任务、Webhook、Chat、批处理和管理员重跑。记录每个入口当前提交的用户、租户、资源、参数、上下文和幂等语义。
    #3.2执行状态
    识别目前被派在一起的概念,并映射到统一模型:
    旧概念1目标实体|迁移注意
    chat/session/task/job|Run|只有用户业务任务是Run;浏览器Session不是
    phase/nobe/action|Step|必须有稳定ID、状态、版本和策略快照|
    retry/container/process|Attempt|每次具体执行独立记录,不覆盖retrycount
    worker/agentprocess|AgentRuntime|可替换进程,不作为永久业务主键
    pod/containerW|SandboxPod|一次Attempt的临时承载,不迁移为业务事实 localstate/cursor
    workspace/homearchive|WorkspaceSnapshot|不可变bytes、checksum.image digestREADY state
    ##3.3外部依
    ##3.4冻结合同
    给旧系统导出建立versioned schema 和golden fixture.不要直接把旧 ORM row dump 当迁移协议;明确时间格式、enum、nullable、tenant,digest、sequence 和 idempotency。
    4.阶段1:建立孩立代码与构建边界
    1.将整个enterprise-agent-platform/复制到独立仓库或独立release artifact. 2.保留backend/ 3.在空父目录运行./scripts/verify.shL1° contracts/" frontend/" depLoy/、docs/和scripts/,不要只烤Python 源码。
    4.发布Pythonwheel和四个前端packages到受控registry;记录checksum/SBOM
    5.把OpenAPI/JSoN Schema generation parity 加入CI
    6.禁止平台import 宿主module、读取宿主绝对路径或宿主环境猜测credential
    L1不通过时停止后续迁移:先消除portabitity和contractdrift.
    ##5.阶段2:把宿主依赖改造成Ports
    1 adantar narkane
                                        QLn1.Col1SpaceUTE
    ##4.阶段1:建立姓立代码与构建边界
    L1不通过时停止后续迁移:先消除portability 和contract drift
    ##5.阶段2:把宿主依改造成Ports
    ###5.1建立立adapter package
    在业务侧创建business-agent-adapter
                                或同等职责的包,平台不反向导入它、按【embedding-guide.md](embedding-guide.md)实现:
        AuthContextProvider
        ResourceResolver
        HostContextVerifier
        PolicyContextProvider
        CredentialBroker
    READ/wRITE Connectors
    Artifact policy/signer/scanner
B6 -production container factory 与worker factory
    每个adapter 都应有contract tests,输入输出只使用公开 dataclass/Pydantic/Protocol、不传ORM entity、数据库 Session 或request global
    ##5.2权迁移
    把旧的隐式权限拆成可审计层:
    1.principal scopes;
    2.Run policy scopes/budget:
    3.ToolGrant scopes/resource prefix;
    4.ToolSpec required scopes;
    5.Runtime/Effect capability scopes.
lee 使用交集而不是并集,浏宽器中隐藏按钮不是授权:旧前端传来的role、toolname、target或credential字段全部视为不可信。
1e2 ##5.3资源号|用迁移
    为业务对象定义 opaque ref 和 canonical resolver。ref 应稳定但不泄露数据库拓扑;resolver 必须校验 tenant、ownership、classification、version和 digest.若旧uRL 被当资源标识,先生成 server-side ref mapping,禁止把任意URL直接带入新 Tool Gateway。 6.阶段3:建立持久化事实源
1e7 ###6.1PostgreSOL schema
    在签立数掘库或签立schema 运行Alembic,返免与主业务事务/迁移生命周期耦合。生产连接配置、Secret、pool、timeout、statement timeout、backup/PITR和 faiLover 由目标平台管理。
    先验证:
    upgrade、downgrade.re-upgrade;
    active Attempt/Lease unique constraints;
    CAS/version conflict;
    database-tineLease expiry;
    tenant boundary:
    Effect unique key;
    mutation+Audit+Outbox 原子性。
    6.2数挺迁移选择
    |方案丨推荐场景丨风险」
        -/--|--1
    |仅新Run使用新平台丨大多数系统最低:需要旧Run只读入口和双查询窗口|
    [只迁terminalRun索引/Artifact丨合规归档和统一搜索丨中等:需保证旧状态与checksum不被改写1
    |迁waiting approval Run|审批周期很长|高;需重新验证 ActionProposal、digest、expiry 和 actor scope
    |迁UNKNOwNEffect]不推荐自动迁移丨极高;先在旧系统reconciliation,再决定人工导入 迁activeRuntimeRun|少物必须无中断场景|很高:必须freeze、commit checkpoint、fenceoldruntime、新建Attempt
    不要把I日PodID或Runneraddress写成新Run主键,若需要可追游,放入受控migrationaudit metadata,而不是业务关系约束
    ##6.3导入规则
    每条记录必须带tenant:
    -使用migration namespace 的幂等键;
    一保留旧系统ID到新stableID 的只读mapping;
    event sequence 在每个新Run 内连续;
    一导入前后计算canonicaldigest;
    terminal historicalRun 不应产生新的 dispatch Outbox
                                        Ln1,Col1 Spaces:4 UTE
    从业务系统内嵌 Agent 迁移到独立平台
    6.阶段3:建立持久化事实源
    #6.3导入规则
    一保国旧系织 ID到斯 stabte ID 的只读mapping:
    -event sequence在每个新 Run 内连续;
    导入前后计算 canonical digest:
    -terminal historical Run不应产生新的 dispatch Outbox;
        active import必须创建generation 更高的新 Attempt,并撤销旧authority;
    -所有异常进入 quarantine table/report,不做部分 silent success.
    ##7.阶段4:Artifact 与Workspace 迁移
    1.为旧文件计算checksum、size、media type、classification、owner 和retention。
    2.把bytes 写到versioned object store 的不可变key;禁止覆盖已有key.
    3.完成malware/DLP scan 后再提交ArtifactVersion=READY
    5.旧workspace只在确实需要恢复时转换为 WorkspaceSnapshot:普通输出文件应是Artifact。 4.建立input/outputLineage和 sourceAttempt/generation;无法确定的lineage明确标为migrated-unknown,不猜测。
    6.Snapshot 须记录旧runtime image/build digest 或明确的migration sentinel,并在恢复策略中决定是否兼容。 7.随机抽样从对象存储恢复并校验checksum:执行版本回浓/恢复演练。
    不直接把NFS/home目录mount给新Sandbox.这样会把l旧权限、symlink、socket、credential和不可审计状态带进新安全边界。
    8.民5:消息、调度与Sandbox
16e 8.1NATS迁移
    新平台只托NATS 用作通知,不要把旧 queue message payload 原样复制为业务事实。正确流程是:
    PostgreSOLtransaction 写领域 mutation+Outbox;
    publisher 发送稳定ID envelope;
    consumertransaction写mutation+Inbox marker: commit后ACK,失败NAK;
    Strea丢失可从Outbox重建通知。
    8.2调度迁移
    先在shadowtenant 或合成资源开启scheduler:
        验证每个ExecutionUnit 同时只有一个activeAttempt;
    -检查tenant fairness.quota、backpressure 和 queue lag;
        检查worker crash、Lease expiry 和 successor Attempt;
    旧scheduler与新scheduler不得同时领取同一Run;
    cutover使用admissionflag 和generation fence,不靠"希望日Pod已停止"。
    #8.3Sandbox迁移
    先通过L3,再在生产等价节点做L4.逐项确认:digest image、non-root、read-only rootfs.drop capabiLities、no default SA token、zero RBAC、projected bootstrap、NetworkPolicy、no arbitrary egress、no hostPath/Secret volume、workspace/resource limits 和强化 Runt ineClass.
    #9.阶段6:MRITE与审批迁移
    WRITE是迁移中风险最高的部分,推荐最后开启。
    1.先只迁READ tools,RITE proposals 记录但不执行。
    3.验证审批前Checkpoint commit 与Lease release 在同一事务。
    4.只允许ApprovaDecisionService持久化决定和PREPAREDEffect。
    5.Effect worker只通过内route+service identity+effect capability 调用DurableEffectExecutor;capability 必须绑定tenant、Effect、Approval、request digest、ToolSpec digest、Connector、target 与scopes 6.在测试外部系统注入timeout,已知失致、con前it后断线和workercrash,验证UNKNOwN不自动重试,
    8.对真实wRITE 采用canarytenant/tool/target allowlist,速步扩大 7.建立reconciliation意询.独立effect-reconcilerservice identity/capability、evidence digest、人工runbook、correctness alert 和审计报表
    旧系统如果由前端直接调用业务写API,必须先切断这条路径;不能让新审批UI只是装饰,实际权限仍在浏览器。
    ##10.阶段7:前端与A2UI迁移
    ###10.1接入顺序
    1.先该入只读Run status 和事件时间线:
    2.接入ProgressCard.EvidenceSumary
    3,接入Artifact 短期授权下载;
    4.最后开启ApprovalCard Action:
    5.未知component、schema mismatch、stalerevision和网络错误必须安全级
                                        Ln116.Col30
    从业务系统内Agent 迁移到独立平台
    ##10.阶段7:前端与A2UI迁移
    ###10.1接入顺序
    ###10.2双读与shadow
    在一段时间内,业务页面可根据Run source读取旧/新API,但一个Run只由一个事实源拥有。不要把两个系统的event 按时间混合排序并推断状态。
    新Run shadow可比较
    Aresource/versiondigest:
        READ Tool结果digest:
        Artifact checksum:
        proposal request digest;
        terminal classification/summary.
    shadow阶段不执行外部wRITE:差异用于验证adapter和workflow,而不是让两个Agent竞争修改同一业务对象。
    ##10.3UI路由切换
    使用featureflag按tenant、workflow和用户组切换。保留killswitch:停止创建新Run、隐藏动作并保留查询/下载。已经创建的 Run继续由其所属事实源完成或进入人工处置。
    11.阶8:Cutover
    推存顺序:
    1.冻结平台和adapter版本,完成L1/L2/L3:
    2.在生产等价环境完成L4签署;
    3.体旧系统新Run admission
    5.确认无未处理UNKNOnNEffect; 4.等特旧activeRun到安全点,或按计划导出COMMITTED Checkpoint;
    6.启用新平台API与scheduler,先canary tenant:
    7.证Runcreate-Attempt-Checkpoint-Artifact-approval/reject-newAttempt; 8.再开启批准后的真实wRITE canary;
    9.步扩大流量并观察correctness signals、queueLag、资源和成本;
24e 1e.旧系统切只读,保留审计与mapping,按retention 计划退役。
    12.回流方案
    回不能让同一Run在两个平台同时执行。
    ##12.1新Runadmission 回滚
    -关团新平台create feature flag;
    -保量pubLicquery.events、Artifact 和审批只读;
    暂停scheduleradmission,但不要删除事实;
    只有全新用户任务才重新路由到旧系统。 对已经active的新Run选择继续完成或在committedCheckpoint 安全暂停;
    ###12.2Runte回浪
    销active capability,令Lease过期井fence generation; 提交或碘认最后一个COITTEDCheckpoint;
    一删除旧 Sandbox只是清理,不是fence;
    若回到旧系统,需要显式反向migration adapter 和新日 ID mapping,不能把原 Pod 重新标记为有效。
    ##12.3WRITE回滚
    立即停止Effect workeradmission;
        PREPAREDEffect可等待:
        EXECUTING丢失executor 必须转UKNOwN并reconciliation;
    已SUCCEEDED的外部写不能用数概库rol1back当作撤销,应执行独立、已审批的补偿操作。
    ##13.验收矩阵
    工作包|最低证据|通过标准
    可移植代码|L1|空父目录copy、wheelclean install.无host import/path/secret、前后端全gate|
    [PostgreSQL/NATS/S3|L2|migration.CAS.database time,redelivery/Inboxversion/checksum|
    Sandbox 与集群策路|L3丨real Job/Pod.digest.projected token,zero RBAC.NetworkPolicy|
    |生产安全/HA|L4|强化runtime,failover/PITR,NATS R3.object restore,credential rotation|
    Host Ports|adaptercontracttestssecurityreview|tenant/actor/ownership/policyfailclosed,timeout 有定错误
    READ Tool|synthetic+canary| scope intersection,resource allowist.schema/size/timeout、无credential 退
                                        Ln116Col30
    ##12.回滚方案
    ###12.1新Run admission回 n
    一只有全新用户任务才重新路由到旧系统。 对已经active 的新Run选择继续完成或在committed Checkpoint 安全暂停;
    ###12.2Runtime 回
        销active capability.令 Lease 过期井 fence generation
        提交或确认最后一个CoMNITTEDCheckpoint:
        删除旧 Sandbox只是清理,不是fence;
        若回到旧系统,需要显式反向migration adapter 和新I旧 ID mapping,不能把原 Pod 重新标记为有效。
    12.3hRITE回
        立即售止Effect workeradmission:
        PREPAREDEffect可等待:
        EXECuTING丢失executor 必须转UNKNOwN并reconciliation:
        已SUCCEEDED的外部写不能用数据库rolLback当作撒销,应执行独立、已审批的补偿操作。
    13.验收矩阵
27e 工作包|最低证据|通过标准|
    可移植代码丨L1|空父目录copy、wheel clean install、无host import/path/secret、前后端全gate
    PostgreSOL/NATS/S3|L2|migration、CAS、database time、redelivery/Inbox、version/checksum|
        生产安全/HA|L4|强化runtime、failover/PITR、NATS R3、objectrestore、credentialrotation
    HostPorts|adapter contract tests+securityreview|tenant/actor/ownership/policy fail closed,timeout有稳定错误
    READ Tool|synthetic+canary|scope intersection、resource allowlist、schema/size/timeout、无credential 世
    RITETool|fault injection+canary|Approvalbinding、唯-Effect、idempotency、UNKNOwNreconciliation
        unit+embedded E2E|strict schema、SSE gap/resync、stale action、safe fallback、Artifact identity
    1运维Igame day| Lease storm、NATS rebuild、object mismatch、credential rotation、DR runbook 可执行
    未运行的gate标UNVERIFIEDYAMLLint、mock和代码审查不能替代目标环境实测。
    14.迁移完成定义
    满足以下条件才算完成:
    平台目录可独立带走,构建和验证不需要原业务仓库;
    务集成只通过pubLiccontracts、HTTP、events、Artifact、SDK和外置adapter
    -Run/Step/Attenpt/Checkpoint/Approval/Effect/Artifact有唯一持久化事实源:
        Runner/Pod被视为临时 Attempt承载,不是用户任务或恢复主键;
        浏览器不能决定tenant、Tool、credential、target 或WRITEpayload;
        WRITER经ApprovaLDecisionService+DurableEffectExecutor;
        旧generation.IESurface、重复action和跨租户访问failclosed:
    L1/L2/L3当前版本证据完整,L4由生产owner签署; Y
        rolLback、UNKNoNEffect、数据库恢复、对象恢复和 credeltial rotation runbook已演练;
        担系纸Agent热行代码已傳用或只读归档,没有隐蔽的第二套scheduLer/Effect 真相。
                                        Ln116.Col30
