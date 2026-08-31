#!/usr/bin/env bash
# Recovery-round runner: executes the observability gate and records its exit.
cd "$(cd -- "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)" || exit 9
if [ -f /tmp/grafana-probe.tar ]; then rm -f /tmp/grafana-probe.tar; fi
bash scripts/test-observability.sh > /tmp/obs6.log 2>&1
rc=$?
echo "OBS6_EXIT=$rc" >> /tmp/obs6.log
exit $rc