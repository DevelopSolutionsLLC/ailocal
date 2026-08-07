#!/usr/bin/env bash
# validate-vscode-e2e.sh — VS Code -> LiteLLM -> gateway -> qwen3-coder.
#
# Named and structured to match validate-claude-e2e.sh / validate-codex-e2e.sh.
#
# HONEST SCOPE, stated up front because this client is different from the other
# two: Claude Code and Codex are CLIs, so their sessions can be driven from a
# script and judged on filesystem outcomes. VS Code chat is GUI-driven. There is
# no supported way to make it send a chat turn headlessly, so this script does
# NOT simulate one and does not claim the chat path works.
#
# What it does instead: verify everything up to the GUI boundary, then tell you
# exactly what to click and check whether the proxy saw it. Every level is
# labelled with how it was established.
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

USER_DIR="$HOME/Library/Application Support/Code/User"
[ -d "$USER_DIR" ] || USER_DIR="$HOME/.config/Code/User"
MODELS_JSON="$USER_DIR/chatLanguageModels.json"
BASE_URL="${AILOCAL_BASE_URL:-http://localhost:4000}"
KEY="$(grep -E '^LITELLM_MASTER_KEY=' "$("$ROOT_DIR/ailocal" profile config-root)/.env" 2>/dev/null | cut -d= -f2-)"

pass=0; fail=0; declare -a FAILED=()
ok()   { printf '  \033[32mPASS\033[0m  %s\n' "$1"; pass=$((pass+1)); }
bad()  { printf '  \033[31mFAIL\033[0m  %s\n' "$1"; fail=$((fail+1)); FAILED+=("$1"); }
note() { printf '  \033[33mMANUAL\033[0m %s\n' "$1"; }
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/output.sh"

echo "══════════════════════════════════════════════════════════════════════"
echo " VS Code -> LiteLLM -> gateway -> qwen3-coder"
echo "══════════════════════════════════════════════════════════════════════"

banner "1. client prerequisites [REAL]"
if command -v code >/dev/null; then ok "code CLI present ($(code --version|head -1))"
else bad "code CLI not on PATH"; fi
if code --list-extensions 2>/dev/null | grep -qix "Gethnet.litellm-connector-copilot"; then
  ok "connector extension installed"
else bad "connector extension missing"; fi

banner "2. provider group [REAL]"
if [ -f "$MODELS_JSON" ]; then
  python3 - "$MODELS_JSON" "$BASE_URL" <<'PY'
import json, sys
path, base = sys.argv[1], sys.argv[2]
try:
    entries = json.load(open(path))
except Exception as exc:
    print(f"  \033[31mFAIL\033[0m  provider file unparseable: {exc}"); raise SystemExit(1)
mine = [e for e in entries if e.get("vendor") == "litellm-connector"]
if not mine:
    print("  \033[31mFAIL\033[0m  no litellm-connector provider group"); raise SystemExit(1)
g = mine[0]
print(f"  \033[32mPASS\033[0m  provider group present (name={g.get('name')})")
if g.get("baseUrl", "").rstrip("/") == base.rstrip("/"):
    print(f"  \033[32mPASS\033[0m  baseUrl points at the proxy ({base})")
else:
    print(f"  \033[31mFAIL\033[0m  baseUrl is {g.get('baseUrl')!r}, expected {base!r}")
    raise SystemExit(1)
if isinstance(g.get("apiKey"), str) and g["apiKey"].startswith("${input:"):
    print("  \033[32mPASS\033[0m  API key is a SecretStorage reference (key already entered)")
else:
    print("  \033[33mMANUAL\033[0m no API key reference — enter it once via")
    print("           'Chat: Manage Language Models'. Cannot be scripted:")
    print("           the value lives in the Keychain.")
PY
  [ $? -eq 0 ] && pass=$((pass+2)) || { fail=$((fail+1)); FAILED+=("provider group"); }
else
  bad "no chatLanguageModels.json — run ailocal vscode"
fi

banner "3. deprecated settings absent [REAL]"
python3 - "$USER_DIR/settings.json" <<'PY'
import json, re, sys
try:
    raw = open(sys.argv[1]).read()
except FileNotFoundError:
    print("  \033[33mMANUAL\033[0m no settings.json"); raise SystemExit
doc = json.loads(re.sub(r",(\s*[}\]])", r"\1", re.sub(r"//[^\n]*", "", raw)))
dead = [k for k in ("litellm-connector.baseUrl", "litellm-connector.backends",
                    "github.copilot.chat.customOAIModels",
                    "github.copilot.agent.autoApprove",
                    "github.copilot.chat.tools.terminal.autoApprove") if k in doc]
if dead:
    print(f"  \033[31mFAIL\033[0m  deprecated keys still present: {dead}")
else:
    print("  \033[32mPASS\033[0m  no deprecated keys")
PY

banner "4. the endpoint the connector reads [REAL]"
if curl -sf -m 10 "$BASE_URL/model/info" -H "Authorization: Bearer $KEY" -o /tmp/vsc-mi.json; then
  N=$(python3 -c "import json;print(len(json.load(open('/tmp/vsc-mi.json')).get('data') or []))")
  ok "/model/info answers with $N models"
else
  bad "/model/info unreachable — start the stack"
fi

banner "5. the gateway handles this route [REAL]"
# /v1/chat/completions is the route the connector uses. Proven here directly,
# independent of VS Code, so a GUI problem is never confused with a gateway one.
if curl -sf -m 120 "$BASE_URL/v1/chat/completions" \
     -H "Authorization: Bearer $KEY" -H 'content-type: application/json' \
     -H 'user-agent: vscode-copilot-chat/1.0' \
     -d '{"model":"ailocal-architecture","max_tokens":8,
          "messages":[{"role":"user","content":"Reply with OK."}],
          "tools":[{"type":"function","function":{"name":"Read",
            "description":"read","parameters":{"type":"object"}}}]}' \
     -o /tmp/vsc-chat.json; then
  ok "the connector's route serves a tool-bearing request"
else
  bad "/v1/chat/completions failed for a vscode-shaped request"
fi

banner "6. has a REAL VS Code request ever reached the proxy? [REAL]"
SEEN=$(docker logs --since 24h ailocal-litellm 2>&1 | grep tool_gateway_metric \
  | python3 -c "
import sys, json
n = 0
for l in sys.stdin:
    try: d = json.loads(l.split('tool_gateway_metric ',1)[1])
    except Exception: continue
    if d.get('event') or d.get('client') != 'vscode': continue
    ua = 'synthetic' if 'copilot-chat/1.0' in str(d) else 'unknown'
    n += 1
print(n)")
echo "        vscode-identified requests in the last 24h: $SEEN"
note "A count here does NOT prove real VS Code works: this script and the"
note "compatibility suite both send vscode-shaped requests themselves. Only a"
note "chat turn you type in the editor proves the GUI path."

echo
banner "the one manual step, and how to check it"
cat <<'TXT'
  1. Open VS Code, open Copilot Chat.
  2. In the model picker choose a "LiteLLM" model (e.g. ailocal-architecture).
  3. Send: "Reply with OK."
  4. Then run, to see whether the proxy actually served it:

       ailocal metrics --since 5m

     A TOOL NEGOTIATION SUMMARY with client=vscode is proof. Nothing else is.
TXT

echo
if [ "$fail" -ne 0 ]; then
  echo " VSCODE E2E: $fail FAILED, $pass passed (up to the GUI boundary)"
  for f in "${FAILED[@]}"; do echo "   - $f"; done
  exit 1
fi
echo " VSCODE E2E: $pass checks passed up to the GUI boundary."
echo " The chat turn itself is UNVERIFIED by this script, by design."
