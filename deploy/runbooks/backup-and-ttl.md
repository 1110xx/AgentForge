# 备份与保留维护（Phase 5 Step 2 数据面实跑验证）

前置：kind 内 Postgres 在 `agent-platform-dependencies`，MinIO bucket `agent-artifacts`
（versioning on）；数据面定时任务另用 bucket `agent-backups`。

## 一次性接线

```bash
scripts/apply-data-plane.sh        # 幂等：
                                   # 1. pg_hba 追加 replication 行 + reload（跨 pod basebackup 需要）
                                   # 2. ConfigMap ttl-maintenance（单一源 deploy/data-plane/ttl_maintenance.sql）
                                   # 3. apply deploy/kind/data-plane-cron.yaml（三个 CronJob）
```

## 日常运维

| 想做什么 | 命令 |
| --- | --- |
| 手动全量备份 | `kubectl -n agent-platform-dependencies create job backup-manual --from=cronjob/platform-base-backup` |
| 查看备份对象 | `kubectl -n agent-platform-dependencies logs job/backup-manual -c ship`（mc ls 输出） |
| 新鲜度检查/告警 | `create job wd-manual --from=cronjob/backup-freshness-watchdog`；**Job Failed = 备份失效告警**（>26h 无新 base） |
| 手动 TTL 清理 | `create job ttl-manual --from=cronjob/ttl-maintenance`（默认 30 天；dry-run 见 SQL 头注释） |
| 时间点恢复演练 | 见 `docs/phase-5-data-plane.md` §1（独立源库 + base+WAL 双 target 恢复） |

## 语义红线

- run_event/audit_event 仅删已终止 Run 的超期行；活跃 Run 历史、事实表（attempt/checkpoint/
  effect_ledger…）永不按 TTL 删。
- TTL 与备份均在依赖命名空间、用官方镜像，不触碰三个主镜像。
- 生产：backup 用独立 REPLICATION 角色 + pg_hba 专用行；WAL 归档是 RPO 关键；kind 持久化是
  演练形态，真实拓扑见 `postgresql-failover.md`。
