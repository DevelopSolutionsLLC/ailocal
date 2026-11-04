#!/usr/bin/env bash
# The gateway is transparent to each client dialect in every AILOCAL_TOOL_GATEWAY mode
# (off/report/filter): each request still succeeds with a usable response. Run by
# `gate.py --full`; recreates the LiteLLM container named by AILOCAL_LITELLM_CONTAINER.
# Claude Code and Codex payloads are real captured shapes; the VS Code payload is
# SYNTHESISED, so it proves the route and headers, not the VS Code client profile.
set -uo pipefail
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/harness.sh"
cd "$ROOT_DIR" || exit 1
export PYTHONPATH="$ROOT_DIR/src${PYTHONPATH:+:$PYTHONPATH}"

PROXY="${AILOCAL_PROXY:-http://127.0.0.1:4000}"
C="${AILOCAL_LITELLM_CONTAINER:-ailocal-litellm}"
KEY="$(python3 -c 'from ailocal import environment; print(environment.value("LITELLM_MASTER_KEY"))')"
[ -n "$KEY" ] || { echo "FAIL  no LITELLM_MASTER_KEY in the ailocal environment"; exit 1; }
ORIGINAL_MODE="$(docker exec "$C" printenv AILOCAL_TOOL_GATEWAY 2>/dev/null || echo off)"
TMP="$(mktemp -d)"

compose() { python3 -c 'import sys; from ailocal import runtime; runtime.compose(*sys.argv[1:])' "$@"; }
wait_healthy() {
  for _ in $(seq 1 40); do
    [ "$(docker inspect "$C" --format '{{.State.Health.Status}}' 2>/dev/null)" = healthy ] && return 0
    sleep 3
  done
  return 1
}
restore() {
  local rc=$?
  echo "==> restoring AILOCAL_TOOL_GATEWAY=$ORIGINAL_MODE"
  AILOCAL_TOOL_GATEWAY="$ORIGINAL_MODE" compose up -d >/dev/null 2>&1; wait_healthy
  exit $rc
}
trap 'rm -rf "$TMP"; restore' EXIT INT TERM

set_mode() {
  AILOCAL_TOOL_GATEWAY="$1" compose up -d >/dev/null 2>&1; wait_healthy
  local got; got="$(docker exec "$C" printenv AILOCAL_TOOL_GATEWAY)"
  [ "$got" = "$1" ] || { echo "FAIL  mode did not take effect: wanted $1 got $got"; exit 1; }
}

OK_PROMPT='"Reply with the single word OK."'
claude_code() {  # /v1/messages, Anthropic tool envelope
  curl -s -m 180 "$PROXY/v1/messages" -H "x-api-key: $KEY" -H 'anthropic-version: 2023-06-01' \
    -H 'content-type: application/json' -H 'user-agent: claude-cli/2.0.0 (cli)' \
    -d '{"model":"ailocal-architecture","max_tokens":16,"messages":[{"role":"user","content":'"$OK_PROMPT"'}],
         "tools":[
           {"name":"Read","description":"Read a file","input_schema":{"type":"object","properties":{"path":{"type":"string"}},"$schema":"https://json-schema.org/draft/2020-12/schema"}},
           {"name":"Workflow","description":"Orchestrate a long multi-step workflow","input_schema":{"type":"object"}},
           {"name":"mcp__lsp__get_hover","description":"Hover","input_schema":{"type":"object"}},
           {"type":"web_search"}]}'
}
codex() {  # /v1/responses, function + namespace + custom envelopes
  curl -s -m 180 "$PROXY/v1/responses" -H "Authorization: Bearer $KEY" -H 'content-type: application/json' \
    -H 'originator: codex_cli_rs' \
    -d '{"model":"ailocal-architecture","max_output_tokens":16,
         "input":[{"type":"message","role":"user","content":'"$OK_PROMPT"'}],
         "tools":[
           {"type":"function","name":"exec_command","description":"Run a command","parameters":{"type":"object","properties":{"cmd":{"type":"string"}}}},
           {"type":"custom","name":"apply_patch","description":"Apply a patch"},
           {"type":"namespace","name":"multi_agent_v1","description":"Spawn sub-agents","tools":[{"name":"spawn"}]},
           {"type":"web_search"}]}'
}
vscode() {  # /v1/chat/completions, OpenAI nested-function envelope
  curl -s -m 180 "$PROXY/v1/chat/completions" -H "Authorization: Bearer $KEY" -H 'content-type: application/json' \
    -H 'user-agent: vscode-copilot-chat/1.0' \
    -d '{"model":"ailocal-architecture","max_tokens":16,"messages":[{"role":"user","content":'"$OK_PROMPT"'}],
         "tools":[
           {"type":"function","function":{"name":"Read","description":"Read a file","parameters":{"type":"object","properties":{"path":{"type":"string"}}}}},
           {"type":"function","function":{"name":"Workflow","description":"Orchestrate","parameters":{"type":"object"}}}]}'
}

# A file, not `python3 <<PY`: a heredoc becomes stdin and the checker read nothing.
cat > "$TMP/has_content.py" <<'PY'
import json, sys
raw = sys.stdin.read()
try:
    d = json.loads(raw)
except ValueError:
    sys.exit("unparseable: " + raw[:160])
if not isinstance(d, dict) or d.get("error"):
    sys.exit("error: " + str(d)[:160])
if not (d.get("content") or d.get("choices") or d.get("output") is not None or d.get("status")):
    sys.exit("no recognisable content: " + str(d)[:160])
PY
probe() {  # probe <label> <fn>
  local why
  if why="$($2 | python3 "$TMP/has_content.py" 2>&1)"; then ok "$1"; else bad "$1 — $why"; fi
}

for m in off report filter; do
  section "=== AILOCAL_TOOL_GATEWAY=$m ==="
  set_mode "$m"
  probe "Claude Code  /v1/messages          [REAL payload shape]" claude_code
  probe "Codex        /v1/responses         [REAL payload shape]" codex
  probe "VS Code      /v1/chat/completions  [SYNTHESISED payload]" vscode
done

section "=== negotiation decisions in filter mode ==="
SINCE="$(date -u +%Y-%m-%dT%H:%M:%SZ)"; sleep 1
claude_code >/dev/null; codex >/dev/null; vscode >/dev/null
cat > "$TMP/decisions.py" <<'PY'
import json, sys
rows = [json.loads(l.split("tool_gateway_metric ", 1)[1]) for l in sys.stdin if "tool_gateway_metric " in l]
rows = [d for d in rows if not d.get("event")]
for d in rows:
    base, got = d["bytes_reachable"] or 1, d["bytes_kept_reachable"]
    print(f"  {d['client']:12} {d['route']:22} tools {d['tools_in']}->{d['tools_kept']}  model got {got}/{base} B "
          f"({100 * (base - got) / base:.0f}% cut)  dropped={d['dropped_names']}")
if len(rows) != 3:
    print(f"  WARNING: expected 3 metric records, saw {len(rows)}")
PY
docker logs --since "$SINCE" "$C" 2>&1 | python3 "$TMP/decisions.py"

report "CLIENT COMPATIBILITY"
