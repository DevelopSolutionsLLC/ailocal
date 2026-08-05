#!/usr/bin/env bash
# test-all.sh — the single regression gate. Run this before every commit.
#
# Unit suites, integration checks and invariants (21 in total; the runner is the
# source of truth for the count). Each reports independently and the script exits
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
    bash scripts/tests/in-container.sh scripts/tests/capability-registry-impl.py \
      AILOCAL_GATEWAY_SOURCE=/app/config/hooks/tool_gateway.py
run "capability negotiator (byte accounting, modes, passthrough)" \
    bash scripts/tests/in-container.sh scripts/tests/tool-gateway-impl.py \
      AILOCAL_GATEWAY_MODULE=/app/config/hooks/tool_gateway.py
run "persona injection" \
    python3 scripts/tests/gateway.py persona
# Both directions matter: a repair layer that fires on a tutorial fence would
# execute commands the model never intended.
run "tool-call repair (repairs real calls, refuses examples)" \
    python3 scripts/tests/gateway.py repair
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
    python3 scripts/tests/gateway.py trace
# The planner comparison is the one benchmark whose SETUP has repeatedly been the
# defect: it once measured a single model three times, and candidates could read
# the answer key. These prove safe defaults, manifest locking, confinement wiring
# and identity-stripped scoring copies -- with no inference.
run "planner comparison (safe defaults, locking, blinding)" \
    python3 scripts/tests/benchmark.py planner
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
# config/profiles/*.yaml are the ONLY authoritative deployment config, and
# config/active-profile has no implicit default. These prove there is one
# parser and that every entry point fails closed rather than assuming 64gb.
# The benchmark library owns alias construction, evidence capture, admission
# geometry and restoration. It was NOT in the gate: a whole suite could fail
# while the gate reported green, which is how a benchmark-only regression
# reaches a planner run unnoticed.
run "benchmark library (aliases, geometry, evidence, confinement)" \
    python3 scripts/tests/benchmark.py library
run "benchmark command (models, planner, gateway dispatch)" \
    python3 scripts/tests/benchmark.py command
run "benchmark runtime stages the generated config (not the authored tree)" \
    python3 scripts/tests/benchmark.py runtime
run "profile resolver (single parser, fail-closed, no 64gb default)" \
    python3 scripts/tests/profiles.py resolver
run "policy ownership (one reader, client policy fails closed)" \
    python3 scripts/tests/profiles.py policy
run "hardware profiles (schema, tiers, dedup)" \
    python3 scripts/tests/profiles.py hardware
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
# Dry-run only (stub `claude` on PATH, no inference), so it stays on every run.
run "client role alias overrides (defaults intact, fails closed)" \
    bash scripts/tests/client-role-override.sh

run "codex MCP is withheld (no grepai/lsp/github, no re-sync)" \
    bash scripts/tests/codex-mcp-withheld.sh

run "commit-msg hook (blocks attribution, allows product names)" \
    bash scripts/tests/commit-msg-hook.sh

run "shell output helpers (streams, colour, one owner)" \
    bash scripts/tests/shell-output.sh
run "validator checks (deterministic, classification, bounded)" \
    python3 scripts/tests/validators.py
run "consolidated suites stay section-isolated" \
    python3 scripts/tests/suite-structure.py
run "generation rolls back on partial failure (never mixed on disk)" \
    python3 scripts/tests/generation-rollback.py
# Claude Code sends auxiliary Anthropic-shaped probes derived from
# ANTHROPIC_BASE_URL; LiteLLM implements none of them, so HEAD /api/hello 404'd.
# Asserts the probe answers 200 AND that nothing else moved to make that true —
# /v1/models stays authenticated, health routes stay put, unknown paths still 404.
run "client compatibility probes (/api/hello, no side effects)" \
    bash scripts/tests/compat-routes.sh

echo
echo "INVARIANTS"

# Generation must be idempotent: the generated config is the deployed config,
# so a generator that is not a fixed point means the running proxy and the repo
# can silently disagree. Compared by hash, not by `git diff`, which would also
# report legitimately uncommitted work.
idempotent() {
  local before after
  _gen="$(./scripts/profile-config state-root)/litellm/config.yaml"
  before="$(md5 -q "$_gen" 2>/dev/null || md5sum "$_gen" | cut -d' ' -f1)"
  python3 scripts/sync-models.py >/dev/null 2>&1 || return 1
    after="$(md5 -q "$_gen" 2>/dev/null || md5sum "$_gen" | cut -d' ' -f1)"
  [ "$before" = "$after" ] || { echo "ailocal sync is not idempotent"; return 1; }
}
run "ailocal sync is a fixed point" idempotent

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

# The architecture-outage invariant. The client must never give up BEFORE the
# proxy: when it did, a long cold prompt eval (789 s measured at 87,791 tokens)
# was abandoned by Claude Code on its own undocumented default while LiteLLM
# waited 900 s and Ollama kept generating into a closed connection. Only the
# deterministic part of that defect is asserted here -- the two numbers agreeing
# -- because reproducing the latency itself costs 13 minutes of GPU time.
timeout_alignment() {
  local proxy client
  proxy="$(sed -n 's/^ *timeout: *\([0-9]*\).*/\1/p' deploy/litellm/config.template.yaml | head -1)"
  client="$(sed -n 's/.*AILOCAL_API_TIMEOUT_MS:-\([0-9]*\)}.*/\1/p' config/clients/configure.template.zsh | head -1)"
  if [ -z "$proxy" ] || [ -z "$client" ]; then
    echo "could not read both timeouts (proxy='$proxy' client='$client')"; return 1
  fi
  if [ "$client" -lt "$((proxy * 1000))" ]; then
    echo "client API_TIMEOUT_MS ${client} is BELOW LiteLLM timeout ${proxy}s"
    echo "the client would abandon requests the proxy is still serving"
    return 1
  fi
  return 0
}
run "client timeout is not below the proxy timeout" timeout_alignment

# Every python module must parse, including the hooks the proxy loads.
python_syntax() {
  python3 - <<'PY'
import ast, glob, sys
bad = 0
for path in glob.glob("scripts/*.py") + glob.glob("deploy/litellm/hooks/*.py"):
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
mods = ["persona_injector", "reasoning_router", "startup", "tool_repair",
        "tool_gateway", "session_observer", "capability_registry"]
bad = []
for name in mods:
    try:
        spec = importlib.util.spec_from_file_location(
            name, f"/app/config/hooks/{name}.py")
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
  run "benchmark, 2 interleaved rounds" env RUNS=2 ./scripts/ailocal benchmark gateway
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
