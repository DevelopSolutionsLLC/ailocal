#!/usr/bin/env bash
# benchmark-tool-gateway.sh — A/B the gateway on the real client, real model.
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
# Usage:  ./scripts/benchmark-tool-gateway.sh [task-prompt]
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
. scripts/lib/compose.sh

PROMPT="${1:-Read the file sample.py in the current directory and tell me exactly what it prints. Use your tools.}"
RUNS="${RUNS:-1}"
WORKDIR="${BENCH_WORKDIR:-/tmp/ailocal-bench}"
OUT="$ROOT/data/benchmarks"
STAMP="$(date +%Y%m%d-%H%M%S)"

mkdir -p "$WORKDIR" "$OUT"
printf 'print("the answer is 42")\n' > "$WORKDIR/sample.py"

info() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }

restart_with() {
  # Recreate the proxy with the requested mode, then WAIT for healthy. A
  # benchmark that starts measuring before the proxy is up measures startup.
  info "switching gateway to '$1' and waiting for health"
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
  local mode="$1" i="$2" log="$OUT/$STAMP-$mode-$i.log"
  local start end
  start=$(python3 -c 'import time;print(time.time())')
  zsh -ic "cd '$WORKDIR' && claude-local -p '$PROMPT' --max-turns 4" \
    > "$log" 2>&1 || true
  end=$(python3 -c 'import time;print(time.time())')
  python3 -c "print(f'{$end-$start:.1f}')"
}

echo "task:   $PROMPT"
echo "runs:   $RUNS per arm"
echo "output: $OUT/$STAMP-*"
echo

for mode in report filter; do
  restart_with "$mode"
  for i in $(seq 1 "$RUNS"); do
    info "run $i/$RUNS  mode=$mode"
    secs=$(run_arm "$mode" "$i")
    # The gateway's own last metric line is the record of what the model saw.
    metric=$(docker logs ailocal-litellm 2>&1 | grep tool_gateway_metric | tail -1)
    echo "$metric" | python3 -c "
import sys, json
line = sys.stdin.read().strip()
d = json.loads(line.split(' ', 1)[1]) if line else {}
kept = d.get('bytes_kept', 0); base = d.get('bytes_reachable', 0)
print(f\"    latency {'$secs'}s | mode={d.get('mode')} applied={d.get('applied')} \"
      f\"| tools {d.get('tools_in')} -> {d.get('tools_kept')} \"
      f\"| model saw {kept} B of {base} B\")
"
  done
  echo
done

info "leaving the proxy in the committed default (off)"
AILOCAL_TOOL_GATEWAY=off dc up -d >/dev/null 2>&1
for _ in $(seq 1 40); do
  [ "$(docker inspect ailocal-litellm --format '{{.State.Health.Status}}')" = healthy ] && break
  sleep 3
done
docker inspect ailocal-litellm --format 'proxy health: {{.State.Health.Status}}'

echo
echo "Outputs for side-by-side comparison (check tool execution, not just time):"
ls -1 "$OUT/$STAMP"-*.log
