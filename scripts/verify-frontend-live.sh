#!/usr/bin/env bash
# Verify the Phase 3.6 frontend entry (POST /v1/chat) against a REAL backend,
# twice: direct uvicorn (backend reference local stack) and through the vite
# dev proxy (/api/agent-platform -> 127.0.0.1:8080 with prefix rewrite).
#
# Static-only gate: no docker, no cluster. Requires: backend/.venv with
# uvicorn, frontend node_modules with vite, curl. Use on git-bash/MSYS or WSL.
#
# Usage:  bash scripts/verify-frontend-live.sh [BACKEND_PORT] [VITE_PORT]
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKEND_PORT="${1:-8080}"
VITE_PORT="${2:-5173}"
BASE_API="http://127.0.0.1:${BACKEND_PORT}"
VITE_API="http://127.0.0.1:${VITE_PORT}/api/agent-platform"
TOKEN="Bearer reference-local-demo"

PASS=0
FAIL=0
declare -a PIDS=()

cleanup() {
  for pid in "${PIDS[@]:-}"; do
    kill "$pid" 2>/dev/null || true
  done
}
trap cleanup EXIT

say()  { printf '%s\n' "$@"; }
pass() { say "  OK   $1"; PASS=$((PASS + 1)); }
fail() { say "  FAIL $1"; FAIL=$((FAIL + 1)); }

backend_up() {
  local code
  code="$(curl -s -o /dev/null -w "%{http_code}" -X POST \
    "${BASE_API}/v1/chat" \
    -H "Idempotency-Key: probe-live" \
    -H "Content-Type: application/json" -d '{"message":"probe"}' \
    2>/dev/null || true)"
  # 401 (unauthenticated) / 201 (authenticated) / 422 both mean the backend is up.
  [ "$code" = "401" ] || [ "$code" = "201" ] || [ "$code" = "422" ]
}

vite_up() {
  curl -s -o /dev/null "http://127.0.0.1:${VITE_PORT}" 2>/dev/null
}

wait_until() {
  local n=0
  while ! "$1"; do
    n=$((n + 1))
    [ "$n" -ge "${2:-40}" ] && return 1
    sleep 0.5
  done
  return 0
}

say "== frontend live verification (Phase 3.6 F-C) =="
say "backend: ${BASE_API}  vite: ${VITE_API}"

## 1. ensure a real backend is listening
if backend_up; then
  say "backend already up on ${BASE_API}"
else
  say "starting backend (reference local stack) on ${BACKEND_PORT}…"
  (
    cd "${ROOT}/backend" &&
      .venv/Scripts/python -m uvicorn \
        enterprise_agent_platform.reference.local_stack:create_app \
        --host 127.0.0.1 --port "${BACKEND_PORT}" \
        >"${ROOT}/scripts/.verify-live-backend.log" 2>&1
  ) &
  PIDS+=($!)
  if wait_until backend_up 40; then
    pass "backend started and healthy"
  else
    fail "backend did not become ready (log: scripts/.verify-live-backend.log)"
    exit 1
  fi
fi

## 2. direct POST /v1/chat (single request: body + status)
say "direct /v1/chat:"
CHAT_FILE="${ROOT}/scripts/.verify-live-chat.json"
STATUS="$(curl -s -o "${CHAT_FILE}" -w "%{http_code}" -X POST \
  "${BASE_API}/v1/chat" \
  -H "Authorization: ${TOKEN}" \
  -H "Idempotency-Key: live-direct-1" \
  -H "Content-Type: application/json" \
  -d '{"message":"analyze failure patterns"}')"
CHAT_BODY="$(cat "${CHAT_FILE}")"
if [ "$STATUS" = "201" ] && echo "$CHAT_BODY" | grep -q "run-view-snapshot/v1"; then
  RUN_ID="$(echo "$CHAT_BODY" | sed -n 's/.*"run_id":"\([a-zA-Z0-9_-]*\)".*/\1/p' | head -1)"
  pass "direct 201 + snapshot (run_id=${RUN_ID})"
elif [ "$STATUS" != "201" ]; then
  fail "direct chat status ${STATUS} (body: ${CHAT_BODY})"
else
  fail "direct chat body missing run-view-snapshot/v1"
fi

## 3. idempotency replay (same key + same body -> same run)
REPLAY="$(curl -s -X POST "${BASE_API}/v1/chat" \
  -H "Authorization: ${TOKEN}" \
  -H "Idempotency-Key: live-direct-1" \
  -H "Content-Type: application/json" \
  -d '{"message":"analyze failure patterns"}')"
if [ "$REPLAY" = "$CHAT_BODY" ]; then
  pass "idempotent replay returns the same run"
else
  fail "idempotent replay mismatch"
fi

## 4. blank message rejected (contract parity with the frontend Launcher)
BLANK_STATUS="$(curl -s -o /dev/null -w "%{http_code}" -X POST \
  "${BASE_API}/v1/chat" \
  -H "Authorization: ${TOKEN}" \
  -H "Idempotency-Key: live-direct-blank" \
  -H "Content-Type: application/json" \
  -d '{"message":"   "}')"
if [ "$BLANK_STATUS" = "422" ]; then
  pass "blank message rejected (422)"
else
  fail "blank message status ${BLANK_STATUS}"
fi

## 5. through the vite dev proxy
say "vite dev proxy /v1/chat:"
if ! vite_up; then
  say "starting vite dev for @platform/embedded-host-example…"
  (
    cd "${ROOT}/frontend" &&
      npm run dev -w @platform/embedded-host-example \
        -- --port "${VITE_PORT}" --strictPort \
        >"${ROOT}/scripts/.verify-live-vite.log" 2>&1
  ) &
  PIDS+=($!)
  if ! wait_until vite_up 60; then
    fail "vite dev did not become ready (log: scripts/.verify-live-vite.log)"
    exit 1
  fi
fi
PROXY_STATUS="$(curl -s -o /dev/null -w "%{http_code}" -X POST \
  "${VITE_API}/v1/chat" \
  -H "Authorization: ${TOKEN}" \
  -H "Idempotency-Key: live-proxy-1" \
  -H "Content-Type: application/json" \
  -d '{"message":"chat via dev proxy"}')"
if [ "$PROXY_STATUS" = "201" ]; then
  pass "proxy 201 (SSE-unbuffered path exercised)"
else
  fail "proxy chat status ${PROXY_STATUS}"
fi

say ""
say "== result: ${PASS} passed, ${FAIL} failed =="
[ "$FAIL" -eq 0 ]