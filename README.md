# Enterprise Agent Platform

这是一个可以整体复制、独立安装和嵌入现有业务系统的企业 Agent 平台参考实现。它把稳定业务事实、Agent 执行、Sandbox、审批、Artifact、A2UI、宿主权限和分布式基础设施拆成明确边界；源码、前端包、Contracts、部署清单和验证入口全部位于本目录内。

当前实现已经不是只有 Contracts 的骨架：L1 覆盖 Run/Step/Attempt/Checkpoint/Lease、恢复、审批、Effect、Artifact、A2UI、FastAPI 公共 API、前端 SDK、SQL adapter、消息与平台清单；L2/L3 分别提供 Compose 依赖集成与 Kind Sandbox Attempt 的可选真实环境门禁，生产 HA、gVisor、外部身份系统和企业依赖仍需要在目标环境完成 L4 验证，不能由本地测试代替。

## 复制后验证

前置条件：Python 3.12+、[uv](https://docs.astral.sh/uv/)、Node.js 22+ 和 npm。

```bash
cd enterprise-agent-platform
./scripts/verify.sh l1
```

l1 会执行 frozen Python/npm 安装，后端测试、复制到空父目录后的导入测试、Ruff、Contracts 重建对比、clean-wheel 安装 smoke、前端测试/typecheck/lint/build、宿主 import/绝对路径/高置信秘密扫描以及 shell 语法检查，任一检查缺失或失败都不会被标成通过。

复制时不需要携带缓存和构建产物：`.dockerignore`、`.gitignore` 和 portability test 会保护这个边界。复制完成后，在新目录重新运行 `./scripts/verify.sh l1` 即可证明没有依赖原来的父仓库。

## API-only 本地演示

这个入口只用于观察独立 HTTP 合同。它使用固定的 reference 身份、合成资源和进程内存储：进程退出即丢失数据，也没有 scheduler/worker，因此创建的 Run 保持 QUEUED。它不会被生产入口隐式加载。

```bash
uv sync --project backend --frozen
backend/.venv/bin/uvicorn \
  enterprise_agent_platform.reference.local_stack:create_app --factory --port 8080
```

另一个终端创建 Run：

```bash
curl -i http://127.0.0.1:8080/v1/runs \
  -H 'Authorization: Bearer reference-local-demo' \
  -H 'Idempotency-Key: local-create-1' \
  -H 'Content-Type: application/json' \
  --data '{
    "workflow_type": "synthetic-analysis",
    "intent": "Analyze a portable synthetic resource",
    "resource_refs": ["synthetic-case:case-42"],
    "parameters": {"analysis_mode": "summary", "max_items": 10},
    "host_context_ref": "reference-context:demo"
  }'
```

包含真实 checkpoint-approval-新 Attempt/Lease-Effect-final RunView 的进程内完整参考链由以下测试证明：

```bash
backend/.venv/bin/pytest -q backend/tests/test_public_smoke.py backend/tests/test_vertical_e2e.py
```

## 嵌入现有系统

宿主只实现稳定 Ports，并从顶层包组装 router；平台不会 import 宿主数据库、用户模型或前端源码：

```python
from enterprise_agent_platform import create_in_memory_container, create_router

container = create_in_memory_container(
    auth_context_provider=auth_port,
    resource_resolver=resource_port,
    host_context_verifier=context_port,
    policy_context_provider=policy_port,
)
host_app.include_router(create_router(container), prefix="/agent-platform")
```

`create_in_memory_container` 只适用于测试。生产 adapter 必须注入 durable PlatformStore、可信身份/资源/策略 Ports、A2UI action handler、Artifact authorizer，以及 scheduler/worker 组合。审批和外部 WRITE 分别通过 public `ApprovalDecisionService`、`DurableEffectExecutor` 组装：浏览器不能调用内部 Effect 执行/对账路由，也不能自行选择 tenant、tool、scope、credential 或 canonical target。Effect 开始执行时，internal route 把经验证的 effect-worker service subject 作为 executor_id，与递增的 execution_epoch 和执行租约一起持久化；Connector finish 和 watchdog 都必须提交且命中该 owner/epoch 对，否则以 `EFFECT_EXECUTOR_FENCE_MISMATCH` 拒绝。Audit 记录实际 worker subject 与 epoch。watchdog 还只能在租约到期后把遗留的 EXECUTING 标记为 UNKNOWN。对账还必须注入独立 `EffectReconciliationAuthorizer` 并提交绑定 Effect 和外部证据的 digest；对账为 FAILED 还要证明 executor 已停止且外部观测稳定。

已知失败的 Effect 不会被原地重放。持有 `effects:recover` scope 的操作者可通过 public recovery command（前端 SDK 为 `recoverFailedEffect`）从已提交 Checkpoint 进入恢复：原 FAILED Effect 保持不变，successor Attempt 必须重新规划、生成新提案并再次审批后才能产生新 WRITE。

Compose 的 api 和 worker 位于显式 runtime profile。`deploy/.env.example` 故意不提供可运行的 factory：目标镜像必须安装独立的宿主 adapter 包，并配置 `AGENT_PLATFORM_CONTAINER_FACTORY` 与 `AGENT_PLATFORM_WORKER_FACTORY`。这避免 reference demo 变成生产权限回退。

## 验证层级

| 命令 | 实际证明 | 不证明 |
| --- | --- | --- |
| `./scripts/verify.sh l1` | 本机 Contracts、领域/服务、公共 API、SDK、copy/wheel portability、静态部署策略 | PostgreSQL/NATS/S3 真实并发与容器网络 |
| `./scripts/verify.sh l2` | Disposable PostgreSQL、NATS JetStream、MinIO、Alembic 与 adapter 集成 | Kubernetes、Sandbox 隔离、HA |
| `./scripts/verify.sh l3` | Disposable Kind 中真实 Job/Pod、RBAC、NetworkPolicy 与 projected identity | gVisor、跨节点 HA、生产依赖 |
| 目标环境 L4 | gVisor、OIDC、HA/failover/PITR、S3 version restore、凭据轮换、容量与成本 | 不能由本仓库自动宣称 |

缺少 Docker、Kind、kubectl 或 Helm 时，请求的 L2/L3 会输出 UNVERIFIED 并以 69 退出，不会静默跳过。

## 目录与文档

- `backend/`：可构建 Python wheel、公共 FastAPI router、控制面与 execution/tool/artifact Ports。
- `frontend/`：TypeScript `agent-ui-protocol`、client、allowlisted catalog、React renderer 和 embedded-host example。
- `deploy/`：Compose L2、Helm/Kind L3、可观测性策略与 runbooks。
- `contracts/`：确定性 JSON Schema、OpenAPI 和 golden fixtures。
- `scripts/`：统一验证、生成物、portability、Compose 和 Kind 入口。
- `docs/architecture.md`：统一执行模型、状态、数据流与分布式边界。
- `docs/security.md`：身份、能力、Sandbox 与残余风险。
- `docs/embedding-guide.md`：后端/前端嵌入合同。
- `docs/migration-guide.md`：从业务仓库内原型迁移到独立平台的方法。
- `docs/implementation.md`：已实现能力和证据边界。
