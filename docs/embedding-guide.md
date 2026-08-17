# 嵌入现有业务系统指南

## 1. 推荐集成形态

"可嵌入"不等于"把平台源码拷进业务后端并直接导入内部表"。推荐把平台保持为独立部署和独立发布单元，只在三条稳定边界与业务系统融合：

1. 业务后端或独立 adapter service 实现 Host Ports 与业务 Connectors；
2. 业务网关把 `/api/agent-platform/` 反向代理到平台 public API，并传递用户 token；
3. 业务前端加载独立 SDK packages、把 AgentPanel 嵌入既有页面。

```mermaid
flowchart LR
    Page[现有业务页面] --> SDK[Agent UI SDK]
    SDK --> GW[业务网关 / same-origin proxy]
    GW --> API[独立 Agent Platform Public API]
    API --> PORT[Host Adapter Service]
    PORT --> IAM[企业身份/策略]
    PORT --> BIZ[业务资源 API]
    API --> EXEC[独立 Scheduler/Sandbox/Tool Gateway]
    EXEC --> CON[Business Connectors]
```

这样可以整体带走 `enterprise-agent-platform/`，也可以独立升级平台，而不会让平台依赖当前业务工程的 model、ORM、路由或部署目录。

## 2. 接入方必须提供的后端能力

### 2.1 AuthContextProvider

验证业务系统或企业 IdP 发出的 token，返回：

- tenant_id：由可信 claim/目录派生，不能读取客户端自报 header；
- actor_id：稳定用户或 service identity；
- scopes：当前 public 操作范围；
- request_id/trace_id：必须与平台传入值一致。

常用 public scopes：

```text
runs:create
runs:read
runs:cancel
runs:act
effects:recover        (仅授予可确认已知失败并启动重新规划的操作者)
artifacts:download
approvals:decide       (Surface-bound approval handler 最终仍校验)
```

### 2.2 ResourceResolver

把浏览器提交的 opaque resource_ref 转换为服务端权威事实：canonical ID、tenant、owner、classification、version 和 digest。建议 ref 形式是 `resource-type:opaque-id`，不包含 hostname、path、query、credential 或可执行表达式。

跨租户、已删除或无权访问的资源统一按不可见处理。Resolver 不返回数据库对象或业务源码类型，只返回平台公开的 ResolvedResource。

### 2.3 HostContextVerifier

把 host_context_ref 绑定同一 tenant 和 actor，校验该上下文确属当前用户，并返回 VerifiedHostContext；跨租户或缺失上下文时 fail closed。

### 2.4 PolicyContextProvider

根据 actor、workflow type、resolved resources 和 verified context 返回：

- allow/deny；
- policy version 和 digest；
- Agent 可用 scopes；
- max runtime、Tool calls、Artifact bytes、模型预算等 budget。

Policy budget 不能携带 endpoint、token、secret、password、credential 或任意 URL。Run 创建时这些权威结果会被快照，便于审计当时为什么允许。

### 2.5 CredentialBroker 与 Connector

Credential Broker 只在 Tool Gateway/Effect Worker 服务端获取短时凭据：Connector 接收 canonical resource ref、operation、validated arguments 和 CredentialMaterial。Connector 要：

- 对 READ 调用设 timeout、结果 schema 和大小限制；
- 对 WRITE 支持稳定 effect_key 幂等或可查询 reconciliation；
- 在外部系统真正以 effect_key 去重：平台消息是 at-least-once，只有该承诺才能形成 effectively-once 结果；
- 明确区分"已知失败"和"外部结果未知"；
- 不把凭据、原始响应 header 或敏感 URL 写入结果、事件或 trace。

## 3. 后端组合方式

### 3.1 独立 adapter package（推荐）

在平台目录之外创建一个很小的 adapter package。它可以依赖业务 SDK/API client 和 enterprise-agent-platform wheel，但平台 package 不能反向依赖它。adapter factory 返回 AgentPlatformContainer。

```python
from enterprise_agent_platform import AgentPlatformContainer

def create_container() -> AgentPlatformContainer:
    return AgentPlatformContainer(
        store=build_postgres_store(),
        control=build_control_service(),
        auth_context_provider=build_auth_port(),
        resource_resolver=build_resource_port(),
        host_context_verifier=build_context_port(),
        policy_context_provider=build_policy_port(),
        notifier=build_event_notifier(),
        ui_actions=build_surface_action_handler(),
        artifact_downloads=build_artifact_download_service(),
    )
```

上例中的 `build_*` 是接入方实现，不属于平台默认代码。生产容器必须使用 durable Store：不要调用 `create_in_memory_container`。

进程配置：

```text
AGENT_PLATFORM_CONTAINER_FACTORY=business_agent_adapter.app:create_container
AGENT_PLATFORM_WORKER_FACTORY=business_agent_adapter.worker:run_worker
```

API factory 可以独立部署，worker factory 必须返回 awaitable。缺少任何 factory 时进程应启动失败。

### 3.2 挂载 Router（适合独立 adapter ASGI service）

如果 adapter 自己管理 health、middleware、CORS 或 reverse-proxy prefix，可只挂公共 router：

```python
from fastapi import FastAPI
from enterprise_agent_platform import create_router

app = FastAPI()
app.include_router(
    create_router(create_container()),
    prefix="/api/agent-platform",
)
```

即使采用 router mount，也建议该 ASGI app 是可单独打包/带走的 adapter service，而不是把平台内部 Store 和 domain imports 散落到主业务后端。

### 3.3 API-only local demo

从平台目录运行：

```bash
uv sync --project backend --frozen
uv run --project backend uvicorn \
  enterprise_agent_platform.reference.local_stack:create_app \
  --factory --host 127.0.0.1 --port 8080
```

该模块必须被显式点名才会启用。它使用固定 synthetic identity 和进程内 Store，仅允许创建、读取和取消 synthetic Run：

```bash
curl --fail-with-body \
  -H 'Authorization: Bearer reference-local-demo' \
  -H 'Idempotency-Key: demo-create-1' \
  -H 'Content-Type: application/json' \
  --data '{
    "workflow_type": "synthetic-analysis",
    "intent": "summarize synthetic signals",
    "resource_refs": ["synthetic-case:case-D01"],
    "parameters": {}
  }' \
  http://127.0.0.1:8080/v1/runs
```

返回 QUEUED 只证明 create path 和 public contract 工作。localstack 没有 scheduler/worker，不会执行这个 Run；需要参考完整状态流时运行 reference vertical tests，而不是对 demo 增加宽松生产 fallback。

## 4. Public API 使用约定

### 4.1 幂等

POST /v1/runs 必须携带 Idempotency-Key。相同 actor、tenant、operation、key 和相同 canonical request 返回原结果；同 key 不同请求被拒绝。业务系统应在用户确认创建时生成稳定请求 ID，不要在 HTTP retry 时重新生成。

Create body 只包含：

- workflow_type
- intent
- 一个或多个 opaque resource_refs
- JSON parameters
- 可选 host_context_ref

不要加入 tenant_id、user role、Tool list、credential、Runner/Pod ID、callback URL 或 object-store path。

### 4.2 乐观并发

查询 Run 会返回 ETag。cancel、rerun 等会改变业务语义的操作应使用 If-Match，避免用户基于旧页面覆盖新状态。409 VERSION_CONFLICT 后重新取 RunView 并让用户确认，不要客户端循环强行覆盖。

### 4.3 Snapshot、事件分页与 SSE

推荐读取流程：

1. GET /v1/runs/{run_id} 取得完整 RunViewSnapshot 和 watermark；
2. 以 watermark 建立 SSE：`Last-Event-ID: <watermark>`；
3. SSE 断开后用同一 cursor 重连；
4. 收到序列间隙时调用 event replay；
5. 服务返回 RESYNC_REQUIRED 时重新获取完整 snapshot。

不要把 NATS 或 SSE 内存消息当 UI 状态。SDK RunProjectionStore 会丢弃重复 seq、缓冲乱序、用 REST replay 修补 gap，并在 retention floor 超出时从 snapshot resync。

### 4.4 稳定错误合同

前端按 ApiErrorEnvelope.code 判断认证、权限、冲突、validation、resync 或 retryable dependency error。不要解析英文 message；message 是安全显示文本，不是程序协议。

### 4.5 已知失败 Effect 的恢复命令

Effect 已收敛为 FAILED、Run/ExecutionUnit/Step 处于 NEEDS_ATTENTION 时，有权操作者可调用：

```text
POST /v1/runs/{run_id}/effects/{effect_id}/recover
Authorization: Bearer <token with effects:recover>
If-Match: "<current-run-version>"
Idempotency-Key: <stable-recovery-request-id>
```

该路由强制要求 `effects:recover` scope、强 If-Match Run version 和 Idempotency-Key：缺少 precondition 会拒绝，version 冲突后应重新读取 RunView，不得用循环覆盖新状态。相同 actor/tenant/operation/key 只能对应相同 canonical request。

成功响应为更新后的 RunViewSnapshot 和 ETag，原 FAILED EffectLedger 保持不变：Run/ExecutionUnit 转为 RECOVERING、Step 转为 ACTIVE，scheduler 从原 COMMITTED Checkpoint 创建新 Attempt。successor Agent 必须重新规划、生成新 ActionProposal 并获得新审批；该命令绝不会重新 dispatch 旧 Effect。

## 5. 前端 SDK 接入

### 5.1 Packages

```text
@platform/agent-ui-protocol   wire types + Zod validators
@platform/agent-ui-client     REST/SSE client + failed-Effect recovery + projection store
@platform/agent-ui-catalog    allowlisted component renderer
@platform/agent-ui-react      Provider + AgentPanel
```

### 5.2 最小 React 组合

```tsx
import { AgentPlatformClient } from "@platform/agent-ui-client";
import {
  AgentPanel,
  AgentPlatformProvider,
} from "@platform/agent-ui-react";
import type { HostBridgeCapabilities } from "@platform/agent-ui-protocol/host";

const client = new AgentPlatformClient({
  baseUrl: "/api/agent-platform/",
  getAccessToken: () => identityClient.getShortLivedToken(),
});

const hostBridge: HostBridgeCapabilities = {
  schema_version: "host-bridge-capabilities/v1",
  navigate: async ({ destination_ref }) => {
    hostRouter.openStableDestination(destination_ref);
  },
  downloadAuthorizedArtifact: async ({ authorization }) =>
    await downloadManager.download(authorization),
};

export function EmbeddedAnalysisPanel() {
  return (
    <AgentPlatformProvider client={client} hostBridge={hostBridge}>
      <AgentPanel run={runSummary} surface={currentSurface} />
    </AgentPlatformProvider>
  );
}
```

`identityClient`、`hostRouter` 和 `downloadManager` 是宿主自己的对象，SDK 不需要知道业务 ORM、页面全局 store 或身份 token 的长期保存位置。

对已知 FAILED Effect，宿主可以把经授权的操作员意图交给 client：

```ts
const recovered = await client.recoverFailedEffect(run.run_id, effect.effect_id, {
  expectedRunVersion: run.version,
  idempotencyKey: recoveryRequestId,
});
```

`recoverFailedEffect` 会 URL-encode Run/Effect ID，发送 POST、强 If-Match 和 Idempotency-Key，严格解析 RunViewSnapshot 并校验返回的 run_id 与请求一致。SDK 不会自动在冲突后重试，也不会替操作者获得 `effects:recover` 权限。

### 5.3 Host Bridge 边界

Host Bridge 只允许三类能力：

- 获取 `audience=enterprise-agent-platform` 的短期 access token；
- 按稳定 destination_ref 请求宿主导航；
- 处理服务端已经签发的 ArtifactDownloadAuthorization。

Bridge 不向 Surface 暴露任意 fetch、eval、DOM、router object、credential store 或业务 API client。

## 6. A2UI Surface 与动作

### 6.1 渲染

服务端只允许固定 catalog 和 protocol version，SDK switch 渲染 ProgressCard、EvidenceSummary、ApprovalCard 与 ArtifactCard；未知 component 显示 Unsupported component，不会动态下载代码。

Surface revision 包含：

- stable surface_id、run_id、revision；
- source Attempt 和 source event seq；
- canonical document 和 checksum。

浏览器 SDK 通过 `GET /v1/runs/{run_id}/surfaces/{surface_id}`（可选 `?revision=N`）读取不可变 revision，并在 `ui.surface.committed` 事件后按需重新拉取（SDK 的 RunProjectionStore 只记录 surface_id/revision，文档按提交顺序异步获取）。

业务页面不得自行把模型文本拼成 HTML，也不要绕过 validator 直接调用动态组件 registry。

### 6.2 Approval Action

ApprovalCard 显示服务端 canonical target 与 request digest，点击后 SDK 发送：

```json
{
  "run_id": "run_stable_id",
  "surface_id": "surface_stable_id",
  "surface_revision": 2,
  "action_ref": "approval:stable-id:approve",
  "client_action_id": "action_unique_id",
  "displayed_digest": "sha256:server-request"
}
```

Action 不包含 approval_id、decision scope、Tool、Connector、target、payload 或 credential。服务端从 immutable Surface 反查 Approval，并通过 ApprovalDecisionService 再次验证权限、摘要、状态、版本和过期时间。

### 6.3 Artifact 下载

- 浏览器直接访问短期 HTTPS URL；
- 经过宿主 download proxy 做额外审计/DLP；
- 在受限桌面环境调用企业下载管理器。

不要把 S3 object key、长期 URL 或 S3 credential 放进 Surface。

## 7. 反向代理与浏览器策略

推荐同源路径 `/api/agent-platform/`，由网关完成：

- TLS termination 和 upstream mTLS；
- public/internal route 分离；
- request body、header 和连接时长限制；
- SSE 禁用代理 buffering，并配置合理 idle/lifetime；
- Authorization、Cookie、query 和 signed URL 日志脱敏；
- CSRF 策略与业务认证模式一致；
- rate limit 按 tenant/actor/operation，而不是仅按 IP。

若跨域部署，只 allowlist 明确 origin/method/header，不使用 credentialed wildcard CORS。Internal Runtime/Effect API 必须使用独立 hostname/network policy，不能因反向代理 prefix 配错而暴露到浏览器。

## 8. 版本与发布兼容

- OpenAPI、JSON Schema、event payload 和 A2UI protocol 都有显式 schema_version。
- 后端新增 optional 字段前先确认前端 strict Zod schema 的兼容策略；
- breaking change 发布新 version 和迁移窗口，不原地改变旧字段含义；
- SDK 解析失败要显示安全错误并停止动作，不"尽力猜测"；
- 宿主 adapter 与平台 wheel 独立版本，通过 contract fixtures 做 CI compatibility test；
- 先部署能同时读取 old/new 的 consumer，再切 producer，最后清理旧合同。

## 9. 生产接入验收清单

- [ ] 平台目录可在空父目录独立复制、构建、安装和执行 L1。
- [ ] Adapter 只依赖根包公开边界，没有平台对宿主源码的反向 import。
- [ ] Auth context 的 tenant/actor/scopes 由可信 token 派生。
- [ ] Resource/Context/Policy Ports 对跨租户和 timeout fail closed。
- [ ] PostgreSQL schema、migration、backup/PITR 与 connection limits 已评审。
- [ ] NATS 只做通知，Outbox/Inbox 在数据库中启用。
- [ ] S3 versioning、checksum、retention、scan 和 download policy 已验证。
- [ ] READ Tool scopes、grant、resource prefix、timeout 和 result size 已登记。
- [ ] WRITE Connector 支持 effect-key 幂等和 UNKNOWN reconciliation。
- [ ] Effect watchdog 只在 executor lease 按数据库时间到期后标记 UNKNOWN。
- [ ] effects:recover 只授予可发起重新规划的操作者，宿主正确传递强 If-Match 与幂等键。
- [ ] FAILED reconciliation 校验 executor inactive 和 stable observation。
- [ ] Internal API 与 public API 网络、DNS、identity 和证书分离。
- [ ] Sandbox 无 Kubernetes RBAC、无 Secret mount、无任意 egress，强化 runtime 完成 L4。
- [ ] 前端只用 allowlisted A2UI catalog，Host Bridge 无任意 fetch/eval 能力。
- [ ] L1、L2、L3 均有当前版本证据，L4 在目标生产等价环境单独签署。
