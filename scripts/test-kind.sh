#!/usr/bin/env bash
set -euo pipefail

root_dir="$(cd -- "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

usage() {
  echo "Usage: test-kind.sh [--keep]"
  echo "Create a disposable Kind cluster and run the real Sandbox Attempt L3 gate."
}

keep_cluster="${KEEP_CLUSTER:-0}"
case "${1:-}" in
  --help|-h)
    usage
    exit 0
    ;;
  --keep)
    keep_cluster=1
    ;;
  "")
    ;;
  *)
    usage >&2
    exit 2
    ;;
esac

for command_name in docker kind kubectl helm uv; do
  command -v "$command_name" >/dev/null 2>&1 || {
    echo "Required command is missing: $command_name" >&2
    exit 69
  }
done

cluster_name="${AGENT_PLATFORM_KIND_CLUSTER:-agent-platform-e2e}"
registry_name="agent-platform-registry"
registry_created=0
rendered_chart="$(mktemp)"
build_suffix="$(date +%s)-$$"
control_repository="localhost:5001/enterprise-agent-platform/control-plane"
runtime_repository="localhost:5001/enterprise-agent-platform/runtime"
control_tag="${control_repository}:${build_suffix}"
runtime_tag="${runtime_repository}:${build_suffix}"

cleanup() {
  rm -f "$rendered_chart"
  if [ "$keep_cluster" != "1" ]; then
    kind delete cluster --name "$cluster_name" >/dev/null 2>&1 || true
    if [ "$registry_created" = "1" ]; then
      docker rm --force "$registry_name" >/dev/null 2>&1 || true
    fi
  else
    echo "Kind cluster retained: $cluster_name"
  fi
}
trap cleanup EXIT

if ! docker inspect "$registry_name" >/dev/null 2>&1; then
  docker run --detach --restart=always \
    --publish 127.0.0.1:5001:5000 \
    --publish '[::1]:5001:5000' \
    --name "$registry_name" registry:2
  registry_created=1
fi
# 等待 registry 就绪
for _ in $(seq 1 30); do
  curl -sf http://127.0.0.1:5001/v2/ >/dev/null 2>&1 && break
  sleep 1
done

if ! kind get clusters 2>/dev/null | grep -qx "$cluster_name"; then
  kind create cluster --name "$cluster_name" --config "$root_dir/deploy/kind/cluster.yaml"
else
  echo "Kind cluster already exists: $cluster_name (reusing)"
fi
kubectl config use-context "kind-$cluster_name" >/dev/null 2>&1 || true

# 等待所有节点容器完全就绪（避免 worker 容器尚未就绪导致 load 失败）
sleep 15

docker network connect kind "$registry_name" >/dev/null 2>&1 || true

if [ -f "$root_dir/deploy/kind/calico.yaml" ]; then
  # 节点 containerd 无法直连 Docker Hub，预加载 calico 镜像到 kind 节点
  kind load docker-image calico/cni:v3.29.3 calico/node:v3.29.3 \
    calico/kube-controllers:v3.29.3 --name "$cluster_name"
  kubectl apply -f "$root_dir/deploy/kind/calico.yaml"
else
  kubectl apply -f https://raw.githubusercontent.com/projectcalico/calico/v3.29.3/manifests/calico.yaml
fi
kubectl -n kube-system wait --for=condition=Ready pod -l k8s-app=calico-node --timeout=300s
kubectl -n kube-system wait --for=condition=Ready pod -l k8s-app=calico-kube-controllers --timeout=300s

# 预加载依赖镜像到 kind 节点（节点 containerd 无法直连 Docker Hub）
kind load docker-image \
  postgres:17-alpine \
  nats:2.11-alpine \
  minio/minio:RELEASE.2025-04-22T22-12-26Z \
  minio/mc:RELEASE.2025-04-16T18-13-26Z \
  --name "$cluster_name"

build_args=()
if [ -n "${UV_INDEX_URL:-}" ]; then
  build_args+=(--build-arg "UV_INDEX_URL=$UV_INDEX_URL")
fi

docker build "${build_args[@]}" -f "$root_dir/deploy/images/control-plane.Dockerfile" -t "$control_tag" "$root_dir"
docker build "${build_args[@]}" -f "$root_dir/deploy/images/runtime.Dockerfile" -t "$runtime_tag" "$root_dir"
docker push "$control_tag"
docker push "$runtime_tag"

# 预加载构建镜像到节点（migrate job 与 helm 部署使用 digest 引用，节点需本地可用）
kind load docker-image "$control_tag" "$runtime_tag" --name "$cluster_name"

control_ref="$(docker image inspect --format '{{index .RepoDigests 0}}' "$control_tag")"
runtime_ref="$(docker image inspect --format '{{index .RepoDigests 0}}' "$runtime_tag")"
control_digest="${control_ref##*@}"
runtime_digest="${runtime_ref##*@}"

"$root_dir/scripts/bootstrap-kind-dependencies.sh" --migration-image "$control_ref"

helm template agent-platform "$root_dir/deploy/helm" \
  --values "$root_dir/deploy/kind/values.yaml" \
  --set-string "images.controlPlane.repository=$control_repository" \
  --set-string "images.controlPlane.digest=$control_digest" \
  --set-string "images.runtime.repository=$runtime_repository" \
  --set-string "images.runtime.digest=$runtime_digest" \
  --set-string "secrets.externalSecretName=agent-platform-kind-dependencies" \
  >"$rendered_chart"

# The helm migrate Job is immutable once created; re-applying it against a
# reused cluster with a fresh digest fails ("field is immutable"). Delete it
# first, mirroring the dependency bootstrap's delete-then-recreate pattern.
kubectl -n agent-platform-control delete job agent-platform-migrate \
  --ignore-not-found --wait=true
kubectl apply -f "$rendered_chart"

# Kind gate network-policy patch: Calico enforces egress by post-DNAT
# destination, and in a Kind cluster the kube-apiserver is hostNetwork on the
# control-plane node (a node IP). The production `10.96.0.1/32` apiserver
# egress rules therefore blackhole the orchestrator's Batch client (its
# submit call stalls to the 30s timeout instead of failing fast). Add one
# orchestrator-scoped all-egress rule here; every other policy stays intact.
kubectl -n agent-platform-control apply -f - <<'EOF'
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

# Both control-plane processes must stay ONLINE for the L3 gate:
#  - agent-platform-api serves the public + Internal runtime API the Pod
#    bootstraps/heartbeats/commits against;
#  - agent-platform-orchestrator runs the scheduler that claims the RUNNING
#    attempt and submits the sandbox Job.
# (legacy replicas=0 here was wrong: with zero API replicas the Pod could
#  never bootstrap.)
kubectl -n agent-platform-control wait --for=condition=complete \
  job/agent-platform-migrate --timeout=300s
kubectl -n agent-platform-control rollout status deployment/agent-platform-api \
  --timeout=240s
kubectl -n agent-platform-control rollout status \
  deployment/agent-platform-orchestrator --timeout=240s

# Expose the Control-Plane API to the test host (in-cluster DNS is only
# resolvable inside the cluster, and the test runner lives on the host).
kubectl -n agent-platform-control port-forward svc/agent-platform-api \
  18080:8080 >/dev/null 2>&1 &
port_forward_pid=$!
cleanup() {
  kill "$port_forward_pid" >/dev/null 2>&1 || true
  rm -f "$rendered_chart"
  if [ "$keep_cluster" != "1" ]; then
    kind delete cluster --name "$cluster_name" >/dev/null 2>&1 || true
    if [ "$registry_created" = "1" ]; then
      docker rm --force "$registry_name" >/dev/null 2>&1 || true
    fi
  else
    echo "Kind cluster retained: $cluster_name"
  fi
}
trap cleanup EXIT
for _ in $(seq 1 30); do
  curl -sf http://127.0.0.1:18080/api/agent-platform/v1/health/ready \
    >/dev/null 2>&1 && break
  sleep 1
done

AGENT_PLATFORM_KIND=1 \
AGENT_PLATFORM_KIND_API_URL="http://127.0.0.1:18080" \
AGENT_PLATFORM_KIND_RUNTIME_IMAGE="$runtime_ref" \
  uv run --project "$root_dir/backend" python -m pytest \
    "$root_dir/backend/tests/kind/test_attempt_job.py"
