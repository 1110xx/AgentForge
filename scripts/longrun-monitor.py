"""Phase 4.4 (G8) long-run snapshot-growth sampler.

Every ``INTERVAL`` seconds it records: run status (API) + the latest
checkpoint seq (turn count) and its ``workflow_cursor`` JSONB byte size
(cluster-internal Postgres) for the monitored run. Output lines go to
stdout/redirect as TSV:  epoch  status  checkpoint_seq  cursor_bytes.

Usage:
    LONG_RUN_ID=run_.... python scripts/longrun-monitor.py > /tmp/longrun.tsv
"""
from __future__ import annotations

import os
import subprocess
import time

import httpx

RID = os.environ.get("LONG_RUN_ID", "").strip()
API = os.environ.get("LONG_RUN_API", "http://127.0.0.1:18080")
INTERVAL = float(os.environ.get("LONG_INTERVAL", "30"))
MAX_SECONDS = float(os.environ.get("LONG_MAX_SECONDS", "3600"))

PSQL = (
    "kubectl", "-n", "agent-platform-dependencies", "exec", "postgres-0", "--",
    "psql", "-U", "agent_platform", "-d", "agent_platform", "-t", "-A", "-c",
)


def sample() -> tuple[str, str, str]:
    status = ""
    with httpx.Client(base_url=API, timeout=30.0, trust_env=False) as c:
        r = c.get(
            f"/v1/runs/{RID}",
            headers={"Authorization": "Bearer reference-local-demo"},
        )
        status = r.json().get("status", "")
    query = (
        f"SELECT checkpoint_seq, pg_column_size(workflow_cursor) "
        f"FROM checkpoint WHERE run_id='{RID}' "
        f"ORDER BY checkpoint_seq DESC LIMIT 1;"
    )
    row = subprocess.run(PSQL + (query,), capture_output=True, text=True).stdout.strip()
    seq, size = (row.split("|") + ["", ""])[:2]
    return status, seq or "", size or ""


def main() -> None:
    assert RID, "LONG_RUN_ID is required"
    print("epoch\tstatus\tcheckpoint_seq\tcursor_bytes", flush=True)
    deadline = time.monotonic() + MAX_SECONDS
    while time.monotonic() < deadline:
        status, seq, size = sample()
        print(f"{int(time.time())}\t{status}\t{seq}\t{size}", flush=True)
        if status in {"SUCCEEDED", "FAILED", "CANCELLED"}:
            break
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()