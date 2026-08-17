    #Task 12:Message Bus、Telemetry 与 Platform Operations 交付报告
    日期:2026-08-07
    #交付结论
    Task 12已在立目录enterprise-agent-platform/内完成,实现不引用 Triage 或其它宿主业务源码,也未执行 Git add、commit 或push。
    平台明确维持三条互不替代的数概路径
    1、PostgresOL中的RunEvent、Checkpoint、EffectLedger、审批、Artifact 元数据、Outbox/Inbox 是业务恢复事实。 2、AuditEvent是受保护写入必须原子产生的安全/合规事实。
    3.NATs只运输通知:OTeltrace/metric是允许丢失的诊断信号,二者都不能里写或恢复业务事实。
    主要实理
        backend/src/enterprise_agent_platform/platform/message_bus.py
        严格、本化的MessageEnvelope,只允许稳定ID与合法W3Ctrace context,拒绝URL/原始业务payload。
2e InemonyessageBus与延迟连接的NatsJetStreamBus,JetStream file storage、显式 ACK/NAK 与 durable pull consumer.
        backend/src/enterprise_agent_platform/platform/telemetry.py InboxConsumer在同一Store transaction 中提交业务mutation 与 Inbox processed marker:只在 commit 后 Ack,失败回滚后 NAK。
        有限操作span:Run、排队和审批等待不能成为长trace。
        Prometheusabelkey与value均采用有限registry:Run/Attempt/Artifact/user/Pod ID 不能成为时序标签。
        8类零容正确性信号使用独立 channel,并提供 OTLP HTTP 与 Prometheus sink:exporter 失败不改变业务控制流。 backend/src/enterprise_agent_platform/platform/entrypoint.py
        提供与银像探针对齐的live/ready endpoint。 APL/worker通过module:callablefactory注入宿主适配,不提供内置管理员或伪认证回退。
        backend/src/enterprise_agent_platform/execution/job_spec.py
        Attempt 默认使用agent-platform-sandboxS ServiceAccount.gVisor RuntimeClass、Attempt PriorityClass.
        Kindprofile必须显式传入runtime_class_name=None,避免把stockKind 证据误写成 gVisor 证据。 automountServiceAccountToken=false:仅投影audience-bound、608秒 token,使用fsGroup=65532与0440权限:不挂载 Secret volume。
    部暑资产
        Compose:deploy/docker-compose.yml
LE 童后的 PostgreSQL、NATS JetStream、MinIO:MinIO bucket versioning:Alembic one-shot migration:API/worker dependency ordering
        :deploy/images/control-plane.Dockerfile 试runner 精确执行migration parity、PostgreSOL concurrency/CAS/database-time、NATS+PostgreSQL Inbox 和 MinIo Artifact 测试 deploy/images/runtime.Dockerfile
4e Hele:deploy/helm/ 一frozenbackendLock,多阶段构建,最终UID/GID65532,生产镜像不制测试代码;生产workload使用digest image。
        两个Pssrestricted namespace,Sandbox SA 零RoleBinding
        orchestrator仅能管理Sandbox namespace 的Job 并观察Pod。
        R有Control Plane SA 能create authentication.k8s.io/tokenreviews
        Control Plane/orchestrator 通过独立projected token 与 NetworkPolicy 访间 Kubernetes API: Sandbox 不存在 API Server egress
        Kind:deploy/kind/ -POB.topology spread、HPA、一个 KEDA NATS policy、ResourceQuota、PriorityClass、ExternalSecret contract、migration hook 和生产 gVisor RuntimeClass。 values.schema.json 对默认values与Kindmerge 后values 均执行JSoN Schema 验证:生产NATS streamreplica 固定为 3
        disposablecluster、Calico NetworkPolicy、PostgreSQL、NATS R3、MinIO/versioning、fake OIDC 与最小测试 CRD
        可原性:deploy/observabiLity/ L3测试创建真实Attempt Job,检查一个Job/一个Pod、digest、projected token、无 Secret volume、Sandbox 零RBAC及 DNS 可用/API TCP 被阻断。
        -OTel memoryLimiter、敏感展性丢弃/清理、batch、OTLP/Prometheus pipeline
        8类correctness breach立即page;14.4x/6x SL0 burn-rate 与正确性告警分离
    ##Runbook路径
        depLoy/runbooks/effect-unknown.md
        dep Loy/runbooks/lease-storm.md
        deploy/runbooks/postgresgl-faiLover.md
        deploy/runbooks/object-checksum,md
        deploy/runbooks/nats-rebuild.md
        deploy/runbooks/credential-rotation,md
        deploy/runbooks/disaster-recovery.md
    #验证证据
    已执行井通过:
    -Task 12 focused pytest:29 passed,5skipped
    Task 12 Ruff:All checks passed 一5个skip 均为显式环境门禁:3个ComposeL2环境变量未配置,2个KindL3gate 未启用。
    Ruff format check:13 files already formatted
        皖墨态#约:11 #通过 包任全部YAMI /1CN 折 He#yalsrh HRACIMe iela'
                                        Ln1,Col1
    #Task 12:Hessage Bus、Telemetry 与 Platform Operations 交付报告 #部署资产
        Control Plane/orchestrator通过独立projected token 与NetworkPolicy 访问Kubernetes API:Sandbox 不存在 API Server egress。
        Kind:deploy/kind/ PDB.topology spread、HPA、-个KEDA NATS policy、ResourceQuota、PriorityClass、ExternalSecret contract、migration hook 和生产 gVisor RuntimeClass values.schema.json对赋认 values 与Kind merge后values 均执行 JSON Schema 验证:生产NATS streamreplica 固定为 3
        disposable cluster、Calico NetworkPolicy、PostgreSOL、NATS R3、MinIO/versioning、fake OIDC 与最小测试 CRD.
        可观测性:deploy/observability/ L3测试创建真实AttemptJob,检查一个Job/一个Pod、digest、projected token、无 Secret volume、Sandbox 零RBAC及 DNS 可用/API TCP 被阳断。
        OTel emorylimiter、敏感属性丢弃/清理、batch、OTLP/Prometheus pipeline
        -8类correctnessbreach立即page:14.4x/6x SL0 burn-rate 与正确性告警分离。
    #Runbook路径
        deploy/runbooks/effect-unknown.md
        deploy/runbooks/lease-storm.md
        depley/runbooks/postgresgl-failover.md
        deploy/runbooks/object-checksum.md
        deploy/runbooks/nats-rebuild.md
        deploy/runbooks/credential-rotation.md
        deploy/runbooks/disaster-recovery.md
    一运维入口为deploy/Docs.md:平台模块说明为backend/src/enterprise_agent_platform/platform/Docs.md
    验证证
    林北场D
7e Task12 focused pytest:29 passed,5 skipped
        Task12Ruff:All checks passed 5个skip均为显式环境门禁:3个ComposeL2环境变量末配置,2个KindL3gate 未启用。
        Rutfformat check:13 files already formatted
        部要态契约:11 项通过,包括全部YAML/JSON解析、Helm values schema、RBAC/NetworkPolicy、digest/non-root、Secret 扫描、脚本--help无副作用。 docker-composeconfig-—quiet):通过(本机 standalone Docker Compose2.40.2)。
LL Backend 全量回归快照: 三个Bash本bash-n:通过。
    来执行、不得称已通过 241 passed,6 skipped,1 failed;唯一失败是并行Task 13已知的 OpenAPI route set |旧期望,缺少新/actions与 Artifact download authorization route,由总任务统一收口,不属于Task 12 回归。
        Compose L2:Docker daemon 当前未运行
        KindL3:本机没有kind与helm命令。
    Dockeragebuild、真实NATS/PostgreSQL/MinIO、Kubernetes Job/NetworkPolicy、gVisor、HA、PITR/DR与成本负载均没有在本次环境实际运行;对应脚本和测试是后续环境门禁,不是当前实测证据。
                                        n1,Col1Spce2
