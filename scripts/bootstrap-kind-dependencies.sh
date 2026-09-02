#!/usr/bin/env bash
set -euo pipefail

root_dir="$(cd -- "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

usage() {
  echo "Usage: bootstrap-kind-dependencies.sh [--migration-image REPOSITORY@sha256:DIGEST]"
  echo "Install ephemeral PostgreSQL, NATS and MinIO/versioning in the current Kind cluster."
}

migration_image=""
case "${1:-}" in
  --help|-h)
    usage
    exit 0
    ;;
  --migration-image)
    migration_image="${2:-}"
    ;;
  "")
    ;;
  *)
    usage >&2
    exit 2
    ;;
esac

for command_name in kubectl openssl; do
  command -v "$command_name" >/dev/null 2>&1 || {
    echo "Required command is missing: $command_name" >&2
    exit 69
  }
done

dependency_namespace="agent-platform-dependencies"
control_namespace="agent-platform-control"
sandbox_namespace="agent-platform-sandbox"

context="$(kubectl config current-context)"
if [[ "$context" != kind-* ]]; then
  echo "Refusing to mutate non-Kind context: $context" >&2
  exit 77
fi

kubectl create namespace "$dependency_namespace" --dry-run=client -o yaml | kubectl apply -f -
kubectl create namespace "$control_namespace" --dry-run=client -o yaml | kubectl apply -f -
kubectl create namespace "$sandbox_namespace" --dry-run=client -o yaml | kubectl apply -f -
kubectl label namespace "$control_namespace" \
  agent.platform/plane=control pod-security.kubernetes.io/enforce=restricted --overwrite
kubectl label namespace "$sandbox_namespace" \
  agent.platform/plane=sandbox pod-security.kubernetes.io/enforce=restricted --overwrite

# Reuse the existing secret's credentials when present: the Postgres/one-shot
# init runs only on first boot (emptyDir), so overwriting the secret with fresh
# random values on a re-run would make every dependent (migrate, api,
# orchestrator) fail password auth against the already-initialized data dir.
if kubectl -n "$dependency_namespace" get secret agent-platform-kind-dependencies \
  >/dev/null 2>&1; then
  postgres_value="$(kubectl -n "$dependency_namespace" get secret agent-platform-kind-dependencies \
    -o jsonpath='{.data.postgres-value}' | base64 -d)"
  minio_value="$(kubectl -n "$dependency_namespace" get secret agent-platform-kind-dependencies \
    -o jsonpath='{.data.minio-value}' | base64 -d)"
  capability_value="$(kubectl -n "$dependency_namespace" get secret agent-platform-kind-dependencies \
    -o jsonpath='{.data.AGENT_PLATFORM_CAPABILITY_KEY}' 2>/dev/null | base64 -d || true)"
else
  postgres_value="$(openssl rand -hex 24)"
  minio_value="$(openssl rand -hex 24)"
  capability_value=""
fi
# HMAC Runtime capability signing key (Phase 5 Step 1, security/runtime_tokens.py):
# stable across re-runs so existing API pods keep verifying bootstrap grants.
if [ -z "$capability_value" ]; then
  capability_value="$(openssl rand -hex 32)"
fi
database_url="postgresql+asyncpg://agent_platform:${postgres_value}@postgres.${dependency_namespace}.svc:5432/agent_platform"

# The Kind NATS is a single node (statefulset replicas=1); JetStream rejects
# replicas>1 in non-clustered mode, so pin R1 here while production values
# default to R3.
kubectl -n "$dependency_namespace" create secret generic agent-platform-kind-dependencies \
  --from-literal=postgres-value="$postgres_value" \
  --from-literal=minio-value="$minio_value" \
  --from-literal=AGENT_PLATFORM_CAPABILITY_KEY="$capability_value" \
  --from-literal=AGENT_PLATFORM_DATABASE_URL="$database_url" \
  --from-literal=AGENT_PLATFORM_NATS_URL="nats://nats.${dependency_namespace}.svc:4222" \
  --from-literal=AGENT_PLATFORM_NATS_STREAM=AGENT_PLATFORM \
  --from-literal=AGENT_PLATFORM_NATS_STREAM_REPLICAS=1 \
  --from-literal=AGENT_PLATFORM_S3_ENDPOINT="http://minio.${dependency_namespace}.svc:9000" \
  --from-literal=AGENT_PLATFORM_S3_BUCKET=agent-artifacts \
  --from-literal=AGENT_PLATFORM_S3_ACCESS_KEY_ID=agent_platform \
  --from-literal=AGENT_PLATFORM_S3_SECRET_ACCESS_KEY="$minio_value" \
  --dry-run=client -o yaml | kubectl apply -f -

# 在 control namespace 也创建同样的 secret（migrate job 在 control namespace，无法跨命名空间引用）
kubectl -n "$control_namespace" create secret generic agent-platform-kind-dependencies \
  --from-literal=postgres-value="$postgres_value" \
  --from-literal=minio-value="$minio_value" \
  --from-literal=AGENT_PLATFORM_CAPABILITY_KEY="$capability_value" \
  --from-literal=AGENT_PLATFORM_DATABASE_URL="$database_url" \
  --from-literal=AGENT_PLATFORM_NATS_URL="nats://nats.${dependency_namespace}.svc:4222" \
  --from-literal=AGENT_PLATFORM_NATS_STREAM=AGENT_PLATFORM \
  --from-literal=AGENT_PLATFORM_NATS_STREAM_REPLICAS=1 \
  --from-literal=AGENT_PLATFORM_S3_ENDPOINT="http://minio.${dependency_namespace}.svc:9000" \
  --from-literal=AGENT_PLATFORM_S3_BUCKET=agent-artifacts \
  --from-literal=AGENT_PLATFORM_S3_ACCESS_KEY_ID=agent_platform \
  --from-literal=AGENT_PLATFORM_S3_SECRET_ACCESS_KEY="$minio_value" \
  --dry-run=client -o yaml | kubectl apply -f -
unset postgres_value minio_value capability_value database_url

kubectl apply -f "$root_dir/deploy/kind/dependencies.yaml"

kubectl -n "$dependency_namespace" rollout status statefulset/postgres --timeout=240s
kubectl -n "$dependency_namespace" rollout status statefulset/minio --timeout=240s
kubectl -n "$dependency_namespace" wait --for=condition=complete job/minio-versioning --timeout=240s

if [ -n "$migration_image" ]; then
  if [[ ! "$migration_image" =~ @sha256:[0-9a-f]{64}$ ]]; then
    echo "Migration image must be digest pinned" >&2
    exit 65
  fi
  kubectl -n "$control_namespace" delete job agent-platform-kind-bootstrap-migrate \
    --ignore-not-found --wait=true
  kubectl apply -f - <<EOF
apiVersion: batch/v1
kind: Job
metadata:
  name: agent-platform-kind-bootstrap-migrate
  namespace: ${control_namespace}
spec:
  backoffLimit: 1
  activeDeadlineSeconds: 300
  template:
    metadata:
      labels:
        app.kubernetes.io/name: agent-platform-kind-bootstrap-migrate
    spec:
      restartPolicy: Never
      automountServiceAccountToken: false
      securityContext:
        runAsNonRoot: true
        seccompProfile:
          type: RuntimeDefault
      containers:
        - name: migrate
          image: ${migration_image}
          command: ["/app/backend/.venv/bin/alembic"]
          args: ["-c", "/app/backend/alembic.ini", "upgrade", "head"]
          envFrom:
            - secretRef:
                name: agent-platform-kind-dependencies
          resources:
            requests:
              cpu: 50m
              memory: 64Mi
            limits:
              cpu: 500m
              memory: 512Mi
          securityContext:
            allowPrivilegeEscalation: false
            readOnlyRootFilesystem: true
            capabilities:
              drop: ["ALL"]
          volumeMounts:
            - name: tmp
              mountPath: /tmp
      volumes:
        - name: tmp
          emptyDir:
            sizeLimit: 64Mi
EOF
  kubectl -n "$control_namespace" wait --for=condition=complete \
    job/agent-platform-kind-bootstrap-migrate --timeout=300s
fi

echo "Kind dependencies are ready; generated values exist only in ephemeral Kubernetes Secrets."
