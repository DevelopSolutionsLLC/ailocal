#!/usr/bin/env bash
# e2e.sh — end-to-end validation of the three client paths.
#
#   ./tests/e2e.sh claude | codex | vscode
#
# Test machinery, not product: these drive REAL client sessions against the REAL
# local model and are opt-in, never part of `ailocal test`.
#
# WHY OUTCOMES AND NOT TRANSCRIPTS. A local model will narrate "I've updated the
# file" with no write behind it, so grepping its output validates the narration.
# File contents are read back, and the tools it actually invoked come from the
# proxy-side session ledger rather than from anything the model said.
#
# VS Code is different and says so: its chat is GUI-driven with no supported
# headless turn, so that section verifies everything up to the GUI boundary and
# does not claim the chat path works.
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ROOT_DIR="$ROOT"
cd "$ROOT"
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/harness.sh"

AILOCAL_STATE="${AILOCAL_STATE:-$("$ROOT/ailocal" profile state-root)}"
RESULTS="$AILOCAL_STATE/e2e"
LEDGERS="$AILOCAL_STATE/captures/sessions"
STAMP="$(date +%Y%m%d-%H%M%S)"
mkdir -p "$RESULTS"
dc() { "$ROOT/ailocal" compose "$@"; }
note() { printf '  \033[33mMANUAL\033[0m %s\n' "$1"; }

# ── bounded client execution ────────────────────────────────────────────────
# Budget, process group, capture, termination escalation and stray sweep, in one
# place. Protocol assertions stay with each client: what a completed Claude turn
# looks like is not the same question as what a Codex stream failing to
# terminate means.
# e2e_run <budget-seconds> <logfile> <command...>
e2e_run() {
  local budget="$1" log="$2"; shift 2
  local rc=0

  if command -v timeout >/dev/null 2>&1; then
    timeout -k 5 "$budget" "$@" > "$log" 2>&1
    rc=$?
  else
    # No GNU timeout: run in its own process group so the whole tree can be
    # signalled, not just the leader.
    set -m
    "$@" > "$log" 2>&1 &
    local pid=$! waited=0
    set +m
    while kill -0 "$pid" 2>/dev/null && [ "$waited" -lt "$budget" ]; do
      sleep 2; waited=$((waited + 2))
    done
    if kill -0 "$pid" 2>/dev/null; then
      kill -TERM -"$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null
      sleep 3
      kill -0 "$pid" 2>/dev/null && { kill -9 -"$pid" 2>/dev/null || kill -9 "$pid" 2>/dev/null; }
      rc=124
    else
      wait "$pid" 2>/dev/null; rc=$?
    fi
  fi
  return "$rc"
}

# e2e_sweep <pattern>...  — nothing of ours may outlive its budget.
e2e_sweep() {
  local pat
  for pat in "$@"; do
    pkill -f "$pat" 2>/dev/null || true
  done
  return 0
}

# e2e_strays <pattern> — count survivors; a validator that leaks is not bounded.
e2e_strays() {
  pgrep -f "$1" 2>/dev/null | wc -l | tr -d ' '
}

# e2e_workdir — a temporary directory registered for removal on exit.
e2e_workdir() {
  local d; d="$(mktemp -d)"
  _E2E_DIRS="${_E2E_DIRS:-} $d"
  printf '%s' "$d"
}


# ── shared preflight ────────────────────────────────────────────────────────
# A concurrent suite that mutates the gateway mode corrupts this run's readings:
# the first attempt reported mode=report during a filter run for exactly that.
e2e_preflight() { # $1=needs-ledger
  command -v docker >/dev/null || { echo "docker missing"; exit 1; }
  docker ps --format '{{.Names}}' | grep -qx ailocal-litellm || {
    echo "ailocal-litellm is not running. ailocal start first."; exit 1; }
  [ "$(docker inspect ailocal-litellm --format '{{.State.Health.Status}}')" = healthy ] || {
    echo "proxy is not healthy; fix that before trusting any result."; exit 1; }
  if pgrep -f "client-compatibility|ailocal test|benchmark-tool-gateway" >/dev/null; then
    echo "Another suite that mutates gateway state is running. Wait for it."
    exit 1
  fi
  GW_MODE="$(docker exec ailocal-litellm printenv AILOCAL_TOOL_GATEWAY 2>/dev/null || echo off)"
  LEDGER_ON="$(docker exec ailocal-litellm printenv AILOCAL_SESSION_LEDGER 2>/dev/null || echo '')"
  banner "gateway mode: $GW_MODE   session ledger: ${LEDGER_ON:-<off>}"
  if [ -n "${1:-}" ] && [ -z "$LEDGER_ON" ]; then
    echo "  The session ledger is off, so tool calls cannot be verified from the"
    echo "  proxy side. Set AILOCAL_SESSION_LEDGER=/app/captures/sessions in .env"
    echo "  and restart. Refusing to run: without it, 'which tools ran' would come"
    echo "  from the model's own account of itself."
    exit 1
  fi
}

# ── Claude Code ─────────────────────────────────────────────────────────────
e2e_claude() {
e2e_preflight ledger
KEEP="${KEEP:-}"
WORK="${CLAUDE_E2E_WORKDIR:-/tmp/ailocal-e2e}"
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
# rc!=0 is NOT good enough here: a broken runner also exits non-zero, so a
# harness can "pass" its own precondition while running zero tests. Require the
# assertion to actually fire.
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

printf "\n logs: $RESULTS/$STAMP-*.log\n"
[ -n "$KEEP" ] && echo " fixture kept at $WORK" || true

}

# ── Codex CLI ───────────────────────────────────────────────────────────────
e2e_codex() {
e2e_preflight
WORK="${CODEX_E2E_WORKDIR:-/tmp/ailocal-codex-e2e}"
ORIGINAL_EXPANSION="$(python3 - <<'PY'
import re
src = open("deploy/litellm/registry.yaml", encoding="utf-8").read()
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
path = "deploy/litellm/registry.yaml"
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
    "capability_registry", "/app/config/hooks/capability_registry.py")
m = importlib.util.module_from_spec(s); sys.modules["capability_registry"] = m
s.loader.exec_module(m)
reg = m.Registry(path="/app/config/registry.yaml",
                 caps_json="/app/generated/capabilities.json",
                 config_path="/app/generated/config.yaml")
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
# The verdict is driven by what Codex's own router logged about the exact tool
# the model called — never by the absence of guessed error strings. Codex says
# "unsupported call", not "unsupported tool"; a pattern list that guesses the
# wording reads a definitive failure as success.
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

printf "\n logs: $RESULTS/$STAMP-codex-*.log\n"

}

# ── VS Code ─────────────────────────────────────────────────────────────────
e2e_vscode() {
USER_DIR="$HOME/Library/Application Support/Code/User"
[ -d "$USER_DIR" ] || USER_DIR="$HOME/.config/Code/User"
MODELS_JSON="$USER_DIR/chatLanguageModels.json"
BASE_URL="${AILOCAL_BASE_URL:-http://localhost:4000}"
KEY="$(grep -E '^LITELLM_MASTER_KEY=' "$("$ROOT/ailocal" profile config-root)/.env" 2>/dev/null | cut -d= -f2-)"

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
  check $? "provider group parses and names the proxy"
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

printf "\n The chat turn itself is UNVERIFIED by this script, by design.\n"

}

# Sourceable: tests that only need the bounded-execution helper source this file
# and never reach the dispatcher.
if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  case "${1:-}" in
    claude) shift; e2e_claude "$@" ;;
    codex)  shift; e2e_codex "$@" ;;
    vscode) shift; e2e_vscode "$@" ;;
    *) echo "usage: tests/e2e.sh <claude|codex|vscode>" >&2; exit 2 ;;
  esac
  report "e2e"
fi
