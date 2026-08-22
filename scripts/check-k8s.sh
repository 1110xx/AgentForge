#!/usr/bin/env bash
set -euo pipefail

root_dir="$(cd -- "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

usage() {
  echo "Usage: check-k8s.sh"
  echo "Static K8s gate: helm lint, chart rendering (kind + production values),"
  echo "and YAML parse of every rendered manifest."
}

case "${1:-}" in
  --help|-h)
    usage
    exit 0
    ;;
  "")
    ;;
  *)
    usage >&2
    exit 2
    ;;
esac

helm_command=""
for candidate in \
  "$root_dir/.tools/bin/helm" \
  "$root_dir/.tools/bin/helm.exe" \
  "$(command -v helm 2>/dev/null || true)"; do
  if [ -n "$candidate" ] && [ -x "$candidate" ] && "$candidate" version >/dev/null 2>&1; then
    helm_command="$candidate"
    break
  fi
done
if [ -z "$helm_command" ]; then
  echo "helm is required (install to .tools/bin/helm or PATH)" >&2
  exit 69
fi

python_command=""
for candidate in \
  "$root_dir/backend/.venv/bin/python" \
  "$root_dir/backend/.venv/Scripts/python.exe" \
  "$(command -v python3 2>/dev/null || true)" \
  "$(command -v python 2>/dev/null || true)"; do
  if [ -n "$candidate" ] && [ -x "$candidate" ] && \
     "$candidate" -c 'import yaml' >/dev/null 2>&1; then
    python_command="$candidate"
    break
  fi
done
if [ -z "$python_command" ]; then
  echo "python3/PyYAML is required (venv or system python with pyyaml)" >&2
  exit 69
fi

temporary_root="$(mktemp -d)"
cleanup() {
  rm -rf "$temporary_root"
}
trap cleanup EXIT

# helm lint validates the chart AND the values against the local
# values.schema.json (additionalProperties/required/pattern enforcement),
# with no cluster or network access required.
"$helm_command" lint "$root_dir/deploy/helm" >/dev/null
echo "helm lint passed (default values, schema validated)"
"$helm_command" lint "$root_dir/deploy/helm" --values "$root_dir/deploy/kind/values.yaml" >/dev/null
echo "helm lint passed (kind values, schema validated)"

render_kind() {
  "$helm_command" template agent-platform "$root_dir/deploy/helm" \
    --values "$root_dir/deploy/kind/values.yaml" \
    --set-string "images.controlPlane.repository=localhost:5001/enterprise-agent-platform/control-plane" \
    --set-string "images.controlPlane.digest=sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" \
    --set-string "images.runtime.repository=localhost:5001/enterprise-agent-platform/runtime" \
    --set-string "images.runtime.digest=sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb" \
    >"$temporary_root/kind.yaml"
}

render_prod() {
  "$helm_command" template agent-platform "$root_dir/deploy/helm" \
    >"$temporary_root/prod.yaml"
}

# Extended profile: exercise the optional Ingress (with TLS) and local scratch
# PVC templates so the gate covers more than the two default profiles.
render_extended() {
  "$helm_command" template agent-platform-ext "$root_dir/deploy/helm" \
    --values "$root_dir/deploy/kind/values.yaml" \
    --set-string "images.controlPlane.repository=localhost:5001/enterprise-agent-platform/control-plane" \
    --set-string "images.controlPlane.digest=sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" \
    --set-string "images.runtime.repository=localhost:5001/enterprise-agent-platform/runtime" \
    --set-string "images.runtime.digest=sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb" \
    --set "ingress.enabled=true" \
    --set "ingress.host=agent-platform.example.org" \
    --set "ingress.tls.enabled=true" \
    --set "ingress.tls.secretName=agent-platform-example-tls" \
    --set "persistence.localScratch.enabled=true" \
    >"$temporary_root/extended.yaml"
}

# Profile 4 (Phase 3.6): production frontend workload — nginx SPA + API
# reverse proxy ConfigMap, frontend Deployment/Service, and the ingress with
# the root path added (SSE buffering annotation present).
render_frontend() {
  "$helm_command" template agent-platform-fe "$root_dir/deploy/helm" \
    --values "$root_dir/deploy/kind/values.yaml" \
    --set-string "images.controlPlane.repository=localhost:5001/enterprise-agent-platform/control-plane" \
    --set-string "images.controlPlane.digest=sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" \
    --set-string "images.runtime.repository=localhost:5001/enterprise-agent-platform/runtime" \
    --set-string "images.runtime.digest=sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb" \
    --set-string "images.frontend.repository=localhost:5001/enterprise-agent-platform/frontend" \
    --set-string "images.frontend.digest=sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc" \
    --set "frontend.enabled=true" \
    --set "ingress.enabled=true" \
    --set "ingress.host=agent-platform.example.org" \
    --set "ingress.tls.enabled=true" \
    --set "ingress.tls.secretName=agent-platform-example-tls" \
    >"$temporary_root/frontend.yaml"
}

render_kind
echo "helm template (kind values) rendered"
render_prod
echo "helm template (production values) rendered"
render_extended
echo "helm template (extended: ingress+pvc) rendered"
render_frontend
echo "helm template (frontend profile) rendered"

"$python_command" - "$temporary_root/kind.yaml" "$temporary_root/prod.yaml" "$temporary_root/extended.yaml" "$temporary_root/frontend.yaml" <<'PY'
import sys

import yaml

for path in sys.argv[1:]:
    with open(path, encoding="utf-8") as handle:
        documents = [doc for doc in yaml.safe_load_all(handle) if doc]
    kinds = sorted({doc["kind"] for doc in documents})
    print(f"{path}: {len(documents)} manifests, kinds={','.join(kinds)}")

# Profile 4 assertions: frontend workload present with the nginx reverse
# proxy ConfigMap, and the ingress carries the SSE annotation + root path.
frontend = [
    doc
    for doc in yaml.safe_load_all(open(sys.argv[4], encoding="utf-8"))
    if doc
]
f_names = {doc.get("metadata", {}).get("name") for doc in frontend if doc.get("kind") == "Deployment"}
assert "agent-platform-frontend" in f_names, "frontend Deployment missing"
assert "agent-platform-api" in f_names, "api Deployment missing"
f_kinds = {doc["kind"] for doc in frontend}
assert {"ConfigMap", "Deployment", "Service", "Ingress"} <= f_kinds, f"frontend kinds: {f_kinds}"
ing = next(doc for doc in frontend if doc["kind"] == "Ingress")
anns = ing.get("metadata", {}).get("annotations", {})
assert anns.get("nginx.ingress.kubernetes.io/proxy-buffering") == "off", anns
paths = ing["spec"]["rules"][0]["http"]["paths"]
assert any(p["path"] == "/" for p in paths), "frontend root path missing in ingress"
conf = next(doc for doc in frontend if doc["kind"] == "ConfigMap")
assert "proxy_buffering off;" in conf["data"]["default.conf"]
print("frontend profile: api+frontend deployments, ConfigMap proxy, ingress SSE annotation OK")
PY

echo "k8s static gate passed"
