# Implementation of `ailocal benchmark gateway`. Not executable on its own.
# benchmarks/tool-gateway.sh — A/B the gateway on the real client, real model.
#
# Runs the SAME task through claude-local twice: once with the gateway in
# report mode (measures, changes nothing = the baseline) and once in filter
# mode. Both runs go through the production path — the real CLI, the real
# proxy, the real qwen3-coder on Ollama. Nothing is staged.
#
# What it records per run: wall-clock latency, the tool payload the model
# actually received (from the gateway's own metric line), and the run's output
# so tool execution can be checked for regression. It does NOT decide whether
# the runs are equivalent — it prints both outputs for a human to compare,
# because "did the agent still do the job" is not a thing a byte count answers.
#
# n=1 per arm by default. That is enough to see a 70% payload change and NOT
# enough to resolve small latency differences; RUNS=n raises it.
#
# Usage:  ./scripts/benchmarks/tool-gateway.sh [task-prompt]
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"   # scripts/benchmarks/ -> repo root
cd "$ROOT"
. scripts/lib/compose.sh

PROMPT="${1:-Read the file sample.py in the current directory and tell me exactly what it prints. Use your tools.}"
RUNS="${RUNS:-1}"
WORKDIR="${BENCH_WORKDIR:-/tmp/ailocal-bench}"
# One benchmark state directory; benchmark_evidence.state_dir() owns the name.
OUT="$AILOCAL_STATE/benchmark/tool-gateway"
STAMP="$(date +%Y%m%d-%H%M%S)"

mkdir -p "$WORKDIR" "$OUT"
printf 'print("the answer is 42")\n' > "$WORKDIR/sample.py"

. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/../lib/output.sh"

# This script flips a production setting. If it dies partway — a bad flag, a
# Ctrl-C, an unhealthy proxy — it must not leave the proxy in a mode the
# operator did not choose. An earlier crash left it in `report` and only turned
# up on a later manual inspection.
restore_default() {
  local rc=$?
  banner "restoring gateway to the committed default (off)"
  AILOCAL_TOOL_GATEWAY=off dc up -d >/dev/null 2>&1 || true
  for _ in $(seq 1 40); do
    [ "$(docker inspect ailocal-litellm --format '{{.State.Health.Status}}' \
        2>/dev/null || echo none)" = healthy ] && break
    sleep 3
  done
  docker inspect ailocal-litellm \
    --format 'proxy health: {{.State.Health.Status}}' 2>/dev/null || true
  exit "$rc"
}
trap restore_default EXIT INT TERM

restart_with() {
  # Recreate the proxy with the requested mode, then WAIT for healthy. A
  # benchmark that starts measuring before the proxy is up measures startup.
  banner "switching gateway to '$1' and waiting for health"
  AILOCAL_TOOL_GATEWAY="$1" AILOCAL_TOOL_GATEWAY_CAPTURE=/app/captures \
    dc up -d >/dev/null 2>&1
  local s=""
  for _ in $(seq 1 40); do
    s=$(docker inspect ailocal-litellm --format '{{.State.Health.Status}}' 2>/dev/null || echo none)
    [ "$s" = healthy ] && break
    sleep 3
  done
  [ "$s" = healthy ] || { echo "proxy did not become healthy (state=$s)"; exit 1; }
  # Confirm the mode actually took effect rather than assuming the env applied.
  local got
  got=$(docker exec ailocal-litellm printenv AILOCAL_TOOL_GATEWAY)
  [ "$got" = "$1" ] || { echo "expected mode '$1' but container has '$got'"; exit 1; }
}

run_arm() {
  # Declared separately on purpose: `local a=$1 b=$a` does NOT work under
  # `set -u`, because bash expands every argument to the `local` builtin before
  # running it, so the later reference sees the variable as still unset.
  local mode="$1"
  local i="$2"
  local log="$OUT/$STAMP-$mode-$i.log"
  local start end
  # Docker's --since takes a wall-clock timestamp. Scope the metric read to
  # THIS run: `docker logs | tail -1` reads whatever is last in the current
  # container's buffer, which — when `dc up -d` finds nothing to change and so
  # does not recreate the container — can be a line from a previous arm. That
  # produced a baseline row with bytes_reachable=0 (a value the current module
  # cannot emit) and went unnoticed until the field was cross-checked.
  start=$(python3 -c 'import time;print(time.time())')
  # --permission-mode acceptEdits: without it, a non-interactive `claude -p`
  # cannot be granted write permission, so every mutating tool call returns
  # "Claude requested permissions to write ... but you haven't granted it yet".
  # That looked exactly like the model failing to edit — the tool calls were
  # in fact perfectly formed. A benchmark of a coding agent that cannot write
  # measures the wrong thing.
  zsh -ic "cd '$WORKDIR' && claude-local -p '$PROMPT' --max-turns 4 \
    --permission-mode acceptEdits" > "$log" 2>&1 || true
  end=$(python3 -c 'import time;print(time.time())')
  python3 -c "print(f'{$end-$start:.1f}')"
}

report_arm() {
  # Largest metric line since this arm began — the turn that carries the full
  # tool declaration, not a follow-up turn that re-declares a subset.
  docker logs --since "$ARM_SINCE" ailocal-litellm 2>&1 \
    | grep tool_gateway_metric \
    | python3 -c "
import sys, json
best = None
for line in sys.stdin:
    try:
        d = json.loads(line.split(' ', 1)[1])
    except Exception:
        continue
    if best is None or d.get('bytes_in', 0) > best.get('bytes_in', 0):
        best = d
if best is None:
    print('    NO METRIC for this arm — the run did not reach the proxy. '
          'Treat this row as missing, not as zero.')
    raise SystemExit
missing = [k for k in ('bytes_reachable', 'bytes_kept', 'mode')
           if k not in best]
if missing:
    print(f'    STALE METRIC (missing {missing}) — the proxy is running an '
          f'older module than this script expects. Row discarded.')
    raise SystemExit
# What the model ACTUALLY received depends on whether the filter was applied.
# In report mode the kept/dropped figures are hypothetical — printing them as
# 'model saw' would describe a request that was never sent, and would make the
# baseline arm look identical to the treatment arm in the results table.
applied = best['applied']
saw_b = best['bytes_kept'] if applied else best['bytes_reachable']
saw_t = best['tokens_est_kept'] if applied else best['tokens_est_in']
saw_n = best['tools_kept'] if applied else best['tools_in']
note = '' if applied else '  (kept/dropped below are hypothetical)'
print(f\"    latency ${1}s | mode={best['mode']} applied={applied}\")
print(f\"      model received: {saw_n} tools, {saw_b} B, ~{saw_t} tokens{note}\")
print(f\"      declared: {best['tools_in']} tools, {best['bytes_in']} B, \"
      f\"~{best['tokens_est_in']} tokens\")
"
}

echo "task:   $PROMPT"
echo "runs:   $RUNS per arm"
echo "output: $OUT/$STAMP-*"
echo

# Arms are INTERLEAVED and the order flips each round. Running all of one arm
# then all of the other confounds the comparison with everything that warms up
# over a session — the model staying resident, Ollama's caches, tiktoken's lazy
# load. The first measured A/B here showed 244s then 81s in that fixed order,
# which is unusable as evidence: cold-then-warm produces the same shape whether
# or not filtering helps.
for round in $(seq 1 "$RUNS"); do
  if [ $((round % 2)) -eq 1 ]; then order="report filter"; else order="filter report"; fi
  banner "round $round/$RUNS  order: $order"
  for mode in $order; do
    restart_with "$mode"
    # Set in the CALLER, not inside run_arm: run_arm is invoked via command
    # substitution, so anything it assigns dies with that subshell. Docker's
    # --since is second-granular, so take the stamp before the run starts.
    #
    # The trailing Z is REQUIRED. Without it `docker logs --since` reads the
    # stamp as local time; from a UTC clock that is hours in the future, so it
    # returns nothing at all — and an empty log looks exactly like "the run
    # produced no metric". Verified: same window, 0 lines without Z, 1 with.
    ARM_SINCE=$(date -u +%Y-%m-%dT%H:%M:%SZ)
    secs=$(run_arm "$mode" "$round")
    report_arm "$secs"
  done
  echo
done

if [ "$RUNS" -lt 2 ]; then
  echo "NOTE: RUNS=1 means one run per arm in a single fixed order. That is"
  echo "enough to confirm the payload change and NOT enough to attribute a"
  echo "latency difference. Use RUNS=2 or more before quoting a speedup."
  echo
fi

# The EXIT trap restores the default mode and reports proxy health.
echo
echo "Outputs for side-by-side comparison (check tool execution, not just time):"
ls -1 "$OUT/$STAMP"-*.log
