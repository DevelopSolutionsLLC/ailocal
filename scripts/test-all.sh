#!/usr/bin/env bash
# test-all.sh — the single regression gate. Run this before every commit.
#
# Six suites plus six invariants. Each reports independently and the script exits
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
# Both directions matter: a repair layer that fires on a tutorial fence would
# execute commands the model never intended.
run "tool-call repair (repairs real calls, refuses examples)" \
    python3 scripts/test-tool-repair.py
# E5. The message this replaces ("No fallback model group found ... Fallbacks=[...]")
# was true and misleading: implementation is the TERMINAL tier, so having no chain is
# intentional, and the real fault was upstream connectivity. Pure functions, so all
# seven states are checked with no proxy and no model.
run "E5 fallback-state classification (seven states, no live model)" \
    python3 scripts/test-fallback-state.py
# E1. The hook READS prompts, system text, tool definitions and tool results in
# order to measure them, so every one of those is a place a secret or a source file
# could enter a log. These tests push secret- and prompt-shaped values through the
# real helpers and prove they never serialize, and that the token components are
# disjoint and sum to the reported total.
run "E1 trace schema, redaction and token reconciliation" \
    python3 scripts/test-request-trace.py
# E3. Declared num_ctx vs what the backend actually serves. nomic-embed-text silently
# CLIPS at 2048 rather than erroring, so an over-declaration yields successful-looking
# embeddings of truncated text — no error to notice, just quietly worse vectors. The
# 8192 over-declaration was corrected at its source in config/profiles/64gb.yaml
# (db8c9e6) and regenerated, so this now guards the corrected state rather than
# reporting a known failure.
run "E3 declared context vs backend capacity" \
    python3 scripts/test-context-limits.py
# E2. Readiness must track the UPSTREAM, not just the process. Measured: both
# /health/liveliness and /health/readiness answer 200 "healthy" in single-digit ms
# with nothing listening on the backend port, so a doctor built on either reports a
# green system during a total Ollama outage. Runs entirely on an ISOLATED proxy and
# a fake upstream on their own ports — it never touches the live stack or the shared
# Ollama daemon that Cadence's index also depends on.
run "E2 isolated readiness transitions (own proxy, fake upstream)" \
    python3 scripts/test-readiness-isolated.py

echo
echo "INTEGRATION"
# Guards a backported LiteLLM fix. The bug it covers is NON-BLOCKING — streamed
# /v1/messages kept working while success logging raised on every request — so
# without a test its return would be invisible. Asserts the observable property
# (no validation error) rather than "the patch is installed", so it also catches
# a LiteLLM upgrade that makes the patch no-op while the bug persists.
run "anthropic streaming logging (no AnthropicResponse validation error)" \
    python3 scripts/test-anthropic-stream-logging.py
run "client compatibility (3 dialects x 3 modes)" \
    ./scripts/test-client-compatibility.sh
# Claude Code sends auxiliary Anthropic-shaped probes derived from
# ANTHROPIC_BASE_URL; LiteLLM implements none of them, so HEAD /api/hello 404'd.
# Asserts the probe answers 200 AND that nothing else moved to make that true —
# /v1/models stays authenticated, health routes stay put, unknown paths still 404.
run "client compatibility probes (/api/hello, no side effects)" \
    ./scripts/test-compat-routes.sh

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
# The runtime must be the version the rest of this gate was validated against.
# A floating tag silently moved us from 1.92.0 to 1.93.0 while the docs still
# claimed the old one, so every "verified on" note referred to a version that was
# no longer running.
run "litellm runtime matches the validated version" "$ROOT/scripts/check-litellm-version.sh"
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
        "session_observer", "capability_registry", "compat_routes"]
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

# Re-running an installer must change nothing. This is the check that catches an
# installation rotting into duplicate MCP stanzas / provider groups / shell
# blocks — invisible until something picks the wrong duplicate.
run "installers are idempotent" ./scripts/test-idempotent-install.sh

# The audit exits 3 when it finds actionable items. That is informational here,
# not a gate failure: untracked notes are a normal working state. Only a hard
# failure (exit 1 = the audit itself broke) fails the gate.
audit_runs() { ./scripts/audit-installation.sh >/dev/null 2>&1; [ $? -ne 1 ]; }
run "installation audit runs cleanly" audit_runs

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
