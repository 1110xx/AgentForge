#!/usr/bin/env bash
# Recovery-round helper: show observability gate progress + final exit.
set -uo pipefail
log="${1:-/tmp/obs-gate2.log}"
if [ ! -f "$log" ]; then
  echo "NO LOG: $log"
  exit 3
fi
echo "== tail -6 =="
tail -6 "$log"
echo "== exit marker =="
grep -E "OBS_EXIT|PASS|FAIL|passed|failed" "$log" | tail -6