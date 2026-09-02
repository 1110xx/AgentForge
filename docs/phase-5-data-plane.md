# Phase 5 Step 2 — 数据面（WAL 归档 + PITR + 定时备份/告警 + TTL）— 2026-09-02 实跑

依据：`docs/phase-4.5-dr-drill.md`（G6 逻辑备份基线）与 `deploy/runbooks/disaster-recovery.md`
恢复顺序；本阶段把 G6 的「逻辑备份等价覆盖全量事实面」升级为**物理 WAL 归档 + 时间点恢复**，
并把备份/清理做成**定时任务**。全程零云成本、零新主镜像（只用官方 postgres/minio/mc 镜像 +
受管 SQL），遵守 MEMORY 硬约束（数据面不进 control-plane/runtime/frontend 三镜像）。

## 0. 现状与设计取舍（真问题）

- kind 集群 PG/MinIO 均为 emptyDir（官方依赖清单），`archive_mode=off`；重启即丢数据。
- PITR 需要 `archive_mode=on`，而现役 PG 打开它必须重启 = emptyDir 数据丢失。
- **结论**：演练用**独立源库** `data-plane-source`（PVC 持久 + 启动即开归档），不触碰共享
  现役 PG（生产 DB 的持久化/归档/主备是宿主职责，见 `deploy/runbooks/postgresql-failover.md`）；
  演练源库从现役库灌入等价快照后独立演进，全程不动现役数据面。

## 1. WAL 归档 → 全量 basebackup → PITR 演练（实跑 PASS）

| 项 | 值 |
| --- | --- |
| 源库 | `data-plane-source`（postgres:17-alpine，官方镜像 entrypoint args 追加 `-c archive_mode=on -c archive_command='test ! -f /wal/%f && cp %p /wal/%f' -c archive_timeout=10`，local-path PVC） |
| 快照 | 现役 `agent_platform` pg_dump -Fc → pg_restore（27 表计数逐表一致；6 条 restore FK 告警为排序噪音，计数零缺失） |
| 归档 | 验证：`pg_switch_wal()` → `/wal` 段生成、`pg_stat_archiver.archived_count=2` ✓ |
| T1 | `2026-09-02 08:07:43+00` `pg_basebackup -Ft -z` → `backup_manifest` + `base.tar.gz`(4.8MB) + `pg_wal.tar.gz` |
| W1 事务 | 08:09:12 提交，+40 run_event（事件流续写，seq 连续） |
| W2 事务 | 08:09:14 提交，+20 run_event |
| 灾难 | 删除 source StatefulSet + 数据 PVC（模拟主库丢失；wal/backup PVC 幸存） |

**时间点恢复 ×2（同一份 base+WAL，不同 target）：**

| 恢复目标 | 结果 | 判定 |
| --- | --- | --- |
| `T_cut = 08:09:16`（W2 后） | run_event=**113**（含 60 窗口行）；27 表计数与 cut 基准**全等** | PASS |
| `T_mid = 08:09:13.5`（W1/W2 之间） | run_event=**93**；dpw1=40 / dpw2=0 | PASS —— 增量窗口精度 |

恢复侧：`tar -xzf base.tar.gz` → 解 pg_wal → `recovery.signal` + `-c restore_command='cp /wal/%f %p'
-c recovery_target_time=… -c recovery_target_action=promote`。DR 六连校验（恢复库）：run_event
重复=0、缺失 seq=1=0、seq 连续(max≠count)=0、活跃 Attempt 重复=0、活跃 Lease 重复=0、
effect_ledger 重复=0 —— **六连零违例**。

## 2. 定时全量备份 + MinIO 归档 + 失败/失效告警（实跑 PASS）

`deploy/kind/data-plane-cron.yaml`（由 `scripts/apply-data-plane.sh` 幂等应用）：

| CronJob | 计划 | 动作 | 验证 |
| --- | --- | --- | --- |
| `platform-base-backup` | 每天 02:00 | init: pg_basebackup -Ft -z（uid70）；main: mc mirror → MinIO `agent-backups/latest-base/` + 时间戳副本 `base-YYYYMMDD-HHMM.tar.gz` | 手动 Job PASS：6.8MiB base + manifest + pg_wal 归档，对象可列 |
| `backup-freshness-watchdog` | 每小时 :15 | `mc find … --newer-than 26h`；无新鲜备份 → **exit 1 → Job Failed = 告警信号**；另 `mc rm --force --older-than 14d` 保留期 | 新鲜路径 exit0 PASS；告警路径已实测两次触发 Failed（grep 缺失/rm 需 --force 期间），信号语义成立 |
| `ttl-maintenance` | 每周日 03:00 | psql 执行受管 TTL SQL（retention_days=30） | 手动 Job PASS（无过期行 → 0 候选正常完成） |

前置（apply 脚本幂等写入）：现役 PG `pg_hba.conf` 追加
`host replication all 0.0.0.0/0 scram-sha-256` + `pg_reload_conf()`（跨 pod pg_basebackup 需要；
kind 一次性；生产用独立 backup role + pg_hba 行，见 runbook）。

**对象侧证据（MinIO agent-backups）：** `latest-base/base.tar.gz`、`backup_manifest`、
`pg_wal.tar.gz`、`base-20260902-0821.tar.gz` 均 STANDARD 对象，版本/保留由 mc 操作维护。

## 3. TTL 维护（run_event / audit_event / outbox / idempotency）

`deploy/data-plane/ttl_maintenance.sql`（单一事实源 → CronJob ConfigMap）：

- **run_event / audit_event**：仅删「Run 已终止（SUCCEEDED/FAILED/CANCELLED）」且早于窗口的行；
  活跃 Run 历史永不删（审计/恢复连续性）。依据列 `occurred_at`。
- **outbox_message**：仅回收 `published_at` 非空且超期。
- **idempotency_record**：回收 `expires_at < now()`（schema 0003 约定）。
- **事实面（run/attempt/execution_* /checkpoint/effect_ledger…）永不按 TTL 删除**。
- 参数化 `-v retention_days=30 -v dry_run=1`（dry-run 默认只报候选）。

验证（现役库，UTC）：播种过期假行 run_event×2 / audit_event×1 / outbox×1 / idempotency×1 →
dry-run 候选齐全且零删除 → 实删后种子清零、总量回到基线
（run_event 60 / audit_event 1260 / outbox_message 43 / idempotency_record 4 / run 9）。

## 4. 局限与生产迁移要点（真实环境职责，已文档化）

- kind 的 PG/MinIO 持久化与归档是**演练形态**：真实部署 = PVC 持久 PG + archive 归档到独立对象
  存储 + 独立站点（见 `deploy/runbooks/postgresql-failover.md` / `docs/phase-4.4-result.md` L4 前置）。
- RPO：备份窗口 26h 告警阈值 + 每日全量 = 最坏 1 天窗口；生产应缩 schedule（WAL 持续归档是
  RPO 关键，演练中 WAL 归档 10s 切段已证明机制可用）。
- backup 连接用 superuser（演练形态）；生产建专用 `backup` 角色（REPLICATION + 最小库权限）。
- 保留期（30d 事件 / 14d base / 26h 新鲜阈值）为默认值，按合规策略调整。
