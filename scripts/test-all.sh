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

# A gate nobody runs protects nothing, so every check reports its own duration and
# anything at or over SLOW_S is flagged. Timing per check, not just in total, is what
# makes the one slow check findable instead of the whole gate feeling slow.
SLOW_S="${AILOCAL_GATE_SLOW_S:-10}"
SLOW=()

run() { # $1=label  $2..=command
  local label="$1"; shift
  local out rc t0 t1 secs
  t0=$(date +%s)
  out="$("$@" 2>&1)"; rc=$?
  t1=$(date +%s); secs=$((t1 - t0))
  local mark=""
  if [ "$secs" -ge "$SLOW_S" ]; then mark=" \033[33m[${secs}s]\033[0m"; SLOW+=("$label (${secs}s)")
  elif [ "$secs" -ge 2 ]; then mark=" (${secs}s)"; fi
  if [ "$rc" -eq 0 ]; then
    printf '  \033[32mPASS\033[0m  %s'"$mark"'\n' "$label"
    pass=$((pass+1))
  else
    printf '  \033[31mFAIL\033[0m  %s'"$mark"'\n' "$label"
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
  echo "      ailocal start"
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
    bash scripts/tests/capability-registry.sh
run "capability negotiator (byte accounting, modes, passthrough)" \
    bash scripts/tests/tool-gateway.sh
run "persona injection" \
    python3 scripts/tests/persona-injection.py
# Both directions matter: a repair layer that fires on a tutorial fence would
# execute commands the model never intended.
run "tool-call repair (repairs real calls, refuses examples)" \
    python3 scripts/tests/tool-repair.py
# E5. The message this replaces ("No fallback model group found ... Fallbacks=[...]")
# was true and misleading: implementation is the TERMINAL tier, so having no chain is
# intentional, and the real fault was upstream connectivity. Pure functions, so all
# seven states are checked with no proxy and no model.
# E1. The hook READS prompts, system text, tool definitions and tool results in
# order to measure them, so every one of those is a place a secret or a source file
# could enter a log. These tests push secret- and prompt-shaped values through the
# real helpers and prove they never serialize, and that the token components are
# disjoint and sum to the reported total.
run "E1 trace schema, redaction and token reconciliation" \
    python3 scripts/tests/request-trace.py
# E3. Declared num_ctx vs what the backend actually serves. nomic-embed-text silently
# CLIPS at 2048 rather than erroring, so an over-declaration yields successful-looking
# embeddings of truncated text — no error to notice, just quietly worse vectors. The
# 8192 over-declaration was corrected at its source in config/profiles/64gb.yaml
# (db8c9e6) and regenerated, so this now guards the corrected state rather than
# reporting a known failure.
# The isolated claude-local root sets ENABLE_LSP_TOOL=1, but a plugin is what puts
# a server behind that tool. Provisioning used to be delegated entirely to
# Cadence, so an ailocal-only machine got the tool switched on with nothing behind
# it. This drives pyright-langserver over stdio against a real repo file and
# requires real symbols back — presence of a plugin is not capability.
run "hardware profiles (schema, tiers, dedup)" \
    python3 scripts/tests/profiles.py
run "Python LSP baseline for claude-local (real documentSymbol)" \
    python3 scripts/tests/lsp-baseline.py

echo
echo "INTEGRATION"
# Guards a backported LiteLLM fix. The bug it covers is NON-BLOCKING — streamed
# /v1/messages kept working while success logging raised on every request — so
# without a test its return would be invisible. Asserts the observable property
# (no validation error) rather than "the patch is installed", so it also catches
# a LiteLLM upgrade that makes the patch no-op while the bug persists.
# MOVED TO --full. This drives nine REAL generations through a local model and
# measured 51s of a 73s gate — the single reason the gate was slow enough to skip.
# The cheap probe below still covers all three dialects on every run, so a broken
# route is caught in seconds; the full matrix proves generation quality, which is
# what --full is for.
if [ -n "$FULL" ]; then
  run "client compatibility (3 dialects x 3 modes)" \
      bash scripts/tests/client-compatibility.sh
fi
# Claude Code sends auxiliary Anthropic-shaped probes derived from
# ANTHROPIC_BASE_URL; LiteLLM implements none of them, so HEAD /api/hello 404'd.
# Asserts the probe answers 200 AND that nothing else moved to make that true —
# /v1/models stays authenticated, health routes stay put, unknown paths still 404.
run "client compatibility probes (/api/hello, no side effects)" \
    bash scripts/tests/compat-routes.sh

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
  bash scripts/sync-models.sh >/dev/null 2>&1 || return 1
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
run "litellm runtime matches the validated version" bash "$ROOT/scripts/check-litellm-version.sh"
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
run "installers are idempotent" bash scripts/tests/idempotent-install.sh

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
  if [ ${#SLOW[@]} -gt 0 ]; then
    printf " %d check(s) at/over %ss — keep the gate fast enough to run:\n" "${#SLOW[@]}" "$SLOW_S"
    printf "   %s\n" "${SLOW[@]}"
  fi
  : ""
[ -n "$FULL" ] || echo " (add --full for the client-compatibility matrix and end-to-end benchmark)"
