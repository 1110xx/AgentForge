"""Phase 4.4 (G8) API load injector.

Drives the deployed Control-Plane API with parallel create_run + poll
traffic so CPU climbs toward the HPA 70%% utilization target. Purely
reference-local (synthetic-analysis workflow, reference token), so it runs
against the Kind cluster without any external model dependency.

Usage:
    LOAD_SECONDS=180 LOAD_WORKERS=24 python scripts/api-load.py
Tunables (env): LOAD_URL (default http://127.0.0.1:18080),
LOAD_SECONDS, LOAD_WORKERS, LOAD_PAUSE.
"""
from __future__ import annotations

import concurrent.futures
import os
import threading
import time
import uuid

import httpx

BASE = os.environ.get("LOAD_URL", "http://127.0.0.1:18080")
HEADERS = {"Authorization": "Bearer reference-local-demo"}
DURATION = float(os.environ.get("LOAD_SECONDS", "180"))
WORKERS = int(os.environ.get("LOAD_WORKERS", "24"))
PAUSE = float(os.environ.get("LOAD_PAUSE", "0.2"))


def one_worker(i: int) -> int:
    end = time.monotonic() + DURATION
    created = 0
    with httpx.Client(base_url=BASE, timeout=30.0, trust_env=False) as c:
        while time.monotonic() < end:
            try:
                r = c.post(
                    "/v1/runs",
                    headers={
                        **HEADERS,
                        "Idempotency-Key": f"load-{i}-{uuid.uuid4().hex[:12]}",
                    },
                    json={
                        "workflow_type": "synthetic-analysis",
                        "intent": "Phase 4.4 load injection (CPU pressure)",
                        "resource_refs": ["synthetic-case:case-42"],
                        "parameters": {"analysis_mode": "summary", "max_items": 2},
                        "host_context_ref": "reference-context:demo",
                    },
                )
                if r.status_code == 201:
                    created += 1
                    rid = r.json()["run_id"]
                    for _ in range(3):
                        c.get(f"/v1/runs/{rid}", headers=HEADERS)
                        time.sleep(0.4)
            except httpx.HTTPError:
                pass
            time.sleep(PAUSE)
    return created


def main() -> None:
    t0 = time.time()
    with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as ex:
        results = list(ex.map(one_worker, range(WORKERS)))
    print(
        f"LOAD DONE: {WORKERS} workers x {DURATION}s -> {sum(results)} runs created "
        f"({time.time() - t0:.0f}s wall)"
    )


if __name__ == "__main__":
    main()