#!/usr/bin/env bash
# test-client-compatibility.sh — Phase H. Prove the gateway is transparent to
# every client dialect it sits behind, in every mode.
#
# The premise is that these clients do NOT behave identically, so nothing here is
# generalised from one route to another: each dialect is exercised on its own
# endpoint, with its own tool envelope, and asserted separately.
#
# The property under test is TRANSPARENCY, not reduction. A gateway that cuts the
# payload but breaks a client is a regression. So every case asserts the request
# still succeeds and still yields a usable response, in off / report / filter.
#
# HONEST SCOPE — read this before trusting a green run:
#   Claude Code  /v1/messages          real captured payload shape, real route
#   Codex        /v1/responses         real captured payload shape, real route
#   VS Code      /v1/chat/completions  route + headers exercised with a
#                                      SYNTHESISED payload. No real VS Code
#                                      session has been captured, so the client
#                                      profile drops nothing and this proves the
#                                      route works, NOT that the profile is right.
#
# Usage: ./scripts/tests/client-compatibility.sh
set -uo pipefail
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/harness.sh"
ROOT="$ROOT_DIR"
cd "$ROOT"
. scripts/lib/compose.sh

PROXY="${AILOCAL_PROXY_URL:-http://127.0.0.1:4000}"
KEY="$(grep -E '^LITELLM_MASTER_KEY=' .env 2>/dev/null | cut -d= -f2-)"
[ -n "$KEY" ] || { echo "No LITELLM_MASTER_KEY in .env"; exit 1; }

ORIGINAL_MODE="$(docker exec ailocal-litellm printenv AILOCAL_TOOL_GATEWAY 2>/dev/null || echo off)"

restore() {
  local rc=$?
  echo
  echo "==> restoring AILOCAL_TOOL_GATEWAY=$ORIGINAL_MODE"
  AILOCAL_TOOL_GATEWAY="$ORIGINAL_MODE" dc up -d >/dev/null 2>&1 || true
  for _ in $(seq 1 40); do
    [ "$(docker inspect ailocal-litellm --format '{{.State.Health.Status}}' \
        2>/dev/null || echo none)" = healthy ] && break
    sleep 3
  done
  exit $rc
}
trap restore EXIT INT TERM

set_mode() {
  AILOCAL_TOOL_GATEWAY="$1" dc up -d >/dev/null 2>&1
  for _ in $(seq 1 40); do
    [ "$(docker inspect ailocal-litellm --format '{{.State.Health.Status}}')" \
      = healthy ] && break
    sleep 3
  done
  local got
  got="$(docker exec ailocal-litellm printenv AILOCAL_TOOL_GATEWAY)"
  [ "$got" = "$1" ] || { echo "mode did not take effect: wanted $1 got $got"; exit 1; }
}


# ── the three dialects, as their real clients send them ─────────────────────
claude_code() {  # /v1/messages, Anthropic tool envelope
  curl -s -m 180 "$PROXY/v1/messages" \
    -H "x-api-key: $KEY" -H 'anthropic-version: 2023-06-01' \
    -H 'content-type: application/json' -H 'user-agent: claude-cli/2.0.0 (cli)' \
    -d '{"model":"ailocal-architecture","max_tokens":16,
         "messages":[{"role":"user","content":"Reply with the single word OK."}],
         "tools":[
           {"name":"Read","description":"Read a file","input_schema":{"type":"object","properties":{"path":{"type":"string"}},"$schema":"https://json-schema.org/draft/2020-12/schema"}},
           {"name":"Workflow","description":"Orchestrate a long multi-step workflow","input_schema":{"type":"object"}},
           {"name":"mcp__lsp__get_hover","description":"Hover","input_schema":{"type":"object"}},
           {"type":"web_search"}]}'
}

codex() {  # /v1/responses, function + namespace + custom envelopes
  curl -s -m 180 "$PROXY/v1/responses" \
    -H "Authorization: Bearer $KEY" -H 'content-type: application/json' \
    -H 'originator: codex_cli_rs' \
    -d '{"model":"ailocal-architecture","max_output_tokens":16,
         "input":[{"type":"message","role":"user","content":"Reply with the single word OK."}],
         "tools":[
           {"type":"function","name":"exec_command","description":"Run a command","parameters":{"type":"object","properties":{"cmd":{"type":"string"}}}},
           {"type":"custom","name":"apply_patch","description":"Apply a patch"},
           {"type":"namespace","name":"multi_agent_v1","description":"Spawn sub-agents","tools":[{"name":"spawn"}]},
           {"type":"web_search"}]}'
}

vscode() {  # /v1/chat/completions, OpenAI nested-function envelope
  curl -s -m 180 "$PROXY/v1/chat/completions" \
    -H "Authorization: Bearer $KEY" -H 'content-type: application/json' \
    -H 'user-agent: vscode-copilot-chat/1.0' \
    -d '{"model":"ailocal-architecture","max_tokens":16,
         "messages":[{"role":"user","content":"Reply with the single word OK."}],
         "tools":[
           {"type":"function","function":{"name":"Read","description":"Read a file","parameters":{"type":"object","properties":{"path":{"type":"string"}}}}},
           {"type":"function","function":{"name":"Workflow","description":"Orchestrate","parameters":{"type":"object"}}}]}'
}

# A usable response, per dialect. Written to a FILE rather than run as a
# heredoc: `python3 <<PY` makes the heredoc itself stdin, so sys.stdin.read()
# saw nothing and every probe reported "unparseable" while the requests were in
# fact succeeding. A checker that cannot read its input fails every case
# identically, which looks like a broken system rather than a broken checker.
cat > /tmp/ailocal-hascontent.py <<'PY'
import json, sys
raw = sys.stdin.read()
try:
    d = json.loads(raw)
except Exception:
    print("unparseable: " + raw[:160]); raise SystemExit(1)
if isinstance(d, dict) and d.get("error"):
    print("error: " + str(d["error"])[:160]); raise SystemExit(1)
if d.get("content"):                       # Anthropic /v1/messages
    raise SystemExit(0)
if d.get("choices"):                       # Chat Completions
    raise SystemExit(0)
if d.get("output") is not None or d.get("status"):   # Responses
    raise SystemExit(0)
print("no recognisable content: " + str(d)[:160])
raise SystemExit(1)
PY

has_content() { python3 /tmp/ailocal-hascontent.py; }

probe() { # $1=label $2=fn
  local out why
  out="$($2)"
  if why="$(printf '%s' "$out" | has_content 2>&1)"; then
    ok "$1"
  else
    bad "$1 — $why"
  fi
}

for m in off report filter; do
  echo
  echo "=== AILOCAL_TOOL_GATEWAY=$m ==="
  set_mode "$m"
  probe "Claude Code  /v1/messages          [REAL payload shape]" claude_code
  probe "Codex        /v1/responses         [REAL payload shape]" codex
  probe "VS Code      /v1/chat/completions  [SYNTHESISED payload]" vscode
done

# ── what the gateway decided, per dialect, in filter mode ───────────────────
echo
echo "=== negotiation decisions in filter mode ==="
set_mode filter
SINCE="$(date -u +%Y-%m-%dT%H:%M:%SZ)"; sleep 1
claude_code >/dev/null; codex >/dev/null; vscode >/dev/null
cat > /tmp/ailocal-decisions.py <<'PY'
import sys, json
seen = 0
for line in sys.stdin:
    if "tool_gateway_metric " not in line:
        continue
    d = json.loads(line.split("tool_gateway_metric ", 1)[1])
    if d.get("event"):
        continue
    seen += 1
    base = d["bytes_reachable"] or 1
    got = d["bytes_kept_reachable"]
    cut = 100 * (base - got) / base
    print("  {:12} {:22} tools {}->{}  model got {}/{} B ({:.0f}% cut)  dropped={}".format(
        d["client"], d["route"], d["tools_in"], d["tools_kept"], got, base, cut,
        d["dropped_names"]))
if seen != 3:
    print("  WARNING: expected 3 metric records, saw {}".format(seen))
PY
docker logs --since "$SINCE" ailocal-litellm 2>&1 | python3 /tmp/ailocal-decisions.py

report "CLIENT COMPATIBILITY" || exit 1
echo
echo "SCOPE: the VS Code payload is SYNTHESISED. Its route and headers are"
echo "proven; its client profile is not validated against a real session and"
echo "therefore drops nothing. Capture a real VS Code payload before trusting"
echo "any VS Code-specific filtering."
