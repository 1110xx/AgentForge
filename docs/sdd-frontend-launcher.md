# SDD：前端入口（AgentLauncher / 浮窗 / 自由对话）

> 状态：已确认方案（Phase 3.6 前端入口 F 项转必做，2026-08-23）
> 关联：`SDD.md`（主架构，§13.1 前端入口缺口、Phase 4 远期）、`sdd-followup-mode.md`（追问模式，本项目已有前端独立 SDD 先例）、`frontend/DESIGN.md`（前端设计约束）、`deploy/helm/README.md`（部署细则）、`deploy/operations.md`
> 目标文件树：后端 `control/chat.py`、`fastapi/router.py`（+1 端点）、`contracts/commands.py`(+1)、前端 `agent-ui-protocol` / `agent-ui-client` / `agent-ui-react`（+Launcher）、`examples/embedded-host-example`（+chat mock）、`.github/workflows/ci.yml`（+frontend job）、Helm（+frontend 工作负载）

---

## 1. 现状声明

### 1.1 已完成（本方案不改动）

- **后端 API 层**：`fastapi/router.py` 公开 `/v1`：`POST /v1/runs`（`CreateRunCommand`，幂等 `Idempotency-Key`）、`GET /runs/{id}`、`GET /runs/{id}/attempts`、`GET /runs/{id}/events`（含 **SSE `/events/stream`**）、`POST /runs/{id}/cancel`、`/reruns`、`/effects/{id}/recover`、`GET /runs/{id}/surfaces/{surface_id}`、`POST /runs/{id}/actions`（UiAction 审批）、`POST|GET /runs/{id}/followups`（追问，**真实语义：触发新 Attempt 调度**，非文档 §1.4 的 session 只读模式）、`GET /artifacts/.../download-authorization`。鉴权：`authenticate_request` + `require_scope(ctx, "runs:create"/"runs:read")`；错误：`ApiErrorEnvelope` + `_error_responses(...)`。
- **前端生态**（`frontend/` npm workspaces monorepo，React 19 + Vite 7 + vitest + TS）：`agent-ui-protocol`（契约 zod/ts，`RunViewSnapshot`/`SurfaceRevision`/`EnterpriseEventEnvelope`/`ArtifactDownloadAuthorization`）、`agent-ui-client`（`AgentPlatformClient`：createRun/cancel/actions/followups/SSE 同步器 + projection）、`agent-ui-catalog`（服务器 catalog 校验）、`agent-ui-react`（`AgentPanel`/`FollowupPanel`/`ApprovalCard`/`AgentPlatformProvider`）、`examples/embedded-host-example`（页面级表单入口 demo，**demo(mock)/live(真实后端 dev proxy) 双模式**，live 走 `AGENT_PLATFORM_BASE_URL` + 前缀重写 + SSE 反缓冲）。
- **测试基建**：前端已有 7 个 vitest 测试文件（协议 fixtures/negative、client/projection/sse、react）；后端 pytest 全量（无 L2 env 57+8 / 带 L2 63+2）；静态门 `check-k8s.sh` + CI（backend + helm 两 job）。

### 1.2 当前缺口 → 本方案目标

| 缺口（SDD §13.1 / Phase 4） | 现状 | 本方案 |
| --- | --- | --- |
| 自由对话端点 | 无自然语言发起层；只有结构化 `POST /v1/runs` | 新增 `POST /v1/chat`（MVP 意图解析） |
| AgentLauncher / 浮窗 | 仅页面级表单 demo，无启动器浮窗形态 | `agent-ui-react` 新增 `AgentLauncher` + `useAgentChat` |
| 正式应用层 | 前端不在 CI；无生产部署形态；live 仅 dev proxy | CI `frontend-gate` job + Helm frontend 工作负载 + ingress 暴露 + 联调验收 |

> **术语对齐**：本文档「应用层」= 可演示、可联调真实后端、可独立部署的**前端入口应用**（launcher + 对话 + 面板），与已有的"宿主嵌入组件库"区分。

---

## 2. 目标定义

### 2.1 P0：自由对话端点（后端）

`POST /v1/chat`：用户自然语言 → 意图解析（MVP 规则映射）→ 复用 `control.create_run` 语义 → 返回 `RunViewSnapshot` + `Location`；对话的后续继续性复用现有 followup 链（追问 = 新 Attempt 调度，P0 已有）。

**约束（硬性）：**

- **不新增会话存储**：没有独立 "conversation" 表/session 存根；一个对话 = chat 发起一个 Run + followups 追问；
- **不做流式打字机**：事件流沿用现有 SSE（`/events/stream`）；
- 鉴权沿用 `authenticate_request` + `require_scope(ctx, "runs:create")`；幂等沿用 `Idempotency-Key`（重放同 key 返回同一 run）；
- 意图解析为规则映射 MVP：覆盖演示 workflow（`synthetic-analysis`），未命中默认兜底，预留注入槽位（后续可换 LLM）。

### 2.2 P1：AgentLauncher / 浮窗（前端组件）

`agent-ui-react` 新增 `AgentLauncher`（浮窗按钮 + 展开对话面板：自由输入 → `client.chat()` → 面板内嵌 `AgentPanel` + `FollowupPanel`）；`AgentPlatformClient` 新增 `chat()`；`agent-ui-protocol` 新增 `ChatCommand` 契约；`embedded-host-example` 以 Launcher 为入口重建，demo/live 双模式保留（mock 增 `/v1/chat` 路由）。

### 2.3 P2：正式应用层（部署 + CI + 联调）

- CI 新增 `frontend-gate` job（npm ci → eslint → vitest → typecheck+build），前端正式纳入门禁；
- Helm 新增 `frontend` 工作负载（nginx serve dist + Service + ingress 路径），values/schema/静态门同步；
- 联调验收：真实后端（durable sqlite 兜底）上 chat→run→SSE→surface→followup 全链脚本化验证。

---

## 3. P0 架构：自由对话端点

### 3.1 数据流

```
用户在 Launcher 输入自然语言消息
        │
        ▼
POST /v1/chat  { message, resource_refs?, workflow_hint?, host_context_ref? }
        │   （鉴权 runs:create + Idempotency-Key）
        ▼
control/chat.py  IntentResolver.classify(message) → IntentPlan{workflow_type, intent}
        │
        ▼
复用 control.create_run（CreateRunCommand 组装：workflow_type / intent=message / resource_refs）→ Run 创建（Runner 正常执行）
        │
        ▼
201 RunViewSnapshot + Location: /v1/runs/{run_id}
（后续继续性 = 现有 followups 追问链 + SSE 事件流，本方案不加代码）
```

### 3.2 契约（`contracts/commands.py` 新增）

```python
class ChatCommand(StrictModel):
    message: Annotated[str, Field(min_length=1, max_length=2000)]
    resource_refs: Annotated[tuple[str, ...], Field(min_length=1)] = ("synthetic-case:demo",)  # 对齐 reference resolver 前缀`}, {
    workflow_hint: str | None = None      # 显式指定 workflow_type 的逃生门（默认自动解析）
    host_context_ref: str | None = None
```

响应：复用 `RunViewSnapshot`（`run-view-snapshot/v1`），无新响应 schemas。错误：复用 `ApiErrorEnvelope`（401 无鉴权 / 403 scope / 422 契约束 / 500）。

### 3.3 意图解析（`control/chat.py` 新增）

```python
class IntentPlan(StrictModel):
    workflow_type: str
    intent: str

def classify_intent(message: str, workflow_hint: str | None = None) -> IntentPlan:
    # MVP：关键词规则映射，覆盖演示 workflow
    # 命中表（message 关键词 → workflow_type），如："分析/日志/故障/失败" → synthetic-analysis
    # workflow_hint 非空 → 直接采用（逃生门）
    # 未命中 → workflow_type 默认 synthetic-analysis，intent = message 原文
```

**槽位**：`classify_intent` 保持纯函数 + 单一入口，将来可在其上层注入 LLM 解析器（不改路由与契约）。

### 3.4 鉴权与幂等（复用现有机制，不改）

| 项 | 机制 | 说明 |
| --- | --- | --- |
| 鉴权 | `authenticate_request` + `require_scope(ctx, "runs:create")` | 对话即发起 Run，scope 与 `POST /v1/runs` 一致 |
| 幂等 | `Idempotency-Key`（必填，缺失 422/REQUEST_VALIDATION_FAILED） | 同 key + 同 message → 同一 run（复用 create_run 幂等语义，含 TTL 视界） |
| 参数安全 | `ValidateControlledParameters` 同 `CreateRunCommand`（workflow_hint → workflow_type 也过 `WORKFLOW_PARAMETER_MODELS` 校验，未注册 workflow 不接受 parameters） | chat 不自带 parameters，规避 authority keys 面 |

### 3.5 测试（`tests/test_chat_endpoint.py`，✅ 已实施 9 用例全绿）

| 用例 | 断言 |
| --- | --- |
| 201 + Location | 合法 message → `status_code=201`，`Location=/v1/runs/{run_id}`，body 为 `run-view-snapshot/v1` |
| 鉴权 401 | 无/坏 token → 401 `ApiErrorEnvelope` |
| scope 403 | 有鉴权但无 `runs:create` scope → 403 |
| 契约 422 | 空 message / 缺 `Idempotency-Key` → 422 |
| 意图映射命中 | "分析 日志 故障" → workflow `synthetic-analysis` 且 intent 保留原文 |
| workflow_hint 逃生门 | `workflow_hint=synthetic-analysis` 显式生效 |
| 幂等重放 | 同 key 同 body 两次 → 同一 `run_id` |
| 继续性 | chat 创建后 `GET /followups` 可追问（复用既有链路语义） |

---

## 4. P1 架构：AgentLauncher / 浮窗

### 4.1 协议层（`agent-ui-protocol`）

新增 `ChatCommand`（与后端契约同形：`message`/`resource_refs`/`workflow_hint`）+ 导出（`protocol/src/index.ts` & `host.ts` 不受影响）。

### 4.2 客户端层（`agent-ui-client`）

```ts
// client.ts 新增
chat(command: ChatCommand, { idempotencyKey }: { idempotencyKey: string }): Promise<RunViewSnapshot>
```
实现：`POST /v1/chat`（baseUrl 拼接 /api/agent-platform/），复用现有鉴权/错误解码/幂等 key 工具（`createIdempotencyKey`）。

### 4.3 React 组件层（`agent-ui-react`）

| 新增 | 职责 |
| --- | --- |
| `AgentLauncher` | 浮窗按钮 + 展开对话面板：输入框 → `useAgentChat().submit()` → 挂载 `AgentPanel`（run_id）+ `FollowupPanel`；关闭/重开清态；浮窗定位/聚焦管理（遵循 `DESIGN.md`：无 dangerouslySetInnerHTML、无动态导入、组件受 catalog 校验） |
| `useAgentChat` | 状态机：`idle → submitting → active(run_id) → error`；调用 `client.chat()`；错误态恢复 |
| 面板集成 | 对话区 + 进度区（AgentPanel）+ 追问区（FollowupPanel）三段布局，窄容器单列（DESIGN.md） |

### 4.4 Demo Mock（`embedded-host-example/src/mock-api.ts`）

`createMockFetch` 增加 `POST v1/chat` 分支：解析 message → 复用现有 run 创建/时间线（run.created → … → SUCCEEDED），返回与后端同形的 `RunViewSnapshot`；`App.tsx` 以 Launcher 为入口重建（demo 模式默认，live 模式连真实后端）。

### 4.5 测试（vitest，追加）

- `agent-ui-client`：`chat()` 请求形状/错误解码/幂等 key 透传；
- `agent-ui-react`：Launcher 渲染/发起/error 恢复/面板切换/关闭清态；
- 现有 7 个测试文件保持全绿。

---

## 5. P2 架构：正式应用层

### 5.1 CI（`.github/workflows/ci.yml` 新增 `frontend-gate`）

```yaml
steps: checkout → setup-node(v22) + npm ci → npm run lint → npm run test → npm run typecheck → npm run build
```
工作目录 `frontend/`（对齐 backend job 的 working-directory 风格）。

### 5.2 Helm（`deploy/helm/`）

| 新增 | 内容 |
| --- | --- |
| `values` | `frontend.enabled`（默认 false）/ `image.repository`+`digest`（与 controlPlane 同为 digest 钉死）/ `replicas`(1) |
| 模板 | `frontend-deployment.yaml`（nginx 静态 + 空 `/tmp` 可写、readOnlyRootFilesystem 风格沿用）、`frontend-service.yaml` |
| ingress | 与 API 同 host 下 `path: /`（SPA）或独立 host；注解 `proxy-buffering: off`（SSE）文档化 |
| schema | `frontend` section 入 `values.schema.json`（`additionalProperties:false`） |
| 门 | `check-k8s.sh` 第四渲染 profile（frontend enabled）全绿；`deploy/helm/README.md` 补 values 表 |

> 生产镜像：构建产物由 CI/CD 注入 digest（同 controlPlane 流程）；本地联调可先用 `vite preview`（dev 模式）验证。

### 5.3 联调验收（`scripts/verify-frontend-live.sh`，新增）

1. 起真实后端：`run.py`（无 env → durable sqlite 兜底）或 L2 compose PG；
2. `npm run build` → 静态预览（或 vite dev proxy，SSE 反缓冲注解）；
3. 脚本断言：chat → 201+Location → run 可读 → SSE 事件流可达 → surface 可读 → followup 回答落库。

---

## 6. 状态管理与存储

| 场景 | 行为 |
| --- | --- |
| 对话发起 | Launcher 内输入 → chat() → run_id 进入 active 态；对话文本不额外持久化（intent 已在 Run 里） |
| Run 进行/完成 | AgentPanel 投影（SSE 同步器）+ FollowupPanel 追问历史（`GET /followups`） |
| 关闭/重开 Launcher | 清 `useAgentChat` 状态；已创建 Run 仍可经面板恢复（run_id 留存） |
| 切页面 / 新对话 | 新 chat = 新 Run = 新面板；旧 Run 追问链数据不受影响（平台持久化） |

**存储**：本方案**不新增任何存储**。对话事实 = Run（intent/事件）+ followup 记录（已有表）；前端只持 ephemeral 状态（React state + 已有 SSE projection）。

---

## 7. 安全与权限

| 层级 | 权限 | 禁止行为 |
| --- | --- | --- |
| `POST /v1/chat` | `runs:create` | 禁止不经 `CreateRunCommand` 校验注入参数/authority keys；`workflow_hint` 未注册 workflow 不接受 parameters |
| Launcher 组件 | 仅渲染 catalog 允许组件 | 无 dangerouslySetInnerHTML、无任意组件动态导入、禁止浏览器直连 Sandbox/S3（下载走 `download-authorization` 授权 URL） |

---

## 8. 实施路线图

> 每阶段独立可验证；后端门：pytest 全绿 + ruff；前端门：lint/test/build；静态门：`check-k8s.sh` 全绿。

### Phase F-A：自由对话端点（后端）✅ 已完成（2026-08-23）

- 文件：`contracts/commands.py`（`ChatCommand` + 拒空白 message validator）、`control/chat.py`（`classify_intent`/`IntentPlan`，规则映射 MVP + `workflow_hint` 逃生门）、`fastapi/router.py`（`POST /v1/chat`）、`tests/test_chat_endpoint.py`（9 用例）
- 契约同步：`scripts/generate-contracts.py` 重新生成（openapi.json 含 `/v1/chat` + `ChatCommand`），`check-generated.sh` parity 通过
- 验收达成：9/9 用例过（201+Location / 401 / 422 缺 key / 422 空与空白 message / 意图映射命中与保留 intent / fallback 默认 workflow / hint 逃生门 / 幂等重放同 run_id / 追问继续性）；ruff 全绿（chat.py/commands.py/test 干净 + router isort 干净）；全量 pytest **66 passed + 8 skipped**（+9）。
- **设计修正（实施中发现）**：空白 message（`"   "`）初版会透传为 `intent=""` 并成功创建 —— 已在 `ChatCommand` 加 `model_validator` 拒绝；resource_refs 默认值对齐 reference resolver 前缀（`synthetic-case:demo`）。

### Phase F-B：SDK + Launcher 组件 ✅ 已完成（2026-08-23）

- 交付：`agent-ui-protocol/src/index.ts`（`ChatCommand` Zod schema + 空白/超长拒绝 + `resource_refs` 默认 `["synthetic-case:demo"]`）、`agent-ui-client/src/client.ts`（`chat()`：POST /v1/chat + 幂等 key + 默认生成/显式注入 + 响应严格解析）、`agent-ui-react/src/use-chat.ts`（`useAgentChat`：乐观 entry + 每消息幂等 key + `onRunCreated` 回调 + 防重入/卸载清理）、`agent-ui-react/src/launcher.tsx`（`AgentLauncher`：右下角浮窗 pill ↔ 折叠面板 + 消息列表 + 输入框 + 发送，EAP_THEME 样式，无新 UI 框架）、`index.tsx` 导出 `useAgentChat`/`AgentLauncher`/类型。
- 测试：`negative.test.ts` +6（默认 refs/显式 hint/空白拒绝/超长/空 refs/extra-forbid）、`client.test.ts` +5（201 解析 + header + body 物化默认 refs/显式 key/空白拒发/超长拒发/422 映射）、`react.test.tsx` +2（展开→发送→/v1/chat→onRunCreated→徽章；空白不发）；**全量 103 passed (was 90)**。
- 门禁：vitest 103/103、eslint 0 错误、`npm run typecheck` + `npm run build` 全 workspace 过（修复两个 `exactOptionalPropertyTypes` 类型问题：`onRunCreated?: ((id)=>void)|undefined`、send 条件展开可选键）。
- 技术细节：`use-chat.ts` 经 `./index.js` 取 `useAgentPlatform`（与 followup-panel 同款循环引用模式，运行时解析安全）；zod `.default()` 物化 `resource_refs` 但省略 `workflow_hint`/`host_context_ref`（undefined 不序列化）。

- 文件：`agent-ui-protocol`（ChatCommand + 导出）、`agent-ui-client`（`chat()` + fixture 测试）、`agent-ui-react`（`AgentLauncher`/`useAgentChat` + vitest）
- 验收：新增单测全过；现有 7 文件全绿；`npm run build` 过。

### Phase F-C：示例重建 + mock 同步 ✅ 已完成（2026-08-23）

- 交付：`embedded-host-example/src/App.tsx` 以 AgentLauncher 为入口重建（手动创建表单收进 `Advanced: create run manually` 折叠区，保留 demo/live 切换 + live token 输入；空态提示引导浮窗发起）；`mock-api.ts` 增 `POST /v1/chat` 分支（空白 422 + 201 snapshot + **Location header** 契约对齐真实后端；workflow_hint/兜底 synthetic-analysis）；`frontend/DESIGN.md` 补 AgentLauncher 设计约束（宿主级浮窗、不经 server catalog、样式/幂等规则）。
- 测试：`embedded-host-example/src/mock-api.test.ts` +4（创建保留 intent + Location / workflow_hint / 空白 422 / 创建后 Run 全流程：snapshot→action→followup answer）——demo 模式闭环脚本化；前端全量 **107 passed (was 103)**。
- 联调：新增 `scripts/verify-frontend-live.sh`（无 docker 静态门）——起 uvicorn reference local stack（或探测已在线后端）+ vite dev，验证：直连 `/v1/chat` 201+snapshot / 幂等重放同 run / 空白 422 / vite dev proxy 201（SSE 反缓冲路径）；本地实测 **4/4 全绿**。联调注意：Windows 控制台 curl 传中文参数会转码失败（400），脚本消息统一 ASCII（中文意图覆盖在 pytest/前端单测）。
- 脚本运行时产物（`.verify-live-*.log/.json`）不入库。

### Phase F-D：正式应用层（CI + Helm + 联调）

- 文件：`.github/workflows/ci.yml`（`frontend-gate`）、`deploy/helm/`（values/schema/templates/README）、`scripts/check-k8s.sh`（+profile）、`scripts/verify-frontend-live.sh`、`deploy/DOCS.md`
- 验收：CI frontend job 绿；`check-k8s.sh` 四 profile 绿；live 联调脚本断言全过。

### Phase F-E：收尾

- 文件：`SDD.md`（§13.1 缺口关闭 + Phase 3.6 条目 + 总结段）、`MEMORY.md`（轮次记录）、`docs/DOCS.md`（索引本文档）
- 验收：文档一致；三扇门全绿。

---

## 9. 关键决策

| 问题 | 决策 |
| --- | --- |
| 自由对话是否新建会话存储？ | **否**。对话 = Run + followup 链（MVP 复用现有一切），避免为演示新增存储面 |
| 意图解析深度？ | **规则映射 MVP**（覆盖 synthetic-analysis，未命中默认兜底 + workflow_hint 逃生门）；LLM 解析留注入槽位 |
| 前端如何部署？ | 独立 frontend 工作负载（nginx 静态 + ingress），digest 钉死与 controlPlane 同流程；本地用 vite preview/live proxy |
| 前端纳入 CI？ | **是**（frontend-gate job），与 backend/helm 并列，杜绝"白写的测试" |
| 是否引入浏览器 e2e（Playwright）？ | **否**（明确范围外）：契约级 pytest + 组件 vitest + shell 联调脚本覆盖，避免依赖膨胀 |
| 与 followup 模式的关系？ | Launcher 追问即现有 FollowupPanel（真实后端 followup = 新 Attempt 调度），本方案不加后端代码 |

---

## 10. 一句话总结

> **把「前端入口」从远期转必做：后端加一个自由对话端点（规则意图解析 + 复用 create_run 语义，不新增存储），前端加 AgentLauncher/浮窗（复用现有四包与 SDK），示例以浮窗为入口重建，最后把前端纳入 CI、Helm 独立部署并脚本化联调验收——做成可演示、可部署、可回归的正式应用层。**