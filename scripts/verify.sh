#!/usr/bin/env bash
set -euo pipefail

root_dir="$(cd -- "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

mode="${1:-l1}"

usage() {
  echo "Usage: $0 {l1|l2|l3|all}"
  echo "  l1  Frozen local unit, contract, portability, wheel and frontend gates"
  echo "  l2  Disposable Docker Compose dependency integration gate"
  echo "  l3  Disposable Kind Sandbox Attempt gate"
  echo "  all Run l1, l2 and l3 in order"
}

unverified() {
  echo "UNVERIFIED: $*" >&2
  exit 69
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || unverified "required command is missing: $1"
}

require_docker_daemon() {
  require_command docker
  docker info >/dev/null 2>&1 || unverified "Docker daemon is unavailable"
}

# Locate the project venv python on POSIX or Windows layouts.
venv_python() {
  if [ -x "$root_dir/backend/.venv/bin/python" ]; then
    echo "$root_dir/backend/.venv/bin/python"
  elif [ -x "$root_dir/backend/.venv/Scripts/python.exe" ]; then
    echo "$root_dir/backend/.venv/Scripts/python.exe"
  fi
}

run_wheel_smoke() {
  local temporary_root wheel_paths python venv_py
  temporary_root="$(mktemp -d)"
  cleanup_wheel_smoke() {
    rm -rf "$temporary_root"
  }
  trap cleanup_wheel_smoke EXIT

  python="$(venv_python)"
  "$python" -m build \
    --wheel --no-isolation --outdir "$temporary_root/dist" "$root_dir/backend"

  wheel_paths=("$temporary_root"/dist/*.whl)
  if [ "${#wheel_paths[@]}" -ne 1 ]; then
    echo "wheel build did not produce exactly one artifact" >&2
    return 1
  fi

  uv venv "$temporary_root/venv"
  uv export --quiet --project "$root_dir/backend" --frozen --no-dev \
    --no-emit-project --output-file "$temporary_root/runtime-requirements.txt"
  if [ -x "$temporary_root/venv/bin/python" ]; then
    venv_py="$temporary_root/venv/bin/python"
  else
    venv_py="$temporary_root/venv/Scripts/python.exe"
  fi
  "$venv_py" -m pip install --require-hashes \
    -r "$temporary_root/runtime-requirements.txt" \
    --no-deps "${wheel_paths[0]}"
  "$venv_py" -I "$root_dir/scripts/wheel-smoke.py" \
    --forbid-source "$root_dir/backend/src"
  trap - EXIT
}

run_l1() {
  require_command uv
  require_command npm
  require_command diff

  uv sync --project "$root_dir/backend" --frozen

  if [ -d "$root_dir/backend/tests" ]; then
    python="$(venv_python)"
    EAP_FULL_PORTABILITY=1 "$python" -m pytest \
      "$root_dir/backend/tests" \
      --ignore="$root_dir/backend/tests/integration" \
      --ignore="$root_dir/backend/tests/kind"
    "$python" -m ruff check \
      "$root_dir/backend/src" "$root_dir/backend/tests" "$root_dir/scripts"
  else
    echo "note: backend/tests is missing from this reconstruction; running ruff on src/scripts only"
    "$(venv_python)" -m ruff check "$root_dir/backend/src" "$root_dir/scripts"
  fi

  "$root_dir/scripts/check-generated.sh"
  "$root_dir/scripts/check-k8s.sh"
  "$(venv_python)" "$root_dir/scripts/check-portability.py"
  run_wheel_smoke

  if [ -f "$root_dir/frontend/package-lock.json" ]; then
    npm --prefix "$root_dir/frontend" ci
    npm --prefix "$root_dir/frontend" test -- --run
    npm --prefix "$root_dir/frontend" run typecheck
    npm --prefix "$root_dir/frontend" run lint
    npm --prefix "$root_dir/frontend" run build
  else
    echo "note: frontend package-lock.json is missing from this reconstruction; skipping frontend gates"
  fi

  echo "[L1] import, path, secret and shell boundary checks"
  for script in "$root_dir"/scripts/*.sh; do
    bash -n "$script"
  done
  echo "L1 VERIFIED"
}

run_l2() {
  require_docker_daemon
  require_command openssl
  echo "[L2] disposable PostgreSQL, NATS and MinIO integration"
  "$root_dir/scripts/test-compose.sh"
  echo "L2 VERIFIED"
}

run_l3() {
  require_docker_daemon
  for command_name in kind kubectl helm uv; do
    require_command "$command_name"
  done
  echo "[L3] disposable Kind Sandbox Attempt"
  "$root_dir/scripts/test-kind.sh"
  echo "L3 VERIFIED"
}

case "$mode" in
  --help|-h)
    usage
    exit 0
    ;;
  l1)
    run_l1
    ;;
  l2)
    run_l2
    ;;
  l3)
    run_l3
    ;;
  all)
    run_l1
    run_l2
    run_l3
    ;;
  *)
    usage >&2
    exit 2
    ;;
esac
