#!/usr/bin/env bash
# Remove the half-applied observability namespace left by interrupted gate
# runs (failed pull rounds left sts/deploy/ds with zero pods), then re-run the
# observability gate from a clean slate and print its result.
set -uo pipefail
cd "$(cd -- "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)" || exit 9

kubectl delete ns agent-platform-observability --wait=true --timeout=120s >/dev/null 2>&1
echo "ns deleted (rc=$?)"

kubectl delete ns agent-platform-control --ignore-not-found >/dev/null 2>&1
echo "control ns reset for a clean helm import too"

bash scripts/test-observability.sh > /tmp/obs7.log 2>&1
rc=$?
echo "OBS7_EXIT=$rc" >> /tmp/obs7.log
tail -4 /tmp/obs7.log
exit $rc