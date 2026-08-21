# SDD：企业级 Agent 追问模式（一个 Run = 一个模型 provider session）

> 状态：Draft（待逐步确认后落地）
> 关联：`architecture.md`（统一执行模型）、`implementation.md`（当前 API/模块）、`security.md`
> 核心前提：**任务型 Agent 的能力依赖模型 provider 的 session 单元**。一个 Run 对应一个模型 provider session，任务在 session 里完成，追问在同一个 session 里继续。

---

## 1. 现状声明

### 1.1 已完成（本方案不改动）

- 治理层：`Run → Attempt → Checkpoint → Approval → Effect`（`docs/architecture.md §2`）。
  - `StepRecord.step_type` + `policy_snapshot`（业务步骤与策略快照）
  - `CheckpointRecord.workflow_cursor` + `active_step_context` + `completed_step_ids` + `model_context_summary_ref`（恢复游标）
  - `EffectLedgerRecord`（已批准的外部写结果）
- 七类生命周期事件总线（`EventType` 恰为 7 个）。
- Sandbox/Kind Pod 隔离（每 Attempt 一个 Pod）。
- 公开 API（`fastapi/router.py`）：`POST /v1/runs`、`GET /v1/runs/{run_id}`、`GET /v1/runs/{run_id}/events`、`POST /v1/runs/{run_id}/actions`（审批）、`cancel`/`rerun`/`recover-effect`。

### 1.2 术语映射（方案 ↔ 本项目代码）

| 方案术语 | 本项目载体 |
| --- | --- |
| 五层原子模块 ContextParse / DataRetrieve / PolicyEnforce / ToolOrchestrate / EffectExecutor | `Step.step_type` + `Checkpoint.workflow_cursor`/`active_step_context` + `EffectLedgerRecord` |
| 七类生命周期事件 | `EventType`（7 个值，完全对应） |
| `POST /api/v1/agent/run` | `POST /v1/runs`（`CreateRunCommand`） |
| `POST /approve` | `POST /v1/runs/{run_id}/actions`（`UiActionCommand` → `ApprovalDecisionService`） |
| 模型 provider 的 session | **当前代码无直接对应**（见 §1.4），需把 `AgentProvider` 升级为 session 单元 |

### 1.3 当前缺口

- Run 完成后，用户无法对结果**追问**。
- 缺乏「模型 session 的延续」：任务跑完，session 被丢弃，用户无法回到同一个上下文继续问。

### 1.4 现状对齐结论（重要）

| 讨论要点 | 当前代码事实 | 结论 |
| --- | --- | --- |
| 一个 Run = 一个模型 provider session | `AgentProvider.decide(context, checkpoint)` 是**无状态**决策（`execution/runtime.py:115`）；`ProviderContext` 只有 `attempt_id`+`generation`，无 session 句柄 | ❌ 未对齐 |
| session 是记忆（模型原生持有） | 记忆 = `Checkpoint` 由平台每轮重建；reference 用确定性 `SyntheticAnalysisAdapter`，无模型 session | ❌ 未对齐 |
| 追问 = 同一 session 继续 | 无 session 概念、无追问入口 | ❌ 未对齐 |
| Checkpoint 是治理账本（审批/Effect/恢复） | ✅ 已有，职责正确 | ✅ 对齐 |

**结论**：当前是「Checkpoint 单元 + 无状态 decide」架构。落地本 SDD 唯一要动的架构点是——把 `AgentProvider` 从「无状态决策」升级为「**每个 Run 一个长生命周期 session**」的 provider（§3.4）。

---

## 2. 目标定义

### 2.1 P0（当前）：追问模式

一个 Run 对应**一个模型 provider session**：任务在 session 里完成，Effect 完成后 session **不销毁**，用户在同一 session 里继续追问，模型基于它自己的 session 记忆回答。

**约束（硬性）：**

- 追问**只读**：模型只「回忆/解释」，不触发任何新的业务操作；
- 不修改任何系统状态（不产生 Effect、不进事件总线）；
- 追问不产生新的 Run、不跨 Run 引用；
- 追问消息绑定 `run_id`（即绑定该 Run 的 session）。

### 2.2 Future：双路由对话层

- **解释型（Explain）**：同一 session 里继续问「为什么」（P0 已有）。
- **行动型（Act）**：识别新任务意图 → 生成 `NewTaskDraft` → 用户确认 → **开全新 Run（全新 session）执行**。

---

## 3. P0 追问模式架构

### 3.1 定位：session 是单元

```
一个 Run = 一个模型 provider session

POST /v1/runs
   │
   ▼
【任务阶段】模型在这个 session 里多轮跑完
   │   模型天然持有该 session 的上下文：意图、工具调用、结果、判定
   │   （平台的 Checkpoint/Step/Approval/Effect 是这套执行的"治理账本"，
   │     不是模型的"记忆"，两者各司其职）
   ▼
Effect 完成，session 不销毁
   │
   ▼
【追问阶段】用户追问 → 消息追加进同一个 session → 模型基于 session 记忆回答（只读）
```

### 3.2 数据流

```
用户在 Run 结果页点击"追问"或输入问题
        │
        ▼
┌───────────────┐
│   UI 层        │  对话面板（绑定 run_id，见 §3.6）
└───────┬───────┘
        │ POST /v1/runs/{run_id}/followups  （只读，幂等）
        ▼
┌───────────────┐
│ 追问服务        │  职责：找到该 Run 的 session，把消息路由进去（§3.3）
│ control/followup│
└───────┬───────┘
        │  → 同一个 RunSessionProvider（§3.4）
        ▼
┌───────────────┐
│ 模型 provider   │  session 里追加一条用户消息 → 模型基于 session 记忆回答
│ （session 单元） │
└───────┬───────┘
        │
        ▼
   返回 answer
```

### 3.3 追问服务职责（`control/followup.py`，新建）

| 职责 | 说明 |
| --- | --- |
| **session 路由** | 根据 `run_id` 找到该 Run 的 provider session，把用户消息追加进去，拿回模型回答 |
| **只读护栏** | 追问调用以 `read_only=True` 进入 session，模型不得触发写/Effect（平台侧兜底拒绝） |
| **幂等** | `client_followup_id`（`Idempotency-Key`）去重，重复追加返回同一回答 |
| **上下文隔离** | 追问只发生在**该 Run 自己的 session**里，不跨 Run |

> 注意：追问服务**不读 Checkpoint 拼历史、不设计记忆**——记忆就是 provider 的 session 本身。

### 3.4 模型 provider session 接缝（复用/升级 `AgentProvider`）

当前 `execution/runtime.py` 的 `AgentProvider.decide(context, checkpoint)` 是无状态决策。本方案把它升级为**每个 Run 一个 session** 的 provider：

```python
class RunSessionProvider(Protocol):
    """宿主注入的、有状态模型 provider：一个 Run 对应一个 session。"""
    async def open(self, run_id: str, intent: str, resource_refs, host_context) -> SessionHandle: ...
    async def run_task(self, handle, *, sandbox_runtime) -> None: ...      # 任务阶段：模型在 session 里跑
    async def followup(self, handle, message: str, *, read_only: bool) -> str: ...  # 追问：追加消息，拿回答
    async def close(self, handle) -> None: ...
```

**关键设计点：**

- **session 由宿主 provider 持有**（agent 框架 / hosted assistant 的长期服务），**不在 Sandbox Pod 内**。因为 Pod 是每 Attempt 一次的临时环境，而 session 要活过整个 Run（任务 + 追问）——所以模型 provider 必须是平台外的长生命周期服务，Pod 内的 runtime 通过内部能力去调用它。
- 任务阶段：模型在 session 里多轮执行；涉及外部写的决策仍走现有 `propose_action → Approval → Effect` 治理路径。
- 追问阶段：`followup(read_only=True)` 把用户消息追加进同一个 session，模型从 session 记忆回答。

> 落地顺序：P0 可先只加「session 接缝 + 追问路由」，任务阶段仍沿用现有执行链；真实模型接入时再把任务循环也迁到 session 内（见 §7 Phase 0 说明）。

### 3.5 记忆：用 provider 原生 session，不新建记忆系统

- **session 就是记忆**：模型 provider 原生持有该 Run 的完整上下文（意图、工具调用、结果、判定、追问历史）。追问时模型直接基于它回答，不需要平台再拼历史。
- **平台不设计记忆系统**：`Checkpoint`/`Step`/`EffectLedger` 是**治理账本**（审批、Effect 执行、故障恢复、合规审计），不是模型的记忆；两者职责分离，不互相替代。
- `CheckpointRecord.model_context_summary_ref` 是「模型上下文摘要」的预留钩子（当前为空）：将来 session 历史过长、需要压缩时，才用它对 session 做摘要再喂模型（类似 pi 的 compaction），属于优化，不是 P0 必需。

### 3.6 UI 设计

```yaml
对话面板组件（frontend/packages/agent-ui-react 新增 FollowupPanel）:
  触发时机: Run 完成后自动展开 / 用户手动点击"追问"
  展示内容:
    - 当前 Run 的 Effect 结果摘要
    - 输入框（用户输入追问）
    - 同一 session 内的历史追问记录（仅当前 Run）
  关闭时机:
    - 用户关闭面板 / 离开当前页面 / 发起新 Run
  状态管理:
    - 追问记录绑定 run_id（= 绑定该 Run 的 session）
    - 页面切换或新 Run 时，追问记录重置
```

---

## 4. Future：双路由对话层

### 4.1 架构升级

在 P0 追问基础上增加**意图分类器**和**行动型路由**：

```
用户输入（同一 session 内）
        │
        ▼
┌─────────────────┐
│  意图分类器        │  纯函数/轻量规则，无数据权限
└────────┬────────┘
         │
    ┌────┴────┐
    ▼         ▼
解释型路由   行动型路由
（P0 已有）  （Future 新增）
```

### 4.2 解释型路由（Explain）

- 判定：疑问词（为什么/怎么回事/什么意思/依据）+ 查询词（数据/结果/规则/阈值），不含动作动词。
- 处理：**在当前 session 里让模型回答**（模型已记得任务过程），不触发任何系统操作。

### 4.3 行动型路由（Act）

- 判定：动作词（改成/修改/删除/发送/创建/帮我/查一下）。
- 处理（**严禁直接执行**）：

```
用户: "帮我把阈值改成 75"
        │
        ▼
识别为行动型意图 → task_type: config_update
        │
        ▼
生成 NewTaskDraft { task_type, params: { target: threshold, value: 75 } }
        │
        ▼
回复确认文案 "已为您准备「修改阈值」任务，确认后执行？"
        │
        ▼
用户点击"确认"
        │
        ▼
POST /v1/runs → 全新 Run = 全新 session（独立生命周期）
```

**关键约束：**

- 行动型路由**绝不直接调用数据库或业务 API**；只产出 `NewTaskDraft`。
- 确认后走标准 `POST /v1/runs` 开全新 Run（全新 session），完整 `Attempt→Checkpoint→Approval→Effect` 生命周期。
- 新 Run 的 ContextParse 从系统实时状态读取（`ResourcePort`），**不继承旧 session 上下文**；高风险任务天然落入现有 `Approval`。
- `NewTaskDraft` 参数仍过 `CreateRunCommand` 的 `AUTHORITY_KEY` 守卫 + `WORKFLOW_PARAMETER_MODELS` 校验。

### 4.4 意图分类器（Future）

```python
def classify_intent(user_input: str) -> Route:
    explain_keywords = ["为什么", "怎么回事", "什么意思", "依据", "数据"]
    act_keywords = ["改成", "修改", "删除", "发送", "创建", "帮我做", "查一下"]
    if any(k in user_input for k in explain_keywords):
        return Route.EXPLAIN
    if any(k in user_input for k in act_keywords):
        return Route.ACT
    return Route.EXPLAIN   # P0 全部走解释型
```

---

## 5. 数据与状态管理

### 5.1 追问记录存储

| 属性 | 说明 |
| --- | --- |
| `run_id` | 绑定到具体 Run（= 该 Run 的 session） |
| `question` / `answer` | 追问内容与模型回答 |
| `timestamp` | 时间戳 |

**存储策略：**

- **session 内的消息由 provider 原生持有**（这是模型的记忆，不在平台持久化）。
- 平台侧只保存一份**轻量追问展示缓存**（Redis TTL 7 天，key 绑定 `run_id`）供 UI 回显；它**不是领域事实**，不进 `PlatformStore`、不进 Outbox/事件总线。
- 若宿主无 Redis，第一版可用进程内 dict（API-only 演示），生产必须 Redis/adapter。

### 5.2 上下文隔离原则

| 场景 | 行为 |
| --- | --- |
| 同一 Run 内多次追问 | 在同一 session 里自然延续，模型记得前文 |
| 切换页面/实体 | 追问面板清空，重新绑定新 Run |
| 新 Run 启动 | 旧 session 关闭/归档，新 Run 开新 session，从零开始 |
| 跨 Run 引用 | **禁止**，"刚才那个任务"必须明确指定 run_id |

---

## 6. 安全与权限

| 层级 | 权限 | 禁止行为 |
| --- | --- | --- |
| 追问服务（P0） | `runs:read`（只读 session） | 禁止写数据库、禁止调用 Effect、禁止访问业务 API/实时库 |
| 意图分类器（Future） | 无数据权限，纯文本分类 | 禁止基于对话内容直接生成 SQL/API 调用 |
| 行动型路由（Future） | `runs:create`（确认后） | 只生成 `NewTaskDraft`，禁止绕过 Approval 直接生效 |

补充：

- 追问端点鉴权沿用 `authenticate_request` + `require_scope(ctx, "runs:read")`，tenant 隔离同现有 Run 读取。
- `followup(read_only=True)` 由平台侧兜底：即使 provider 误发写意图，追问路由也拒绝落任何 Effect。

---

## 7. 实施路线图

> 每阶段独立可验证，遵循 `./scripts/verify.sh` 门禁（L1/L2/L3）。

### Phase 0：模型 provider session 接缝（架构对齐）✅ 已完成

- 文件：`execution/session.py`（新增 `RunSessionProvider` Protocol：`open`/`run_task`/`followup`/`close`，保留现有 `AgentProvider` 兼容）、`reference/session.py`（`InMemoryRunSessionProvider` 桩）、`fastapi/dependencies.py`（容器 `run_sessions` 字段）
- 验收：定义四方法；reference 提供 in-memory 桩；单测 5/5 通过；ruff 全绿。

### Phase 1：追问路由端点 ✅ 已完成

- 文件：`control/followup.py`（`FollowupService`）、`contracts/commands.py`（`FollowupCommand`）、`contracts/models.py`（`FollowupAnswer`）、`fastapi/router.py`（`POST /v1/runs/{run_id}/followups`）、`fastapi/dependencies.py`（`FollowupHandler` + `followups` 字段）、`__init__.py` / `reference/local_stack.py`（接线）
- 验收：单测 10/10 通过；幂等追加；追问不写 RunEvent（watermark 不变）；契约 parity 绿；ruff 全绿。

### Phase 1.5：应用层 demo（模型 provider 跑通完整环节）✅ 已完成

- 文件：`reference/model_provider.py`（`ReferenceModelSessionProvider`：`run_task` 真实驱动 `read→analyze→propose→approval→Effect→success` 完整 vertical）、`scripts/demo_followup.py`（demo）、`tests/test_model_provider.py`
- 验收：`open → run_task → followup ×3 → close` 全链路跑通；Run/Effect 均 SUCCEEDED；追问答案来自 session 内真实任务事实；单测 15/15；ruff 全绿。

### Phase 2：追问展示缓存 ⏸️ 暂缓（用户决定 P0 先不做）

- 文件：独立 `followup_store.py`（`FollowupStore` Protocol：`get/append/clear(run_id)`；Redis 实现 + in-memory 实现）
- 验收：TTL 7 天；`clear(run_id)` 在新 Run/切页时清空。

### Phase 3：前端对话面板

- 文件：`frontend/packages/agent-ui-protocol`（Zod `FollowupCommand`）、`agent-ui-client`（`submitFollowup`/`listFollowups`）、`agent-ui-react`（`FollowupPanel`：Effect 摘要 + 输入框 + 当前 session 内历史）
- 验收：vitest 全绿；Run 完成后可追问；切页/新 Run 清空。

### Phase 4：文档与验证收敛

- 文件：`docs/architecture.md`（§3 补 session 接缝 + 追问层）、`docs/implementation.md`（API 表补 1 行）、`docs/embedding-guide.md`、`docs/security.md`
- 验收：`./scripts/verify.sh l1` frontend gate 全绿；L2 集成冒烟。

### Phase 5（Future，P0 稳定后）：双路由

- 文件：`control/followup.py`（`classify_intent` + `NewTaskDraft` 生成 + 确认流）
- 验收：解释型追问准确率 > 90%；行动型必须用户确认后才 `POST /v1/runs`；新 Run 独立生命周期、新 session 与旧上下文隔离。

---

## 8. 关键决策

| 问题 | P0 建议 | Future 考虑 |
| --- | --- | --- |
| 记忆在哪？ | **模型 provider 的 session 原生持有**，平台不建记忆系统 | session 过长时用 `model_context_summary_ref` 做摘要（类似 pi compaction） |
| session 生命周期 | 一个 Run 一个 session，Effect 后不销毁，追问继续用 | 追问结束/新 Run 时 `close` |
| 追问记录是否持久化？ | 只做 7 天 Redis 展示缓存，**不进事实源**（session 才是记忆） | 按需延长 |
| 是否支持跨 Run 追问？ | **不支持**，每 Run 独立 session | 未来可考虑「工作区」，但底层仍按 Run 隔离 |
| 行动型路由确认方式？ | P0 不涉及 | 强制二次确认，高风险走现有 Approval（`runs:act`） |

---

## 9. 一句话总结

> **一个 Run = 一个模型 provider session。任务在 session 里做完，Effect 后 session 不销毁，追问就是在同一个 session 里继续问——模型用自己的 session 记忆回答，平台只负责治理（审批/Effect/审计）和把追问消息路由进 session，不设计记忆系统。Future 在这层加「红绿灯」：绿灯继续答，红灯生成 NewTaskDraft、用户确认后开全新 Run（全新 session）。**
