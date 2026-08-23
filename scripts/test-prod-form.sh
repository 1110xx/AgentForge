#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# Phase 4.2 (G.3 验收) — L3 动态门在生产形态重跑 + G2/G3 接线验证。
#
# 生产形态 = golden `deploy/prod/values.yaml`（真 digest、三工作负载、Ingress
# + TLS）叠加 kind 可用性覆写：gVisor 可选（验收条款）、KEDA 归 4.4、域名用
# e2e 宿主（`agent-platform.e2e.local`，curl --resolve 代替公网 DNS）。
# 凭据全部来自 ESO→Vault（`agent-platform-dependencies`，非 bootstrap Secret）。
#
# 验证点：
#   · Secret 注入：api pod env 含 Vault 里的 DATABASE_URL / DEEPSEEK key（生产模型 key 注入路径）
#   · Ingress：NodePort 30080/30443；根路径 SPA 200、/api/agent-platform 最长前缀 200、SSE 反缓冲 off
#   · TLS：cert-manager 签发 leaf（SAN=host），CA 链可验（--cacert），非自签伪造
#   · L3 动态门：真实 Run→Attempt Job→SUCCEEDED（pytest attempt 测试）
#
# 用法: scripts/test-prod-form.sh [--keep]
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

root_dir="$(cd -- "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

usage() {
  echo "Usage: test-prod-form.sh [--keep]"
  echo "Re-run the L3 dynamic gate in production form (golden values + ESO + TLS ingress)."
}
keep_cluster="${KEEP_CLUSTER:-0}"
case "${1:-}" in
  --help|-h) usage; exit 0 ;;
  --keep) keep_cluster=1 ;;
  "") ;;
  *) usage >&2; exit 2 ;;
esac

# Remove leftover kubectl port-forward listeners on our ports: a leaked pf
# from an aborted prior run renders the CURRENT run's pf unable to bind
# ("Unable to listen on port ... Only one usage") and the gate then talks to
# a half-dead tunnel (502s). Netstat gives WINDOWS pids; taskkill is the
# reliable kill on this box (git-bash kill is hit-or-miss).
cleanup_host_ports() {
  local port
  local pids
  for port in $1; do
    pids="$(netstat -ano 2>/dev/null | awk -v port="$port" \
      '$2 ~ (":" port "$") && $4 == "LISTENING" {print $NF}' | sort -u)"
    for pid in $pids; do
      taskkill //F //PID "$pid" >/dev/null 2>&1 || kill "$pid" 2>/dev/null || true
    done
  done
}
cleanup_host_ports "30443 30080 18080"

for command_name in docker kind kubectl helm python; do
  command -v "$command_name" >/dev/null 2>&1 || {
    echo "Required command is missing: $command_name" >&2
    exit 69
  }
done

context="$(kubectl config current-context)"
if [[ "$context" != kind-* ]]; then
  echo "Refusing to run against non-Kind context: $context" >&2
  exit 77
fi
cluster_name="${AGENT_PLATFORM_KIND_CLUSTER:-agent-platform-e2e}"
control_namespace="agent-platform-control"

# ── 1. 镜像：确认 golden digest 在本地 registry 有已推送 tag，并 load 进 kind 节点 ──
echo "== [1/6] loading golden images into kind nodes =="
python - "$root_dir/deploy/prod/image-refs.json" <<'PYEOF'
import json
import subprocess
import sys

refs = json.load(open(sys.argv[1], encoding="utf-8"))
rows = subprocess.run(
    ["docker", "images", "--format", "{{.Repository}}:{{.Tag}}"],
    check=True, capture_output=True, text=True,
).stdout.splitlines()
missing = []
for key in ("controlPlane", "runtime", "frontend"):
    image = refs[key]
    repo, digest = image["repository"], image["digest"]
    found = False
    for repo_tag in rows:
        insp = subprocess.run(
            ["docker", "image", "inspect", "--format", "{{json .RepoDigests}}", repo_tag],
            capture_output=True, text=True,
        ).stdout
        if f"{repo}@{digest}" in insp:
            print(f"golden {key}: {repo_tag} -> {digest[:12]}", file=sys.stderr)
            found = True
            break
    if not found:
        missing.append(digest[:12])
if missing:
    print("Run scripts/build-images.sh --push first; missing local images: "
          + ", ".join(missing), file=sys.stderr)
    sys.exit(65)
PYEOF
kind load docker-image \
  localhost:5001/enterprise-agent-platform/control-plane:p41-demo \
  localhost:5001/enterprise-agent-platform/runtime:p41-demo \
  localhost:5001/enterprise-agent-platform/frontend:p41-demo3 \
  --name "$cluster_name"

# ── 2. 接线（幂等）：ESO + cert-manager + ingress-nginx + Vault + SecretStore ──
echo "== [2/6] ensuring G2/G3 wiring (bootstrap-prod-wiring.sh) =="
"$root_dir/scripts/bootstrap-prod-wiring.sh"

# ── 3. golden 形态部署（helm upgrade，migrate hook 由已就位 Secret 满足） ──
echo "== [3/6] purging raw-applied chart objects (kind gate applies via kubectl, so helm import would fail on ownership) =="
kubectl delete namespace agent-platform-control agent-platform-sandbox \
  --wait=true --timeout=600s 2>/dev/null || true
# Namespace deletion can linger in Terminating; never proceed into an
# intermediate state (the ESO re-wire would land in a dying namespace).
for ns in agent-platform-control agent-platform-sandbox; do
  for _ in $(seq 1 120); do
    kubectl get namespace "$ns" >/dev/null 2>&1 || break
    sleep 5
  done
  if kubectl get namespace "$ns" >/dev/null 2>&1; then
    echo "namespace $ns still present after purge (finalizer?) " >&2
    exit 79
  fi
done
kubectl delete priorityclass agent-platform-control-critical \
  agent-platform-attempt --ignore-not-found 2>/dev/null || true
kubectl delete clusterrole agent-platform-tokenreview \
  --ignore-not-found 2>/dev/null || true
kubectl delete clusterrolebinding agent-platform-control-plane-tokenreview \
  --ignore-not-found 2>/dev/null || true
# Re-materialize the platform Secret (ESO -> Vault round-trip) in the fresh
# control namespace before the pre-upgrade migrate hook of helm needs it.
"$root_dir/scripts/bootstrap-prod-wiring.sh" --eso-only

echo "== [3/6] deploying production golden values =="
kubectl -n "$control_namespace" apply -f - <<'EOF'
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
  --namespace "$control_namespace" --create-namespace \
  --values "$root_dir/deploy/prod/values.yaml" \
  --set ingress.host=agent-platform.e2e.local \
  --set sandbox.runtimeClassName="" \
  --set autoscaling.enabled=false \
  --set controlPlane.replicas=2 \
  --set ingress.tls.clusterIssuer=agent-platform-demo-issuer \
  --wait --timeout 600s
# NOTE: golden keeps letsencrypt-prod as the real production issuer contract;
# the demo gate switches to the in-cluster demo CA (bootstrap-prod-wiring.sh)
# so the TLS path completes end-to-end without public DNS/ACME.
# NOTE: api pinned to 2 replicas — kind has only 2 schedulable workers
# (control-plane node is tainted); golden 3 replicas with maxSkew 1
# DoNotSchedule cannot fit on two nodes. HA skew sizing belongs to Phase 4.4
# capacity validation on a real node pool; this gate validates wiring.
# The remaining rollouts happen right below (after the comment).
kubectl -n "$control_namespace" rollout status deployment/agent-platform-api \
  --timeout=300s
kubectl -n "$control_namespace" rollout status \
  deployment/agent-platform-orchestrator --timeout=300s
kubectl -n "$control_namespace" rollout status \
  deployment/agent-platform-frontend --timeout=300s

# ── 4. G2 断言：凭据注入生效（生产模型 key 路径 + 数据端点） ──
echo "== [4/6] G2 secret injection assertions =="
api_pod="$(kubectl -n "$control_namespace" get pod -l app.kubernetes.io/name=agent-platform-api \
  -o jsonpath='{.items[0].metadata.name}')"
vault_db_url="$(kubectl -n agent-platform-control get secret agent-platform-dependencies \
  -o jsonpath='{.data.AGENT_PLATFORM_DATABASE_URL}' | base64 -d)"
pod_db_url="$(kubectl -n "$control_namespace" exec "$api_pod" -- \
  printenv AGENT_PLATFORM_DATABASE_URL)"
[[ "$pod_db_url" == "$vault_db_url" ]] || {
  echo "API DATABASE_URL != Vault value (injection broken)" >&2
  exit 78
}
pod_model_key="$(kubectl -n "$control_namespace" exec "$api_pod" -- \
  printenv AGENT_PLATFORM_DEEPSEEK_API_KEY 2>/dev/null || true)"
[[ -n "$pod_model_key" && "$pod_model_key" == sk-prod-demo-* ]] || {
  echo "model API key not injected into api pod" >&2
  exit 78
}
echo "G2 OK: api pod env <- Vault (DATABASE_URL + DeepSeek key)"

# ── 5. G3 断言：域名/TLS/最长前缀（通过 ingress-nginx NodePort） ──
echo "== [5/6] G3 TLS/ingress assertions =="
node_ip="$(kubectl get nodes -o jsonpath='{.items[0].status.addresses[?(@.type=="InternalIP")].address}')"
kubectl -n ingress-nginx rollout status deployment/ingress-nginx-controller \
  --timeout=240s
host="agent-platform.e2e.local"
# The leaf is cert-manager's ingress-shim Certificate (named after the TLS
# secret, in the control namespace); wait for it before curling so the SAN is
# real (not just the bootstrap CA chain).
kubectl -n "$control_namespace" wait --for=condition=Ready \
  certificate/agent-platform-api-tls --timeout=240s
# Transport: kind has no LoadBalancer and WSL2 docker networking keeps the
# host-side NodePort closed, so expose the ingress controller Service with
# kubectl port-forward (https service port 443 -> local 30443). Routing,
# host matching, longest-prefix and certificate logic are identical to the
# production LB path — only the transport hop differs.
cleanup_host_ports "30443 30080"
kubectl -n ingress-nginx port-forward svc/ingress-nginx-controller \
  30443:443 >/dev/null 2>&1 &
ingress_pf_pid=$!
resolve_args=(--resolve "$host:30443:127.0.0.1")
# Curl transport uses an IP URL with a Host header (immune to local DNS
# quirks); the SNI-based SAN/cert checks below keep the real hostname via
# openssl/s_client so the certificate path is still proven end to end.
curl_host_args=(-H "Host: $host" --noproxy '*')
url_root="https://127.0.0.1:30443/"
url_api="https://127.0.0.1:30443/api/agent-platform/v1/health/ready"
curl_args=(--cacert "$root_dir/.tmp-prod-form-ca.crt")
kubectl -n cert-manager get secret agent-platform-demo-ca \
  -o jsonpath='{.data.ca\.crt}' | base64 -d > "$root_dir/.tmp-prod-form-ca.crt"
# kubectl port-forward binds asynchronously; wait until the TLS listener
# answers before asserting (a race here previously surfaced as curl DNS/refused
# noise while the wiring itself was fine).
for _ in $(seq 1 30); do
  code="$(curl -sk "${curl_host_args[@]}" -o /dev/null -w '%{http_code}' \
    "$url_root" 2>/dev/null || true)"
  [ "$code" = 200 ] && break
  sleep 1
done
root_code="$(curl -sk "${curl_host_args[@]}" -o /dev/null -w '%{http_code}' "$url_root")"
api_code="$(curl -sk "${curl_host_args[@]}" -o /dev/null -w '%{http_code}' "$url_api")"
[[ "$root_code" == 200 && "$api_code" == 200 ]] || {
  echo "ingress status: root=$root_code api=$api_code (expected 200/200)" >&2
  exit 78
}
sse_buffering="$(kubectl -n "$control_namespace" get ingress agent-platform-api \
  -o jsonpath='{.metadata.annotations.nginx\.ingress\.kubernetes\.io/proxy-buffering}')"
[[ "$sse_buffering" == "off" ]] || {
  echo "SSE proxy-buffering != off (ingress annotation missing)" >&2
  exit 78
}
leaf_san="$(echo | openssl s_client -connect 127.0.0.1:30443 \
  -servername "$host" 2>/dev/null | openssl x509 -noout -ext subjectAltName)"
[[ "$leaf_san" == *"$host"* ]] || {
  echo "leaf certificate SAN missing $host" >&2
  exit 78
}
# CA chain verifies via openssl: this curl build links Schannel, which ignores
# --cacert (Windows store only); the certificate path is proven with s_client
# + openssl verify against the demo root CA, plus -verify_hostname on the
# requested hostname (leaf is signed by OUR CA — not forged/self-signed).
echo | openssl s_client -connect 127.0.0.1:30443 -servername "$host" \
  -verify_hostname "$host" -CAfile "$root_dir/.tmp-prod-form-ca.crt" \
  -showcerts 2>/dev/null | openssl verify -CAfile \
  "$root_dir/.tmp-prod-form-ca.crt" >/dev/null 2>&1
[[ ${PIPESTATUS[0]} -eq 0 && ${PIPESTATUS[1]} -eq 0 ]] || {
  echo "CA chain verification failed (leaf not signed by demo root CA)" >&2
  exit 78
}
rm -f "$root_dir/.tmp-prod-form-ca.crt"
unset verify_code
echo "G3 OK: root+api 200 via https, leaf SAN=$host, CA chain verified, SSE buffering off"

# ── 6. L3 动态门（真实 Run→Attempt Job→SUCCEEDED，生产形态） ──
echo "== [6/6] L3 dynamic gate in production form =="
cleanup_host_ports "18080"
kubectl -n "$control_namespace" port-forward svc/agent-platform-api \
  18080:8080 >/dev/null 2>&1 &
port_forward_pid=$!
cleanup() {
  kill "${port_forward_pid:-}" >/dev/null 2>&1 || true
  kill "${ingress_pf_pid:-}" >/dev/null 2>&1 || true
  rm -f "$root_dir/.tmp-prod-form-ca.crt"
}
trap cleanup EXIT
api_ready_loop=0
for _ in $(seq 1 30); do
  curl -sf http://127.0.0.1:18080/api/agent-platform/v1/health/ready \
    >/dev/null 2>&1 && break
  api_ready_loop=1
  sleep 1
done
curl -sf http://127.0.0.1:18080/api/agent-platform/v1/health/ready \
  >/dev/null 2>&1 || {
  echo "api not reachable through port-forward (gate precondition failed)" >&2
  exit 78
}

# git-bash $root_dir is a POSIX path (/d/..); native python on Windows needs
# the drive form — convert once (cygpath -m gives forward slashes).
root_dir_native="$(cygpath -m "$root_dir" 2>/dev/null || echo "$root_dir")"
runtime_ref="$(python -c "import json; d=json.load(open('$root_dir_native/deploy/prod/image-refs.json')); print(d['runtime']['repository'] + '@' + d['runtime']['digest'])")"
AGENT_PLATFORM_KIND=1 \
AGENT_PLATFORM_KIND_API_URL="http://127.0.0.1:18080" \
AGENT_PLATFORM_KIND_RUNTIME_IMAGE="$runtime_ref" \
  uv run --project "$root_dir/backend" python -m pytest \
    "$root_dir/backend/tests/kind/test_attempt_job.py"

echo "=== test-prod-form.sh PASSED（G2 注入 + G3 TLS + L3 生产形态全绿） ==="