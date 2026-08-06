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
# Usage: ailocal validate e2e claude [--keep]
set -uo pipefail
. "$(cd "$(dirname "$0")" && pwd)/e2e.sh"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
AILOCAL_STATE="${AILOCAL_STATE:-$(python3 "$ROOT/lib/profile-config" state-root)}"
cd "$ROOT"

KEEP=""
[ "${1:-}" = "--keep" ] && KEEP=1
WORK="${CLAUDE_E2E_WORKDIR:-/tmp/ailocal-e2e}"
LEDGERS="$AILOCAL_STATE/captures/sessions"
RESULTS="$AILOCAL_STATE/e2e"
STAMP="$(date +%Y%m%d-%H%M%S)"
mkdir -p "$RESULTS"

pass=0; fail=0; declare -a FAILED=()
ok()  { printf '  \033[32mPASS\033[0m  %s\n' "$1"; pass=$((pass+1)); }
bad() { printf '  \033[31mFAIL\033[0m  %s\n' "$1"; fail=$((fail+1)); FAILED+=("$1"); }
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/output.sh"

# ── preflight ───────────────────────────────────────────────────────────────
command -v docker >/dev/null || { echo "docker missing"; exit 1; }
docker ps --format '{{.Names}}' | grep -qx ailocal-litellm || {
  echo "ailocal-litellm is not running. ailocal start first."; exit 1; }
[ "$(docker inspect ailocal-litellm --format '{{.State.Health.Status}}')" = healthy ] || {
  echo "proxy is not healthy; fix that before trusting any result."; exit 1; }

if pgrep -f "test-client-compatibility|test-all.sh|benchmark-tool-gateway" >/dev/null; then
  echo "Another suite that MUTATES the gateway mode is running"
  echo "(test-client-compatibility / test-all / benchmark). Running concurrently"
  echo "corrupts this run's gateway readings — the first attempt reported"
  echo "mode=report during a filter run for exactly this reason. Wait for it."
  exit 1
fi
GW_MODE="$(docker exec ailocal-litellm printenv AILOCAL_TOOL_GATEWAY 2>/dev/null || echo off)"
LEDGER_ON="$(docker exec ailocal-litellm printenv AILOCAL_SESSION_LEDGER 2>/dev/null || echo '')"
banner "gateway mode: $GW_MODE   session ledger: ${LEDGER_ON:-<off>}"
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
# unittest.main() in the file, invoked directly. `unittest discover -s tests`
# raised "Start directory is not importable" (no __init__.py) and reported
# "Ran 0 tests" with rc=5 — which the precondition below then read as "the test
# fails", the exact 'empty is not failure' trap. A runner that cannot fail
# correctly cannot validate anything.
cat > "$WORK/tests/test_inventory.py" <<'EOF'
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "src"))
from inventory import Warehouse


class TestTotalUnits(unittest.TestCase):
    def test_total_units(self):
        wh = Warehouse()
        wh.add_item("A", 2)
        wh.add_item("B", 3)
        self.assertEqual(wh.total_units(), 5)


if __name__ == "__main__":
    unittest.main()
EOF
cat > "$WORK/run_tests.sh" <<'EOF'
#!/bin/sh
cd "$(dirname "$0")" && python3 tests/test_inventory.py 2>&1
EOF
chmod +x "$WORK/run_tests.sh"
( cd "$WORK" && git init -q && git add -A \
  && git -c user.email=e2e@test -c user.name=e2e commit -qm "initial" )

banner "fixture at $WORK (total_units multiplies; the test expects 5, gets 6)"
# rc!=0 is NOT good enough here: a broken runner also exits non-zero, and that
# is how the first version of this harness "passed" its own precondition while
# running zero tests. Require the assertion to actually fire.
BASE_TEST="$(cd "$WORK" && ./run_tests.sh; echo "rc=$?")"
if printf '%s' "$BASE_TEST" | grep -q 'AssertionError' \
   && printf '%s' "$BASE_TEST" | grep -q 'FAILED'; then
  ok "fixture precondition: the test genuinely FAILS on an assertion before the fix"
else
  bad "fixture precondition: expected a real AssertionError, got: $(printf '%s' "$BASE_TEST" | tr '\n' ' ' | tail -c 120)"
fi

# ── task runner ─────────────────────────────────────────────────────────────
# Turn budgets are generous on purpose. A 30B spends several turns exploring
# before answering, and at --max-turns 6 two tasks used all six on tool calls and
# had none left to reply — which the harness then scored as "answered blind".
# That was a budget artefact, not a capability finding.
#
# Note the per-task overrides below, not just this default: raising the default
# alone changed nothing, because every call site passed an explicit 6. The
# exploratory tasks get the LARGEST budget precisely because they explore before
# they answer.
run_task() { # $1=slug  $2=prompt  $3=max-turns
  local slug="$1" prompt="$2" turns="${3:-14}"
  local log="$RESULTS/$STAMP-$slug.log"
  rm -f "$LEDGERS"/*.json 2>/dev/null
  local start end
  start=$(python3 -c 'import time;print(time.time())')
  # Bounded: an unbounded real client session blocks the run indefinitely and
  # leaves its process tree behind. A cold architecture prefill is slow, so the
  # budget is generous rather than tight.
  local budget="${AILOCAL_CLAUDE_TIMEOUT:-900}"
  e2e_run "$budget" "$log" \
    zsh -ic "cd '$WORK' && claude-local -p '$prompt' --max-turns $turns --permission-mode acceptEdits"
  local rc=$?
  e2e_sweep "claude-local -p" "claude -p"
  end=$(python3 -c 'import time;print(time.time())')
  LAST_TIMEDOUT=""
  if [ "$rc" -eq 124 ]; then
    LAST_TIMEDOUT=1
    echo "  TIMEOUT after ${budget}s — session terminated, tree swept" >&2
  fi
  LAST_TRUNCATED=""
  LAST_SECS=$(python3 -c "print(f'{$end-$start:.0f}')")
  LAST_LOG="$log"
  # Turn exhaustion means the model never got to answer. That is INCONCLUSIVE
  # about the answer, and must not be reported as a wrong answer.
  if grep -q "Reached max turns" "$log"; then LAST_TRUNCATED=1; else LAST_TRUNCATED=""; fi
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
banner "task 1/5: explain the repo"
run_task explain "Describe what this repository does. Read the files first." 16
echo "        ${LAST_SECS}s | tools: ${LAST_TOOLS:-<none recorded>}"
if [ -n "$LAST_TOOLS" ]; then ok "1. proxy recorded tool calls for the session"
else bad "1. no tool calls reached the proxy ledger"; fi
if grep -qiE 'inventory|warehouse|stock|sku' "$LAST_LOG"; then
  ok "1. the answer references real repo contents"
elif [ -n "$LAST_TRUNCATED" ]; then
  printf '  \033[33mINCONCLUSIVE\033[0m  1. hit the turn limit before answering\n'
else bad "1. answer does not reference repo contents — answered blind"; fi

# ── 2. find a symbol (LSP or grepai) ───────────────────────────────────────
echo
banner "task 2/5: find a symbol across files"
run_task symbol "Which file defines the function stock_report, and what does it call? Use your search or LSP tools." 16
echo "        ${LAST_SECS}s | tools: ${LAST_TOOLS:-<none recorded>}"
if grep -q 'reporting.py' "$LAST_LOG"; then
  ok "2. located stock_report in reporting.py"
elif [ -n "$LAST_TRUNCATED" ]; then
  printf '  \033[33mINCONCLUSIVE\033[0m  2. hit the turn limit before answering\n'
else bad "2. did not identify reporting.py"; fi
if used_prefix "mcp__lsp__"; then
  ok "2. used an LSP tool — MCP reaches the model AND is path-independent"
elif used_prefix "mcp__grepai__"; then
  # The fixture lives outside any indexed workspace, so grepai legitimately has
  # nothing for it (verified: `grepai search stock_report` -> No results found).
  # Reaching for it and falling back is correct behaviour, not a failure.
  ok "2. used an MCP tool (grepai); the fixture is outside the index, so a "\
"fallback to Read/Grep is correct"
else
  bad "2. no MCP tool used at all — MCP is registered but never reached"
fi

# ── 3. modify a file (the observable one) ──────────────────────────────────
echo
banner "task 3/5: fix the bug (file contents are read back)"
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
banner "task 4/5: run the test suite"
run_task tests "Run ./run_tests.sh and tell me whether the tests pass." 12
echo "        ${LAST_SECS}s | tools: ${LAST_TOOLS:-<none recorded>}"
if used Bash; then ok "4. Bash executed"
else bad "4. Bash was not used"; fi
POST_TEST="$(cd "$WORK" && ./run_tests.sh; echo "rc=$?")"
case "$POST_TEST" in
  *rc=0*) ok "4. the suite now PASSES — the model's fix is actually correct";;
  *) bad "4. suite still failing after the fix: $(printf '%s' "$POST_TEST" | tr '\n' ' ' | tail -c 140)";;
esac

# ── 5. summarize changes (git) ────────────────────────────────────────────
echo
banner "task 5/5: summarize the changes via git"
run_task summarize "Run git diff and summarize what changed in this repository." 12
echo "        ${LAST_SECS}s | tools: ${LAST_TOOLS:-<none recorded>}"
if used Bash; then ok "5. git operations run through Bash"
else bad "5. Bash not used for git"; fi
if grep -qiE 'total_units|sum|inventory' "$LAST_LOG"; then
  ok "5. summary names the actual change"
else bad "5. summary does not reference the real diff"; fi

# ── gateway behaviour during these real sessions ───────────────────────────
echo
banner "gateway decisions observed during the run"
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
