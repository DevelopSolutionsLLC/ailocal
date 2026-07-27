#!/usr/bin/env bash
# validate-claude-e2e.sh — prove the whole path, not the layer.
#
#   Claude Code -> LiteLLM -> gateway -> MCP/LSP -> qwen3-coder
#
# Every task below is a REAL claude-local session against the REAL local model,
# in a REAL git repo, with the gateway in its configured mode. Nothing is stubbed
# and nothing is asserted from the model's own prose: each task is judged on an
# observable outcome — a file's contents, a git delta, a tool call recorded in the
# session ledger by the proxy.
#
# WHY OUTCOMES AND NOT TRANSCRIPTS
# A local model will happily narrate "I've updated the file" with no write behind
# it. Grepping its output for "done" would validate the narration, not the work.
# So: file contents are read back, and the tools it actually invoked come from the
# proxy-side ledger rather than from anything the model said.
#
# --permission-mode acceptEdits is REQUIRED. A non-interactive `claude -p` cannot
# be granted write permission otherwise, and every mutating tool returns
# "Claude requested permissions to write ... but you haven't granted it yet" —
# which looks exactly like a broken model. Learned the hard way.
#
# Usage: ./scripts/validate-claude-e2e.sh [--keep]
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

KEEP=""
[ "${1:-}" = "--keep" ] && KEEP=1
WORK="${CLAUDE_E2E_WORKDIR:-/tmp/ailocal-e2e}"
LEDGERS="$ROOT/data/tool-captures/sessions"
RESULTS="$ROOT/data/e2e"
STAMP="$(date +%Y%m%d-%H%M%S)"
mkdir -p "$RESULTS"

pass=0; fail=0; declare -a FAILED=()
ok()  { printf '  \033[32mPASS\033[0m  %s\n' "$1"; pass=$((pass+1)); }
bad() { printf '  \033[31mFAIL\033[0m  %s\n' "$1"; fail=$((fail+1)); FAILED+=("$1"); }
info(){ printf '\033[1;34m==>\033[0m %s\n' "$*"; }

# ── preflight ───────────────────────────────────────────────────────────────
command -v docker >/dev/null || { echo "docker missing"; exit 1; }
docker ps --format '{{.Names}}' | grep -qx ailocal-litellm || {
  echo "ailocal-litellm is not running. ./scripts/start.sh first."; exit 1; }
[ "$(docker inspect ailocal-litellm --format '{{.State.Health.Status}}')" = healthy ] || {
  echo "proxy is not healthy; fix that before trusting any result."; exit 1; }

GW_MODE="$(docker exec ailocal-litellm printenv AILOCAL_TOOL_GATEWAY 2>/dev/null || echo off)"
LEDGER_ON="$(docker exec ailocal-litellm printenv AILOCAL_SESSION_LEDGER 2>/dev/null || echo '')"
info "gateway mode: $GW_MODE   session ledger: ${LEDGER_ON:-<off>}"
if [ -z "$LEDGER_ON" ]; then
  echo "  The session ledger is off, so tool calls cannot be verified from the"
  echo "  proxy side. Set AILOCAL_SESSION_LEDGER=/app/captures/sessions in .env"
  echo "  and restart. Refusing to run: without it, 'which tools ran' would come"
  echo "  from the model's own account of itself."
  exit 1
fi

# ── fixture: a small but real Python project ─────────────────────────────────
# Real enough for pyright to resolve symbols across files, and for a test to
# actually fail before the fix and pass after it.
rm -rf "$WORK"; mkdir -p "$WORK/src" "$WORK/tests"
cat > "$WORK/src/inventory.py" <<'EOF'
"""Warehouse inventory helpers."""


class Warehouse:
    def __init__(self):
        self.items: dict[str, int] = {}

    def add_item(self, sku: str, quantity: int) -> None:
        """Add quantity of sku to the warehouse."""
        self.items[sku] = self.items.get(sku, 0) + quantity

    def remove_item(self, sku: str, quantity: int) -> None:
        """Remove quantity of sku from the warehouse."""
        self.items[sku] = self.items.get(sku, 0) - quantity

    def total_units(self) -> int:
        """Total units across every sku."""
        # BUG: multiplies instead of summing.
        total = 1
        for count in self.items.values():
            total *= count
        return total
EOF
cat > "$WORK/src/reporting.py" <<'EOF'
"""Reporting built on the inventory module."""
from inventory import Warehouse


def stock_report(wh: Warehouse) -> str:
    return f"{len(wh.items)} skus, {wh.total_units()} units"
EOF
cat > "$WORK/tests/test_inventory.py" <<'EOF'
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from inventory import Warehouse


def test_total_units():
    wh = Warehouse()
    wh.add_item("A", 2)
    wh.add_item("B", 3)
    assert wh.total_units() == 5
EOF
cat > "$WORK/run_tests.sh" <<'EOF'
#!/bin/sh
cd "$(dirname "$0")" && python3 -m unittest discover -s tests -q 2>&1
EOF
chmod +x "$WORK/run_tests.sh"
( cd "$WORK" && git init -q && git add -A \
  && git -c user.email=e2e@test -c user.name=e2e commit -qm "initial" )

info "fixture at $WORK (total_units multiplies; the test expects 5, gets 6)"
BASE_TEST="$(cd "$WORK" && ./run_tests.sh; echo "rc=$?")"
case "$BASE_TEST" in
  *rc=0*) bad "fixture precondition: the test should FAIL before the fix"; ;;
  *) ok "fixture precondition: test fails before the fix (as intended)";;
esac

# ── task runner ─────────────────────────────────────────────────────────────
run_task() { # $1=slug  $2=prompt  $3=max-turns
  local slug="$1" prompt="$2" turns="${3:-8}"
  local log="$RESULTS/$STAMP-$slug.log"
  rm -f "$LEDGERS"/*.json 2>/dev/null
  local start end
  start=$(python3 -c 'import time;print(time.time())')
  zsh -ic "cd '$WORK' && claude-local -p '$prompt' --max-turns $turns \
    --permission-mode acceptEdits" > "$log" 2>&1
  end=$(python3 -c 'import time;print(time.time())')
  LAST_SECS=$(python3 -c "print(f'{$end-$start:.0f}')")
  LAST_LOG="$log"
  # Tools the PROXY saw, not what the model claimed.
  LAST_TOOLS="$(python3 - "$LEDGERS" <<'PY'
import glob, json, os, sys
d = sys.argv[1]
files = sorted(glob.glob(os.path.join(d, "*.json")), key=os.path.getmtime)
if not files:
    print(""); raise SystemExit
led = json.load(open(files[-1]))
print(" ".join(led.get("tool_call_sequence") or []))
PY
)"
}

used() { printf '%s' "$LAST_TOOLS" | tr ' ' '\n' | grep -qx "$1"; }
used_prefix() { printf '%s' "$LAST_TOOLS" | tr ' ' '\n' | grep -q "^$1"; }

echo
echo "══════════════════════════════════════════════════════════════════════"
echo " Claude Code -> LiteLLM -> gateway -> MCP/LSP -> qwen3-coder"
echo "══════════════════════════════════════════════════════════════════════"

# ── 1. explain the repo (read + search) ─────────────────────────────────────
echo
info "task 1/5: explain the repo"
run_task explain "Describe what this repository does. Read the files first." 6
echo "        ${LAST_SECS}s | tools: ${LAST_TOOLS:-<none recorded>}"
if [ -n "$LAST_TOOLS" ]; then ok "1. proxy recorded tool calls for the session"
else bad "1. no tool calls reached the proxy ledger"; fi
if grep -qiE 'inventory|warehouse|stock' "$LAST_LOG"; then
  ok "1. the answer references real repo contents (inventory/warehouse/stock)"
else bad "1. answer does not reference repo contents — likely answered blind"; fi

# ── 2. find a symbol (LSP or grepai) ───────────────────────────────────────
echo
info "task 2/5: find a symbol across files"
run_task symbol "Which file defines the function stock_report, and what does it call? Use your search or LSP tools." 6
echo "        ${LAST_SECS}s | tools: ${LAST_TOOLS:-<none recorded>}"
if grep -q 'reporting.py' "$LAST_LOG"; then
  ok "2. located stock_report in reporting.py"
else bad "2. did not identify reporting.py"; fi
if used_prefix "mcp__grepai__" || used_prefix "mcp__lsp__"; then
  ok "2. used an MCP tool (grepai or lsp) — MCP reaches the model"
else
  bad "2. no MCP tool used; fell back to Read/Grep (MCP present but unused)"
fi

# ── 3. modify a file (the observable one) ──────────────────────────────────
echo
info "task 3/5: fix the bug (file contents are read back)"
run_task edit "The total_units method in src/inventory.py multiplies counts instead of summing them. Fix it so it returns the sum. Use the Edit tool." 8
echo "        ${LAST_SECS}s | tools: ${LAST_TOOLS:-<none recorded>}"
if used Edit || used Write || used MultiEdit; then
  ok "3. a mutating tool was invoked"
else bad "3. no mutating tool invoked"; fi
if grep -qE 'total \+= count|sum\(' "$WORK/src/inventory.py"; then
  ok "3. FILE ON DISK now sums (verified by reading it back, not by the model's claim)"
else
  bad "3. file unchanged or still multiplying — the edit did not land"
fi
if ! (cd "$WORK" && git diff --quiet); then
  ok "3. git sees a real delta"
else bad "3. git reports no change"; fi

# ── 4. run the tests (bash) ───────────────────────────────────────────────
echo
info "task 4/5: run the test suite"
run_task tests "Run ./run_tests.sh and tell me whether the tests pass." 6
echo "        ${LAST_SECS}s | tools: ${LAST_TOOLS:-<none recorded>}"
if used Bash; then ok "4. Bash executed"
else bad "4. Bash was not used"; fi
POST_TEST="$(cd "$WORK" && ./run_tests.sh; echo "rc=$?")"
case "$POST_TEST" in
  *rc=0*) ok "4. the suite now PASSES — the model's fix is actually correct";;
  *) bad "4. suite still failing after the fix: $(printf '%s' "$POST_TEST" | tail -2 | tr '\n' ' ')";;
esac

# ── 5. summarize changes (git) ────────────────────────────────────────────
echo
info "task 5/5: summarize the changes via git"
run_task summarize "Run git diff and summarize what changed in this repository." 6
echo "        ${LAST_SECS}s | tools: ${LAST_TOOLS:-<none recorded>}"
if used Bash; then ok "5. git operations run through Bash"
else bad "5. Bash not used for git"; fi
if grep -qiE 'total_units|sum|inventory' "$LAST_LOG"; then
  ok "5. summary names the actual change"
else bad "5. summary does not reference the real diff"; fi

# ── gateway behaviour during these real sessions ───────────────────────────
echo
info "gateway decisions observed during the run"
docker logs --since 30m ailocal-litellm 2>&1 \
  | grep tool_gateway_metric > /tmp/e2e-metrics.log || true
python3 - <<'PY'
import json
rows = []
for line in open("/tmp/e2e-metrics.log", encoding="utf-8", errors="replace"):
    if "tool_gateway_metric " not in line:
        continue
    d = json.loads(line.split("tool_gateway_metric ", 1)[1])
    if d.get("event") or d.get("client") != "claude-code":
        continue
    rows.append(d)
if not rows:
    print("  no gateway records for claude-code (is the gateway off?)")
else:
    big = max(rows, key=lambda r: r.get("bytes_in", 0))
    print(f"  requests seen            {len(rows)}")
    print(f"  largest declared payload {big['tools_in']} tools / {big['bytes_in']} B")
    print(f"  model received           {big['tools_kept']} tools / "
          f"{big['bytes_kept_reachable']} B")
    base = big["bytes_reachable"] or 1
    print(f"  reduction                "
          f"{100*(base-big['bytes_kept_reachable'])/base:.1f}%")
    print(f"  dropped groups           {big['dropped_groups']}")
    print(f"  applied                  {big['applied']}  (mode={big['mode']})")
PY

echo
echo "══════════════════════════════════════════════════════════════════════"
[ -n "$KEEP" ] && echo " fixture kept at $WORK" || true
echo " logs: $RESULTS/$STAMP-*.log"
if [ "$fail" -ne 0 ]; then
  echo " CLAUDE E2E: $fail FAILED, $pass passed"
  for f in "${FAILED[@]}"; do echo "   - $f"; done
  exit 1
fi
echo " CLAUDE E2E: all $pass checks passed"
