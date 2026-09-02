# Phase 4.5 备份恢复演练记录（G6）— 2026-09-02 PASS

依据：`deploy/runbooks/disaster-recovery.md`（恢复顺序：PostgreSQL 事实 → 对象版本 → 事件完整性；通过条件：RPO/RTO 达标、零 audit gap、零重复 Effect、零 stale fence）。

## 1. 演练环境

| 项 | 值 |
| --- | --- |
| PG | `enterprise-agent-platform-postgres-1`（postgres:17-alpine，L2 栈健康） |
| 对象存储 | `agent-artifacts` bucket（MinIO，versioning **enabled** —— runbook 第 2 步前提） |
| 数据面 | 生产库 `agent_platform`（schema v0003_idempotency_ttl，27 张表） |

## 2. 备份（2026-09-02 01:12 UTC）

- 命令：`pg_dump -U agent_platform -Fc agent_platform`（custom 格式）
- 产物：`backups/dr-drill-20260902.dump`（166,880 B）
- 校验和：`sha256 = d48068719afb6516f76eaa5d4bde0f4149a8612440142fa79e6012cab25e9cb6`
- WAL boundary：无归档配置（演练形态），逻辑备份等价覆盖全量事实面；PITR 真档为宿主职责（runbook 已文档）

## 3. 恢复（隔离库，不触碰生产）

- `CREATE DATABASE agent_platform_dr` → `pg_restore -Fc -d agent_platform_dr`，**exit=0**
- schema 版本：DR 库 `alembic_version = 0003_idempotency_ttl` == 生产库 ✓

## 4. 完整性校验（runbook 第 3 步，DR 库 SQL）

| 检查 | 结果 |
| --- | --- |
| run_event 重复 (run_id,event_seq) | 0 |
| 缺失首事件（event_seq=1） | 0 |
| event_seq 连续性（max == count 每 run） | 0 |
| 每 execution_unit 活跃 Attempt（PROVISIONING/CLAIMED/RUNNING/CHECKPOINTING）>1 | 0 |
| 每 execution_unit 活跃 Lease（ACTIVE/RESERVED）>1 | 0 |
| effect_ledger 重复 (tenant_id,effect_id) | 0 |

## 5. 行数对照（生产 vs DR）

| 表 | 生产 | DR |
| --- | --- | --- |
| run | 2 | 2 |
| attempt | 2 | 2 |
| run_event | 198 | 198 |
| audit_event | 42 | 42 |
| checkpoint | 40 | 40 |
| artifact | 0 | 0 |
| effect_ledger | 0 | 0 |
| outbox 未发布（published_at IS NULL） | 194 | 194 |

## 6. 对象存储（runbook 第 2 步）

- `mc version info`：`agent-artifacts versioning is enabled` ✓
- 对象列表：空 —— 与 artifact 表 0 行一致；当前快照无 COMMITTED artifact，空集无 checksum 违例（非空集的逐对象校验在真实 DR 场景按 runbook 执行）

## 7. 结论

- **PASS**：备份 → 隔离恢复 → 完整性六连零违例 → 行数逐表一致 → 对象版本前提满足。
- 后续项：PITR 真档/跨区 DR 为宿主职责（runbook 已文档化）；本演练产物 `backups/` 不提交（gitignore 外，仅记录留档）。