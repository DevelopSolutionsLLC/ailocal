#!/usr/bin/env bash
# diagnose-ttfb.sh — time to FIRST STREAMED BYTE, the number that explains
# "the client reported an API error but every layer says 200".
#
# ── the finding this script exists to make permanent [REAL, 2026-07-27] ──
# Identical request (a real captured 61-tool Claude Code payload), same model,
# same warm cache, gateway toggled:
#
#     gateway off      TTFB  95.4 s , 88.6 s
#     gateway filter   TTFB   1.0 s ,  0.18 s
#
# During those ~90 seconds LiteLLM sends NOTHING: Ollama is prompt-evaluating
# 24,448 tokens of tool schemas before the model emits its first token. The HTTP
# status is 200, the gateway completed, tool repair found nothing wrong — and the
# client sitting on the socket sees only silence. Any first-byte or idle timeout
# below that threshold surfaces as "API error" with no failing component
# anywhere in the stack.
#
# This is why the symptom was intermittent: it depends on payload size, whether
# the model is resident, and whether task negotiation classified the request into
# a smaller tool set.
#
# It also means the tool gateway is not only an efficiency feature. It is the
# difference between a ~90 s silent wait and a sub-second first byte.
#
# Usage: ./scripts/diagnose-ttfb.sh [--payload FILE]
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PAYLOAD="${2:-}"
KEY="$(grep -E '^LITELLM_MASTER_KEY=' .env | cut -d= -f2-)"
PROXY="${AILOCAL_PROXY_URL:-http://127.0.0.1:4000}"

if [ -z "$PAYLOAD" ]; then
  PAYLOAD=/tmp/ailocal-ttfb-req.json
  python3 - "$PAYLOAD" <<'PY'
import glob, json, os, sys
caps = sorted(glob.glob("data/tool-captures/*.json"), key=os.path.getsize)
if not caps:
    print("no captured payloads — run a real client with "
          "AILOCAL_TOOL_GATEWAY_CAPTURE set first", file=sys.stderr)
    raise SystemExit(1)
# The LARGEST real capture: the turn-1 declaration is the condition that hurts.
d = json.load(open(caps[-1]))
json.dump({"model": "ailocal-architecture", "max_tokens": 32, "stream": True,
           "messages": [{"role": "user", "content": "Say exactly: OK"}],
           "tools": d["tools"]}, open(sys.argv[1], "w"))
print(f"using a real capture: {len(d['tools'])} tools", file=sys.stderr)
PY
  [ -s "$PAYLOAD" ] || exit 1
fi

echo "══════════════════════════════════════════════════════════════════════"
echo " TIME TO FIRST STREAMED BYTE"
echo "══════════════════════════════════════════════════════════════════════"
echo " Payload: $PAYLOAD ($(wc -c < "$PAYLOAD") bytes)"
echo " Gateway: $(docker exec ailocal-litellm printenv AILOCAL_TOOL_GATEWAY 2>/dev/null || echo unknown)"
echo

OUT=/tmp/ailocal-ttfb-sse.txt
for i in 1 2; do
  T=$(curl -sN -m 900 -o "$OUT" -w '%{time_starttransfer}' "$PROXY/v1/messages" \
        -H "x-api-key: $KEY" -H 'anthropic-version: 2023-06-01' \
        -H 'content-type: application/json' -H 'user-agent: claude-cli/2.0.0' \
        --data @"$PAYLOAD")
  printf '  run %d  first byte after %ss\n' "$i" "$T"
done

echo
echo " SSE completeness (a stream can be slow AND malformed; check both):"
EV=$(grep -o '^event: [a-z_]*' "$OUT" | sed 's/event: //' | tr '\n' ' ')
echo "   events:      $EV"
if grep -q message_stop "$OUT"; then
  echo "   message_stop PRESENT — the stream terminated properly"
else
  echo "   message_stop MISSING — the client will wait forever; this is"
  echo "                anthropics/claude-code#54434, a different failure from a"
  echo "                slow first byte and must not be confused with it"
fi
grep -o '"stop_reason": "[a-z_]*"' "$OUT" | tail -1 | sed 's/^/   /'

echo
echo " Interpreting this:"
echo "   > ~60 s first byte  a client idle/first-byte timeout will fire while"
echo "                       every server-side component reports success."
echo "                       Enable the gateway: AILOCAL_TOOL_GATEWAY=filter"
echo "   < ~2 s              healthy."
