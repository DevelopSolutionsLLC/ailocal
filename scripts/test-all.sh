#!/usr/bin/env bash
# test-all.sh — the single regression gate. Run this before every commit.
#
# Six suites plus two invariants. Each reports independently and the script exits
# non-zero if ANY of them fails or could not run.
#
# "Could not run" is treated as failure, not as a skip. Several suites need PyYAML
# and the registry, which exist only inside the proxy image — a host-only run
# would silently cover a fraction of the behaviour and still print green. That is
# the specific failure this file exists to prevent, so a stopped container fails
# the gate rather than reducing it.
#
# Usage:
#   ./scripts/test-all.sh            # everything except the slow model benchmark
#   ./scripts/test-all.sh --full     # also runs the end-to-end client benchmark
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

FULL=""
[ "${1:-}" = "--full" ] && FULL=1
CONTAINER="${AILOCAL_LITELLM_CONTAINER:-ailocal-litellm}"

pass=0; fail=0
declare -a FAILED=()

run() { # $1=label  $2..=command
  local label="$1"; shift
  local out rc
  out="$("$@" 2>&1)"; rc=$?
  if [ "$rc" -eq 0 ]; then
    printf '  \033[32mPASS\033[0m  %s\n' "$label"
    pass=$((pass+1))
  else
    printf '  \033[31mFAIL\033[0m  %s\n' "$label"
    printf '%s\n' "$out" | grep -E 'FAIL|Error|error|Traceback|not idempotent' \
      | head -6 | sed 's/^/          /'
    fail=$((fail+1)); FAILED+=("$label")
  fi
}

echo "══════════════════════════════════════════════════════════════════════"
echo " ailocal regression gate"
echo "══════════════════════════════════════════════════════════════════════"

# ── preflight: the container-backed suites cannot be skipped ────────────────
if ! docker ps --format '{{.Names}}' | grep -qx "$CONTAINER"; then
  echo
  echo "  $CONTAINER is not running."
  echo "  The registry, negotiator and compatibility suites all need it. Refusing"
  echo "  to run a reduced set and report success — start the stack first:"
  echo "      ./scripts/start.sh"
  exit 1
fi
health="$(docker inspect "$CONTAINER" --format '{{.State.Health.Status}}')"
if [ "$health" != healthy ]; then
  echo
  echo "  $CONTAINER health is '$health', not healthy. Fix that before trusting"
  echo "  any result below."
  exit 1
fi

echo
echo "UNIT / BEHAVIOUR"
run "capability registry (+ no-hard-coded-literals assertion)" \
    ./scripts/test-capability-registry.sh
run "capability negotiator (byte accounting, modes, passthrough)" \
    ./scripts/test-tool-gateway.sh
run "session observer (three dialects)" \
    python3 scripts/test-session-observer.py
run "verification classification (+ exit codes)" \
    ./scripts/test-verify-session.sh
run "persona injection" \
    python3 scripts/test-persona-injection.py

echo
echo "INTEGRATION"
run "client compatibility (3 dialects x 3 modes)" \
    ./scripts/test-client-compatibility.sh

echo
echo "INVARIANTS"

# sync-models.sh must be idempotent: the generated config is the deployed config,
# so a generator that is not a fixed point means the running proxy and the repo
# can silently disagree. Compared by hash, not by `git diff`, which would also
# report legitimately uncommitted work.
idempotent() {
  local before after
  before="$(md5 -q config/litellm/config.yaml 2>/dev/null \
            || md5sum config/litellm/config.yaml | cut -d' ' -f1)"
  ./scripts/sync-models.sh >/dev/null 2>&1 || return 1
  after="$(md5 -q config/litellm/config.yaml 2>/dev/null \
           || md5sum config/litellm/config.yaml | cut -d' ' -f1)"
  [ "$before" = "$after" ] || { echo "sync-models.sh is not idempotent"; return 1; }
}
run "sync-models.sh is a fixed point" idempotent

# Every shell script must parse. Cheap, and it has caught real breakage here.
shell_syntax() {
  local bad=0
  for f in scripts/*.sh config/clients/*.zsh; do
    [ -e "$f" ] || continue
    bash -n "$f" 2>&1 || bad=1
  done
  return $bad
}
run "all shell scripts parse (bash -n)" shell_syntax

# Every python module must parse, including the hooks the proxy loads.
python_syntax() {
  python3 - <<'PY'
import ast, glob, sys
bad = 0
for path in glob.glob("scripts/*.py") + glob.glob("config/litellm/*.py"):
    try:
        ast.parse(open(path, encoding="utf-8").read())
    except SyntaxError as exc:
        print(f"{path}: {exc}"); bad = 1
sys.exit(bad)
PY
}
run "all python modules parse" python_syntax

# The hooks the proxy is configured to load must actually be loadable. A
# registered-but-unimportable callback takes the container down at boot, which
# has happened here — a sibling import that works on the host fails under
# LiteLLM's spec_from_file_location loader.
hooks_importable() {
  docker exec -i "$CONTAINER" python - <<'PY'
import importlib.util, sys
mods = ["persona_injector", "model_registrar", "tool_repair", "tool_gateway",
        "session_observer", "capability_registry"]
bad = []
for name in mods:
    try:
        spec = importlib.util.spec_from_file_location(
            name, f"/app/config/{name}.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
    except Exception as exc:
        bad.append(f"{name}: {type(exc).__name__}: {exc}")
for b in bad:
    print(b)
sys.exit(1 if bad else 0)
PY
}
run "every registered hook imports inside the proxy image" hooks_importable

if [ -n "$FULL" ]; then
  echo
  echo "END TO END (slow: drives a real client against the local model)"
  run "benchmark, 2 interleaved rounds" env RUNS=2 ./scripts/benchmark-tool-gateway.sh
fi

echo
echo "══════════════════════════════════════════════════════════════════════"
if [ "$fail" -ne 0 ]; then
  echo " REGRESSION GATE: $fail FAILED, $pass passed"
  for f in "${FAILED[@]}"; do echo "   - $f"; done
  exit 1
fi
echo " REGRESSION GATE: all $pass checks passed"
[ -n "$FULL" ] || echo " (add --full for the end-to-end client benchmark)"
