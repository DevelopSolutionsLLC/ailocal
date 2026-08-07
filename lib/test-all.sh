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
#   ./lib/test-all.sh            # everything except the slow model benchmark
#   ./lib/test-all.sh --full     # also runs the end-to-end client benchmark
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# The suites declare their own roots: tests/harness.py is the one test
# environment owner, so nothing needs exporting here.

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

# Container health is /health/liveliness — the proxy PROCESS is up. It does not
# mean the router is serving /v1/models, which is what several checks actually
# need: the client wrapper validates an alias override against that endpoint and
# FAILS CLOSED after 5s, so a gate started while the proxy is still loading
# reports a behaviour failure that is really a readiness race. Wait for the real
# condition, bounded, and refuse rather than run against a half-ready proxy.
# 401 counts as ready: it proves the router is answering THIS route, which is
# the condition the dependent checks need. That keeps the probe independent of
# whether a master key is readable here.
_ready=""
for _i in $(seq 1 60); do
  _code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 \
             http://127.0.0.1:4000/v1/models 2>/dev/null || echo 000)"
  case "$_code" in 200|401) _ready=1; break ;; esac
  sleep 1
done
if [ -z "$_ready" ]; then
  echo
  echo "  $CONTAINER is healthy but /v1/models did not serve within 60s."
  echo "  Checks that resolve aliases through the proxy would fail as behaviour"
  echo "  regressions. Refusing to run: PRECONDITION NOT MET."
  exit 1
fi

echo
echo "UNIT / BEHAVIOUR"
run "capability registry (+ no-hard-coded-literals assertion)" \
    bash tests/in-container.sh tests/capability-registry-impl.py \
      AILOCAL_GATEWAY_SOURCE=/app/config/hooks/tool_gateway.py
run "capability negotiator (byte accounting, modes, passthrough)" \
    bash tests/in-container.sh tests/tool-gateway-impl.py \
      AILOCAL_GATEWAY_MODULE=/app/config/hooks/tool_gateway.py
run "persona injection" \
    python3 tests/gateway.py persona
# Both directions matter: a repair layer that fires on a tutorial fence would
# execute commands the model never intended.
run "tool-call repair (repairs real calls, refuses examples)" \
    python3 tests/gateway.py repair
# The trace hook reads prompts, system text, tool definitions and tool results in
# order to measure them, so each is a place a secret could enter a log. Redaction
# and disjoint token accounting are the invariants.
run "E1 trace schema, redaction and token reconciliation" \
    python3 tests/gateway.py trace
# Blinding and manifest locking are the invariants: a candidate must not be able
# to read the answer key, and the comparison must not measure one model twice.
run "planner comparison (safe defaults, locking, blinding)" \
    python3 tests/benchmark.py planner
# Gated here so a benchmark-only regression cannot reach a planner run while the
# gate still reports green.
run "benchmark library (aliases, geometry, evidence, confinement)" \
    python3 tests/benchmark.py library
run "benchmark command (models, planner, gateway dispatch)" \
    python3 tests/benchmark.py command
run "benchmark runtime stages the generated config (not the authored tree)" \
    python3 tests/benchmark.py runtime
# Non-inference paths must acquire no worktree; a leak per gate run accumulates.
run "benchmark leaks no git worktree" \
    python3 tests/benchmark.py worktree
run "profile resolver (single parser, fail-closed, no 64gb default)" \
    python3 tests/profiles.py resolver
run "policy ownership (one reader, client policy fails closed)" \
    python3 tests/profiles.py policy
run "hardware profiles (schema, tiers, dedup)" \
    python3 tests/profiles.py hardware
run "Python LSP baseline for claude-local (real documentSymbol)" \
    python3 tests/lsp-baseline.py

echo
echo "INTEGRATION"
# --full only: drives nine real generations through a local model. The cheap
# probe below covers all three dialects on every run, so a broken route is still
# caught in seconds; the full matrix proves generation quality.
if [ -n "$FULL" ]; then
  run "client compatibility (3 dialects x 3 modes)" \
      bash tests/client-compatibility.sh
fi
# Dry-run only (stub `claude` on PATH, no inference), so it stays on every run.
run "client role alias overrides (defaults intact, fails closed)" \
    bash tests/clients.sh roles

run "codex MCP is withheld (no grepai/lsp/github, no re-sync)" \
    bash tests/clients.sh codex


run "shell output helpers (streams, colour, one owner)" \
    bash tests/shell-output.sh
run "validator checks (deterministic, classification, bounded, search quota)" \
    python3 tests/validators.py
run "consolidated suites stay section-isolated" \
    python3 tests/suite-structure.py
run "generation rolls back on partial failure (never mixed on disk)" \
    python3 tests/generation-rollback.py
# Provisioning writes OUTSIDE the checkout, so the rule that an edited profile
# is never overwritten is the one that protects an operator's policy.
run "install: provisioning, provenance and tier selection" \
    python3 tests/install.py
# Claude Code sends auxiliary Anthropic-shaped probes derived from
# ANTHROPIC_BASE_URL; LiteLLM implements none of them, so HEAD /api/hello 404'd.
# Asserts the probe answers 200 AND that nothing else moved to make that true —
# /v1/models stays authenticated, health routes stay put, unknown paths still 404.
run "client compatibility probes (/api/hello, no side effects)" \
    bash tests/compat-routes.sh

echo
echo "INVARIANTS"

# Generation must be idempotent: the generated config is the deployed config,
# so a generator that is not a fixed point means the running proxy and the repo
# can silently disagree. Compared by hash, not by `git diff`, which would also
# report legitimately uncommitted work.
idempotent() {
  local before after
  _gen="$(python3 lib/profile-config state-root)/litellm/config.yaml"
  before="$(md5 -q "$_gen" 2>/dev/null || md5sum "$_gen" | cut -d' ' -f1)"
  python3 lib/sync-models.py >/dev/null 2>&1 || return 1
    after="$(md5 -q "$_gen" 2>/dev/null || md5sum "$_gen" | cut -d' ' -f1)"
  [ "$before" = "$after" ] || { echo "ailocal sync is not idempotent"; return 1; }
}
run "ailocal sync is a fixed point" idempotent

# Every shell script must parse. Cheap, and it has caught real breakage here.
shell_syntax() {
  local bad=0
  for f in ailocal lib/*.sh tests/*.sh benchmarks/*.sh clients/*.sh clients/*.zsh; do
    [ -e "$f" ] || continue
    bash -n "$f" 2>&1 || bad=1
  done
  return $bad
}
# The runtime must be the version the rest of this gate was validated against;
# a floating tag moves it silently.
litellm_version() {
  python3 -c "import sys; sys.path.insert(0, 'src')
from ailocal.checks import services as S
r = S.check_litellm_version(); print(r.summary)
sys.exit(0 if r.status.value == 'pass' else 1)"
}
run "litellm runtime matches the validated version" litellm_version
run "all shell scripts parse (bash -n)" shell_syntax

# The client must never give up before the proxy, or it abandons requests the
# proxy is still serving while the backend generates into a closed connection.
timeout_alignment() {
  local proxy client
  proxy="$(sed -n 's/^ *timeout: *\([0-9]*\).*/\1/p' deploy/litellm/config.template.yaml | head -1)"
  client="$(sed -n 's/.*AILOCAL_API_TIMEOUT_MS:-\([0-9]*\)}.*/\1/p' clients/configure.template.zsh | head -1)"
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
# lib/profile-config is Python with no extension, so it is named explicitly —
# without it, the one file every shell entry point shells out to goes unchecked.
paths = sorted(set(
    glob.glob("src/**/*.py", recursive=True)
    + glob.glob("lib/**/*.py", recursive=True)
    + glob.glob("benchmarks/**/*.py", recursive=True)
    + glob.glob("tests/**/*.py", recursive=True)
    + glob.glob("deploy/litellm/hooks/*.py")
    + ["lib/profile-config"]))
for path in paths:
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
run "installers are idempotent" bash tests/idempotent-install.sh

# The audit exits 3 when it finds actionable items. That is informational here,
# not a gate failure: untracked notes are a normal working state. Only a hard
# failure (exit 1 = the audit itself broke) fails the gate.
audit_runs() { ./ailocal audit >/dev/null 2>&1; [ $? -ne 1 ]; }
run "installation audit runs cleanly" audit_runs

if [ -n "$FULL" ]; then
  echo
  echo "END TO END (slow: drives a real client against the local model)"
  run "benchmark, 2 interleaved rounds" env RUNS=2 ./ailocal benchmark gateway
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
