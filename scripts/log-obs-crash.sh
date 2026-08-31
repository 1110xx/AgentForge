#!/usr/bin/env bash
# Recovery-round diagnostic — dump crash reason of the observability workloads.
set -uo pipefail
NS=agent-platform-observability
for obj in statefulset/agent-platform-tempo statefulset/agent-platform-prometheus deploy/agent-platform-otel-collector; do
  echo "===== $obj last crash log ====="
  kubectl logs -n $NS $obj --tail=16 2>&1 | grep -iE "error|fatal|panic|refused|level=error|level=warn" | tail -6
  echo "===== $obj Available msg ====="
  kubectl get -n $NS $obj -o jsonpath='{.status.conditions[?(@.type=="Available")].message}' 2>&1
  echo
done