#!/usr/bin/env bash
set -euo pipefail

root_dir="$(cd -- "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

temporary_root="$(mktemp -d)"
cleanup() {
  rm -rf "$temporary_root"
}
trap cleanup EXIT

if [ -x "$root_dir/backend/.venv/bin/python" ]; then
  python_command=("$root_dir/backend/.venv/bin/python")
elif [ -x "$root_dir/backend/.venv/Scripts/python.exe" ]; then
  python_command=("$root_dir/backend/.venv/Scripts/python.exe")
elif command -v uv >/dev/null 2>&1; then
  python_command=(uv run --project "$root_dir/backend" python)
else
  exit 69
fi

"${python_command[@]}" "$root_dir/scripts/generate-contracts.py" \
  --output-root "$temporary_root/contracts"

diff -ru "$root_dir/contracts/schemas" "$temporary_root/contracts/schemas"
diff -u "$root_dir/contracts/openapi.json" "$temporary_root/contracts/openapi.json"
echo "generated contract parity passed"
