# 项目上下文：企业 Agent 平台前端开发者手册

> **阅读对象：** 负责前端模块开发（Phase 3 追问面板 / Phase 5 双路由 UI）的 agent
> **目标：** 让新 agent 在 5 分钟内理解项目全貌、所处阶段、架构约束和剩余任务
> **关联文档：** `architecture.md`（总体架构）、`implementation.md`（实现证据）、`sdd-followup-mode.md`（后端追问协议）、`sdd-frontend-dual-mode.md`（前端追问 UI SDD）、`embedding-guide.md`（宿主集成）、`DESIGN.md`（前端设计令牌）

---

## 1. 项目一句话

> 这是一套**企业级、多租户、可审批、可恢复、可审计的 Agent 执行平台**。一个 Run = 一个模型 provider session，任务在 session 里完成，用户在同一个 session 里追问，平台只负责治理。

### 1.1 核心价值

| 能力 | 说明 |
|------|------|
| **任务型 Agent** | 长时间、多步骤、可中断的 Agent 工作流 |
| **审批护栏** | WRITE 操作必须审批，审批通过才执行 Effect |
| **可恢复** | Checkpoint → Lease → Attempt 失败后可恢复 |
| **多租户** | tenant_id 隔离所有数据 |
| **前端 SDK** | React 组件 + SSE 投影同步，宿主可嵌入 |

### 1.2 三个不要搞混的东西

| 概念 | 职责 | 不要理解为 |
|------|------|-----------|
| **RunSessionProvider** | 模型 provider 的 session（记忆） | 不要把它当成平台 Checkpoint |
| **Checkpoint** | 治理账本（恢复、审批、审计） | 不要把它当成模型的记忆 |
| **Surface** | A2UI 声明式 UI 文档（{component, props}） | 不要把它当成自由 HTML/JSX |

---

## 2. 目录结构（前端 agent 需要关心的）

```
reconstructed/
├── docs/                                    ← 所有文档
│   ├── architecture.md                      ← 总体架构（必读）
│   ├── implementation.md                    ← 实现证据（必读）
│   ├── sdd-followup-mode.md                 ← 后端追问协议
│   ├── sdd-frontend-dual-mode.md            ← ★ 你的主参考：前端追问 UI SDD
│   ├── project-context.md                   ← 本文件
│   ├── security.md                          ← 安全
│   ├── embedding-guide.md                   ← 宿主集成
│   └── …其他文档…
│
├── backend/                                 ← Python 后端（你不需要改，但要了解 API）
│   ├── src/enterprise_agent_platform/
│   │   ├── fastapi/router.py                ← Public API 路由
│   │   ├── control/followup.py              ← 追问服务（Phase 1 已完成）
│   │   ├── execution/session.py             ← RunSessionProvider 协议
│   │   └── reference/deepseek_provider.py   ← DeepSeek v4 Flash 实现
│   ├── config.toml                          ← 模型提供商配置（已配好）
│   └── tests/
│
├── frontend/                                ← ★ 你的工作区域
│   ├── package.json                         ← npm workspaces 根
│   ├── DESIGN.md                            ← 设计令牌（color/font/spacing）
│   ├── packages/
│   │   ├── agent-ui-protocol/               ← ★ Zod schema（需新增追问类型）
│   │   │   └── src/index.ts
│   │   ├── agent-ui-client/                 ← ★ API 客户端（需新增追问方法）
│   │   │   ├── src/client.ts
│   │   │   └── tests/
│   │   ├── agent-ui-catalog/                ← Surface 组件渲染器（已有 4 个组件）
│   │   │   └── src/index.tsx
│   │   ├── agent-ui-react/                  ← ★ React 组件（需新增追问面板）
│   │   │   └── src/index.tsx                ← 已有 AgentPanel/Provider
│   │   └── tests/
│   └── examples/
│       └── embedded-host-example/           ← ★ Demo 示例（需更新 mock）
│           ├── src/App.tsx
│           └── src/mock-api.ts
│
├── contracts/                               ← JSON Schema / OpenAPI / golden fixtures
├── deploy/                                  ← Docker / Helm / Kind / Compose
└── scripts/                                 ← generate-contracts / verify.sh
```

### 2.1 npm Workspaces 依赖链

```
agent-ui-protocol (Zod schemas)  ← 底层，无运行时依赖
       ↑
agent-ui-client (API 客户端)      ← 依赖 protocol
       ↑
agent-ui-catalog (Surface 渲染)   ← 依赖 protocol
       ↑
agent-ui-react (React 组件)       ← 依赖 client + catalog + protocol
       ↑
embedded-host-example (Demo)     ← 依赖 react
```

---

## 3. 当前状态

### 3.1 已完成（✅）

| 层 | 模块 | 状态 |
|----|------|------|
| **后端协议** | `RunSessionProvider` Protocol（open/run_task/followup/close） | ✅ |
| **后端追问** | `POST /v1/runs/{run_id}/followups` 路由 + `FollowupService` | ✅ |
| **后端模型** | `DeepSeekModelSessionProvider`（真实 DeepSeek v4 Flash） | ✅ |
| **后端回退** | `ReferenceModelSessionProvider`（无 API key 时 demo 模式） | ✅ |
| **后端配置** | `config.toml`（一个文件切换所有提供商/参数） | ✅ |
| **后端测试** | `test_deepseek_provider.py`（单元+集成） | ✅ |
| **前端协议** | Zod schema：RunState、RunView、Event、Surface、ActionCommand、Artifact | ✅ |
| **前端客户端** | `AgentPlatformClient`：createRun/getRun/streamEvents/submitAction/… | ✅ |
| **前端投影** | `RunProjectionStore` + `RunProjectionSynchronizer`（SSE 同步） | ✅ |
| **前端 Catalog** | ProgressCard / EvidenceSummary / ApprovalCard / ArtifactCard | ✅ |
| **前端 React** | `AgentPlatformProvider` + `useRunProjection` + `AgentPanel` | ✅ |
| **前端 Demo** | embedded-host-example（create-run → 全 mock SSE 流程） | ✅ |

### 3.2 待完成（📋）

| 优先级 | 层 | 模块 | 状态 |
|--------|----|------|------|
| **P0** | **前端协议** | `FollowupCommand` / `FollowupAnswer` / `FollowupRecord` / `FollowupHistoryPage` Zod schema | ❌ 待实现 |
| **P0** | **前端客户端** | `client.submitFollowup()` / `client.listFollowups()` | ❌ 待实现 |
| **P0** | **前端 React** | `FollowupPanel` 组件 + `useFollowupHistory` hook | ❌ 待实现 |
| **P0** | **前端集成** | `AgentPanel` 底部嵌入 `FollowupPanel` | ❌ 待实现 |
| **P0** | **前端 Demo** | mock-api.ts 增加追问路由 | ❌ 待实现 |
| **P0** | **前端测试** | 追问面板/客户端/协议单元测试 | ❌ 待实现 |
| **Future** | **前端双路由** | `intent-classifier.ts` / `NewTaskDraftCard` / 双路由 UI | ❌ 待实现 |

### 3.3 关键 API 端点（前端需要调用的）

```text
POST   /v1/runs                              → RunViewSnapshot        ← 创建 Run（已有）
GET    /v1/runs/{run_id}                      → RunViewSnapshot        ← 查询 Run（已有）
GET    /v1/runs/{run_id}/events               → RunEventPage           ← 事件分页（已有）
GET    /v1/runs/{run_id}/events/stream        → SSE 事件流             ← 实时同步（已有）
POST   /v1/runs/{run_id}/actions              → RunViewSnapshot        ← UI 动作（已有）
POST   /v1/runs/{run_id}/followups            → FollowupAnswer         ← ★ 追问（需前端调用）
GET    /v1/runs/{run_id}/followups            → FollowupHistoryPage    ← ★ 追问历史（需前端调用）
```

---

## 4. 架构约束（前端 agent 必须遵守）

### 4.1 追问护栏

| 规则 | 原因 |
|------|------|
| 追问必须 `read_only: true` | 后端 404 拒绝非只读追问 |
| 追问不进入事件总线 | 追问不产生 RunEvent、不改变 watermark |
| 追问记录不持久化在平台事实库 | 前端 React state 管理，切换 Run 清空 |
| 追问幂等靠 `client_followup_id` | 防重复提交 |

### 4.2 Surface 安全约束

| 规则 | 原因 |
|------|------|
| 不能动态加载任意组件名 | 只渲染 catalog allowlist 中的组件 |
| 不能 `dangerouslySetInnerHTML` | XSS 防护 |
| 不能直连 Sandbox Pod | 所有数据通过 API 代理 |
| Action 请求须携带 `displayed_digest` | 防中间人篡改 |

### 4.3 数据流隔离

```
RunProjection (SSE 实时同步)          FollowupHistory (REST 调用)
─────────────────────────             ─────────────────────────
- 驱动 Run 状态、Surface 更新          - 独立于事件流
- 持续整个 Run 生命周期                - 仅在追问面板展开时使用
- 影响 AgentPanel 渲染                 - 影响 FollowupPanel 渲染
- 有 watermark / resync 机制           - 无 watermark，纯 CRUD
```

### 4.4 设计令牌（来自 DESIGN.md）

```
Color mode: light（宿主可 CSS 覆盖）
Primary:    #2563eb
Bg:         #ffffff / #f8fafc
Text:       #0f172a
Secondary:  #475569
Border:     #cbd5e1
Font:       继承宿主 font-family, base 14px
Radius:     8px
Spacing:    4px unit
```

---

## 5. 数据模型速览（前端 schema）

### 5.1 Run 状态机（前端关心的状态）

```
QUEUED → RUNNING → WAITING_APPROVAL → SUCCEEDED
                ↘ FAILED / CANCELLED
```

追问面板只在 `SUCCEEDED / FAILED / CANCELLED`（即 `runEnded`）时展开。

### 5.2 追问协议（待前端实现）

```typescript
// POST 请求体
interface FollowupCommand {
  question: string;           // 用户输入
  client_followup_id: string; // 幂等键
  read_only: true;            // 必须为 true
}

// POST 响应
interface FollowupAnswer {
  schema_version: "followup-answer/v1";
  run_id: string;
  client_followup_id: string;
  answer: string;
  answered_at: string;        // ISO-8601
}

// GET 响应（追问历史）
interface FollowupHistoryPage {
  schema_version: "followup-history-page/v1";
  run_id: string;
  total_count: number;
  records: FollowupRecord[];
}

// 历史记录条目
interface FollowupRecord {
  schema_version: "followup-record/v1";
  run_id: string;
  followup_seq: number;       // 从 0 递增
  question: string;
  answer: string;
  answered_at: string;
  client_followup_id: string;
}
```

### 5.3 未来双路由协议

```typescript
interface NewTaskDraft {
  schema_version: "new-task-draft/v1";
  run_id: string;             // 当前 Run ID（来源 session）
  task_type: string;          // 如 "config_update"
  params: Record<string, JsonValue>;
  summary: string;            // 面向用户的描述
}

// 追问响应可以是 answer 或 draft
type FollowupResponse = FollowupAnswer | NewTaskDraft;
```

---

## 6. 现有 React 组件结构（需要修改/扩展）

### 6.1 AgentPanel（已有）

```
AgentPanel(runId)
├── header: Run intent + 状态 badge
├── progress: ExecutionUnit + Attempt 状态列表
├── surfaces: Surface[] → 通过 catalog 渲染
│   ├── ProgressCard
│   ├── EvidenceSummary
│   ├── ApprovalCard
│   └── ArtifactCard
└── [待追加] FollowupPanel ◄── Phase 3
```

### 6.2 useRunProjection Hook（已有）

```typescript
function useRunProjection(runId: string): RunProjectionSnapshot {
  // SSE 同步：snapshot → stream → replay → resync
  // 返回 { run, status, watermark, surfaces }
}
```

### 6.3 集成点（最小改动）

在 `AgentPanel` 底部追加 `FollowupPanel`：

```tsx
const runEnded = run.status === "SUCCEEDED" || run.status === "FAILED" || run.status === "CANCELLED";

return (
  <section data-agent-panel={runId}>
    <Header /><Progress /><Surfaces />
    <FollowupPanel
      runId={runId}
      runEnded={runEnded}
      effectSummary={firstSurfaceTitle}
    />
  </section>
);
```

---

## 7. 核心决策记录（为什么这么设计）

| 决策 | 原因 | 替代方案（被否决） |
|------|------|---------------------|
| **追问不走事件总线** | 追问不产生业务事实，不进 watermark/outbox | 走事件总线 → 污染 watermark，增加复杂度 |
| **追问历史用 React state** | 追问只在当前 Run 页面展示，切 Run 清空 | Redis 缓存 → 过度设计，Phase 3 不需要 |
| **Session 是模型的记忆** | 平台不设计记忆系统，模型原生 session 最可靠 | 平台建记忆 → 与模型 session 重复，双倍复杂度 |
| **前端轻量分类器（Future）** | 无网络延迟，实时显示图标/提示 | 纯后端分类 → 前端输入时无法实时反馈 |
| **追问面板是独立组件** | 宿主可选择不展示、自定义位置 | 内置在 AgentPanel → 不可组合 |

---

## 8. 实施步骤（按顺序执行）

### Step 1：协议层（agent-ui-protocol）

1. 在 `src/index.ts` 新增 4 个 Zod schema：`FollowupCommand`、`FollowupAnswer`、`FollowupRecord`、`FollowupHistoryPage`
2. 运行 `npm run build -w @platform/agent-ui-protocol` 验证
3. 运行 `npm run test -w @platform/agent-ui-protocol` 确认不破坏已有测试

### Step 2：客户端层（agent-ui-client）

1. 在 `src/client.ts` 新增 2 个方法：`submitFollowup(runId, question)`、`listFollowups(runId)`
2. 创建 `tests/followup.test.ts` 单元测试（mock fetch）
3. 运行 `npm run test -w @platform/agent-ui-client` 验证

### Step 3：React 组件层（agent-ui-react）

1. 创建 `src/use-followup-history.ts` hook
2. 创建 `src/followup-panel.tsx` 组件
3. 修改 `src/index.tsx`：在 `AgentPanel` 底部嵌入 `FollowupPanel`
4. 创建 `tests/followup-panel.test.tsx` 测试
5. 创建 `tests/use-followup-history.test.ts` 测试
6. 运行 `npm run test -w @platform/agent-ui-react` 验证

### Step 4：Demo Mock 更新

1. 修改 `examples/embedded-host-example/src/mock-api.ts`：新增 `POST .../followups` 和 `GET .../followups` 路由
2. 手动运行 `npm run dev -w @platform/embedded-host-example` 验证追问面板交互

### Step 5（Future）：双路由 UI

1. 创建 `src/intent-classifier.ts` 本地分类器
2. 创建 `src/new-task-draft-card.tsx` 确认卡片
3. 修改 `src/followup-panel.tsx` 集成双路由预览
4. 编写测试

---

## 9. 常见前端 agent 陷阱

| 陷阱 | 正确做法 |
|------|---------|
| 把追问历史存到 localStorage/IndexedDB | ❌ 追问只在当前页面有效，React state 足够 |
| 把追问面板做成全局路由外的独立页面 | ❌ 追问面板是 `AgentPanel` 的内嵌子组件 |
| 在追问输入框内支持 Markdown/富文本 | ❌ 纯文本输入，模型回答可在 Surface 中渲染 |
| 追问时显示 loading spinner 但无 error 处理 | ✅ 必须处理网络错误，显示重试按钮 |
| 追问面板在新 Run 创建后自动展开 | ❌ 面板只在 Run 结束后（SUCCEEDED/FAILED/CANCELLED）展开 |
| 忘记幂等键 | ✅ 每个 `submitFollowup` 必须有 `client_followup_id` |
| 把追问响应当成可执行代码 | ❌ 追问 answer 是纯文本，由 Surface catalog 渲染 |

---

## 10. 验证方法

```bash
# L1 前端门禁（每次提交必须通过）
cd reconstructed/frontend

npm run lint                 # eslint
npm run typecheck            # tsc --noEmit（所有 workspace）
npm run test                 # vitest（所有 workspace）
npm run build                # 所有 workspace 构建

# 手动验证（嵌入式示例）
cd examples/embedded-host-example
npm run dev                  # 打开浏览器，测试追问面板交互
```

---

## 11. 文档索引

| 需要了解什么 | 读哪个文档 |
|-------------|-----------|
| 项目整体架构 | `architecture.md` §1-3 |
| 现有代码实现状态 | `implementation.md` §10, §16 |
| 后端追问 API 设计 | `sdd-followup-mode.md` §3 |
| **前端追问面板具体实现** | **`sdd-frontend-dual-mode.md` §2-4** |
| 前端双路由 UI 设计 | `sdd-frontend-dual-mode.md` §5 |
| 前端设计令牌/颜色/字体 | `DESIGN.md` |
| 前端协议 Zod schema | `agent-ui-protocol/src/index.ts` |
| 宿主集成安全约束 | `embedding-guide.md` §5, §6 |
| 现有 demo mock 实现 | `embedded-host-example/src/mock-api.ts` |
| 现有 React 组件实现 | `agent-ui-react/src/index.tsx` |
| 后端配置/API Key | `backend/config.toml` |
| 测试运行 | `implementation.md` §15 |

---

> **一句话送给前端 agent：** 在 `AgentPanel` 底部加一个 `FollowupPanel`，调 `POST /v1/runs/{run_id}/followups` 发追问，用 React state 管历史，切换 Run 清空。双路由是 Future，先只做纯解释型追问。所有代码路径和验收标准在 `sdd-frontend-dual-mode.md` 里。