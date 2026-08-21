# SDD：前端双路游模式 UI — 对话面板与追问体验

> 状态：Draft v1 — 供前端开发 agent 使用
> 关联：`sdd-followup-mode.md`（后端追问协议与 session 架构）、`DESIGN.md`（前端设计令牌）
> 本 SDD 覆盖 **Phase 3（前端对话面板）** 和 **Phase 5 Future（双路由分类器前端视图）** 的完整 UI 需求。
> 阅读顺序：§1 架构总览 → §2 协议层 → §3 客户端 → §4 React 组件 → §5 双路由 → §6 状态与存储 → §7 验收标准

---

## 1. 架构总览

### 1.1 三层架构

```
┌─────────────────────────────────────────────────────────────┐
│  agent-ui-react (React 组件层)                               │
│  ┌─────────────────┐  ┌──────────────────┐  ┌────────────┐ │
│  │ AgentPanel       │  │ FollowupPanel    │  │ DualRoute  │ │
│  │ (Run 状态+Surface)│  │ (追问历史+输入框) │  │ 分类器 UI  │ │
│  └────────┬────────┘  └────────┬─────────┘  └─────┬──────┘ │
│           │                    │                    │        │
├───────────┼────────────────────┼────────────────────┼────────┤
│  agent-ui-client (API 客户端层)                         │
│  ┌────────┴────────┐  ┌───────┴────────┐  ┌────────┴────┐ │
│  │ RunProjection    │  │ FollowupClient │  │ Classifier  │ │
│  │ (SSE 同步)       │  │ (追问 REST)    │  │ (Future)    │ │
│  └─────────────────┘  └────────────────┘  └─────────────┘ │
│                                                            │
├────────────────────────────────────────────────────────────┤
│  agent-ui-protocol (Zod 协议层)                             │
│  ┌─────────────┐  ┌──────────────┐  ┌──────────────────┐  │
│  │ Run 类型     │  │ Followup 类型│  │ DualRoute 类型    │  │
│  │ (已有)       │  │ (本 SDD 新增)│  │ (Future 新增)     │  │
│  └─────────────┘  └──────────────┘  └──────────────────┘  │
└────────────────────────────────────────────────────────────┘
                              │
                              ▼
                      Backend Public API
                      POST /v1/runs/{run_id}/followups
                      GET  /v1/runs/{run_id}/followups
```

### 1.2 职责边界

| 层 | 职责 | 不负责 |
|---|------|--------|
| **agent-ui-protocol** | 定义 wire 类型 + Zod validator；`FollowupCommand`、`FollowupAnswer`、`FollowupHistoryPage` | 业务逻辑、状态管理 |
| **agent-ui-client** | `submitFollowup()`、`listFollowups()`、跟随投影同步的追问缓存 | UI 渲染、响应式订阅 |
| **agent-ui-react** | `FollowupPanel` 组件 + `useFollowupHistory()` hook + `AgentPanel` 集成 | API 调用、wire 序列化 |
| **agent-ui-catalog** | 新增 `ActiveFollowupInput` 组件（输入框的 Surface Document 形态，可选） | 追问历史管理 |

### 1.3 渲染流程

```
用户输入追问文本
        │
        ▼
FollowupPanel 组件
  1. 追加问题到本地历史（optimistic UI）
  2. 调用 client.submitFollowup(runId, question)
        │
        ▼
POST /v1/runs/{run_id}/followups
  { question: "为什么你选择这个方案？", read_only: true }
        │
        ▼
后端服务 (control/followup.py)
  → 找到 Run 的 provider session
  → 以 read_only=True 追加消息
  → 模型基于 session 记忆回答
        │
        ▼
返回 { answer: "因为…", answered_at: "..." }
        │
        ▼
FollowupPanel 更新本地历史
  3. 存储 answer
  4. 滚动到底部
```

---

## 2. 协议层（agent-ui-protocol）新增类型

文件：`reconstructed/frontend/packages/agent-ui-protocol/src/index.ts`

### 2.1 FollowupCommand

```typescript
/** POST /v1/runs/{run_id}/followups 请求体 */
export const FollowupCommand = z
  .object({
    /** 用户追问文本 */
    question: z.string().min(1).max(10_000),
    /** 由前端生成的幂等键（去重） */
    client_followup_id: z.string(),
    /** 必须为 true — 后端兜底只读护栏 */
    read_only: z.literal(true),
  })
  .strict();
export type FollowupCommand = z.infer<typeof FollowupCommand>;
```

### 2.2 FollowupAnswer

```typescript
/** POST /v1/runs/{run_id}/followups 响应体 */
export const FollowupAnswer = z
  .object({
    schema_version: z.literal("followup-answer/v1"),
    run_id: z.string(),
    client_followup_id: z.string(),
    answer: z.string(),
    answered_at: IsoDateTime,
  })
  .strict();
export type FollowupAnswer = z.infer<typeof FollowupAnswer>;
```

### 2.3 FollowupRecord（历史记录条目）

```typescript
export const FollowupRecord = z
  .object({
    schema_version: z.literal("followup-record/v1"),
    run_id: z.string(),
    followup_seq: NonNegativeInt,   // 该 Run 内追问序号，从 0 开始
    question: z.string(),
    answer: z.string(),
    answered_at: IsoDateTime,
    client_followup_id: z.string(),
  })
  .strict();
export type FollowupRecord = z.infer<typeof FollowupRecord>;
```

### 2.4 FollowupHistoryPage

```typescript
/** GET /v1/runs/{run_id}/followups 响应 — 分页追问历史 */
export const FollowupHistoryPage = z
  .object({
    schema_version: z.literal("followup-history-page/v1"),
    run_id: z.string(),
    total_count: NonNegativeInt,
    records: z.array(FollowupRecord),
  })
  .strict();
export type FollowupHistoryPage = z.infer<typeof FollowupHistoryPage>;
```

### 2.5 协议总导出

```typescript
__all__ = [
  // 已有类型...
  "FollowupCommand",
  "FollowupAnswer",
  "FollowupRecord",
  "FollowupHistoryPage",
];
```

---

## 3. 客户端层（agent-ui-client）新增方法

文件：`reconstructed/frontend/packages/agent-ui-client/src/client.ts`

### 3.1 新增接口方法

在 `AgentPlatformClient` 类中增加：

```typescript
/** POST /v1/runs/{run_id}/followups — 发送追问 */
async submitFollowup(
  runId: string,
  question: string,
  options: IdempotentRequestOptions = {},
): Promise<FollowupAnswer> {
  const clientFollowupId =
    options.idempotencyKey ?? createIdempotencyKey("followup");
  const command: FollowupCommand = {
    question,
    client_followup_id: clientFollowupId,
    read_only: true,
  };
  return this.request<FollowupAnswer>("POST", `/v1/runs/${encode(runId)}/followups`, {
    signal: options.signal,
    headers: { "Idempotency-Key": clientFollowupId },
    body: command,
    parse: (value) => {
      const result = FollowupAnswer.safeParse(value);
      if (!result.success) {
        throw new AgentPlatformProtocolError(
          "followup response was not followup-answer/v1",
        );
      }
      return result.data;
    },
  });
}

/** GET /v1/runs/{run_id}/followups — 获取该 Run 的追问历史 */
async listFollowups(
  runId: string,
  options: RequestOptions = {},
): Promise<FollowupHistoryPage> {
  return this.request<FollowupHistoryPage>(
    "GET",
    `/v1/runs/${encode(runId)}/followups`,
    {
      signal: options.signal,
      parse: (value) => {
        const result = FollowupHistoryPage.safeParse(value);
        if (!result.success) {
          throw new AgentPlatformProtocolError(
            "followup history was not followup-history-page/v1",
          );
        }
        return result.data;
      },
    },
  );
}
```

### 3.2 客户端导出更新

```typescript
// 已有导出...
export { submitFollowup, listFollowups } from "./client.js";
```

---

## 4. React 组件层（agent-ui-react）

### 4.1 FollowupPanel 组件

文件：`reconstructed/frontend/packages/agent-ui-react/src/followup-panel.tsx`

```typescript
interface FollowupPanelProps {
  runId: string;
  /** Run 已结束（SUCCEEDED/FAILED/CANCELLED），此时可以展开追问面板 */
  runEnded: boolean;
  /** Effect 结果摘要文本（可选）— 由后端 RunView 中的 surfaces 提炼 */
  effectSummary?: string;
}

interface FollowupEntry {
  followupSeq: number;
  question: string;
  answer: string | null;  // null = 等待中
  answeredAt: string | null;
  clientFollowupId: string;
  status: "sending" | "done" | "error";
}
```

**组件状态：**

```
FollowupPanel
├── 状态
│   ├── history: FollowupEntry[]     ← 追问历史（本地 + 服务端）
│   ├── inputValue: string           ← 输入框当前值
│   ├── sending: boolean             ← 是否正在发送
│   └── error: string | null         ← 最近的错误消息
├── 属性
│   ├── runId: string                ← 绑定 Run
│   ├── runEnded: boolean            ← 是否可展开
│   └── effectSummary?: string       ← 结果摘要
└── 方法
    ├── sendFollowup()               ← 发送追问
    ├── loadHistory()                ← 加载历史
    └── clearHistory()               ← 切换 Run 时清空
```

**渲染规格：**

```
┌─────────────────────────────────────────┐
│ 💬 追问 (2)                    [收起 ▲] │  ← 标题栏（追问数量 + 展开/收起）
├─────────────────────────────────────────┤
│  效果摘要：                             │  ← 可选，Run 的结果摘要
│  分析完成，发现 3 个异常模式...          │
├─────────────────────────────────────────┤
│                                         │
│  Q1: 为什么选择这个方案？               │  ← 历史追问列表
│  A1: 因为该方案在测试集上...            │
│                                         │
│  Q2: 数据来源是什么？                   │
│  A2: 数据来自生产环境...                │
│                                         │
├─────────────────────────────────────────┤
│ ┌───────────────────────────────────┐   │
│ │ 输入追问...               [发送]  │   │  ← 输入框 + 发送按钮
│ └───────────────────────────────────┘   │
│                                         │
│ 只读模式 · 模型基于本次任务上下文回答    │  ← 底部提示（护栏提示）
└─────────────────────────────────────────┘
```

**行为规则：**

| 场景 | 行为 |
|------|------|
| Run 还在 RUNNING | 追问面板折叠或禁用，显示「任务执行中…」 |
| Run 已 SUCCEEDED/FAILED | 面板可展开，输入框可交互 |
| 用户输入问题点击发送 | optimistic UI 追加条目 → POST → 更新 answer |
| 网络错误 | 标记条目 status=error，显示重试按钮 |
| 切换 Run / 离开页面 | 清空本地历史，收起面板 |
| Run 重新执行（rerun） | 清空追问历史（新 session，旧追问无效） |

### 4.2 useFollowupHistory Hook

文件：`reconstructed/frontend/packages/agent-ui-react/src/use-followup-history.ts`

```typescript
interface UseFollowupHistoryOptions {
  runId: string;
  /** 初始加载历史（页面挂载时调用 listFollowups） */
  loadOnMount?: boolean;
}

interface UseFollowupHistoryResult {
  entries: FollowupEntry[];
  sending: boolean;
  error: string | null;
  send: (question: string) => Promise<void>;
  reload: () => Promise<void>;
  clear: () => void;
}
```

**数据流：**

```
useFollowupHistory(runId)
  │
  ├── on mount (loadOnMount=true)
  │     → client.listFollowups(runId)
  │     → 初始化 entries[]
  │
  ├── send(question)
  │     → 生成 clientFollowupId
  │     → append { status: "sending", answer: null }
  │     → client.submitFollowup(runId, question)
  │     → update answer in entries[]
  │
  ├── on unmount / runId change
  │     → clear() 本地状态
  │
  └── reload()
        → client.listFollowups(runId)
        → 重置 entries[]
```

### 4.3 集成到 AgentPanel

在 `AgentPanel` 组件底部追加 `FollowupPanel`：

```typescript
// agent-ui-react/src/index.tsx — AgentPanel 更新

import { FollowupPanel } from "./followup-panel.js";

export function AgentPanel({ runId }: AgentPanelProps): ReactElement | null {
  const projection = useRunProjection(runId);
  // ... 已有代码：header, progress, surfaces ...

  const runEnded =
    projection.run?.status === "SUCCEEDED" ||
    projection.run?.status === "FAILED" ||
    projection.run?.status === "CANCELLED";

  // 从 surfaces 中提取效果摘要
  const effectSummary = useMemo(() => {
    // 从 EvidenceSummary 或 ArtifactCard 的第一个 surface 中提取摘要文本
    const surfaces = projection.surfaces;
    if (surfaces.size === 0) return undefined;
    const firstSurface = surfaces.values().next().value;
    if (!firstSurface) return undefined;
    const doc = firstSurface.document as { props?: { title?: string } };
    return doc?.props?.title;
  }, [projection.surfaces]);

  return (
    <section data-agent-panel={runId}>
      {/* 已有内容 */}
      <div style={headerStyle}>...</div>
      <div style={progressStyle}>...</div>
      <div style={sectionStyle}>{/* surfaces */}</div>

      {/* Phase 3：追问面板 */}
      <FollowupPanel
        runId={runId}
        runEnded={runEnded}
        effectSummary={effectSummary}
      />
    </section>
  );
}
```

### 4.4 Demo Mock 更新

为了支持 demo 模式下的追问测试，需要在 mock-api.ts 中增加：

```typescript
// mock-api.ts — 新增追问路由处理

// POST /v1/runs/{run_id}/followups
if (method === "POST" && parts[1] === "runs" && parts[3] === "followups" && parts.length === 4) {
  const body = JSON.parse(String(init?.body ?? "{}"));
  // Demo 模式返回模拟回答
  return jsonResponse(200, {
    schema_version: "followup-answer/v1",
    run_id: runId,
    client_followup_id: body.client_followup_id ?? "mock-fid",
    answer: `[Demo] 基于本次任务的分析结果：${generateMockAnswer(body.question)}`,
    answered_at: new Date().toISOString(),
  });
}

// GET /v1/runs/{run_id}/followups
if (method === "GET" && parts[1] === "runs" && parts[3] === "followups" && parts.length === 4) {
  return jsonResponse(200, {
    schema_version: "followup-history-page/v1",
    run_id: runId,
    total_count: mockHistory.length,
    records: mockHistory,
  });
}
```

---

## 5. Phase 5 Future：双路由分类器 UI

### 5.1 架构视图

```
用户输入（FollowupPanel 输入框）
        │
        ▼
┌─────────────────────────────┐
│  意图分类器 (classify.py)     │  ← 后端纯函数，无数据权限
│  explain_keywords            │
│  act_keywords                │
└──────────┬──────────────────┘
           │
    ┌──────┴──────┐
    ▼             ▼
解释型路由       行动型路由
(Phase 3)        (Phase 5 Future)
    │             │
    ▼             ▼
追问面板显示     显示确认卡片
模型回答         "已为您准备「修改阈值」任务"
                ┌──────────────┐
                │ [确认] [取消] │
                └──────────────┘
                    │ 确认
                    ▼
                POST /v1/runs
                (全新 Run, 全新 session)
```

### 5.2 前端分类器指示器 (FollowupPanel 增强)

在输入框旁增加分类指示器，让用户感知当前输入将被如何处理：

```
┌─────────────────────────────────────────┐
│ Q: 帮我查一下昨天的销售数据         [发送]│
│                                    🔧   │  ← 行动型指示器（扳手图标）
│ 将创建新任务 · 确认后执行                │  ← 提示文字
└─────────────────────────────────────────┘

┌─────────────────────────────────────────┐
│ Q: 为什么这个指标这么高？           [发送]│
│                                    💬   │  ← 解释型指示器（对话气泡）
│ 在当前会话中追问 · 只读模式             │  ← 提示文字
└─────────────────────────────────────────┘
```

**实现方式：**
- 前端提供一个轻量本地分类器（纯文本关键词匹配），在用户输入时实时切换指示器图标
- 最终分类由后端决定，前端指示器仅为 UX 预览，不绑定后端行为

```typescript
// agent-ui-react/src/intent-classifier.ts (轻量本地版本，仅用于 UI 预览)

const EXPLAIN_KEYWORDS = [
  "为什么", "怎么回事", "什么意思", "依据", "解释", "说明",
  "原因", "理由", "原理", "逻辑", "what", "why", "how",
  "explain", "meaning", "reason",
];

const ACT_KEYWORDS = [
  "改成", "修改", "删除", "发送", "创建", "帮我", "查一下",
  "设置", "更新", "添加", "移除", "执行", "运行", "change",
  "update", "delete", "create", "set", "run", "execute",
];

type IntentPreview = "explain" | "act" | "unknown";

function previewIntent(input: string): IntentPreview {
  const lower = input.toLowerCase();
  const hasAct = ACT_KEYWORDS.some((k) => lower.includes(k));
  const hasExplain = EXPLAIN_KEYWORDS.some((k) => lower.includes(k));
  if (hasAct && !hasExplain) return "act";
  if (hasExplain) return "explain";
  return "unknown"; // 默认走 explain
}
```

### 5.3 行动型确认卡片 (NewTaskDraftCard)

当后端分类为行动型时，返回 `NewTaskDraft` 而非普通 answer，前端展示确认卡片：

```typescript
// agent-ui-protocol — Future 新增类型

export const NewTaskDraft = z
  .object({
    schema_version: z.literal("new-task-draft/v1"),
    run_id: z.string(),           // 当前 Run ID（来源 session）
    task_type: z.string(),        // 任务类型，如 "config_update"
    params: JsonObjectSchema,     // 提取的参数
    summary: z.string(),          // 面向用户的描述
  })
  .strict();
export type NewTaskDraft = z.infer<typeof NewTaskDraft>;

// 追问响应可以是 FollowupAnswer 或 NewTaskDraft
export const FollowupResponse = z.union([FollowupAnswer, NewTaskDraft]);
```

```typescript
// agent-ui-react — NewTaskDraftCard 组件

interface NewTaskDraftCardProps {
  draft: NewTaskDraft;
  onConfirm: () => void;    // → POST /v1/runs
  onCancel: () => void;     // → 关闭卡片
}

// 渲染：
// ┌─────────────────────────────────────┐
// │ 🔧 新任务确认                        │
// │                                     │
// │ 任务类型：配置修改                    │
// │ 参数：阈值 → 75                      │
// │                                     │
// │ 说明：将「失败阈值」从 70 修改为 75   │
// │                                     │
// │     [取消]              [确认执行]   │
// └─────────────────────────────────────┘
```

### 5.4 双路由状态管理

```typescript
// FollowupPanel 扩展状态

type FollowupMode = "idle" | "explain" | "act_confirming" | "act_creating";

interface DualRouteState {
  mode: FollowupMode;
  draft: NewTaskDraft | null;    // 行动型待确认的任务草稿
  creatingRun: boolean;          // 是否正在创建新 Run
}
```

**状态转换：**

```
idle ──输入解释型问题──→ explain (追问面板正常显示答案)
idle ──输入行动型问题──→ act_confirming (显示确认卡片)
act_confirming ──确认──→ act_creating (POST /v1/runs)
act_confirming ──取消──→ idle
act_creating ──成功──→ idle (跳转到新 Run)
act_creating ──失败──→ act_confirming (保留草稿，显示错误)
explain ──新输入行动型问题──→ act_confirming
```

---

## 6. 状态管理与存储

### 6.1 前端状态策略

| 状态 | 位置 | 持久化 | 说明 |
|------|------|--------|------|
| 追问历史记录 | `useFollowupHistory` hook | React state，切换 Run 时清空 | 服务端也有完整历史，前端仅做展示缓存 |
| 输入框内容 | `FollowupPanel` 组件 state | React state，提交后清空 | 不持久化 |
| 双路由模式 | `FollowupPanel` 组件 state | React state | 每次输入重新计算 |
| 追问幂等键 | 调用时生成 | 不存储 | `createIdempotencyKey("followup")` |
| Run 结束状态 | `RunProjection` | 投影自动同步 | 驱动面板展开/收起 |

### 6.2 追问与投影的关系

```
RunProjection (SSE同步)          FollowupHistory (REST调用)
─────────────────────             ──────────────────────
- 驱动 Run 状态变化               - 独立于事件流
- 决定 runEnded 标志              - 通过 REST API 获取/提交
- 提供 effectSummary              - 本地 state 管理
- 生命周期：Run 持续期间          - 生命周期：Run 结束后展开
```

**注意：** 追问不写入事件总线（`control/followup.py` 保证不触发 watermark 变更），因此 `RunProjection` 不需要处理追问事件。追问历史完全独立于 SSE 事件流。

### 6.3 切页/切换 Run 清理

```typescript
// AgentPanel — runId 变化时自动清理
useEffect(() => {
  return () => {
    // 追问历史由 useFollowupHistory 在 unmount 时自动 clear
    // 不需要额外操作
  };
}, [runId]);
```

---

## 7. 验收标准

### Phase 3 P0（追问面板）

| # | 验收项 | 验证方式 |
|---|--------|----------|
| 1 | `FollowupCommand` Zod schema 通过 strict parse | vitest |
| 2 | `FollowupAnswer` Zod schema 通过 strict parse | vitest |
| 3 | `client.submitFollowup(runId, question)` 发送 POST 并返回正确 answer | vitest (mock backend) |
| 4 | `client.listFollowups(runId)` 返回 `FollowupHistoryPage` | vitest (mock backend) |
| 5 | `FollowupPanel` 在 Run SUCCEEDED 后展开 | vitest (render runEnded=true) |
| 6 | 输入问题 → optimistic UI 条目 → answer 更新 | vitest + 人工检查 |
| 7 | 网络错误时显示重试 | vitest |
| 8 | 切换 Run 时追问记录清空 | vitest |
| 9 | Demo mock 支持追问路由，返回模拟回答 | 人工运行 example |
| 10 | Demo mock 追问历史可 GET 分页 | 人工运行 example |
| 11 | 所有新增代码 ruff/tsc 全绿 | CI |
| 12 | FollowupPanel 与 AgentPanel 集成后渲染无报错 | vitest |

### Phase 5 Future（双路由 UI，暂不强制）

| # | 验收项 | 验证方式 |
|---|--------|----------|
| F1 | `previewIntent()` 本地分类器关键词匹配正确 | vitest |
| F2 | 行动型输入时输入框显示 🔧 指示器 + 提示文字 | vitest |
| F3 | 解释型输入时输入框显示 💬 指示器 + 提示文字 | vitest |
| F4 | `NewTaskDraftCard` 渲染确认卡片 | vitest |
| F5 | 确认 → POST /v1/runs 成功后跳转到新 Run | 人工 + vitest |
| F6 | 取消 → 返回 idle 状态 | vitest |
| F7 | `NewTaskDraft` Zod schema 通过 strict parse | vitest |

---

## 8. 文件清单（前端 agent 需要创建/修改的文件）

### 新建文件

| 文件路径 | 职责 |
|----------|------|
| `frontend/packages/agent-ui-react/src/followup-panel.tsx` | `FollowupPanel` 组件 |
| `frontend/packages/agent-ui-react/src/use-followup-history.ts` | `useFollowupHistory` hook |
| `frontend/packages/agent-ui-react/src/intent-classifier.ts` | 本地轻量意图预览分类器 (Phase 5) |
| `frontend/packages/agent-ui-react/src/new-task-draft-card.tsx` | 行动型确认卡片 (Phase 5) |
| `frontend/packages/agent-ui-react/tests/followup-panel.test.tsx` | 追问面板测试 |
| `frontend/packages/agent-ui-react/tests/intent-classifier.test.ts` | 分类器测试 (Phase 5) |
| `frontend/packages/agent-ui-client/tests/followup.test.ts` | 追问 API 客户端测试 |

### 修改文件

| 文件路径 | 修改内容 |
|----------|----------|
| `frontend/packages/agent-ui-protocol/src/index.ts` | 新增 `FollowupCommand`、`FollowupAnswer`、`FollowupRecord`、`FollowupHistoryPage`、`NewTaskDraft` (Phase 5) |
| `frontend/packages/agent-ui-client/src/client.ts` | 新增 `submitFollowup()`、`listFollowups()` 方法 |
| `frontend/packages/agent-ui-client/src/errors.ts` | 可选：新增 `AgentPlatformFollowupError` |
| `frontend/packages/agent-ui-react/src/index.tsx` | 集成 `FollowupPanel` 到 `AgentPanel` |
| `frontend/packages/agent-ui-react/tsconfig.json` | 确保新文件在编译范围内 |
| `frontend/examples/embedded-host-example/src/mock-api.ts` | 新增追问路由 mock |
| `frontend/examples/embedded-host-example/src/App.tsx` | 可选：显示追问面板 |

---

## 9. 关键设计决策

| 问题 | 决策 | 理由 |
|------|------|------|
| 追问历史存哪里？ | 服务端 `FollowupHistoryPage` REST API + 前端 React state | 追问不进入事件总线（不触发 watermark），因此独立存储；前端 state 即可满足展示需求，无需 Redis 缓存 |
| 乐观更新策略？ | 发送时立即追加条目 (answer=null, status=sending) | 提升 UX 感知速度，失败时标记 error 可重试 |
| 幂等如何保证？ | 前端生成 `client_followup_id`，后端去重 | 防止网络重试导致重复追问 |
| 双路由指示器在前端还是后端？ | 前端轻量预览（仅 UI 指示器），后端最终分类 | 前端分类纯文本匹配，速度快、无需网络；后端分类作为权威判定 |
| `FollowupPanel` 是独立组件还是内置在 `AgentPanel`？ | 独立组件 `FollowupPanel`，默认嵌入 `AgentPanel` 底部 | 保持组件可组合性，宿主可选择不展示或自定义位置 |
| 行动型确认成功后如何跳转？ | 触发 navigate host bridge → 打开新 Run 的 AgentPanel | 复用现有宿主跳转机制，无需额外路由 |

---

## 10. 实施路线图

### Step 1：协议层（1 小时）
- 在 `agent-ui-protocol/src/index.ts` 新增 4 个 Zod schema
- 运行 `tsc --noEmit` 验证类型

### Step 2：客户端层（1 小时）
- 在 `agent-ui-client/src/client.ts` 新增 2 个方法
- 编写单元测试 `tests/followup.test.ts`
- 运行 `vitest run` 验证

### Step 3：React 组件（3 小时）
- 创建 `use-followup-history.ts` hook
- 创建 `followup-panel.tsx` 组件
- 集成到 `agent-ui-react/src/index.tsx` 的 `AgentPanel`
- 编写测试 `tests/followup-panel.test.tsx`
- 运行 `vitest run` 验证

### Step 4：Demo Mock 更新（1 小时）
- 在 `examples/embedded-host-example/src/mock-api.ts` 新增追问路由
- 手动运行 example，验证追问面板正常工作

### Step 5（Future）：双路由分类器（2 小时）
- 创建 `intent-classifier.ts`
- 扩展 `FollowupPanel` 支持双路由预览
- 创建 `NewTaskDraftCard` 组件
- 编写测试

---

> **一句话总结：** 在现有 AgentPanel 底部嵌入 FollowupPanel，用户通过 REST API 在同一 Run 的 provider session 内追问；输入框旁有本地轻量分类器预览（解释型/行动型），行动型确认后开全新 Run 执行。追问历史独立于事件流，前端 React state 管理，切换 Run 自动清空。