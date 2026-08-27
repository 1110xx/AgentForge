# SDD：双链路实时流式事件 — Agent 中间执行过程可视化

> 状态：Implemented（v1）
> 关联：`SDD.md §11.4/§11.5`、`docs/sdd-followup-mode.md`、`docs/architecture.md`
> 前提：后端 Agent 为 pi-agent-core（`Agent.prompt/continue_` 事件驱动主循环），
> 事件通过 `Agent.subscribe` 实时派发（`runtime.py` 生命周期壳内订阅）
> 阅读顺序：§1 双链路总览 → §2 持久链路（event）→ §3 瞬态链路（stream-chunk）→ §4 SSE 双帧 → §5 前端路由 → §6 落地清单 → §7 验收

---

## 1. 双链路总览

用户要看到 agent 的中间执行过程，但**逐 token 的过程数据不能全部写库**。因此拆两条独立链路：

```
┌──────────────────────────────────────────────────────────────────────┐
│ pi-agent-core Agent（Runner 进程内）                                    │
│   Agent.subscribe → runtime._on_agent_event                            │
│     ├─ ToolExecutionStartEvent / EndEvent                              │
│     ├─ ToolExecutionUpdateEvent（中间输出）                             │
│     ├─ StreamThinkingDeltaEvent / StreamTextDeltaEvent（逐 token）      │
│     └─ TurnEndEvent（整轮结束的原子快照点）                             │
└───────────────┬──────────────────────────────┬─────────────────────────┘
                │ ① 持久链路                    │ ② 瞬态链路
                ▼                              ▼
        OP_EMIT_EVENT                  OP_STREAM_CHUNK（id=0，fire-and-forget）
        ↓ append_event                 ↓ 不落库
        PG 事件日志 ← EnterpriseEventEnvelope      InMemoryRunChunkRelay（有界）
        ↓ Outbox                       ↓
        SSE `event` 帧                 SSE `stream-chunk` 帧
        ↓ 可回放/可重连                ↓ 只在线会话有效，断连即丢
        └──► 前端 recentEvents(事件)      └──► 前端 streamChunks（UI-only）
```

- **① 持久链路**：`tool.execution.started / ended`、`agent.turn.completed` 是
  第一公民平台事件（`enterprise-event/v1`），走 `append_event` 落 PG → Outbox →
  SSE `event` 帧。前端**刷新/重连/回放**都从这里恢复。
- **② 瞬态链路**：`ToolExecutionUpdate` 与 StreamThinking/Text delta **只**走
  内存 relay → SSE `stream-chunk` 帧。断连直接丢，不参与回放（打字机实时效果）。

关键语义（对应“刷新后不从成千上万 delta 恢复”）：
**回放数据源 = `agent.turn.completed` 持久事件中的完整聚合（thinking + 完整
回复文本 + 工具汇总），而不是瞬态 delta。**

---

## 2. 持久链路（①）

### 2.1 新增事件类型

| EventType | Payload | payload_schema | 触发时机 |
|-----------|---------|----------------|---------|
| `tool.execution.started` | ToolExecutionStartedPayload | `tool-execution/v1` | pi ToolExecutionStartEvent |
| `tool.execution.ended` | ToolExecutionEndedPayload | `tool-execution/v1` | pi ToolExecutionEndEvent |
| `agent.turn.completed` | AgentTurnCompletedPayload | `agent-turn/v1` | pi TurnEndEvent |

### 2.2 载荷

```jsonc
// agent.turn.completed（重连/回放唯一完整来源）
{
  "kind": "agent.turn.completed",
  "turn_seq": 1,
  "thinking": "完整聚合思考文本（裁剪到 16KiB）",
  "message_text": "完整聚合回复文本（裁剪到 16KiB）",
  "tool_calls": [{"call_id": "c1", "tool_name": "synthetic.results.read", "status": "succeeded"}]
}
```

```jsonc
// tool.execution.started / ended
{
  "kind": "tool.execution.started",
  "call_id": "c1",
  "tool_name": "synthetic.results.read",
  "args": {"max_items": 100}   // 有界 + 凭据键脱敏
}
```

### 2.3 安全性（写库前强制）

- `args` / `result` 键数上限（20）与字符串长度上限（2 000）在 Runtime 侧裁剪；
- 凭据类键名（`api_key/token/secret/authorization/cookie/password/...`）**在
  Runtime 源头标 `[REDACTED]`**，值不离开 Runner；
- 子进程只能通过 bridge 发这 3 种事件类型，其余类型 `EVENT_TYPE_NOT_ALLOWED`
  拒绝（公共事件日志不是子进程自由通道）。

### 2.4 事件携带信息

事件信封完整携带 `run_id / attempt_id / event_seq / trace_id`，与现有平台事件
完全同构，可直接消费 Outbox / 审计 / 事件页。

---

## 3. 瞬态链路（②）

### 3.1 传输

- 子进程 `PipeClient.send_notify(OP_STREAM_CHUNK, chunk)`：`{"id":0, "op":"stream_chunk"}`，
  无 reply 匹配，**主进程不回包**（省管道流量）；写锁保证与 request 帧不交错。
- 主进程 `_op_stream_chunk` 只 `relay.push(run_id, chunk)`，**不调用 append_event**。

### 3.2 内存 relay（`platform/run_chunks.py`）

- 每 run 有界队列（默认 500 条），容量超限丢最旧；
- run 数量有界（默认 1000），超限丢最旧 run；
- 进程内共享：Phase-1 子进程/内存组合中 worker 与 api 同进程，SSE 可直接 drain；
  生产拆分部署需将 chunk 走**短保留的瞬态传输**（如 NATS JetStream 短 TTL subject），
  持久链路不受影响。

### 3.3 chunk 载荷

```jsonc
// 帧 event: stream-chunk
{
  "run_id": "run_1", "attempt_id": "att_1",
  "kind": "thinking.delta" | "text.delta" | "tool.execution.started"
        | "tool.execution.updated" | "tool.execution.ended",
  "delta": "正在分析…",          // thinking/text delta
  "partial": "...",              // tool 中间输出
  "call_id": "c1", "tool_name": "...",
  "args": {...}, "is_error": false
}
```

**无 `event_seq`、无 id、不持久化、断连丢弃。**

---

## 4. SSE 双帧

`GET /v1/runs/{run_id}/events/stream` 现在输出两类帧：

| 帧 event | 内容 | 是否可回放 | id / event_seq |
|----------|------|-----------|----------------|
| `event`（默认，如 `run.status.changed`） | EnterpriseEventEnvelope | ✅ 可回放/重放 | ✅ 有 |
| `stream-chunk` | 瞬态 chunk | ❌ 断连即丢 | ❌ 无 |

- 每轮循环先 drain relay 的 chunk（有界批量），再取持久事件页；
- 心跳/生命周期与现状一致；`stream-chunk` 帧不参与 `Last-Event-ID` 游标。

---

## 5. 前端路由

### 5.1 projection（`agent-ui-client`）

- `recentEvents` **只缓存** `event` 帧（正式 EnterpriseEventEnvelope）；
- 新增 `streamChunks`（有界 500 条环形缓冲）**只给 UI 渲染**：
  - `projection.ingestChunk(chunk)` 不推进 watermark、不入 recentEvents、不触发 replay；
  - resync 重建快照时 `streamChunks` 清空（回放靠 `agent.turn.completed`）。

### 5.2 AgentPanel（`agent-ui-react`）LiveActivityPanel

- 完成轮（`agent.turn.completed` 持久事件）：第 N 轮 + 可折叠思考 + 工具汇总 + 完整回复；
- 进行中（stream-chunks）：思考增量（💭）+ 工具行（名称/参数/中间输出）+ 回复打字机；
- 重连/刷新：从持久事件重渲染轮次，不依赖瞬态 delta。

---

## 6. 落地清单（代码位置）

| 层 | 文件 | 改动 |
|----|------|------|
| 契约 | `contracts/enums.py` | EventType +3 |
| 契约 | `contracts/events.py` | 3 个 payload + EVENT_PAYLOAD_CONTRACTS |
| 传输 | `execution/pipe_transport.py` | `OP_EMIT_EVENT`/`OP_STREAM_CHUNK` + `send_notify` + 写锁 |
| 桥接 | `execution/runtime.py` | `_on_agent_event` + `AgentEventSink` + 裁剪/脱敏 |
| 子进程 | `execution/subprocess_runtime.py` | `PipeAgentEventSink` 接线 |
| 主进程 | `execution/subprocess_orchestrator.py` | `_op_emit_event`（append_event）/`_op_stream_chunk`（relay） |
| relay | `platform/run_chunks.py`（新） | 有界内存 relay |
| SSE | `fastapi/sse.py` | `chunk_frame()` + 双帧输出 |
| 容器 | `fastapi/dependencies.py`、`router.py` | `chunk_streamer` 接线 |
| 前端协议 | `frontend/.../agent-ui-protocol/index.ts` | 3 payload + StreamChunk schema |
| 前端解析 | `frontend/.../agent-ui-client/sse.ts` | 双帧解析 + onChunk |
| 前端同步 | `frontend/.../agent-ui-client/client.ts` | SseEvent 联合 + 分流 |
| 前端投影 | `frontend/.../agent-ui-client/projection.ts` | streamChunks（UI-only） |
| 前端渲染 | `frontend/.../agent-ui-react/live-activity.tsx` | LiveActivityPanel |
| 文档 | `SDD.md §11.4/§11.5`、`docs/sdd-live-streaming.md` | 本设计 |

## 7. 验收

1. 子进程发 `emit_event` → 主进程 append_event → 事件页可查、Outbox 出队；
   非白名单类型/坏载荷被拒绝（`EVENT_TYPE_NOT_ALLOWED` / `INVALID_EVENT_PAYLOAD`）。
2. 子进程发 `stream_chunk` → 主进程只入 relay，不产生任何持久事件；
   relay 有界（超限丢最旧）。
3. SSE 输出两种帧；`stream-chunk` 帧无 id/event_seq；前端解析器区分两类并分别
   走 `onEvent` / `onChunk`。
4. 前端 projection：recentEvents 只含正式事件；streamChunks 独立环形缓冲、
   resync 清空、不推进 watermark、不属于另一 run 时抛错。
5. 端到端：Agent 运行中前端实时看到思考/工具/打字机；刷新后从
   `agent.turn.completed` 完整恢复轮次。