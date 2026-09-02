#!/usr/bin/env bash
# Phase 5 Step 2 — 数据面定时任务应用（幂等）：ConfigMap(受管 TTL SQL) + CronJobs。
# 用法: scripts/apply-data-plane.sh
set -euo pipefail
root_dir="$(cd -- "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

context="$(kubectl config current-context)"
if [[ "$context" != kind-* ]]; then
  echo "Refusing to mutate non-Kind context: $context" >&2
  exit 77
fi
ns="agent-platform-dependencies"
kubectl get namespace "$ns" >/dev/null 2>&1 || { echo "namespace $ns missing" >&2; exit 78; }

# pg_hba: allow the backup CronJob's pg_basebackup replication connection over
# the cluster network (kind-only, disposable; production DB grants a dedicated
# backup role + pg_hba replication line per runbooks/postgresql-failover.md).
kubectl -n "$ns" exec postgres-0 -- sh -c 'grep -q "host replication all 0.0.0.0/0" $PGDATA/pg_hba.conf || echo "host replication all 0.0.0.0/0 scram-sha-256" >> $PGDATA/pg_hba.conf; psql -U agent_platform -d agent_platform -tAc "select pg_reload_conf()" >/dev/null'

# Refresh the ConfigMap from the single-source SQL file (kubectl apply merge).
kubectl create configmap ttl-maintenance --namespace "$ns" \
  --from-file=ttl_maintenance.sql="$root_dir/deploy/data-plane/ttl_maintenance.sql" \
  --dry-run=client -o yaml | kubectl apply -f - >/dev/null
echo "configmap ttl-maintenance synced (single source: deploy/data-plane/ttl_maintenance.sql)"

kubectl apply -f "$root_dir/deploy/kind/data-plane-cron.yaml"
echo "data-plane CronJobs applied (platform-base-backup / backup-freshness-watchdog / ttl-maintenance)"
