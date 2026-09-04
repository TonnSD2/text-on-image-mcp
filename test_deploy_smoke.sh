#!/usr/bin/env bash
# Post-deploy smoke test for a running instance (proxy + auth included).
# Usage: BASE=https://mcp.example.com TOKEN=<one of TOI_USERS> bash test_deploy_smoke.sh
set -euo pipefail
: "${BASE:?set BASE=https://your-domain}"
: "${TOKEN:?set TOKEN=a valid token from TOI_USERS}"
INIT='{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"smoke","version":"1"}}}'

code_noauth=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$BASE/mcp" \
  -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' -d "$INIT")
[ "$code_noauth" = 401 ] && echo "1: unauthenticated -> 401 OK" || { echo "1: FAIL (got $code_noauth)"; exit 1; }

hdr=$(curl -s -D - -o /dev/null -X POST "$BASE/mcp?token=$TOKEN&scene=smoke" \
  -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' -d "$INIT")
echo "$hdr" | grep -qi 'mcp-session-id' && echo "2: handshake via TLS proxy OK" || { echo "2: FAIL (no session id)"; exit 1; }

# Long-lived SSE through the proxy: initialize the session then keep tools/list
t0=$(date +%s)
hdr_file=$(mktemp)
curl -s -D "$hdr_file" -o /dev/null -X POST "$BASE/mcp?token=$TOKEN&scene=smoke" \
  -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' -d "$INIT"
SID=$(grep -i '^mcp-session-id:' "$hdr_file" | tr -d '\r' | awk '{print $2}')
LIST='{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}'
n=$(curl -s -X POST "$BASE/mcp?token=$TOKEN&scene=smoke" -H "mcp-session-id: $SID" \
  -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' -d "$LIST" \
  | grep -oE '"name"\s*:' | wc -l | tr -d ' ')
[ "${n:-0}" -ge 36 ] && echo "3: tools/list through proxy -> $n tools OK ($(($(date +%s)-t0))s)" || { echo "3: FAIL ($n tools)"; exit 1; }

echo "DEPLOY SMOKE PASSED"