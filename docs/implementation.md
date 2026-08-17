# 实现说明与证据边界

## 1. 阅读方法

本文件描述当前目录中已经存在的实现，不把目标架构、部署模板或测试替身写成生产事实。判断一个能力时应同时区分四件事：

1. 是否存在领域模型或接口；
2. 是否存在可执行参考实现；
3. 是否存在生产适配入口；
4. 是否在相应环境完成验证。

例如，S3 adapter、Helm values 和 NetworkPolicy 已存在，表示接口和清单可审查；只有真实对象存储、集群和故障演练通过后，才表示生产能力得到验证。

## 2. 目录结构

```text
enterprise-agent-platform/
    backend/     Python 包、Alembic 和测试
    frontend/    TypeScript protocol/client/catalog/React SDK 与嵌入示例
    deploy/      Compose、镜像、Helm、Kind、可观测性和 runbooks
    contracts/   版本化 JSON Schema、OpenAPI 和 golden fixtures
    scripts/     合同生成、可移植性检查和 L1/L2/L3 验证入口
    docs/        架构、安全、接入、迁移与交付证据
```

整个目录是交付单元。backend 不通过相对路径读取外部业务源码，前端 workspaces 不依赖宿主私有组件库，镜像 context 和脚本也限制在本目录。

## 3. Backend 模块

| 模块 | 当前职责 | 当前实现状态 |
| --- | --- | --- |
| contracts/ | 严格 Pydantic command、event、view、A2UI、Artifact、error 合同与 schema export | 已实现并有 deterministic golden tests |
| domain/ | Run/Step/ExecutionUnit/Attempt/Lease/Checkpoint/Approval/Effect 等不可变记录与纯 FSM | 已实现；不依赖 FastAPI/SQLAlchemy/Kubernetes |
| control/ | Run create/cancel/rerun、Attempt reserve/activate/heartbeat、Checkpoint、审批、Lease/FAILED Effect 恢复、查询投影、fair scheduler | 已实现共享 services；单 Agent admission 是当前策略 |
| persistence/ | 持久化 Ports 与独立 adapter（PlatformStore/PlatformTransaction、InMemoryPlatformStore、SQLAlchemy store、Alembic） | 已实现 |
| integration/ | 宿主拥有的集成 Ports（AuthContextProvider、ResourceResolver、HostContextVerifier、PolicyContextProvider） | 协议已实现；具体企业系统 adapter 由接入方实现 |
| security/ | Auth、Resource、HostContext、Policy 四个可信 Host Ports 和 canonical authorization snapshot | 协议与验证边界已实现；具体企业系统 adapter 由接入方单独实现 |
| execution/ | Pod bootstrap、TokenReview adapter、Capability issuer/verifier、revocation、generation fence 和 scope intersection；Agent Runtime loop、Kubernetes Job spec 与 async orchestrator adapter | 已实现；通用生产 worker/factory 不内置 |
| tools/ | Tool registry/grant、READ gateway、Credential/Connector Ports、ActionProposal/Effect、executor Lease/epoch 与 durable effect execution | 已实现共享边界；真实业务 Connector 与 Broker 由接入方提供 |
| artifacts/ | 不可变 Artifact 与 WorkspaceSnapshot 服务（ObjectStore、S3ObjectStore、download authorization） | 已实现 |
| ui/ | allowlisted catalog、Surface validation/persistence、stale action binding 和 approval action handler | 已实现，公开 catalog 为四个组件 |
| fastapi/ | 可挂 public router/app、SSE、稳定错误合同、内部 Runtime/Effect API | 已实现；public 与 internal app 必须在网络和身份层隔离 |
| reference/ | synthetic 可执行纵切与显式 API-only Localstack | 仅用于测试、学习和 copy-and-run：不是生产 adapter |
| platform/ | NATS/Inbox、Outbox publisher、OTel/Prometheus、进程 factory entrypoint | adapter 与进程入口已实现：不保存领域真相 |

## 4. 稳定公开 Python 边界

嵌入/适配应用优先以包根导入，避免依赖内部目录结构：

```python
from enterprise_agent_platform import (
    AgentPlatformContainer,
    ApprovalDecisionService,
    AuthContextProvider,
    Connector,
    CredentialBroker,
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
)
```

重要边界：

- `create_router(container)`：把公共 `/v1` router 挂到适配应用；
- `create_app(container)`：创建独立 public FastAPI app；
- `create_in_memory_container(...)`：仅测试和演示，四个可信 Host Ports 仍为必填，不提供 permissive auth；
- `AgentPlatformContainer`：组合 Store、Control service、Host Ports、SSE notifier、可选 UI Action 和 Artifact download authorizer；
- `ApprovalDecisionService`：消费持久化 Approval，一次性写入决定和可选 Effect；
- `DurableEffectExecutor`：authority snapshot 固化 tool spec、connector、scopes、target 与 payload digest；internal route 把已验证 effect-worker subject 作为 executor identity 传入；service 再校验 effect capability、payload digest、审批和 connector binding，并用该 identity/epoch/lease ownership fence 执行和收尾 Effect（PREPARED→EXECUTING→SUCCEEDED/FAILED/UNKNOWN）；reconciliation 另行使用 reconciler identity、evidence 与状态 fence；
- `FailedEffectRecoveryService`：在保留旧 FAILED Effect 不变的前提下、从持久化 Checkpoint 进入新 Attempt 的重新规划流程。

`PlatformStore` 和 `PlatformTransaction` 是生产持久化 adapter 的公开 Protocol。调用方不应导入 `persistence.tables` 或修改内部 SQLAlchemy model。

## 5. Public HTTP API

当前 public router 提供：

| Method | Path | 说明 |
| --- | --- | --- |
| POST | /v1/runs | 校验 Idempotency-Key、Host authority 并创建 Run |
| GET | /v1/runs/{run_id} | 查询持久化 RunViewSnapshot 与 ETag |
| GET | /v1/runs/{run_id}/events | 按 after_event_seq 分页重放事件 |
| GET | /v1/runs/{run_id}/events/stream | SSE 增量流，支持 Last-Event-ID |
| GET | /v1/runs/{run_id}/surfaces/{surface_id} | 读取一个不可变 A2UI surface revision（默认最新，可用 ?revision= 指定） |
| POST | /v1/runs/{run_id}/cancel | 需要 If-Match，提交取消意图 |
| POST | /v1/runs/{run_id}/effects/{effect_id}/recover | 需要 effects:recover、If-Match 和 Idempotency-Key，保留旧 FAILED Effect 并请求新 Attempt 重新规划 |
| POST | /v1/runs/{run_id}/actions | 处理 Surface-bound UI Action；需显式组装 handler |
| GET | /v1/runs/{run_id}/artifacts/{artifact_id}/versions/{version}/download-authorization | 签发短期下载授权；需显式组装 policy/signer |

公共错误统一为 `api-error/v1`，且 trace ID、retryable 和稳定 code 可被 SDK 解析。身份从 Authorization adapter 派生，客户端自报 tenant header 会被拒绝。

### 5.1 Internal API 不是 public API

`fastapi/internal.py` 提供 Runtime bootstrap/restore/heartbeat、READ tool、Artifact、ActionProposal、final Checkpoint、failure、Surface publish 和 Effect execution。它要求 Runtime capability 或 service identity，必须只在受控内部网络暴露。

Effect 执行路径是：

```text
POST /internal/v1/tenants/{tenant_id}/effects/{effect_id}/execute
Authorization: Bearer <effect-worker service identity>
X-Effect-Capability: <tenant/effect/approval/request/tool/target-bound token>
```

浏览器和普通业务后端不得调用该路由。tenant_id 出现在内部 path 不代表 caller 可以选择 tenant：Effect worker identity、capability claims 和 PostgreSQL Effect facts 必须一致。UNKNOWN 对账使用分离的 internal reconcile route 与 reconciler identity；对账为 FAILED 时还要通过 executor inactive 和 stable observation fence。

## 6. 持久化实现

### 6.1 PostgreSQL 事实

SQLAlchemyStore 与 Alembic 覆盖 Run、authorization snapshot、Step、ExecutionUnit、Attempt、Lease、Checkpoint、WorkspaceSnapshot、Artifact/Version、ActionProposal、Approval、EffectLedger、UISurface/Revision、RunEvent、Idempotency、Outbox、Inbox 和 Audit 等事实。

正确性依赖：

- tenant-qualified key 和查询；
- database time；
- version CAS；
- 每 Run 严格递增 event sequence；
- active Attempt/Lease 唯一约束；
- generation fence；
- mutation、Audit、Outbox/Inbox 同事务；
- Effect effect_key 唯一性；
- Effect 领取时的已验证 worker executor_id、递增 execution_epoch 和 database-time execution lease 的 CAS，以及 finish/watchdog 对 expected owner/epoch 的强制匹配；
- Checkpoint 只引用 READY immutable objects。

### 6.2 进程内 Store

InMemoryPlatformStore 用于 L1、单元测试、reference harness 和 Local API demo。它验证相同领域协议，但不能证明：

- 多进程/多副本并发；
- 数据库故障和连接池行为；
- migration rollback；
- 持久化跨进程重启；
- PostgreSQL isolation/locking 的真实表现。

## 7. Checkpoint、Snapshot 与恢复实现

Checkpoint 记录：

- completed Step IDs 与 active Step context；
- input/output Artifact versions；
- WorkspaceSnapshot ID；
- resolved Tool call IDs；
- Effect states 与 consumed budget；
- model context summary ref；
- runtime image digest。

ControlPlane 在所有 referenced Artifact/Snapshot READY、ownership/generation/checksum/image digest 合法时推进 current_checkpoint_id。

审批 pause 在一个事务内提交 Checkpoint 并释放 Lease。Lease 过期 recovery 使用 Inbox 去重并新建 successor Attempt/Lease；Approval 拒绝或 Effect 成功后也从持久化 Checkpoint 重新 admission。原 Pod 不是恢复目标。

对已知 FAILED Effect，FailedEffectRecoveryService 要求 `effects:recover`、Run version CAS 和幂等键，且只接受与 NEEDS_ATTENTION Run/ExecutionUnit/Step 和现有 COMMITTED Checkpoint 绑定的 Effect。成功后原 Effect 仍为 FAILED，Run/ExecutionUnit 为 RECOVERING、Step 为 ACTIVE，scheduler 创建新 Attempt；successor Agent 必须重新规划、提案和审批，而不是重放旧 Effect。

Runtime 当前 reference vertical 的 terminal completion 最后一步仍由 ReferenceTerminalAdapter 模拟，因为共享 public command service 尚未暴露通用 successor Runtime 提交 terminal success 入口。该限制记录在 `reference/README.md`，不能把 reference harness 描述为完整生产 Runtime。

## 8. Tool、Approval 与 Effect 实现

### 8.1 READ 路径

ToolGateway 把 invocation/result facts 落入 durable store/object store，实现五层 scope intersection、runtime fence、ToolGrant、ToolSpec、resource prefix、schema/size/timeout、call ID 幂等、Credential Broker 和 Connector 调用。当前 invocation/result repository 还包含进程内实现：生产组装应把需要恢复的 invocation/result 接到 durable store。

### 8.2 WRITE 路径

共享生产语义由两项公开 service 构成：

- `ApprovalDecisionService`：审批 CAS、idempotency、Event、Audit、Outbox 和 PREPARED Effect；
- `DurableEffectExecutor`：Effect PREPARED→EXECUTING→SUCCEEDED/FAILED/UNKNOWN。authority snapshot 固化 tool spec、connector、scopes、target 与 payload digest；internal execute route 验证 effect-worker service identity，并把实际 worker subject 作为 executor_id 传入，执行领取持久化该 executor_id、递增 execution_epoch 和租约截止点；每个 Connector finish 都必须命中领取时的 expected owner/epoch，Audit actor/details 记录真实 worker subject/epoch。watchdog 同样必须命中它所观测的 expected owner/epoch，然后才能在租约到期后把遗留 EXECUTING 变为 UNKNOWN；owner/epoch mismatch 以 EFFECT_EXECUTOR_FENCE_MISMATCH 拒绝，租约内以 EFFECT_EXECUTOR_STILL_ACTIVE 拒绝。UNKNOWN reconciliation 需要专用 capability 和 evidence digest，对账为 FAILED 还需要 executor inactive 和 stable observation。

Effect transport 是 at-least-once，Connector 必须在外部系统兑现稳定 effect_key 的幂等语义；因此只能把整体结果描述为 effectively-once，而不能宣称跨系统 exactly-once。

`tools/effects.py` 还保留较小的 in-memory/reference primitives，用于局部协议测试：新生产适配应优先组装根包公开的 shared services，而不是创建第二套 approval/effect 真相。

## 9. Artifact 与 UI 实现

### 9.1 Artifact

- LocalObjectStore 防路径逃逸和覆盖，适合开发；
- S3ObjectStore 把 SDK 隔离在线程中，使用 immutable put 语义；
- ArtifactService/WorkspaceSnapshotService 校验 generation、checksum、scan 和 READY；
- ArtifactDownloadService 重新做 policy check，生成最长 15 分钟的授权 URL，并持久化 Audit。

生产仍需提供真实 malware/DLP scanner、S3 client、download signer、classification/retention policy 和 Lifecycle 配置。

### 9.2 A2UI

后端 catalog 当前固定为：

- ProgressCard
- EvidenceSummary
- ApprovalCard
- ArtifactCard

SurfaceValidator 做协议、catalog、字段、深度、item、string、unsafe content 和 canonical checksum 校验。Surface revision 在 PostgreSQL 不可变保存，并与 source Attempt/generation/event seq 绑定。ApprovalCard 由控制面基于持久化 Approval/ActionProposal 构造：Runtime 不在释放 Lease 后伪造审批内容。

## 10. Frontend packages

| Package | 职责 |
| --- | --- |
| @platform/agent-ui-protocol | wire types + Zod validators |
| @platform/agent-ui-client | Bearer API client、Run/Event 请求、recoverFailedEffect、严格 SSE parser、Artifact authorization、projection store |
| @platform/agent-ui-catalog | allowlisted component renderer |
| @platform/agent-ui-react | Provider、projection hook、AgentPanel 和 Host Bridge 连接 |
| @platform/embedded-host-example | 与任何业务系统无关的 React/Vite 嵌入示例 |

SDK 不持有业务权限。getAccessToken、navigation 和 authorized download 由宿主 Bridge 注入：Surface 只能触发稳定 action/artifact intent。

## 11. MessageBus 与可观测性

- OutboxPublisher 从 PostgreSQL 选择未发布消息，发布到 MessageBus 后标记 published；
- MessageEnvelope 只允许稳定 ID、schema/version、causation 和 W3C trace context；
- DiagnosticTelemetry 只创建有限操作 span，不保持数小时 Run trace；
- correctness signal 与普通 SLO metric 分离，避免高基数 ID 进入 Prometheus label。

NATS 和 telemetry 不能用于恢复 Run；恢复始终读取 PostgreSQL。

## 12. 进程与部署入口

`platform/entrypoint.py` 提供两个模式：

- `api`：从 `AGENT_PLATFORM_CONTAINER_FACTORY=module:callable` 构造 FastAPI 或 AgentPlatformContainer；
- `worker`：从 `AGENT_PLATFORM_WORKER_FACTORY=module:callable` 构造 awaitable worker loop。

未配置或返回错误类型时进程直接失败，不回退为假认证或假 worker。Compose 的 runtime profile 才启动 API/worker；L2 test profile 只启动依赖和精确集成测试。

Helm/Kind/Compose 的详细职责见 `deploy/DOCS.md`。生产 Chart 不安装企业托管 PostgreSQL、NATS、S3、OIDC、External Secrets Operator、KEDA 或 telemetry backend，而是通过 endpoint/Secret contracts 接入。

## 13. 显式 Localstack

`enterprise_agent_platform.reference.local_stack:create_app` 是唯一用于 copy-and-run 的简单 API factory：

- token 定为本地演示 token；
- tenant 和 actor 固定为 synthetic identity；
- 使用 InMemoryPlatformStore；
- 只接受 `synthetic-case:`、`synthetic-dataset:` resource refs、`reference-context:` context 和 `synthetic-analysis` workflow；
- 每次进程重启数据消失；
- create/read/cancel Run；
- 不含 scheduler、worker、NATS、S3、真实 Connector 或 durable Effect；
- create 后 Run 停留在 QUEUED。

任何 module 都不会隐式导入 Localstack，production entrypoint 也不会选择它。若把该 factory 填入生产配置，是接入方的显式错误配置，不是平台 fallback。

## 14. Contracts 与生成物

`scripts/generate-contracts.py` 从 Pydantic 和 public FastAPI app 生成 `contracts/schemas/` 和 `contracts/openapi.json`。

`scripts/check-generated.sh` 在临时目录重生成并做 byte-level diff，防止代码合同和提交生成物漂移。

Golden fixtures 覆盖 event、RunView、approval、Artifact authorization、capability、Tool invocation、A2UI Surface 等跨语言样本。前端 Zod schema 与后端 Pydantic schema 都应对这些版本保持一致：破坏性变更必须发布新 schema version，而不是原地放宽解析。

## 15. 验证等级

统一入口：

```bash
./scripts/verify.sh l1
./scripts/verify.sh l2
./scripts/verify.sh l3
```

脚本请求的工具或环境不可用时返回非零并打印 UNVERIFIED，不会把 skip 写成 pass。

| Level | 证明内容 | 不证明内容 |
| --- | --- | --- |
| L1 | frozen Python/npm dependencies、backend unit/contracts/E2E、Ruff、generated parity、clean wheel install、全目录 copy portability、前端 test/typecheck/lint/build、secret/path/import/symlink 边界 | 真实数据库、消息队列、对象存储、Kubernetes、强化 runtime |
| L2 | disposable Compose 的真实 PostgreSQL/NATS/MinIO、Alembic 与 adapter 集成 | Kubernetes、Sandbox 隔离、HA |
| L3 | disposable Kind 的真实镜像/Job/Pod、digest、projected token、Sandbox 零 RBAC、无 Secret volume、DNS 和 API egress 阻断 | 生产 CNI、gVisor、跨节点 HA/PITR、真实企业依赖 |
| L4 | 生产等价隔离、HA/failover/PITR、NATS R3、对象版本恢复、OIDC/Secret/KMS、credential rotation、真实 Connector reconciliation、攻击与容量成本演练 | 只可由目标环境的证据声明；仓库内没有自动把 L4 标为通过的命令 |

每次交付的实际运行结果应写入带日期的 task report：长期文档只描述 gate contract，不固化易过期的通过数量。

## 16. 当前已知缺口

- 没有通用生产 Host adapter：这是有意的独立边界，需要在接入项目实现。
- 没有内置生产 worker loop：entrypoint 要求显式 factory。
- local stack 不执行 queued Run。
- reference vertical 最终 terminal success 仍有 reference-only adapter。
- Artifact/WorkspaceSnapshot 的 generation-fenced publish service 当前附带进程内 repository：SQLAlchemy Store 已有 durable metadata 表与操作，但生产组装仍需提供把 publish service 接到 durable Store 的 repository adapter，不能沿用 reference repository。
- 多 Agent dependency graph、跨 execution-unit admission 和 Run 聚合终态尚未实现：当前只有单 Agent 策略。
- 强化 RuntimeClass、企业网络、OIDC、KMS、Secret manager、HA/PITR 和真实 Connector 需要 L4 环境验证。
- 生产数据 retention、right-to-delete、DLP、legal hold 和成本模型需要接入组织定义。

这些缺口不能通过添加宽松 fallback 掩盖；应保持进程 fail closed，并在 [migration-guide.md](migration-guide.md) 中作为接入工作包管理。
