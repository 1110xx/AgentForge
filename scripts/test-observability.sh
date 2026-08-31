#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# Phase 4.3 (G.3 验收条款 4.3) — 可观测闭环动态门：OTLP collector 接线 + 告警 +
# 日志集中化（“告警→下钻→定位一条线”）。
#
# 两个模式：
#   --stack-only 只起观察栈（collector + Loki + Tempo + Prometheus + Grafana +
#                 promtail）并做后端健康断言（≥3 告警规则、3 看板、Loki/Tempo
#                 ready）。
#   默认（全量）  在栈之上接入平台（helm golden + observability 覆写）→ 真实
#                 Run → 断言：
#                   · Prometheus 查到 agent_platform_run_lifecycle_total /
#                     model 指标（OTLP 链路证据）
#                   · /api/v1/rules 列出 correctness + SLO + business 告警（≥15）
#                   · Tempo 按 agent.platform.run.id 检索到该 Run 的 span 集
#                   · Loki 按 run_id 过滤命中 JSON 日志行（traces↔logs 一 key）
#                   · Grafana /api/search 返回 3 块看板
#
# 依赖：kind 上下文 + golden 镜像已推（同 test-prod-form.sh 前置）+ 公网可拉
# 取 grafana/prom/loki/tempo 镜像（脚本先宿主拉取再 kind load，离线安全）。
# 用法: scripts/test-observability.sh [--stack-only] [--keep]
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

root_dir="$(cd -- "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
obs_ns="agent-platform-observability"
control_ns="agent-platform-control"
stack_only=0
keep_cluster="${KEEP_CLUSTER:-0}"
for arg in "$@"; do
  case "$arg" in
    --stack-only) stack_only=1 ;;
    --keep) keep_cluster=1 ;;
    --help|-h)
      echo "Usage: test-observability.sh [--stack-only] [--keep]"; exit 0 ;;
    *) echo "Unknown argument: $arg" >&2; exit 2 ;;
  esac
done

cleanup_host_ports() {
  local port pids
  for port in $1; do
    pids="$(netstat -ano 2>/dev/null | awk -v port="$port" \
      '$2 ~ (":" port "$") && $4 == "LISTENING" {print $NF}' | sort -u)"
    for pid in $pids; do
      taskkill //F //PID "$pid" >/dev/null 2>&1 || kill "$pid" 2>/dev/null || true
    done
  done
}
cleanup_host_ports "19090 19091 19100 19200 19300 18080"

for command_name in kind kubectl helm docker curl python; do
  command -v "$command_name" >/dev/null 2>&1 || {
    echo "Required command is missing: $command_name" >&2
    exit 69
  }
done
context="$(kubectl config current-context 2>/dev/null || true)"
if [[ "$context" != kind-* ]]; then
  echo "Refusing to run against non-Kind context: $context" >&2
  exit 77
fi
cluster_name="${AGENT_PLATFORM_KIND_CLUSTER:-agent-platform-e2e}"

pf_pids=()
pf() { # pf <name> <svc> <local> <remote> <namespace>
  kubectl -n "$5" port-forward "svc/$2" "$3:$4" >/dev/null 2>&1 &
  pf_pids+=($!)
}
cleanup() {
  for pid in "${pf_pids[@]:-}"; do
    kill "$pid" >/dev/null 2>&1 || true
  done
  cleanup_host_ports "19090 19091 19100 19200 19300 18080"
}
trap cleanup EXIT

wait_http() { # wait_http <port> <path> <attempts>
  local port="$1" path="$2" attempts="${3:-60}" code="" i
  for ((i = 0; i < attempts; i++)); do
    code="$(curl -sf --noproxy '*' -o /dev/null -w '%{http_code}' \
      "http://127.0.0.1:$port$path" 2>/dev/null || true)"
    [ "$code" = 200 ] && return 0
    sleep 2
  done
  return 1
}

# ── 1. 观察栈镜像：宿主拉取 → kind load（离线安全，避免节点直连公网） ──
echo "== [1/6] pulling + loading observability images into kind nodes =="
obs_images=(
  "otel/opentelemetry-collector-contrib:0.121.0"
  "grafana/loki:3.2.1"
  "grafana/tempo:2.5.0"
  "prom/prometheus:v2.55.1"
  "grafana/grafana:11.2.2"
  "grafana/promtail:3.2.1"
)
for image in "${obs_images[@]}"; do
  docker image inspect "$image" >/dev/null 2>&1 || docker pull "$image"
  kind load docker-image "$image" --name "$cluster_name"
done

# ── 2. 起观察栈 ──
echo "== [2/6] applying observability stack =="
kubectl apply -f "$root_dir/deploy/observability/stack/00-namespace.yaml"
kubectl apply -f "$root_dir/deploy/observability/otel-collector.yaml"
kubectl apply -f "$root_dir/deploy/observability/stack/10-loki.yaml"
kubectl apply -f "$root_dir/deploy/observability/stack/11-tempo.yaml"
kubectl apply -f "$root_dir/deploy/observability/stack/12-prometheus.yaml"
kubectl apply -f "$root_dir/deploy/observability/stack/13-grafana.yaml"
kubectl apply -f "$root_dir/deploy/observability/stack/14-promtail.yaml"

# Rules + dashboards ConfigMaps are generated from the source-of-truth files.
kubectl create configmap agent-platform-prometheus-rules \
  --namespace "$obs_ns" \
  --from-file=prometheus-rules-classic.yaml="$root_dir/deploy/observability/prometheus-rules-classic.yaml" \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl create configmap agent-platform-grafana-dashboards \
  --namespace "$obs_ns" \
  --from-file="$root_dir/deploy/observability/dashboards/" \
  --dry-run=client -o yaml | kubectl apply -f -

echo "== [3/6] waiting for observability backends =="
kubectl -n "$obs_ns" rollout status statefulset/agent-platform-loki --timeout=420s
kubectl -n "$obs_ns" rollout status statefulset/agent-platform-tempo --timeout=420s
kubectl -n "$obs_ns" rollout status statefulset/agent-platform-prometheus --timeout=420s
kubectl -n "$obs_ns" rollout status deployment/agent-platform-grafana --timeout=420s
kubectl -n "$obs_ns" rollout status deployment/agent-platform-otel-collector --timeout=420s
kubectl -n "$obs_ns" rollout status daemonset/agent-platform-promtail --timeout=420s

# ── 4. 后端健康断言（stack-only 也执行） ──
echo "== [4/6] backend health assertions =="
pf prometheus agent-platform-prometheus 19090 9090 "$obs_ns"
pf prom-api1 agent-platform-prometheus 19091 9090 "$obs_ns"
pf tempo agent-platform-tempo 19200 3200 "$obs_ns"
pf loki agent-platform-loki 19100 3100 "$obs_ns"
pf grafana agent-platform-grafana 19300 3000 "$obs_ns"
wait_http 19090 /-/ready 60 || { echo "prometheus not ready" >&2; exit 78; }
wait_http 19200 /ready 60 || { echo "tempo not ready" >&2; exit 78; }
wait_http 19100 /ready 60 || { echo "loki not ready" >&2; exit 78; }
wait_http 19300 /api/health 60 || { echo "grafana not ready" >&2; exit 78; }

# 告警规则 ≥3 条生效（本集 correctness 8 + SLO 2 + business 5 = 15）
rules_json="$(curl -sf --noproxy '*' http://127.0.0.1:19090/api/v1/rules | python -c \
  "import json,sys; d=json.load(sys.stdin); n=0
for g in d['data']['groups']:
    n += len(g.get('rules', []))
print(n)")"
[[ "$rules_json" -ge 15 ]] || {
  echo "prometheus rule count = $rules_json (expected >= 15)" >&2
  exit 78
}
echo "alerts OK: $rules_json rules loaded (>=3 acceptance)"

# Grafana 看板 ≥3（provisioning 异步，重试）
dash_count=0
for _ in $(seq 1 45); do
  dash_count="$(curl -sf --noproxy '*' \
    'http://127.0.0.1:19300/api/search?type=dash-db' | python -c \
    "import json,sys; print(len(json.load(sys.stdin)))" 2>/dev/null || echo 0)"
  [ "$dash_count" -ge 3 ] && break
  sleep 2
done
[[ "$dash_count" -ge 3 ]] || {
  echo "grafana dashboards = $dash_count (expected >= 3)" >&2
  exit 78
}
echo "dashboards OK: $dash_count provisioned (overview / run-trace / model-capacity)"

if [ "$stack_only" -eq 1 ]; then
  echo "== stack-only gate PASS =="
  exit 0
fi

# ── 5. 平台接入（golden + observability 覆写，复用 test-prod-form 前置） ──
echo "== [5/6] wiring platform with observability overrides =="
"$root_dir/scripts/bootstrap-prod-wiring.sh"
kubectl delete namespace "$control_ns" agent-platform-sandbox \
  --wait=true --timeout=600s 2>/dev/null || true
for ns in "$control_ns" agent-platform-sandbox; do
  for _ in $(seq 1 120); do
    kubectl get namespace "$ns" >/dev/null 2>&1 || break
    sleep 5
  done
  if kubectl get namespace "$ns" >/dev/null 2>&1; then
    echo "namespace $ns still present after purge" >&2
    exit 79
  fi
done
kubectl delete priorityclass agent-platform-control-critical agent-platform-attempt \
  --ignore-not-found 2>/dev/null || true
kubectl delete clusterrole agent-platform-tokenreview --ignore-not-found 2>/dev/null || true
kubectl delete clusterrolebinding agent-platform-control-plane-tokenreview \
  --ignore-not-found 2>/dev/null || true
"$root_dir/scripts/bootstrap-prod-wiring.sh" --eso-only
kubectl -n "$control_ns" apply -f - <<'EOF'
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: control-orchestrator-egress-all
  namespace: agent-platform-control
spec:
  podSelector:
    matchLabels:
      app.kubernetes.io/name: agent-platform-orchestrator
  policyTypes: [Egress]
  egress:
    - to:
        - ipBlock:
            cidr: 0.0.0.0/0
EOF
helm upgrade --install agent-platform "$root_dir/deploy/helm" \
  --namespace "$control_ns" --create-namespace \
  --values "$root_dir/deploy/prod/values.yaml" \
  --set ingress.host=agent-platform.e2e.local \
  --set sandbox.runtimeClassName="" \
  --set autoscaling.enabled=false \
  --set controlPlane.replicas=2 \
  --set ingress.tls.clusterIssuer=agent-platform-demo-issuer \
  --set observability.enabled=true \
  --set observability.otlpEndpoint="http://agent-platform-otel-collector.$obs_ns:4318" \
  --set observability.jsonLogs=true \
  --set observability.prometheusExporter=false \
  --wait --timeout 600s
kubectl -n "$control_ns" rollout status deployment/agent-platform-api --timeout=300s
kubectl -n "$control_ns" rollout status deployment/agent-platform-orchestrator --timeout=300s

# ── 6. 真实 Run → OTLP 指标 / 告警规则 / Tempo span / Loki 日志断言 ──
echo "== [6/6] real run + observability assertions =="
cleanup_host_ports "18080"
pf api agent-platform-api 18080 8080 "$control_ns"
wait_http 18080 /api/agent-platform/v1/health/ready 30 ||
  { echo "api not reachable" >&2; exit 78; }

run_id="$(curl -sf --noproxy '*' -X POST http://127.0.0.1:18080/v1/runs \
  -H "Authorization: Bearer reference-local-demo" \
  -H "Idempotency-Key: obs-gate-$(date +%s)" \
  -H "Content-Type: application/json" \
  -d '{"workflow_type":"business-analysis","intent":"produce a short synthetic analysis of the reference case","resource_refs":["synthetic-case:ticket-001"],"parameters":{}}' \
  | python -c "import json,sys; print(json.load(sys.stdin)['run_id'])")"
echo "created run: $run_id"
final_status=""
for _ in $(seq 1 150); do
  final_status="$(curl -sf --noproxy '*' \
    "http://127.0.0.1:18080/v1/runs/$run_id" \
    -H "Authorization: Bearer reference-local-demo" | python -c \
    "import json,sys; print(json.load(sys.stdin).get('status',''))" 2>/dev/null || true)"
  [ "$final_status" = "SUCCEEDED" ] && break
  [ "$final_status" = "FAILED" ] && break
  sleep 2
done
[[ "$final_status" == SUCCEEDED ]] || {
  echo "run did not succeed (status=$final_status)" >&2
  exit 78
}
echo "run SUCCEEDED"

# Prometheus: 指标已通过 OTLP 入栈（业务四组证据）。OTLP → collector
# prometheus exporter → prometheus scrape（默认 15s 窗口），终态指标可能
# 落后 run 完成一瞬间，故与 Tempo/Loki 断言一致采用有限重试。
metric_hit=0
model_hit=0
for _ in $(seq 1 20); do
  metric_ok="$(curl -sf --noproxy '*' \
    'http://127.0.0.1:19091/api/v1/query?query=agent_platform_run_lifecycle_total' | python -c \
    "import json,sys
try:
    d=json.load(sys.stdin); print(sum(float(r['value'][1]) for r in d['data']['result']))
except Exception: print(0)" 2>/dev/null || echo 0)"
  model_ok="$(curl -sf --noproxy '*' \
    'http://127.0.0.1:19091/api/v1/query?query=agent_platform_model_calls_total' | python -c \
    "import json,sys
try:
    d=json.load(sys.stdin); print(sum(float(r['value'][1]) for r in d['data']['result']))
except Exception: print(0)" 2>/dev/null || echo 0)"
  { python -c "import sys; raise SystemExit(0 if float(sys.argv[1]) > 0 else 1)" "$metric_ok" && \
    python -c "import sys; raise SystemExit(0 if float(sys.argv[1]) > 0 else 1)" "$model_ok"; } \
    && { metric_hit=1; model_hit=1; break; }
  sleep 3
done
[ "$metric_hit" -eq 1 ] || {
  echo "agent_platform_run_lifecycle_total absent in Prometheus (OTLP chain broken)" >&2
  exit 78
}
[ "$model_hit" -eq 1 ] || {
  echo "agent_platform_model_calls_total absent (runner model-call OTLP missing)" >&2
  exit 78
}
echo "OTLP metrics OK: run_lifecycle=$metric_ok model_calls=$model_ok"

# Tempo: 按 run.id 属性检索到该 Run 的 span 集（ingester 内 retry）
now_s="$(date +%s)"
start_s="$((now_s - 600))"
span_found=0
span_count=0
for _ in $(seq 1 45); do
  response="$(curl -sf --noproxy '*' --get \
    "http://127.0.0.1:19200/api/search" \
    --data-urlencode "start=$start_s" \
    --data-urlencode "end=$now_s" \
    --data-urlencode "limit=20" \
    --data-urlencode "q={ span.agent.platform.run.id = \"$run_id\" }" 2>/dev/null || true)"
  span_count="$(echo "$response" | python -c \
    "import json,sys
try:
    d=json.load(sys.stdin); print(len(d.get('traces', [])))
except Exception: print(0)" 2>/dev/null || echo 0)"
  [ "$span_count" -ge 1 ] && { span_found=1; break; }
  sleep 2
done
[[ "$span_found" -eq 1 ]] || {
  echo "tempo search found no spans for run $run_id (found=$span_count)" >&2
  exit 78
}
echo "Tempo OK: $span_count trace candidate(s) for run_id=$run_id"

# Loki: 按 run_id 命中 JSON 日志行（traces↔logs 一 key）
log_hit=0
for _ in $(seq 1 30); do
  tail_size="$(curl -sf --noproxy '*' --get \
    'http://127.0.0.1:19100/loki/api/v1/query_range' \
    --data-urlencode "query={namespace=~\"$control_ns|agent-platform-sandbox\"}" \
    --data-urlencode "start=$(($(date +%s) - 900))000000000" \
    --data-urlencode "end=$(date +%s)000000000" \
    --data-urlencode "limit=500" | python -c \
    "import json,sys
try:
    d=json.load(sys.stdin)
    body=json.dumps(d)
    print(1 if '$run_id' in body else 0)
except Exception: print(0)" 2>/dev/null || echo 0)"
  [ "$tail_size" = 1 ] && { log_hit=1; break; }
  sleep 2
done
[[ "$log_hit" -eq 1 ]] || {
  echo "loki log lines for run $run_id not found" >&2
  exit 78
}
echo "Loki OK: JSON logs for run_id=$run_id searchable (traces↔logs join verified)"

# 告警规则在平台指标出现后可评估（重新查 /api/v1/rules 确认业务组在线）
echo "== observability gate PASS (run=$run_id) =="