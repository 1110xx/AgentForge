#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# IT 故障工单排查（it-incident-investigate）公网演示脚本
#
# 对真实运行中的平台跑一条 incident:// 故障排查 Run：
#   创建 Run（scheme 门禁 live 放行 incident://）→ 轮询状态 → 拉事件确认
#   Agent 通过 internal read 端点读取真实工单/日志/指标（远程真模型）。
#
# 环境变量（均有默认）：
#   BASE_URL  控制面地址           默认 http://127.0.0.1:80/api/agent-platform/v1
#   HOST_HEADER 公网 Host（演示隧道）默认 agent-platform.tyx-lab.online
#   TOKEN     演示令牌             默认 reference-local-demo
#
# 用法: scripts/demo-it-incident.sh [--stream]
#   --stream  在 Run 终态后追加监听 SSE 事件流（Ctrl+C 退出）
#
# 与 SDD-it-incident-investigate-v1.5.md §12 的对应：
#   · incident:// 引用放行 = §12.1 偏差 #2（resolver 声明式 scheme 门禁）
#   · Agent 读到真实内容   = §12.1 偏差 #5 + §12.2.1（read 钩子 / wire 修复）
#   · 审批/驳回/rerun 演示 = §12.3 注明：live 生产链路只有 OPEN 提案、无
#     WAITING_APPROVAL 桥；完整审批闭环在共享服务层 harness（M3 测试
#     已证明）。本脚本不伪造该环节，仅在文件末尾给出动作端点说明。
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

BASE_URL="${BASE_URL:-http://127.0.0.1:80/api/agent-platform/v1}"
HOST_HEADER="${HOST_HEADER:-agent-platform.tyx-lab.online}"
TOKEN="${TOKEN:-reference-local-demo}"
TICKET_ID="${TICKET_ID:-T20260907}"
SERVICE="${SERVICE:-pay-service}"

stream_mode=0
for arg in "$@"; do
  case "$arg" in
    --stream) stream_mode=1 ;;
    --help|-h)
      grep '^#' "$0" | sed 's/^# \{0,1\}//' | head -20; exit 0 ;;
    *) echo "Unknown argument: $arg" >&2; exit 2 ;;
  esac
done

api() { # api <method> <path> [data]
  local method="$1" path="$2" data="${3:-}"
  local args=(-s -X "$method" "$BASE_URL$path" -H "Host: $HOST_HEADER" \
    -H "Authorization: Bearer $TOKEN")
  # /runs create 需要幂等键（平台门禁），其余 op 无副作用也无妨。
  if [ "$method" = "POST" ]; then
    args+=(-H "Idempotency-Key: demo-incident-$(date +%s)-$$")
  fi
  if [ -n "$data" ]; then
    args+=(-H "Content-Type: application/json" -d "$data")
  fi
  curl "${args[@]}"
}

PY="${PYTHON:-python3}"; command -v python3 >/dev/null 2>&1 || PY="python"

json_get() { "$PY" -c "import sys,json;d=json.load(sys.stdin);print($1)" 2>/dev/null || true; }

# intent 与 resource_refs 统一在 python 里序列化（bash 引号零转义）。
export INC_TICKET_ID="$TICKET_ID" INC_SERVICE="$SERVICE"
build_payload() {
  "$PY" - <<'PY'
import json, os
ticket = os.environ["INC_TICKET_ID"]
service = os.environ["INC_SERVICE"]
intent = (
    f"Investigate IT incident ticket {ticket} ({service} 504). "
    "Follow this exact script. "
    "STEP 1: call remote_read_tool once with "
    f"arguments={{\"resource_ref\":\"incident://ticket/{ticket}\"}}. "
    "STEP 2: call remote_read_tool once with "
    f"arguments={{\"resource_ref\":\"incident://logs/{service}\"}}. "
    "STEP 3: call remote_read_tool once with "
    f"arguments={{\"resource_ref\":\"incident://metrics/{service}\"}}. "
    "Do NOT repeat any read. "
    "STEP 4: write the markdown report to "
    f"/tmp/workspace/incident-report-{ticket}.md using file_write. "
    "STEP 5: publish it once with remote_publish_artifact: "
    f"workspace_path=/tmp/workspace/incident-report-{ticket}.md, "
    f"logical_name=incident-report-{ticket}.md, classification=report. "
    "Then reply with a short confirmation. Never call remote_propose_action."
)
print(json.dumps({
    "workflow_type": "it-incident-investigate",
    "intent": intent,
    "resource_refs": [f"incident://ticket/{ticket}"],
    "host_context_ref": "reference-context:demo",
}))
PY
}

payload="$(build_payload)"

echo "═══ 1. 创建 it-incident-investigate Run ═══"
resp=$(api POST /runs "$payload")
run_id=$(printf '%s' "$resp" | json_get "d.get('run_id') or d.get('error',{}).get('message','parse-failed')")
echo "run_id=$run_id"
if [ -z "$run_id" ] || [ "${#run_id}" -lt 12 ]; then
  echo "创建失败，原始响应："; printf '%s\n' "$resp"; exit 1
fi

echo
echo "═══ 2. 轮询 Run 至终态 ═══"
for i in $(seq 1 30); do
  status=$(api GET "/runs/$run_id" | json_get "d.get('view',{}).get('status','')")
  echo "  [$i] $status"
  case "$status" in
    SUCCEEDED|FAILED|CANCELLED) break ;;
  esac
  sleep 10
done

echo
echo "═══ 3. 事件证据（确认 Agent 是否发生真实 incident:// 读取）═══"
api GET "/runs/$run_id/events?limit=100" | "$PY" -c "
import sys, json
from collections import Counter
d = json.load(sys.stdin)
evs = d.get('events', [])
print('events total:', len(evs))
for k, c in Counter(e['payload'].get('kind', '?') for e in evs).most_common():
    print(f'  {c:>3}  {k}')
"
snap=$(api GET "/runs/$run_id")
echo "$snap" | "$PY" -c "
import sys, json
v = json.load(sys.stdin).get('view', {})
print('final status:', v.get('status'))
print('artifacts:', len(v.get('artifacts', [])), 'approvals:', len(v.get('approvals', [])))
print('intent:', (v.get('intent') or '')[:160])
"

echo
echo "═══ 4. 人工评审说明（SDD v1.3 §12.3）═══"
cat <<EOF
live 生产链路中 remote_propose_action 只落 OPEN 提案，没有
WAITING_APPROVAL + ApprovalCard 桥（也未部署 effect executor），因此
本演示不伪造“审批后闭环”：

  · 完整 approve→Effect→外部交接 / reject→rerun(round2) 闭环：
    reference harness + 共享服务层（M3 测试 tests/test_incident_closure.py）。
  · 动作端点（当 Run 暴露审批 surface 时由 UI 调用）：
      POST $BASE_URL/runs/\$run_id/actions
      {surface_id, surface_revision, action_ref: "approval:{id}:approve|reject",
       client_action_id, displayed_digest}
EOF

if [ "$stream_mode" = "1" ]; then
  echo
  echo "═══ 5. SSE 事件流（Ctrl+C 退出）═══"
  curl -sN "$BASE_URL/runs/$run_id/events/stream" -H "Host: $HOST_HEADER" \
    -H "Authorization: Bearer $TOKEN"
fi
