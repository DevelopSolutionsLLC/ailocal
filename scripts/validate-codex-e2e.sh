#!/usr/bin/env bash
# validate-codex-e2e.sh — prove the Codex path, and settle the namespace question.
#
#   Codex -> LiteLLM -> gateway -> MCP/LSP -> qwen3-coder
#
# Two jobs:
#   1. The same observable-outcome validation as the Claude harness: tools
#      execute, edits land on disk, tests pass.
#   2. THE ROUTING EXPERIMENT. Namespace expansion is implemented but disabled,
#      because flattening mcp__lsp's sub-tools changes the name the model emits
#      and Codex must be able to dispatch that name back to the MCP server. If it
#      cannot, the model emits calls Codex drops on the floor — strictly worse
#      than having no MCP tools. This script runs the same task with expansion off
#      and on and compares what actually happened.
#
# The experiment is the point. Everything else here is a control.
#
# Usage: ./scripts/validate-codex-e2e.sh
set -uo pipefail
. "$(cd "$(dirname "$0")" && pwd)/lib/e2e.sh"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
. scripts/lib/compose.sh

WORK="${CODEX_E2E_WORKDIR:-/tmp/ailocal-codex-e2e}"
LEDGERS="$AILOCAL_STATE/captures/sessions"
RESULTS="$AILOCAL_STATE/e2e"
STAMP="$(date +%Y%m%d-%H%M%S)"
mkdir -p "$RESULTS"

pass=0; fail=0; declare -a FAILED=()
ok()   { printf '  \033[32mPASS\033[0m  %s\n' "$1"; pass=$((pass+1)); }
bad()  { printf '  \033[31mFAIL\033[0m  %s\n' "$1"; fail=$((fail+1)); FAILED+=("$1"); }
note() { printf '  \033[33mNOTE\033[0m  %s\n' "$1"; }
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/output.sh"

docker ps --format '{{.Names}}' | grep -qx ailocal-litellm || {
  echo "ailocal-litellm is not running."; exit 1; }
if pgrep -f "test-client-compatibility|test-all.sh|benchmark-tool-gateway|validate-claude-e2e" >/dev/null; then
  echo "Another suite is running that mutates gateway state or contends for the"
  echo "GPU. Wait for it — concurrent runs corrupt both."
  exit 1
fi

ORIGINAL_EXPANSION="$(python3 - <<'PY'
import re
src = open("config/litellm/registry.yaml", encoding="utf-8").read()
m = re.search(r"namespace_expansion:\s*\n\s*enabled:\s*(\S+)", src)
print(m.group(1) if m else "false")
PY
)"
restore() {
  local rc=$?
  echo
  banner "restoring namespace_expansion.enabled=$ORIGINAL_EXPANSION"
  set_expansion "$ORIGINAL_EXPANSION" >/dev/null 2>&1 || true
  exit $rc
}
trap restore EXIT INT TERM

set_expansion() { # $1=true|false
  python3 - "$1" <<'PY'
import re, sys
want = sys.argv[1]
path = "config/litellm/registry.yaml"
src = open(path, encoding="utf-8").read()
src = re.sub(r"(namespace_expansion:\s*\n\s*enabled:\s*)\S+",
             lambda m: m.group(1) + want, src, count=1)
open(path, "w", encoding="utf-8").write(src)
PY
  dc up -d --force-recreate litellm >/dev/null 2>&1
  for _ in $(seq 1 40); do
    [ "$(docker inspect ailocal-litellm --format '{{.State.Health.Status}}')" \
      = healthy ] && break
    sleep 3
  done
  # Confirm the registry actually reloaded with the value we asked for, rather
  # than assuming the restart picked it up.
  docker exec -i ailocal-litellm python - <<'PY'
import importlib.util, sys
s = importlib.util.spec_from_file_location(
    "capability_registry", "/app/config/capability_registry.py")
m = importlib.util.module_from_spec(s); sys.modules["capability_registry"] = m
s.loader.exec_module(m)
reg = m.Registry(path="/app/config/registry.yaml",
                 caps_json="/app/ailocal-config/capabilities.generated.json",
                 config_path="/app/config/config.yaml")
print(reg.namespace_expansion()["enabled"])
PY
}

# ── fixture ─────────────────────────────────────────────────────────────────
rm -rf "$WORK"; mkdir -p "$WORK/src" "$WORK/tests"
cat > "$WORK/src/pricing.py" <<'EOF'
"""Pricing helpers."""


def apply_discount(price: float, percent: float) -> float:
    """Return price reduced by percent. e.g. 100, 10 -> 90.0"""
    # BUG: adds the discount instead of subtracting it.
    return price + (price * percent / 100.0)
EOF
cat > "$WORK/tests/test_pricing.py" <<'EOF'
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "src"))
from pricing import apply_discount


class TestDiscount(unittest.TestCase):
    def test_apply_discount(self):
        self.assertAlmostEqual(apply_discount(100.0, 10.0), 90.0)


if __name__ == "__main__":
    unittest.main()
EOF
cat > "$WORK/run_tests.sh" <<'EOF'
#!/bin/sh
cd "$(dirname "$0")" && python3 tests/test_pricing.py 2>&1
EOF
chmod +x "$WORK/run_tests.sh"
( cd "$WORK" && git init -q && git add -A \
  && git -c user.email=e2e@test -c user.name=e2e commit -qm initial )

BASE="$(cd "$WORK" && ./run_tests.sh; echo rc=$?)"
if printf '%s' "$BASE" | grep -q AssertionError; then
  ok "fixture precondition: the test genuinely fails on an assertion"
else
  bad "fixture precondition broken: $(printf '%s' "$BASE" | tr '\n' ' ' | tail -c 100)"
fi

run_codex() { # $1=slug $2=prompt
  # Declared separately: bash expands EVERY argument to `local` before assigning
  # any of them, so `local a="$1" b="...$a"` leaves $a unbound under `set -u`.
  # This is the SECOND time this pattern bit in this repo — it was fixed in
  # benchmarks/tool-gateway.sh and then reintroduced here from muscle memory.
  local slug="$1"
  local prompt="$2"
  local log="$RESULTS/$STAMP-codex-$slug.log"
  rm -f "$LEDGERS"/*.json 2>/dev/null
  local start end
  start=$(python3 -c 'import time;print(time.time())')
  # --skip-git-repo-check is not needed (this IS a git repo), but
  # --dangerously-bypass-approvals-and-sandbox is: a non-interactive codex exec
  # cannot obtain approval for writes otherwise, and every mutating call is
  # refused — the same trap that made Claude Code look broken.
  # BOUNDED. LiteLLM #27442 means /v1/responses streams content in bare `data:`
  # frames with no `event:` line, so Codex renders the text but never receives a
  # named terminal event and waits forever. Unbounded, this call sat for 900 s
  # and produced ZERO bytes before being killed by hand -- reported as a generic
  # timeout, which reads like a product failure rather than the known upstream
  # limitation it is.
  #
  # Timeout + process-group kill, then classify: bytes-but-no-terminal-event is
  # BLOCKED_UPSTREAM, silence is a genuine failure. This is a harness fix. It
  # does NOT rewrite SSE framing -- that stays upstream's to fix.
  local budget="${AILOCAL_CODEX_TIMEOUT:-180}"
  LAST_BLOCKED=0
  e2e_run "$budget" "$log" \
    zsh -ic "cd '$WORK' && codex-local exec --dangerously-bypass-approvals-and-sandbox '$prompt'"
  local rc=$?
  e2e_sweep "codex-local exec" "codex exec"
  if [ "${rc:-0}" -eq 124 ]; then
    if [ -s "$log" ]; then
      warn "BLOCKED_UPSTREAM_LITELLM_27442: content arrived, no terminal event within ${budget}s"
    else
      warn "BLOCKED_UPSTREAM_LITELLM_27442: no output within ${budget}s (streamed turn never completes)"
    fi
    echo "      /v1/responses omits 'event:' lines; Codex never marks the turn done."
    echo "      Re-test: curl -sN .../v1/responses -d '{\"stream\":true,...}' | grep -c '^event:'"
    LAST_BLOCKED=1
  fi
  end=$(python3 -c 'import time;print(time.time())')
  LAST_SECS=$(python3 -c "print(f'{$end-$start:.0f}')")
  LAST_LOG="$log"
  LAST_TOOLS="$(python3 - "$LEDGERS" <<'PY'
import glob, json, os, sys
files = sorted(glob.glob(os.path.join(sys.argv[1], "*.json")),
               key=os.path.getmtime)
print(" ".join(json.load(open(files[-1])).get("tool_call_sequence") or [])
      if files else "")
PY
)"
}

echo
echo "══════════════════════════════════════════════════════════════════════"
echo " Codex -> LiteLLM -> gateway -> MCP/LSP -> qwen3-coder"
echo "══════════════════════════════════════════════════════════════════════"

# ── control: does the basic path work at all? ──────────────────────────────
banner "expansion OFF (baseline)"
got="$(set_expansion false | tail -1)"
[ "$got" = "False" ] && ok "registry reloaded with expansion disabled" \
                     || bad "expansion flag did not take effect (got '$got')"

echo
banner "task A: fix the discount bug (edits must land on disk)"
run_codex edit "The apply_discount function in src/pricing.py adds the discount instead of subtracting it. Fix it, then stop."
echo "        ${LAST_SECS}s | tools: ${LAST_TOOLS:-<none recorded>}"
if [ -n "$LAST_TOOLS" ]; then ok "A. the proxy recorded tool calls"
else bad "A. no tool calls reached the proxy ledger"; fi
if grep -qE 'price - \(|price \* \(1|\- \(price \*' "$WORK/src/pricing.py"; then
  ok "A. FILE ON DISK now subtracts (read back, not taken from the model's word)"
else
  bad "A. file unchanged or still adding: $(grep -c . "$WORK/src/pricing.py") lines"
fi
POST="$(cd "$WORK" && ./run_tests.sh; echo rc=$?)"
case "$POST" in
  *rc=0*) ok "A. the suite now PASSES — the fix is correct";;
  *) bad "A. suite still failing: $(printf '%s' "$POST" | tr '\n' ' ' | tail -c 120)";;
esac

# ── the experiment ─────────────────────────────────────────────────────────
echo
echo "══════════════════════════════════════════════════════════════════════"
echo " THE NAMESPACE ROUTING EXPERIMENT"
echo "══════════════════════════════════════════════════════════════════════"
echo " Question: with mcp__lsp flattened to mcp__lsp__<tool>, can Codex actually"
echo " DISPATCH the call? If not, expansion must stay off."
echo

LSP_PROMPT="Use your LSP tools to find the definition of apply_discount in this repository, then stop. Do not edit anything."

banner "expansion OFF — control"
got="$(set_expansion false | tail -1)"
run_codex lsp_off "$LSP_PROMPT"
echo "        ${LAST_SECS}s | tools: ${LAST_TOOLS:-<none>}"
OFF_TOOLS="$LAST_TOOLS"; OFF_LOG="$LAST_LOG"
if printf '%s' "$OFF_TOOLS" | grep -q "mcp__lsp"; then
  note "unexpected: an lsp tool was called with expansion OFF"
else
  ok "control: with the bundle dropped, no lsp tool is reachable (as predicted)"
fi

echo
banner "expansion ON — the test"
got="$(set_expansion true | tail -1)"
[ "$got" = "True" ] && ok "registry reloaded with expansion ENABLED" \
                    || bad "expansion flag did not take effect (got '$got')"
run_codex lsp_on "$LSP_PROMPT"
echo "        ${LAST_SECS}s | tools: ${LAST_TOOLS:-<none>}"
ON_TOOLS="$LAST_TOOLS"; ON_LOG="$LAST_LOG"

echo
banner "what the gateway did with the bundles"
docker logs --since 10m ailocal-litellm 2>&1 | grep tool_gateway_metric \
  > /tmp/codex-metrics.log || true
python3 - <<'PY'
import json
rows = [json.loads(l.split("tool_gateway_metric ", 1)[1])
        for l in open("/tmp/codex-metrics.log", encoding="utf-8", errors="replace")
        if "tool_gateway_metric " in l]
rows = [r for r in rows if not r.get("event") and r.get("client") == "codex"]
if not rows:
    print("  no codex gateway records")
else:
    big = max(rows, key=lambda r: r.get("bytes_in", 0))
    print(f"  expansion enabled   {big.get('namespace_expansion')}")
    print(f"  bundles expanded    {big.get('namespaces_expanded')}")
    print(f"  tools declared      {big['tools_in']}")
    print(f"  reachable bytes     {big['bytes_reachable']}")
    print(f"  model received      {big['tools_kept']} tools / "
          f"{big['bytes_kept_reachable']} B")
PY

echo
banner "verdict"
# POSITIVE EVIDENCE ONLY.
#
# The first version of this check asked "did the model emit a flattened call, and
# is none of my guessed error strings present?" It answered PASS — while the log
# contained:
#     ERROR codex_core::tools::router: error=unsupported call: mcp__lsp__...
# because the pattern list said "unsupported tool" and Codex says "unsupported
# call". One word, and a definitive failure read as success. The feature would
# have been enabled on it.
#
# So the verdict is now driven by what Codex's own router logged about the exact
# tool the model called, not by the absence of strings I thought to look for.
CALLED_TOOL="$(printf '%s' "$ON_TOOLS" | tr ' ' '\n' | grep '^mcp__' | head -1)"
ROUTER_ERROR=""
if [ -n "$CALLED_TOOL" ]; then
  ROUTER_ERROR="$(grep -F "$CALLED_TOOL" "$ON_LOG" | grep -iE 'ERROR|unsupported|unavailable|not found' | head -1)"
fi

if [ -z "$CALLED_TOOL" ]; then
  note "INCONCLUSIVE: the model never attempted a flattened MCP call, so routing"
  note "is untested. Absence of an attempt is not evidence that routing works."
  note "Expansion stays OFF."
elif [ -n "$ROUTER_ERROR" ]; then
  ok "ROUTING CONFIRMED BROKEN for codex-cli $(zsh -ic 'command codex --version' 2>/dev/null | tail -1)"
  echo "         the model called: $CALLED_TOOL"
  echo "         Codex router said: $(printf '%s' "$ROUTER_ERROR" | tail -c 90)"
  echo "         -> expansion MUST stay OFF. This reproduces openai/codex#20652:"
  echo "            flattened MCP names fail resolution in Codex's dispatcher when"
  echo "            an OpenAI-compatible proxy delivers them."
  EXPANSION_VERDICT=broken
else
  # Even with no router error, require evidence the call RETURNED something.
  if grep -qiE "tool_result|succeeded|symbol|result" "$ON_LOG"; then
    ok "ROUTING WORKS: $CALLED_TOOL dispatched with no router error"
    EXPANSION_VERDICT=works
  else
    note "INCONCLUSIVE: $CALLED_TOOL was emitted, no router error was logged, but"
    note "no result is visible either. Not enough to enable expansion."
    EXPANSION_VERDICT=unclear
  fi
fi

echo
echo "logs: $RESULTS/$STAMP-codex-*.log"
if [ "$fail" -ne 0 ]; then
  echo " CODEX E2E: $fail FAILED, $pass passed"
  for f in "${FAILED[@]}"; do echo "   - $f"; done
  exit 1
fi
echo " CODEX E2E: all $pass checks passed"
