# Task 12：Message Bus、Telemetry 与 Platform Operations 交付报告

日期：2026-08-07

## 交付结论

Task 12 已在独立目录 `enterprise-agent-platform/` 内完成，实现不引用 Triage 或其它宿主业务源码，也未执行 Git add、commit 或 push。

平台明确维持三条互不替代的数据路径：

1. PostgreSQL 中的 RunEvent、Checkpoint、EffectLedger、审批、Artifact 元数据、Outbox/Inbox 是业务恢复事实。
2. AuditEvent 是受保护写入必须原子产生的安全/合规事实。
3. NATS 只运输通知：OTel trace/metric 是允许丢失的诊断信号，二者都不能重写或恢复业务事实。

主要实现：

- `backend/src/enterprise_agent_platform/platform/message_bus.py`：严格、白化的 MessageEnvelope，只允许稳定 ID 与合法 W3C trace context，拒绝 URL/原始业务 payload。InMemoryMessageBus 与延迟连接的 NatsJetStreamBus，JetStream file storage、显式 ACK/NAK 与 durable pull consumer。
- `backend/src/enterprise_agent_platform/platform/telemetry.py`：InboxConsumer 在同一 Store transaction 中提交业务 mutation 与 Inbox processed marker：只在 commit 后 Ack，失败回滚后 NAK。有限操作 span：Run、排队和审批等待不能成为长 trace。Prometheus label key 与 value 均采用有限 registry：Run/Attempt/Artifact/user/Pod ID 不能成为时序标签。8 类零容忍正确性信号使用独立 channel，并提供 OTLP HTTP 与 Prometheus sink：exporter 失败不改变业务控制流。
- `backend/src/enterprise_agent_platform/platform/entrypoint.py`：提供与镜像探针对齐的 live/ready endpoint。API/worker 通过 `module:callable` factory 注入宿主适配，不提供内置管理员或伪认证回退。
- `backend/src/enterprise_agent_platform/execution/job_spec.py`：Attempt 默认使用 `agent-platform-sandbox` ServiceAccount、gVisor RuntimeClass、Attempt PriorityClass。Kind profile 必须显式传入 `runtime_class_name=None`，避免把 stock Kind 证据误写成 gVisor 证据。`automountServiceAccountToken=false`：仅投影 audience-bound、600 秒 token，使用 `fsGroup=65532` 与 `0440` 权限，不挂载 Secret volume。

## 部署资产

- Compose：`deploy/docker-compose.yml`：重建后的 PostgreSQL、NATS JetStream、MinIO；MinIO bucket versioning；Alembic one-shot migration；API/worker dependency ordering。
- 镜像：`deploy/images/control-plane.Dockerfile`、`deploy/images/runtime.Dockerfile`：frozen backend lock、多阶段构建、最终 UID/GID 65532，生产镜像不打包测试代码；生产 workload 使用 digest image。test-runner 精确执行 migration parity、PostgreSQL concurrency/CAS/database-time、NATS+PostgreSQL Inbox 和 MinIO Artifact 测试。
- Helm：`deploy/helm/`：两个 PSA restricted namespace、Sandbox SA 零 RoleBinding；orchestrator 仅能管理 Sandbox namespace 的 Job 并观察 Pod；只有 Control Plane SA 能 create `authentication.k8s.io/tokenreviews`；Control Plane/orchestrator 通过独立 projected token 与 NetworkPolicy 访问 Kubernetes API；Sandbox 不存在 API Server egress。
- Kind：`deploy/kind/`：PDB、topology spread、HPA、一个 KEDA NATS policy、ResourceQuota、PriorityClass、ExternalSecret contract、migration hook 和生产 gVisor RuntimeClass。`values.schema.json` 对默认 values 与 Kind merge 后 values 均执行 JSON Schema 验证：生产 NATS stream replica 固定为 3。disposable cluster、Calico NetworkPolicy、PostgreSQL、NATS R3、MinIO/versioning、fake OIDC 与最小测试 CRD。
- 可观测性：`deploy/observability/`：OTel memory limiter、敏感属性丢弃/清理、batch、OTLP/Prometheus pipeline。8 类 correctness breach 立即 page；14.4x/6x SLO burn-rate 与正确性告警分离。
- L3 测试创建真实 Attempt Job，检查一个 Job/一个 Pod、digest、projected token、无 Secret volume、Sandbox 零 RBAC 及 DNS 可用/API TCP 被阻断。

## Runbook 路径

- `deploy/runbooks/effect-unknown.md`
- `deploy/runbooks/lease-storm.md`
- `deploy/runbooks/postgresql-failover.md`
- `deploy/runbooks/object-checksum.md`
- `deploy/runbooks/nats-rebuild.md`
- `deploy/runbooks/credential-rotation.md`
- `deploy/runbooks/disaster-recovery.md`

运维入口为 `deploy/DOCS.md`；平台模块说明为 `backend/src/enterprise_agent_platform/platform/DOCS.md`。

## 验证证据

已执行并通过：

- Task 12 focused pytest：29 passed，5 skipped。
- Task 12 Ruff：All checks passed。
- 5 个 skip 均为显式环境门禁：3 个 Compose L2 环境变量未配置，2 个 Kind L3 gate 未启用。
- Ruff format check：13 files already formatted。
- 部署静态契约：11 项通过，包括全部 YAML/JSON 解析、Helm values schema、RBAC/NetworkPolicy、digest/non-root、Secret 扫描、脚本 `--help` 无副作用。
- `docker compose config --quiet`：通过（本机 standalone Docker Compose 2.40.2）。
- 三个 Bash 脚本 `bash -n`：通过。

未执行、不得称已通过：

- Backend 全量回归快照：241 passed，6 skipped，1 failed；唯一失败是并行 Task 13 已知的 OpenAPI route set 旧期望，缺少新 `/actions` 与 Artifact download authorization route，由总任务统一收口，不属于 Task 12 回归。
- Compose L2：Docker daemon 当前未运行。
- Kind L3：本机没有 kind 与 helm 命令。
- Docker image build、真实 NATS/PostgreSQL/MinIO、Kubernetes Job/NetworkPolicy、gVisor、HA、PITR/DR 与成本负载均没有在本次环境实际运行；对应脚本和测试是后续环境门禁，不是当前实测证据。
