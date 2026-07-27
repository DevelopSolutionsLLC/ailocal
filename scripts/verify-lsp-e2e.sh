#!/usr/bin/env bash
# verify-lsp-e2e.sh — prove the LSP chain against a REAL repository.
#
#   Claude Code -> MCP (mcpls) -> language server -> symbols
#
# ── the trap this script exists to avoid ──
# mcpls answers a call made before its language server has finished loading with
# a JSON-RPC *error* ("still initializing, wait and retry"), NOT with an empty
# result. A probe that reads response["result"] and ignores response["error"]
# sees {"symbols":[]} and reports "0 symbols, no error" — which is
# indistinguishable from "this language does not work". That misdiagnosis has
# already happened on this machine once.
#
# So this script: retries with a real delay, and ALWAYS prints response["error"]
# before drawing any conclusion. An empty result after N retries with no error is
# a genuine negative; an empty result WITH an error is a timing artifact.
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

MCPLS="${MCPLS_BIN:-$HOME/.cadence/bin/mcpls}"
CONFIG="${MCPLS_CONFIG:-$HOME/Documents/DevelopSolutions/cadence/config/mcpls.toml}"
REPO="${1:-$ROOT}"
RETRIES="${LSP_RETRIES:-6}"
DELAY="${LSP_DELAY:-10}"

pass=0; fail=0
ok(){ printf '  \033[32mPASS\033[0m  %s\n' "$1"; pass=$((pass+1)); }
bad(){ printf '  \033[31mFAIL\033[0m  %s\n' "$1"; fail=$((fail+1)); }
note(){ printf '  \033[33mNOTE\033[0m  %s\n' "$1"; }

[ -x "$MCPLS" ] || { echo "mcpls not executable at $MCPLS"; exit 1; }
[ -f "$CONFIG" ] || { echo "mcpls config missing at $CONFIG"; exit 1; }

echo "══════════════════════════════════════════════════════════════════════"
echo " LSP END-TO-END   repo=$REPO"
echo "══════════════════════════════════════════════════════════════════════"

# One long-lived mcpls process handles the whole session: language servers are
# spawned per-process, so a fresh process per call would pay initialization
# every time and never get past the lag.
call_mcpls() { # stdin: newline-delimited JSON-RPC requests
  MCPLS_CONFIG="$CONFIG" timeout 180 "$MCPLS" 2>/dev/null
}

probe() { # $1=tool $2=args-json  -> prints result or error
  local tool="$1" args="$2"
  {
    printf '%s\n' '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"verify","version":"1"}}}'
    printf '%s\n' '{"jsonrpc":"2.0","method":"notifications/initialized"}'
    sleep 2
    printf '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"%s","arguments":%s}}\n' "$tool" "$args"
    sleep 25
  } | call_mcpls | python3 -c "
import sys, json
for line in sys.stdin:
    line = line.strip()
    if not line.startswith('{'): continue
    try: d = json.loads(line)
    except Exception: continue
    if d.get('id') != 2: continue
    # ALWAYS surface the error before anything reads the result.
    if 'error' in d:
        print('ERROR:' + json.dumps(d['error'])[:200]); raise SystemExit
    r = d.get('result') or {}
    c = r.get('content') or []
    txt = c[0].get('text','') if c else json.dumps(r)
    print('RESULT:' + txt[:400]); raise SystemExit
print('NORESPONSE:')
"
}

echo
echo "── 1. tool inventory ──"
TOOLS=$( { printf '%s\n' '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"v","version":"1"}}}'
           printf '%s\n' '{"jsonrpc":"2.0","method":"notifications/initialized"}'
           sleep 1
           printf '%s\n' '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}'
           sleep 4; } | call_mcpls | python3 -c "
import sys, json
for line in sys.stdin:
    line=line.strip()
    if not line.startswith('{'): continue
    try: d=json.loads(line)
    except Exception: continue
    if d.get('id')==2 and 'result' in d:
        t=[x['name'] for x in d['result'].get('tools',[])]
        print(len(t)); print(' '.join(t[:6])); raise SystemExit
print(0)")
N=$(echo "$TOOLS" | head -1)
[ "${N:-0}" -gt 0 ] && ok "mcpls exposes $N LSP tools" || bad "no tools exposed"
echo "$TOOLS" | tail -1 | sed 's/^/        /'

echo
echo "── 2. workspace symbol lookup, WITH retry and error reporting ──"
# A symbol that definitely exists in this repo.
SYM="${LSP_SYMBOL:-ToolGateway}"
for i in $(seq 1 "$RETRIES"); do
  OUT=$(probe workspace_symbol_search "{\"query\":\"$SYM\",\"workspace_root\":\"$REPO\"}")
  case "$OUT" in
    ERROR:*)
      note "attempt $i/$RETRIES -> ${OUT#ERROR:}"
      case "$OUT" in *initializ*|*wait*|*retry*)
          note "  server still loading; sleeping ${DELAY}s (this is EXPECTED, not a failure)"
          sleep "$DELAY"; continue ;;
      esac
      bad "hard error from mcpls: ${OUT#ERROR:}"; break ;;
    RESULT:*)
      BODY="${OUT#RESULT:}"
      if printf '%s' "$BODY" | grep -q '"symbols": *\[\]'; then
        note "attempt $i/$RETRIES -> empty symbol list, NO error"
        [ "$i" -lt "$RETRIES" ] && { sleep "$DELAY"; continue; }
        bad "empty after $RETRIES attempts with no error — genuine negative"
      else
        ok "symbol '$SYM' resolved"
        printf '%s\n' "$BODY" | head -c 240 | sed 's/^/        /'
      fi
      break ;;
    *) note "attempt $i/$RETRIES -> no response"; sleep "$DELAY" ;;
  esac
done

echo
echo "══════════════════════════════════════════════════════════════════════"
[ "$fail" -eq 0 ] && echo " LSP E2E: $pass passed" || echo " LSP E2E: $fail failed, $pass passed"
exit $([ "$fail" -eq 0 ] && echo 0 || echo 1)
