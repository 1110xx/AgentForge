#!/usr/bin/env bash
# Recovery-round: force pod rotation for the obs stack (STS pods re-read the
# fixed ConfigMaps on restart), then rerun the observability gate.
set -uo pipefail
ROOT="$(cd -- "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NS=agent-platform-observability
cd "$ROOT" || exit 9

kubectl -n "$NS" delete pod -l app=agent-platform-tempo --ignore-not-found
kubectl -n "$NS" delete pod -l app=agent-platform-prometheus --ignore-not-found
kubectl -n "$NS" delete pod -l app.kubernetes.io/name=agent-platform-otel-collector --ignore-not-found

bash scripts/test-observability.sh > /tmp/obs9.log 2>&1
rc=$?
echo "OBS9_EXIT=$rc" >> /tmp/obs9.log
echo "--- tail ---"
tail -6 /tmp/obs9.log
exit $rc