# 从业务系统内嵌 Agent 迁移到独立平台

## 1. 迁移目标

迁移完成后，Agent 平台应当是可单独复制、构建、发布和回滚的产品单元，原业务系统保留业务事实、用户入口和策略权威：独立平台负责 Run 生命周期、远端执行、Checkpoint、审批、Effect、Artifact、事件和 A2UI。

目标依赖方向必须是单向的：

```text
业务系统/adapter → enterprise-agent-platform → enterprise-agent-platform public contracts
                 -X-> 业务系统源码、ORM、路由、部署目录
```

不要把"代码抓到新目录"当作迁移完成。只要平台仍导入宿主 model、读取宿主绝对路径、复用宿主数据库 Session、把用户 Session 当 Run、或依赖某个固定 Pod/home，它就仍是内嵌实现。

## 2. 推荐迁移策略

默认推荐"新 Run 进入新平台、旧 Run 留在原系统只读完成/归档"，而不是搬迁正在运行的 Runtime/Pod。原因是活动 Run 可能已经绑定旧审批、旧 Tool 权限、未完成外部 WRITE 和不可验证的本地文件状态。

只有同时满足以下条件，才考虑迁移活动 Run：

- 旧系统能导出完整、版本化且 checksum 可验证的 Checkpoint；
- 所有已完成 Step、Artifact lineage、Tool call 和 Effect state 可枚举；
- 不存在结果未知的外部 WRITE，或可在新平台重建同一 effect_key 并 reconciliation；
- 可冻结旧 scheduler/worker，并以 generation fence 阻止旧 Runtime 继续写；
- 迁移后使用新 Attempt、新 Lease、新 Sandbox，不复活旧 Pod。

## 3. 阶段 0：现状盘点与冻结边界

先建立迁移清单，不立即改执行代码。

### 3.1 业务入口

盘点所有创建 Agent 任务的入口：页面按钮、REST、定时任务、Webhook、Chat、批处理和管理员重跑。记录每个入口当前提交的用户、租户、资源、参数、上下文和幂等语义。

### 3.2 执行状态

识别目前被混在一起的概念，并映射到统一模型：

| 旧概念 | 目标实体 | 迁移注意 |
| --- | --- | --- |
| chat/session/task/job | Run | 只有用户业务任务是 Run；浏览器 Session 不是 |
| phase/node/action | Step | 必须有稳定 ID、状态、版本和策略快照 |
| retry/container/process | Attempt | 每次具体执行独立记录，不覆盖 retry count |
| worker/agent process | Agent Runtime | 可替换进程，不作为永久业务主键 |
| pod/container | Sandbox Pod | 一次 Attempt 的临时承载，不迁移为业务事实 |
| local state/cursor | Checkpoint | 可恢复的业务游标，是 PostgreSQL 事实 |
| workspace/home archive | WorkspaceSnapshot | 不可变 bytes、checksum、image digest、READY state |

### 3.3 外部依赖

盘点旧系统的数据库、消息、对象存储、身份、凭据、外部连接器和审批入口依赖，标注哪些可以直接迁移、哪些必须在新平台重建，避免迁移后仍保留隐蔽的第二套事实源。

### 3.4 冻结合同

给旧系统导出建立 versioned schema 和 golden fixture。不要直接把旧 ORM row dump 当迁移协议；明确时间格式、enum、nullable、tenant、digest、sequence 和 idempotency。

## 4. 阶段 1：建立独立代码与构建边界

1. 将整个 `enterprise-agent-platform/` 复制到独立仓库或独立 release artifact。
2. 保留 `backend/`、`contracts/`、`frontend/`、`deploy/`、`docs/` 和 `scripts/`，不要只拷 Python 源码。
3. 在空父目录运行 `./scripts/verify.sh l1`。
4. 发布 Python wheel 和四个前端 packages 到受控 registry；记录 checksum/SBOM。
5. 把 OpenAPI/JSON Schema generation parity 加入 CI。
6. 禁止平台 import 宿主 module、读取宿主绝对路径或从宿主环境猜测 credential。

L1 不通过时停止后续迁移：先消除 portability 和 contract drift。

## 5. 阶段 2：把宿主依赖改造成 Ports

### 5.1 建立独立 adapter package

在业务侧创建 `business-agent-adapter` 或同等职责的包，平台不反向导入它。按 [embedding-guide.md](embedding-guide.md) 实现：

- AuthContextProvider
- ResourceResolver
- HostContextVerifier
- PolicyContextProvider
- CredentialBroker
- READ/WRITE Connectors
- Artifact policy/signer/scanner
- production container factory 与 worker factory

每个 adapter 都应有 contract tests，输入输出只使用公开 dataclass/Pydantic/Protocol，不传 ORM entity、数据库 Session 或 request global。

### 5.2 权限迁移

把旧的隐式权限拆成可审计层：

1. principal scopes；
2. Run policy scopes/budget；
3. ToolGrant scopes/resource prefix；
4. ToolSpec required scopes；
5. Runtime/Effect capability scopes。

使用交集而不是并集。浏览器中隐藏按钮不是授权：旧前端传来的 role、tool name、target 或 credential 字段全部视为不可信。

### 5.3 资源引用迁移

为业务对象定义 opaque ref 和 canonical resolver。ref 应稳定但不泄露数据库拓扑；resolver 必须校验 tenant、ownership、classification、version 和 digest。若旧 URL 被当资源标识，先生成 server-side ref mapping，禁止把任意 URL 直接带入新 Tool Gateway。

## 6. 阶段 3：建立持久化事实源

### 6.1 PostgreSQL schema

在独立数据库或独立 schema 运行 Alembic，避免与主业务事务/迁移生命周期耦合。生产连接配置、Secret、pool、timeout、statement timeout、backup/PITR 和 failover 由目标平台管理。

先验证：

- upgrade、downgrade、re-upgrade；
- active Attempt/Lease unique constraints；
- CAS/version conflict；
- database-time Lease expiry；
- tenant boundary；
- Effect unique key；
- mutation + Audit + Outbox 原子性。

### 6.2 数据迁移选择

| 方案 | 推荐场景 | 风险 |
| --- | --- | --- |
| 仅新 Run 使用新平台 | 大多数系统 | 最低：需要旧 Run 只读入口和双查询窗口 |
| 只迁 terminal Run 索引/Artifact | 合规归档和统一搜索 | 中等：需保证旧状态与 checksum 不被改写 |
| 迁 waiting approval Run | 审批周期很长 | 高：需重新验证 ActionProposal、digest、expiry 和 actor scope |
| 迁 UNKNOWN Effect | 不推荐自动迁移 | 极高：先在旧系统 reconciliation，再决定人工导入 |
| 迁 active Runtime Run | 少数必须无中断场景 | 很高：必须 freeze、commit checkpoint、fence old runtime、新建 Attempt |

不要把旧 Pod ID 或 Runner address 写成新 Run 主键。若需要可追溯，放入受控 migration audit metadata，而不是业务关系约束。

### 6.3 导入规则

- 每条记录必须带 tenant；
- 使用 migration namespace 的幂等键；
- 保留旧系统 ID 到新 stable ID 的只读 mapping；
- event sequence 在每个新 Run 内连续；
- 导入前后计算 canonical digest；
- terminal historical Run 不应产生新的 dispatch Outbox；
- active import 必须创建 generation 更高的新 Attempt，并撤销旧 authority；
- 所有异常进入 quarantine table/report，不做部分 silent success。

## 7. 阶段 4：Artifact 与 Workspace 迁移

1. 为旧文件计算 checksum、size、media type、classification、owner 和 retention。
2. 把 bytes 写到 versioned object store 的不可变 key；禁止覆盖已有 key。
3. 完成 malware/DLP scan 后再提交 ArtifactVersion=READY。
4. 建立 input/output lineage 和 source Attempt/generation；无法确定的 lineage 明确标为 migrated-unknown，不猜测。
5. 旧 workspace 只在确实需要恢复时转换为 WorkspaceSnapshot：普通输出文件应是 Artifact。
6. Snapshot 须记录旧 runtime image/build digest 或明确的 migration sentinel，并在恢复策略中决定是否兼容。
7. 随机抽样从对象存储恢复并校验 checksum：执行版本回滚/恢复演练。

不直接把 NFS/home 目录 mount 给新 Sandbox。这样会把旧权限、symlink、socket、credential 和不可审计状态带进新安全边界。

## 8. 阶段 5：消息、调度与 Sandbox

### 8.1 NATS 迁移

新平台只把 NATS 用作通知，不要把旧 queue message payload 原样复制为业务事实。正确流程是：

- PostgreSQL transaction 写领域 mutation + Outbox；
- publisher 发送稳定 ID envelope；
- consumer transaction 写 mutation + Inbox marker；
- commit 后 ACK，失败 NAK；
- Stream 丢失可从 Outbox 重建通知。

### 8.2 调度迁移

先在 shadow tenant 或合成资源开启 scheduler：

- 验证每个 ExecutionUnit 同时只有一个 active Attempt；
- 检查 tenant fairness、quota、backpressure 和 queue lag；
- 检查 worker crash、Lease expiry 和 successor Attempt；
- 旧 scheduler 与新 scheduler 不得同时领取同一 Run；
- cutover 使用 admission flag 和 generation fence，不靠"希望旧 Pod 已停止"。

### 8.3 Sandbox 迁移

先通过 L3，再在生产等价节点做 L4。逐项确认：digest image、non-root、read-only rootfs、drop capabilities、no default SA token、zero RBAC、projected bootstrap、NetworkPolicy、no arbitrary egress、no hostPath/Secret volume、workspace/resource limits 和强化 RuntimeClass。

## 9. 阶段 6：WRITE 与审批迁移

WRITE 是迁移中风险最高的部分，推荐最后开启。

1. 先只迁 READ tools，WRITE proposals 记录但不执行。
2. 验证审批前 Checkpoint commit 与 Lease release 在同一事务。
3. 只允许 ApprovalDecisionService 持久化决定和 PREPARED Effect。
4. Effect worker 只通过内部 route + service identity + effect capability 调用 DurableEffectExecutor；capability 必须绑定 tenant、Effect、Approval、request digest、ToolSpec digest、Connector、target 与 scopes。
5. 在测试外部系统注入 timeout、已知失败、commit 前后断线和 worker crash，验证 UNKNOWN 不自动重试。
6. 建立 reconciliation 查询、独立 effect-reconciler service identity/capability、evidence digest、人工 runbook、correctness alert 和审计报表。
7. 对真实 WRITE 采用 canary tenant/tool/target allowlist，逐步扩大。

旧系统如果由前端直接调用业务写 API，必须先切断这条路径；不能让新审批 UI 只是装饰，实际权限仍在浏览器。

## 10. 阶段 7：前端与 A2UI 迁移

### 10.1 接入顺序

1. 先接入只读 Run status 和事件时间线；
2. 接入 ProgressCard、EvidenceSummary；
3. 接入 Artifact 短期授权下载；
4. 最后开启 ApprovalCard Action；
5. 未知 component、schema mismatch、stale revision 和网络错误必须安全降级。

### 10.2 双读与 shadow

在一段时间内，业务页面可根据 Run source 读取旧/新 API，但一个 Run 只由一个事实源拥有。不要把两个系统的 event 按时间混合排序并推断状态。

新 Run shadow 可比较：

- resource/version digest；
- READ Tool 结果 digest；
- Artifact checksum；
- proposal request digest；
- terminal classification/summary。

shadow 阶段不执行外部 WRITE：差异用于验证 adapter 和 workflow，而不是让两个 Agent 竞争修改同一业务对象。

### 10.3 UI 路由切换

使用 feature flag 按 tenant、workflow 和用户组切换。保留 killswitch：停止创建新 Run、隐藏动作并保留查询/下载。已经创建的 Run 继续由其所属事实源完成或进入人工处置。

## 11. 阶段 8：Cutover

推荐顺序：

1. 冻结平台和 adapter 版本，完成 L1/L2/L3；
2. 在生产等价环境完成 L4 签署；
3. 停止旧系统新 Run admission；
4. 等待旧 active Run 到安全点，或按计划导出 COMMITTED Checkpoint；
5. 确认无未处理 UNKNOWN Effect；
6. 启用新平台 API 与 scheduler，先 canary tenant；
7. 验证 Run create-Attempt-Checkpoint-Artifact-approval/reject-new Attempt；
8. 再开启批准后的真实 WRITE canary；
9. 逐步扩大流量并观察 correctness signals、queue lag、资源和成本；
10. 旧系统切只读，保留审计与 mapping，按 retention 计划退役。

## 12. 回滚方案

回滚不能让同一 Run 在两个平台同时执行。

### 12.1 新 Run admission 回滚

- 关闭新平台 create feature flag；
- 保留 public query、events、Artifact 和审批只读；
- 暂停 scheduler admission，但不要删除事实；
- 只有全新用户任务才重新路由到旧系统；
- 对已经 active 的新 Run 选择继续完成或在 committed Checkpoint 安全暂停。

### 12.2 Runtime 回滚

- 撤销 active capability，令 Lease 过期并 fence generation；
- 提交或确认最后一个 COMMITTED Checkpoint；
- 删除旧 Sandbox 只是清理，不是 fence；
- 若回到旧系统，需要显式反向 migration adapter 和新旧 ID mapping，不能把原 Pod 重新标记为有效。

### 12.3 WRITE 回滚

- 立即停止 Effect worker admission；
- PREPARED Effect 可等待；
- EXECUTING 丢失 executor 必须转 UNKNOWN 并 reconciliation；
- 已 SUCCEEDED 的外部写不能用数据库 rollback 当作撤销，应执行独立、已审批的补偿操作。

## 13. 验收矩阵

| 工作包 | 最低证据 | 通过标准 |
| --- | --- | --- |
| 可移植代码 | L1 | 空父目录 copy、wheel clean install、无 host import/path/secret、前后端全 gate |
| PostgreSQL/NATS/S3 | L2 | migration、CAS、database time、redelivery/Inbox、version/checksum |
| Sandbox 与集群策略 | L3 | real Job/Pod、digest、projected token、zero RBAC、NetworkPolicy |
| 生产安全/HA | L4 | 强化 runtime、failover/PITR、NATS R3、object restore、credential rotation |
| Host Ports | adapter contract tests + security review | tenant/actor/ownership/policy fail closed，timeout 有稳定错误 |
| READ Tool | synthetic + canary | scope intersection、resource allowlist、schema/size/timeout、无 credential 泄漏 |
| WRITE Tool | fault injection + canary | Approval binding、唯一 Effect、idempotency、UNKNOWN reconciliation |
| 前端 | unit + embedded E2E | strict schema、SSE gap/resync、stale action、safe fallback、Artifact identity |
| 运维 | game day | Lease storm、NATS rebuild、object mismatch、credential rotation、DR runbook 可执行 |

未运行的 gate 标 UNVERIFIED。YAML lint、mock 和代码审查不能替代目标环境实测。

## 14. 迁移完成定义

满足以下条件才算完成：

- 平台目录可独立带走，构建和验证不需要原业务仓库；
- 业务集成只通过 public contracts、HTTP、events、Artifact、SDK 和外置 adapter；
- Run/Step/Attempt/Checkpoint/Approval/Effect/Artifact 有唯一持久化事实源；
- Runner/Pod 被视为临时 Attempt 承载，不是用户任务或恢复主键；
- 浏览器不能决定 tenant、Tool、credential、target 或 WRITE payload；
- WRITE 必须经 ApprovalDecisionService + DurableEffectExecutor；
- 旧 generation、旧 Surface、重复 action 和跨租户访问 fail closed；
- L1/L2/L3 当前版本证据完整，L4 由生产 owner 签署；
- rollback、UNKNOWN Effect、数据库恢复、对象恢复和 credential rotation runbook 已演练；
- 原系统 Agent 执行代码已停用或只读归档，没有隐蔽的第二套 scheduler/Effect 真相。
