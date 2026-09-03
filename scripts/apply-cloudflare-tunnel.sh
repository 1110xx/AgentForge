#!/usr/bin/env bash
# Cloudflare named-tunnel -> in-cluster ingress (form B of
# deploy/runbooks/cloudflare-tunnel-demo.md). Idempotent; credentials only ever
# enter a k8s Secret (never the repo). Requires the interactive steps already
# done:  cloudflared tunnel login && cloudflared tunnel create <name> &&
#          cloudflared tunnel route dns <name> agent-platform.tyx-lab.online
# Usage: bash scripts/apply-cloudflare-tunnel.sh <tunnel-name> <credentials.json>
set -euo pipefail
NS="agent-platform-control"
TUNNEL_NAME="${1:-}"
CRED_FILE="${2:-}"
[ -n "$TUNNEL_NAME" ] && [ -f "$CRED_FILE" ] || {
  echo "usage: $0 <tunnel-name> <path-to-UUID.json>" >&2; exit 64; }

kubectl get ns "$NS" >/dev/null 2>&1 || { echo "namespace $NS missing (run the gate first)" >&2; exit 78; }

tmp="$(mktemp -d)"
printf 'tunnel: %s\ncredentials-file: /etc/cloudflared/credentials/credentials.json\n\ningress:\n  - hostname: agent-platform.tyx-lab.online\n    service: http://ingress-nginx-controller.ingress-nginx.svc.cluster.local:80\n  - service: http_status:404\n' \
  "$TUNNEL_NAME" > "$tmp/config.yml"

kubectl -n "$NS" create configmap cloudflared-config \
  --from-file=config.yml="$tmp/config.yml" --dry-run=client -o yaml | kubectl apply -f -
kubectl -n "$NS" create secret generic cloudflared-credentials \
  --from-file=credentials.json="$CRED_FILE" --dry-run=client -o yaml | kubectl apply -f -
kubectl apply -f "$(dirname "$0")/../deploy/kind/cloudflared-tunnel.yaml"

echo "== waiting for rollout =="
kubectl -n "$NS" rollout status deploy/cloudflared-tunnel --timeout=180s
echo "== tunnel logs (tail) =="
sleep 4
kubectl -n "$NS" logs deploy/cloudflared-tunnel --tail=15
echo "== teardown: kubectl -n $NS delete deploy cloudflared-tunnel (secret/configmap optional) =="
