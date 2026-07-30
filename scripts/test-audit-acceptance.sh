#!/usr/bin/env bash
# test-audit-acceptance.sh — the ONE command that covers the whole audit.
#
# WHY THIS IS SEPARATE FROM test-all.sh. The regression gate has to stay fast
# enough that nobody is tempted to skip it before a commit. Two of the audit
# harnesses cannot be fast: E4 streams real generations from a 30B model and
# deliberately waits out a keep-alive eviction, and F launches twelve fresh client
# sessions end to end. Folding them into the gate would push it past ten minutes
# and the gate would quietly stop being run. So the cheap, deterministic pieces
# (E2, E3) live in test-all.sh where they run constantly, and the slow live ones
# live here where they are run on purpose.
#
# COVERAGE
#   1. ailocal regression gate      (includes E1, E2, E3, E5 unit + integration)
#   2. Cadence regression gate      (includes the embedding-payload rule)
#   3. E4 streaming/disconnect matrix
#   4. F twelve-cycle client lifecycle
#
# Temporary artifacts go under data/ (gitignored) and every harness cleans up its
# own isolated containers and ports on exit, including on a failed assertion.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CADENCE="${CADENCE_REPO:-$HOME/Documents/DevelopSolutions/cadence}"
cd "$REPO"

QUICK=""
[ "${1:-}" = "--quick" ] && QUICK="--quick"

pass=0; fail=0; FAILED=()
run() {
  local label="$1"; shift
  printf '\n──────────────────────────────────────────────────────────────\n'
  printf '▶ %s\n' "$label"
  printf '──────────────────────────────────────────────────────────────\n'
  if "$@"; then
    printf '  PASS  %s\n' "$label"; pass=$((pass+1))
  else
    printf '  FAIL  %s\n' "$label"; fail=$((fail+1)); FAILED+=("$label")
  fi
}

echo "══════════════════════════════════════════════════════════════"
echo " AUDIT ACCEPTANCE — ailocal + Cadence"
echo "══════════════════════════════════════════════════════════════"

run "ailocal regression gate" ./scripts/test-all.sh

if [ -x "$CADENCE/scripts/test-cadence-all.sh" ]; then
  run "Cadence regression gate" "$CADENCE/scripts/test-cadence-all.sh"
else
  echo "  — Cadence gate skipped (not found at $CADENCE)"
fi

run "E4 streaming / disconnect matrix" \
    python3 scripts/test-stream-matrix.py --json data/e4-matrix.json $QUICK

run "F twelve-cycle client lifecycle" \
    python3 scripts/test-client-lifecycle.py --json data/f-lifecycle.json $QUICK

echo
echo "══════════════════════════════════════════════════════════════"
if [ "$fail" -gt 0 ]; then
  echo " AUDIT ACCEPTANCE: $fail of $((pass+fail)) suites FAILED"
  for f in "${FAILED[@]}"; do echo "   - $f"; done
  exit 1
fi
echo " AUDIT ACCEPTANCE: all $pass suites passed"
echo " artifacts: data/e4-matrix.json  data/f-lifecycle.json  (gitignored)"
exit 0
