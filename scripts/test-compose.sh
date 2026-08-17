#!/usr/bin/env bash
set -euo pipefail

root_dir="$(cd -- "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

keep_stack="${KEEP_STACK:-0}"

usage() {
  echo "Usage: test-compose.sh [--keep]"
  echo "Run the disposable PostgreSQL/NATS/MinIO L2 integration gate."
}

case "${1:-}" in
  --help|-h)
    usage
    exit 0
    ;;
  --keep)
    keep_stack=1
    ;;
  "")
    ;;
  *)
    usage >&2
    exit 2
    ;;
esac

for command_name in docker openssl; do
  command -v "$command_name" >/dev/null 2>&1 || {
    echo "Required command is missing: $command_name" >&2
    exit 69
  }
done

if docker compose version >/dev/null 2>&1; then
  compose_cli=(docker compose)
elif command -v docker-compose >/dev/null 2>&1; then
  compose_cli=(docker-compose)
else
  echo "Docker Compose v2 is required" >&2
  exit 69
fi

compose_file="$root_dir/deploy/docker-compose.yml"
environment_file="$(mktemp)"
project_name="agent-platform-l2-${PPID}-$$"

cleanup() {
  if [ "$keep_stack" != "1" ]; then
    "${compose_cli[@]}" \
      --project-name "$project_name" \
      --env-file "$environment_file" \
      -f "$compose_file" \
      down --volumes --remove-orphans >/dev/null 2>&1 || true
  else
    echo "Compose project retained: $project_name"
  fi
  rm -f "$environment_file"
}
trap cleanup EXIT

postgres_value="$(openssl rand -hex 24)"
minio_value="$(openssl rand -hex 24)"
{
  printf 'POSTGRES_PASSWORD=%s\n' "$postgres_value"
  printf 'MINIO_ROOT_PASSWORD=%s\n' "$minio_value"
  printf 'POSTGRES_USER=agent_platform\n'
  printf 'POSTGRES_DB=agent_platform\n'
  printf 'MINIO_ROOT_USER=agent_platform\n'
  printf 'MINIO_BUCKET=agent-artifacts\n'
} >"$environment_file"
unset postgres_value minio_value

compose=(
  "${compose_cli[@]}"
  --project-name "$project_name"
  --env-file "$environment_file"
  -f "$compose_file"
  --profile test
)

"${compose[@]}" config --quiet
"${compose[@]}" up --detach --build --wait postgres nats minio
"${compose[@]}" run --rm --no-deps -T --build minio-init
"${compose[@]}" run --rm --no-deps -T --build migrate
"${compose[@]}" run --rm --no-deps -T --build test-runner
