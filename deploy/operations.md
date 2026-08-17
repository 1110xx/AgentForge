#独立部署与平台运维边界
部署资产只依赖enterprise-agent-platform/。镜像构建上下文、Alembic测试路径和清单不引用宿主业务仓库。
##三条不可混滑的数据路径
PostgreSQL保存Run、Step、Attempt、Checkpoint、EffectLedger、审批、Artifact元数据、Outbox/Inbox和审计事实,是恢复与裁决的唯一事实源。
NATS JetStream只运输带稳定ID、schema version、causation 和 trace context 的通知Stream 可由 PostgreSQL Outbox 重建,消费者以 Inbox 去重。
OpenTelemetry/Prometheus是允许丢失的诊断信号。排队或等待审批不保持长 span:Attempt dispatch、bootstrap、tool和checkpoint 等有限操作用稳定 ID/link关联。
AuditEvent是独立安全/合规事实。受保护写入与审计必须在同一数据库事务中完成,不能用日志或trace替代
##L2 DockerCompose
../scripts/test-compose.sh在临时文件中生成本次测试凭据,按健康状态启动 PostgreSQL、NATS JetStream 和 MinIO,开启 Bucket versioning,执行 Alembic,再运行migration、PostgreSQL repository/concurrency、NATS redelivery与MinI0 checksum测试,默认退出时删除
ComposeProject和volumes;只有KEEP_STACK=1才保留诊断环境。
                                        AGENTPLATFORMwORKER_FACTORY:对应adapter 包需单独构建进镜像。平台没有内置管理员、伪认证或reference falLback.reference
API与worker位于显式Composeruntimeprofile,不会被L2默认启动。选择该profile前必须提供AGENTPLATFORMCoNTAINER_FACTORY和
local_stack仅是API-only 进程内演示,不是durable worker factory.
## 生产 Helm 与 Sandbox
Chart 不安装企业 PostgreSQL、NATS、S3、OIDC、External Secrets Operator、KEDA 或 OTel 后端：它们是外部托管依赖；values schema 强制 endpoint、外部 Secret 引用、NATS R3 和 digest image。

ControlPlane 只有 TokenReview 所需权限，并使用显式、短时 projected identity。
Orchestrator 只能在 Sandbox namespace 创建、观察和删除 Job，并只读观察 Pod。
两个 namespace 默认拒绝网络；只有指定 ControlPlane Pod 可访问 Kubernetes API；Sandbox 仅允许 DNS 与受控 API/model/tool/artifact/OTel 代理。Kind profile 故意清空 RuntimeClass，只提供功能和策略证据。
生产默认 agent-platform-gvisor RuntimeClass，安装前必须确认节点存在 runsc；容器 hardening 不能代替 hostile-code isolation。
HPA API；KEDA JetStream lag 扩 orchestrator；ResourceQuota 限制 namespace；PriorityClass 避免 Attempt 抢占控制面。NATS production stream 要求三副本；PostgreSQL HA、对象存储 versioning/retention 和 OIDC 可用性由依赖平台负责。

## 正确性、门禁与残余验证
验证层级：
1. L1：本机 pytest/Ruff/Contracts/wheel/frontend/portability。
2. L2：真实 PostgreSQL/NATS/MinIO 与 migrations。
3. L3：Kind 真实 Job/Pod、projected identity、RBAC 与 NetworkPolicy。
4. L4：目标环 gVisor、跨节点故障、数据库 failover/PITR、NATS R3、S3 version restore、凭据轮换和容量成本。

K8s 静态门：scripts/check-k8s.sh 对 deploy/helm 执行 helm lint、kind 与生产两套 values 的 helm template 渲染，并对全部渲染 manifest 做 YAML 解析（33 个资源 / 17 类 kind）；backend/tests/kind/test_attempt_job.py 为 L3 动态门（需 Kind 集群，scripts/test-kind.sh 编排）。
本仓库不能凭静态清单宣称 L4 已通过。故障处理入口位于 runbooks/。
