-- Phase 5 Step 2 — 事件/审计/Outbox/幂等 TTL 维护（受管 SQL，经 psql 执行）
--
-- 策略（与 SDD/runbooks 一致，见 docs/phase-5-data-plane.md）：
--   · run_event / audit_event：仅删除「已终止 Run」的、早于保留窗口的历史事件；
--     活跃 Run 的事件永不删除（恢复/审计连续性需要）。
--   · outbox_message：仅回收已发布（published_at 非空）且早于窗口的记录。
--   · idempotency_record：回收已过期的键（expires_at < now()，schema 0003 约定）。
--   · effect_ledger / checkpoint / attempt / run = 事实面，永不按 TTL 删除。
--   · 幂等：重复执行只删「当时已超期」的行；无副作用。
--
-- 用法（psql 参数化，默认 30 天、dry-run=1 只报告不删）：
--   psql $DATABASE_URL -v retention_days=30 -v dry_run=1 -f ttl_maintenance.sql
--   psql $DATABASE_URL -v retention_days=30 -v dry_run=0 -f ttl_maintenance.sql

\if :{?retention_days}
\else
  \set retention_days 30
\endif
\if :{?dry_run}
\else
  \set dry_run 1
\endif
\set cutoff (now() - make_interval(days => :retention_days))

\echo '== TTL maintenance (retention horizon in days + dry_run flag via psql vars) =='

-- 1) run_event — 已终止 Run 的过期历史
\echo '-- run_event candidates:'
SELECT count(*) AS expired_run_events FROM run_event e
WHERE e.occurred_at < :cutoff
  AND EXISTS (
    SELECT 1 FROM run r
    WHERE r.tenant_id = e.tenant_id AND r.run_id = e.run_id
      AND r.status IN ('SUCCEEDED', 'FAILED', 'CANCELLED')
  );

\if :dry_run
\echo 'DRY RUN - no rows deleted (candidates reported above).'
\else
DELETE FROM run_event e
WHERE e.occurred_at < :cutoff
  AND EXISTS (
    SELECT 1 FROM run r
    WHERE r.tenant_id = e.tenant_id AND r.run_id = e.run_id
      AND r.status IN ('SUCCEEDED', 'FAILED', 'CANCELLED')
  );
\echo 'run_event deletes executed.'
\endif

-- 2) audit_event — 已终止 Run 的过期审计记录
\echo '-- audit_event candidates:'
SELECT count(*) AS expired_audit_events FROM audit_event a
WHERE a.occurred_at < :cutoff
  AND EXISTS (
    SELECT 1 FROM run r
    WHERE r.tenant_id = a.tenant_id AND r.run_id = a.run_id
      AND r.status IN ('SUCCEEDED', 'FAILED', 'CANCELLED')
  );

\if :dry_run
\echo 'DRY RUN - no rows deleted (candidates reported above).'
\else
DELETE FROM audit_event a
WHERE a.occurred_at < :cutoff
  AND EXISTS (
    SELECT 1 FROM run r
    WHERE r.tenant_id = a.tenant_id AND r.run_id = a.run_id
      AND r.status IN ('SUCCEEDED', 'FAILED', 'CANCELLED')
  );
\echo 'audit_event deletes executed.'
\endif

-- 3) outbox_message — 已发布且超期的投递记录
\echo '-- outbox candidates:'
SELECT count(*) AS expired_outbox FROM outbox_message
WHERE published_at IS NOT NULL AND published_at < :cutoff;

\if :dry_run
\echo 'DRY RUN - no rows deleted (candidates reported above).'
\else
DELETE FROM outbox_message
WHERE published_at IS NOT NULL AND published_at < :cutoff;
\echo 'outbox deletes executed.'
\endif

-- 4) idempotency_record — 过期键回收（expires_at 为 null = 永不回收）
\echo '-- idempotency candidates:'
SELECT count(*) AS expired_idempotency FROM idempotency_record
WHERE expires_at IS NOT NULL AND expires_at < now();

\if :dry_run
\echo 'DRY RUN - no rows deleted (candidates reported above).'
\else
DELETE FROM idempotency_record
WHERE expires_at IS NOT NULL AND expires_at < now();
\echo 'idempotency deletes executed.'
\endif

\echo '== TTL maintenance complete =='
