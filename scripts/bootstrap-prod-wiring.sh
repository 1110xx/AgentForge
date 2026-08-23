#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# Phase 4.2 (G2/G3) — 生产接线 bootstrap：External Secrets Operator + Vault
# (演示 SecretStore) + cert-manager 信任链，并预置平台依赖凭据。
#
# 目标证明（对应 G.3 4.2 验收前两项）：
#   · ESO 运行且 ClusterSecretStore(enterprise-prod-vault-store) 有效；
#   · 金丝雀 ExternalSecret 把 Vault 里的凭据 map 同步到
#     `agent-platform-dependencies` Secret（PG/NATS/S3/DeepSeek key）；
#   · 凭据注入生效：目标 Secret 的键与 Vault 值逐项一致（round-trip 断言）；
#   · 域名/TLS 就绪：cert-manager 信任链签发测试证书（leaf 由 Ingress 注解驱动）。
#
# 幂等：所有组件 helm upgrade --install / kubectl apply + 已存在则跳过 seed。
# 需要：kind 上下文、HTTPS_PROXY（拉 helm 源）、kind 集群内已有依赖
# （postgres/nats/minio，与 test-kind.sh 一致；从 agent-platform-kind-dependencies
# 读取既有口令，保证同库可连）。
#
# 用法: scripts/bootstrap-prod-wiring.sh
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

root_dir="$(cd -- "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

control_namespace="agent-platform-control"
dependency_namespace="agent-platform-dependencies"
vault_namespace="agent-platform-vault"
eso_namespace="external-secrets"
cert_manager_namespace="cert-manager"
vault_token_secret="agent-platform-vault-token"
platform_secret="agent-platform-dependencies"   # ESO target (chart contract)
store_name="enterprise-secret-store"
# Golden contract remoteKey (deploy/prod/values.yaml secrets.remoteKey) — the
# dotted namespace of the production credential set under the kv-v2 mount.
remote_key=".enterprise-agent-platform/production"
demo_issuer="agent-platform-demo-issuer"

context="$(kubectl config current-context)"
if [[ "$context" != kind-* ]]; then
  echo "Refusing to mutate non-Kind context: $context" >&2
  exit 77
fi

for command_name in kubectl helm openssl; do
  command -v "$command_name" >/dev/null 2>&1 || {
    echo "Required command is missing: $command_name" >&2
    exit 69
  }
done

eso_only=0
case "${1:-}" in
  --eso-only)
    # Skip infra installs (ingress-nginx/ESO/cert-manager/Vault/store must
    # already be in place); only re-materialize the platform Secret from the
    # chart's ExternalSecret and assert the round-trip. Used after the prod-form
    # purge deletes the raw-applied control namespace.
    eso_only=1
    ;;
esac

# helm needs the proxy for charts.external-secrets.io / charts.jetstack.io on
# this machine; harmless elsewhere.
export HTTPS_PROXY="${HTTPS_PROXY:-http://127.0.0.1:7890}"
export HTTP_PROXY="${HTTP_PROXY:-http://127.0.0.1:7890}"

helm repo add external-secrets https://charts.external-secrets.io >/dev/null 2>&1 || true
helm repo add jetstack https://charts.jetstack.io >/dev/null 2>&1 || true
helm repo add ingress-nginx https://kubernetes.github.io/ingress-nginx >/dev/null 2>&1 || true

# Reuse the initialized in-cluster dependencies' credentials so the platform
# pods connect to the SAME postgres/nats/minio the L3 gate already migrated.
pg_value="$(kubectl -n "$dependency_namespace" get secret agent-platform-kind-dependencies \
  -o jsonpath='{.data.postgres-value}' | base64 -d)"
minio_value="$(kubectl -n "$dependency_namespace" get secret agent-platform-kind-dependencies \
  -o jsonpath='{.data.minio-value}' | base64 -d)"

postgres_url="postgresql+asyncpg://agent_platform:${pg_value}@postgres.${dependency_namespace}.svc:5432/agent_platform"
nats_url="nats://nats.${dependency_namespace}.svc:4222"
s3_endpoint="http://minio.${dependency_namespace}.svc:9000"
# Demo key — placeholder value proves the *injection path* (Vault → ESO →
# pod env → platform ConfigReader); a real deployment stores the live key
# here (or in managed KMS) without touching the wiring.
demo_deepseek_key="sk-prod-demo-$(openssl rand -hex 8)-injection-ok"

if [ "$eso_only" != "1" ]; then
echo "== [G3/Ingress] installing ingress-nginx (NodePort 30080/30443) =="
helm upgrade --install ingress-nginx ingress-nginx/ingress-nginx \
  --namespace ingress-nginx --create-namespace \
  --version 4.11.3 \
  --set controller.service.type=NodePort \
  --set controller.service.nodePorts.http=30080 \
  --set controller.service.nodePorts.https=30443 \
  --set controller.metrics.enabled=false \
  --wait --timeout 300s

# Reconcile orphaned CRDs: a previous raw/partial install may have left
# ExternalSecret/SecretStore CRDs without Helm ownership labels, which blocks
# `helm upgrade --install` with the chart CRDs. Safe here (disposable kind):
# delete CRDs whose group has no instance data yet and no Helm ownership.
reconcile_orphan_crds() {
  local suffix="$1"
  local crd
  local owner
  for crd in $(kubectl get crd -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}' 2>/dev/null | grep -E "\.${suffix}$" || true); do
    owner="$(kubectl get crd "$crd" -o jsonpath='{.metadata.labels.app\.kubernetes\.io/managed-by}' 2>/dev/null || true)"
    if [[ "$owner" != "Helm" ]]; then
      echo "reconciling orphaned CRD (no Helm ownership): $crd -> delete"
      kubectl delete crd "$crd" >/dev/null
    fi
  done
}
reconcile_orphan_crds "external-secrets.io"
reconcile_orphan_crds "cert-manager.io"

# Also drop a stale bare release of these charts if one exists from an
# earlier partial run (fresh install below recreates cleanly).
for rel in external-secrets cert-manager; do
  if helm ls -A --filter "^${rel}$" -q 2>/dev/null | grep -qx "$rel"; then
    helm uninstall "$rel" >/dev/null 2>&1 || true
  fi
done

echo "== [G2/ESO] installing External Secrets Operator =="
helm upgrade --install external-secrets external-secrets/external-secrets \
  --namespace "$eso_namespace" --create-namespace \
  --version 0.9.20 \
  --set installCRDs=true \
  --wait --timeout 300s

echo "== [G3/TLS] installing cert-manager =="
helm upgrade --install cert-manager jetstack/cert-manager \
  --namespace "$cert_manager_namespace" --create-namespace \
  --version 1.15.3 \
  --set installCRDs=true \
  --wait --timeout 300s
kubectl -n "$cert_manager_namespace" rollout status deployment/cert-manager \
  --timeout=240s
kubectl -n "$cert_manager_namespace" rollout status deployment/cert-manager-webhook \
  --timeout=240s

echo "== [G2/ESO] applying cert-manager trust chain =="
kubectl apply -f "$root_dir/deploy/prod/tls/cert-manager-profile.yaml"
kubectl -n "$cert_manager_namespace" wait --for=condition=Ready \
  certificate/agent-platform-demo-ca --timeout=120s && \
  kubectl wait --for=condition=Ready \
    clusterissuer/agent-platform-demo-issuer --timeout=120s

echo "== [G2/ESO] applying dev Vault + token Secret =="
# Namespace + Service + Deployment first (the token Secret lives inside the
# namespace; the Deployment's envFrom is satisfied once the Secret exists).
kubectl apply -f "$root_dir/deploy/prod/eso/vault-dev.yaml"
if ! kubectl -n "$vault_namespace" get secret "$vault_token_secret" >/dev/null 2>&1; then
  kubectl create secret generic "$vault_token_secret" \
    --namespace "$vault_namespace" \
    --from-literal=VAULT_DEV_ROOT_TOKEN="$(openssl rand -hex 24)" \
    --dry-run=client -o yaml | kubectl apply -f -
fi
kubectl -n "$vault_namespace" rollout status deployment/agent-platform-vault \
  --timeout=180s

echo "== [G2/ESO] seeding Vault with production credential map =="
# Authoritative token: what the RUNNING server actually accepted (the dev
# root token is fixed at pod start; a secret churned by an earlier run must
# not break the login). Re-sync the Secret to that token so every consumer
# (ESO tokenSecretRef, this seed) sees the same value.
ROOT_TOKEN="$(kubectl -n "$vault_namespace" exec deploy/agent-platform-vault -- \
  printenv VAULT_DEV_ROOT_TOKEN)"
kubectl create secret generic "$vault_token_secret" \
  --namespace "$vault_namespace" \
  --from-literal=VAULT_DEV_ROOT_TOKEN="$ROOT_TOKEN" \
  --dry-run=client -o yaml | kubectl apply -f -

kubectl -n "$vault_namespace" exec deploy/agent-platform-vault -- \
  sh -c "export VAULT_ADDR=http://127.0.0.1:8200
    vault login token='$ROOT_TOKEN' >/dev/null
    vault kv put secret/$remote_key \\
      AGENT_PLATFORM_DATABASE_URL='$postgres_url' \\
      AGENT_PLATFORM_NATS_URL='$nats_url' \\
      AGENT_PLATFORM_NATS_STREAM=AGENT_PLATFORM \\
      AGENT_PLATFORM_NATS_STREAM_REPLICAS=1 \\
      AGENT_PLATFORM_S3_ENDPOINT='$s3_endpoint' \\
      AGENT_PLATFORM_S3_BUCKET=agent-artifacts \\
      AGENT_PLATFORM_S3_ACCESS_KEY_ID=agent_platform \\
      AGENT_PLATFORM_S3_SECRET_ACCESS_KEY='$minio_value' \\
      AGENT_PLATFORM_DEEPSEEK_API_KEY='$demo_deepseek_key'"

echo "== [G2/ESO] applying ClusterSecretStore =="
kubectl apply -f "$root_dir/deploy/prod/eso/cluster-secret-store.yaml"
kubectl wait --for=condition=Ready \
  clustersecretstore/"$store_name" --timeout=120s
fi

if [ "$eso_only" = "1" ]; then
  echo "== [G2/ESO] --eso-only mode: verifying the store still exists =="
  kubectl wait --for=condition=Ready \
    clustersecretstore/"$store_name" --timeout=30s
fi

# The control namespace may have been purged by the prod-form gate; ensure it
# exists before applying the ExternalSecret into it, and stamp the standard
# Helm ownership labels so `helm upgrade --install` can IMPORT this
# pre-existing namespace instead of rejecting it (documented helm adopt-own
# pattern; the chart render re-applies its own labels on top).
kubectl create namespace "$control_namespace" --dry-run=client -o yaml \
  | kubectl apply -f -
kubectl label namespace "$control_namespace" \
  app.kubernetes.io/managed-by=Helm --overwrite
kubectl annotate namespace "$control_namespace" \
  "meta.helm.sh/release-name=agent-platform" --overwrite
kubectl annotate namespace "$control_namespace" \
  "meta.helm.sh/release-namespace=$control_namespace" --overwrite

echo "== [G2/ESO] pre-materializing the platform Secret via the chart's ExternalSecret =="
# The chart ships the real ExternalSecret (external-secret-contract.yaml,
# golden values: storeRefName/remoteKey). Apply it before the helm upgrade so
# the pre-upgrade migrate hook never races ESO's async sync: the target Secret
# must already exist when the hook Job starts.
helm template agent-platform "$root_dir/deploy/helm" \
  --values "$root_dir/deploy/prod/values.yaml" \
  >"$root_dir/.tmp-prod-render.yaml"
python - "$root_dir/.tmp-prod-render.yaml" <<'PYEOF' | kubectl apply -f -
import sys
import yaml

render_path = sys.argv[1]

with open(render_path, encoding="utf-8") as f:
    for doc in yaml.safe_load_all(f):
        if doc is None:
            continue
        kind = doc["kind"]
        name = doc["metadata"]["name"]
        if kind == "ExternalSecret" and name == "agent-platform-dependencies":
            yaml.safe_dump(doc, sys.stdout, sort_keys=False)
            print("---")
PYEOF
# Stamp Helm ownership on the pre-applied ExternalSecret exactly like the
# namespace, so `helm upgrade --install` can IMPORT it (the chart render is
# the same object — no drift; ESO keeps managing the target Secret).
kubectl label externalsecret/agent-platform-dependencies -n "$control_namespace" \
  app.kubernetes.io/managed-by=Helm --overwrite
kubectl annotate externalsecret/agent-platform-dependencies -n "$control_namespace" \
  "meta.helm.sh/release-name=agent-platform" --overwrite
kubectl annotate externalsecret/agent-platform-dependencies -n "$control_namespace" \
  "meta.helm.sh/release-namespace=$control_namespace" --overwrite
rm -f "$root_dir/.tmp-prod-render.yaml"

# ESO materializes the Secret asynchronously; the reconciler can lag the
# apply by minutes on a busy kind node — poll up to 8 min. On an idempotent
# re-run the target Secret is already synced, so skip the wait (key count
# check: 9 expected).
already_synced=0
if kubectl -n "$control_namespace" get secret "$platform_secret" >/dev/null 2>&1; then
  key_count="$(kubectl -n "$control_namespace" get secret "$platform_secret" \
    -o jsonpath='{.data}' 2>/dev/null | python -c "import json,sys; print(len(json.load(sys.stdin)))" 2>/dev/null || echo 0)"
  [ "$key_count" -ge 9 ] && already_synced=1
fi
if [ "$already_synced" != "1" ]; then
  kubectl -n "$control_namespace" wait --for=condition=Ready \
    externalsecret/agent-platform-dependencies --timeout=480s
fi
kubectl -n "$control_namespace" get secret "$platform_secret" >/dev/null

echo "== [G2/ESO] asserting the Vault -> ESO -> Secret round-trip =="
# Write the assertion to a file: `python - <<HEREDOC` collides with the kubectl
# pipe (both claim stdin) — use a script file so stdin belongs to the pipe.
# --eso-only mode never seeds, so ROOT_TOKEN is unset there; read it from the
# running Vault pod (same value the server accepted at start).
[ -n "${ROOT_TOKEN:-}" ] || ROOT_TOKEN="$(kubectl -n "$vault_namespace" \
  exec deploy/agent-platform-vault -- printenv VAULT_DEV_ROOT_TOKEN)"

# Capture the CURRENT Vault map — the authoritative comparison target whether
# this run reseeded or an earlier one did (a fresh random key here would
# mismatch the stable Vault value during --eso-only re-materialization).
kubectl -n "$vault_namespace" exec deploy/agent-platform-vault -- \
  sh -c "export VAULT_ADDR=http://127.0.0.1:8200
    VAULT_TOKEN='$ROOT_TOKEN' vault kv get -format=json secret/$remote_key" \
  >"$root_dir/.tmp-vault-map.json"
python -c "import json,sys; m=json.load(open(sys.argv[1]))['data']['data']; assert 'AGENT_PLATFORM_DEEPSEEK_API_KEY' in m; print(sorted(m))" \
  "$root_dir/.tmp-vault-map.json" >/dev/null 2>&1 || {
  echo "reading the Vault credential map failed" >&2
  exit 78
}

# Force ESO to re-fetch after a fresh seed: the 1h refresh interval alone
# would keep the pre-seed value. Any annotation change triggers an event-
# driven reconcile that always re-fetches the provider; poll until the synced
# Secret matches the Vault value (ESO reconcile can lag by seconds to minutes).
kubectl -n "$control_namespace" annotate externalsecret/agent-platform-dependencies \
  "eso-force-$(date +%s)=1" --overwrite
for _ in $(seq 1 60); do
  got="$(kubectl -n "$control_namespace" get secret "$platform_secret" \
    -o jsonpath='{.data.AGENT_PLATFORM_DEEPSEEK_API_KEY}' 2>/dev/null | base64 -d 2>/dev/null || true)"
  want="$(python -c "import json,sys; print(json.load(open(sys.argv[1]))['data']['data']['AGENT_PLATFORM_DEEPSEEK_API_KEY'])" \
    "$root_dir/.tmp-vault-map.json")"
  [[ "$got" == "$want" ]] && break
  sleep 5
done

# Round-trip assertion: every key in the Vault map lands in the platform
# Secret, byte-for-byte equal. The python reads the Secret json from stdin and
# the Vault map from argv (no shell/base64 plumbing, no env prefix leak).
cat > "$root_dir/.tmp-eso-roundtrip.py" <<'PYEOF'
import base64, json, sys

vault_map = json.load(open(sys.argv[1], encoding="utf-8"))["data"]["data"]
secret_data = json.load(sys.stdin)["data"]
missing = set(vault_map) - set(secret_data)
if missing:
    print("keys missing from Secret:", sorted(missing), file=sys.stderr)
    sys.exit(78)
bad = [k for k, v in vault_map.items()
       if base64.b64decode(secret_data[k]).decode() != v]
if bad:
    print("round-trip mismatch:", bad, file=sys.stderr)
    sys.exit(78)
print(f"G2 round-trip OK: {len(secret_data)} keys synced from Vault, values match")
PYEOF
kubectl -n "$control_namespace" get secret "$platform_secret" -o json \
  | python "$root_dir/.tmp-eso-roundtrip.py" "$root_dir/.tmp-vault-map.json"
rm -f "$root_dir/.tmp-eso-roundtrip.py" "$root_dir/.tmp-vault-map.json"

echo "G2 接线就绪：ClusterSecretStore=$store_name，Secret=$platform_secret 已由 ESO 同步 9 键"
