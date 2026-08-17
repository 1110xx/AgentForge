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
if [ -x "$root_dir/backend/.venv/bin/python" ]; then
  python_command="$root_dir/backend/.venv/bin/python"
elif [ -x "$root_dir/backend/.venv/Scripts/python.exe" ]; then
  python_command="$root_dir/backend/.venv/Scripts/python.exe"
else
  echo "backend virtualenv is required" >&2
  exit 69
fi

temporary_root="$(mktemp -d)"
cleanup() {
  rm -rf "$temporary_root"
}
trap cleanup EXIT

"$helm_command" lint "$root_dir/deploy/helm" >/dev/null
echo "helm lint passed"

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

render_kind
echo "helm template (kind values) rendered"
render_prod
echo "helm template (production values) rendered"

"$python_command" - "$temporary_root/kind.yaml" "$temporary_root/prod.yaml" <<'PY'
import sys

import yaml

for path in sys.argv[1:]:
    with open(path, encoding="utf-8") as handle:
        documents = [doc for doc in yaml.safe_load_all(handle) if doc]
    kinds = sorted({doc["kind"] for doc in documents})
    print(f"{path}: {len(documents)} manifests, kinds={','.join(kinds)}")
PY

echo "k8s static gate passed"
