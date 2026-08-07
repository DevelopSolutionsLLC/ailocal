#!/usr/bin/env bash
# clients.sh — assertions over the GENERATED client configuration.
#
#   clients.sh [roles|codex]     (default: all sections)
#
# Both sections read what generation.py generated under the state root. They
# make no inference call, need no running proxy, and never mutate deployed
# config. Nothing executes at source time: the sections are functions and the
# dispatch is at the bottom.
set -uo pipefail
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/harness.sh"

# One resolution of the generated-client root for both sections.
_state_root() {
  [ -n "${_STATE_ROOT:-}" ] || _STATE_ROOT="$("$ROOT_DIR/ailocal" profile state-root)"
  printf '%s' "$_STATE_ROOT"
}

roles_checks() {
CONFIGURE="$(_state_root)/clients/configure.zsh"

STUB="$(temp_dir)"   # harness owns cleanup; do not install a private EXIT trap
cat > "$STUB/claude" <<'EOF'
#!/bin/sh
echo "OPUS=$ANTHROPIC_DEFAULT_OPUS_MODEL"
echo "SONNET=$ANTHROPIC_DEFAULT_SONNET_MODEL"
echo "HAIKU=$ANTHROPIC_DEFAULT_HAIKU_MODEL"
echo "FABLE=$ANTHROPIC_DEFAULT_FABLE_MODEL"
EOF
chmod +x "$STUB/claude"

# Runs claude-local with the stub first on PATH. Prints its output; returns rc.
run() { env "$@" PATH="$STUB:$PATH" zsh -c "source '$CONFIGURE' >/dev/null 2>&1; claude-local --dry" 2>&1; }

echo "client role alias overrides"

out="$(run AILOCAL_UNUSED=1)"
check $([ "$(grep -c '^OPUS=ailocal-architecture$' <<<"$out")" = 1 ] && echo 0 || echo 1) \
  "no override: architecture slot keeps its production alias" "$out"
check $([ "$(grep -c '^SONNET=ailocal-implementation$' <<<"$out")" = 1 ] && echo 0 || echo 1) \
  "no override: implementation slot unchanged" "$out"
check $([ "$(grep -c '^HAIKU=ailocal-fast$' <<<"$out")" = 1 ] && echo 0 || echo 1) \
  "no override: fast slot unchanged" "$out"
check $([ "$(grep -c '^FABLE=ailocal-review$' <<<"$out")" = 1 ] && echo 0 || echo 1) \
  "no override: review slot unchanged" "$out"

# A production alias is used as the "valid" target so the test needs no
# temporary alias and no LiteLLM mutation.
out="$(run AILOCAL_ARCHITECTURE_ALIAS_OVERRIDE=ailocal-review)"
check $([ "$(grep -c '^OPUS=ailocal-review$' <<<"$out")" = 1 ] && echo 0 || echo 1) \
  "valid override reaches the client command environment" "$out"
check $([ "$(grep -c '^SONNET=ailocal-implementation$' <<<"$out")" = 1 ] && echo 0 || echo 1) \
  "overriding architecture leaves implementation untouched" "$out"
check $([ "$(grep -c '^HAIKU=ailocal-fast$' <<<"$out")" = 1 ] && echo 0 || echo 1) \
  "overriding architecture leaves fast untouched" "$out"
check $([ "$(grep -c '^FABLE=ailocal-review$' <<<"$out")" = 1 ] && echo 0 || echo 1) \
  "overriding architecture leaves review untouched" "$out"

# Fail closed. Falling back to production here would silently measure the
# production model while reporting the candidate's name.
out="$(run AILOCAL_ARCHITECTURE_ALIAS_OVERRIDE=bench-does-not-exist)"; rc=$?
check $([ "$rc" != 0 ] && echo 0 || echo 1) \
  "unknown override fails before launching the client" "rc=$rc"
check $([ "$(grep -c '^OPUS=' <<<"$out")" = 0 ] && echo 0 || echo 1) \
  "unknown override never reaches the client at all" "$out"
check $(grep -q "not served by LiteLLM" <<<"$out" && echo 0 || echo 1) \
  "unknown override explains itself on stderr" "$out"

# OUTCOME, not configuration. The previous suite proved the slot variable reached
# the client and passed while nine turns silently served the production model:
# settings.json pins `model`, which OUTRANKS ANTHROPIC_DEFAULT_*. Verified
# precedence (code.claude.com/docs/en/settings):
#   --model > settings.json "model" > ANTHROPIC_DEFAULT_*_MODEL
tpl="$RESOURCES/clients/configure.template.zsh"
check $(grep -q -- '--model "$AILOCAL_ARCHITECTURE_ALIAS_OVERRIDE"' "$tpl" && echo 0 || echo 1) \
  "architecture override passes --model, the highest-precedence mechanism"
check $(grep -q 'claude "${_model_args\[@\]}" "$@"' "$tpl" && echo 0 || echo 1) \
  "--model args reach the claude invocation"
check $([ -z "$(AILOCAL_ARCHITECTURE_ALIAS_OVERRIDE= zsh -c "source '$CONFIGURE'; typeset -p _model_args" 2>/dev/null)" ] && echo 0 || echo 1) \
  "no override adds no --model argument (defaults untouched)"
# The proxy log is the only authority on which model actually ran. Asserted here
# as a capability; the benchmark calls it live before every candidate.
# Asserted through the IMPORT surface, not by grepping a file: what matters is
# that the capability exists, not where it lives.
check $(python3 -c "
import sys, inspect
sys.path.insert(0, '$ROOT_DIR/tests/benchmarks'); sys.path.insert(0, '$ROOT_DIR/src')
import suite as B
assert callable(B.served_models_since)
" >/dev/null 2>&1 && echo 0 || echo 1) \
  "harness can read served aliases from the proxy log"
check $(python3 -c "
import sys, inspect
sys.path.insert(0, '$ROOT_DIR/tests/benchmarks'); sys.path.insert(0, '$ROOT_DIR/src')
import suite as B
assert 'INVALID_ROUTING' in inspect.getsource(B.verify_routing)
" >/dev/null 2>&1 && echo 0 || echo 1) \
  "routing mismatch is classified INVALID_ROUTING, not warned about"

# The override block is hand-maintained and MUST live outside the spliced region,
# or generation.py would erase it on the next regeneration.
tpl="$RESOURCES/clients/configure.template.zsh"
gen_begin=$(grep -n "BEGIN GENERATED claude slots" "$tpl" | cut -d: -f1)
gen_end=$(grep -n "END GENERATED claude slots" "$tpl" | cut -d: -f1)
ovr=$(grep -n "_ailocal_ovr=(" "$tpl" | cut -d: -f1)
check $([ -n "$ovr" ] && [ "$ovr" -gt "$gen_end" ] && echo 0 || echo 1) \
  "override logic sits outside the generated region" "ovr=$ovr gen=$gen_begin-$gen_end"
check $([ "$(grep -c 'AILOCAL_ARCHITECTURE_ALIAS_OVERRIDE' "$CONFIGURE")" -ge 1 ] && echo 0 || echo 1) \
  "override logic survives generation.py regeneration"
}

codex_checks() {
GEN="$(_state_root)/clients/codex/config.toml"

echo "CODEX MCP IS WITHHELD"

check $([ -f "$GEN" ] && echo 0 || echo 1) "generated codex config exists"

# 1. The generated artifact carries no MCP server blocks.
n=$(grep -cE '^\[mcp_servers' "$GEN" 2>/dev/null || true)
check $([ "${n:-0}" -eq 0 ] && echo 0 || echo 1) \
  "generated codex config declares zero [mcp_servers.*] blocks (found ${n:-0})"

# 2. The DEPLOYED artifact, if this machine has one, also carries none. This is
#    the copy Codex actually reads, and it is where a re-sync would show up.
DEPLOYED="${XDG_CONFIG_HOME:-$HOME/.config}/ailocal/codex/config.toml"
if [ -f "$DEPLOYED" ]; then
  d=$(grep -cE '^\[mcp_servers' "$DEPLOYED" 2>/dev/null || true)
  check $([ "${d:-0}" -eq 0 ] && echo 0 || echo 1) \
    "deployed codex config declares zero [mcp_servers.*] blocks (found ${d:-0})"
  # Named servers, in case a future template nests them differently.
  for srv in grepai lsp github; do
    h=$(grep -cE "^\[mcp_servers\.${srv}" "$DEPLOYED" 2>/dev/null || true)
    check $([ "${h:-0}" -eq 0 ] && echo 0 || echo 1) \
      "deployed codex config has no ${srv} MCP server"
  done
else
  printf '  \033[33mSKIP\033[0m  codex not deployed on this machine\n'
fi

# 3. ailocal does not invoke a global Cadence MCP sync. THE core assertion: with
#    Cadence installed, the old code path ran unconditionally, so this is what
#    actually keeps Codex clean rather than luck about what Cadence holds.
IC="$ROOT_DIR/src/ailocal/clients.py"
inv=$(grep -cE '^[^#]*cadence[[:space:]]+mcp[[:space:]]+sync' "$IC" 2>/dev/null || true)
check $([ "${inv:-0}" -eq 0 ] && echo 0 || echo 1) \
  "clients.py does not invoke 'cadence mcp sync' (found ${inv:-0})"

# 4. …and no other ailocal script does either.
other=$(grep -rlE '^[^#]*cadence[[:space:]]+mcp[[:space:]]+sync' "$ROOT_DIR/scripts" 2>/dev/null \
        | grep -v "$(basename "$0")" || true)
check $([ -z "$other" ] && echo 0 || echo 1) \
  "no ailocal script re-applies Cadence MCP registrations (${other:-none})"

# 5. Absence of Codex MCP must not be reported as a failure.
warned=$(grep -cE 'warn .*codex.*no MCP|may have no MCP servers' "$IC" 2>/dev/null || true)
check $([ "${warned:-0}" -eq 0 ] && echo 0 || echo 1) \
  "an empty Codex MCP section is not warned about (found ${warned:-0})"

# 6. claude-local's registrations are preserved, not owned: .claude.json must be
#    protected rather than rewritten. Withholding MCP from Codex must not become
#    an excuse to strip Claude's.
pres=$(grep -cE '\.claude\.json' "$IC" 2>/dev/null || true)
check $([ "${pres:-0}" -ge 1 ] && echo 0 || echo 1) \
  "clients.py still preserves .claude.json (claude-local MCP survives)"



# ── Integration contract agrees with the generated clients ─────────────────
# Cadence reads the deployed integration-contract.json to decide whether a tool is
# usable. It previously published claude_native_lsp.execution="failing" (while
# the LSP baseline was green) and codex_mcp_lsp.configured=true (while Codex
# shipped zero MCP servers) -- historical experiment outcomes, not deployed
# state. A stale fact here makes Cadence apply the wrong policy, so the contract
# is checked AGAINST the generated configuration rather than on its own.
echo
echo "INTEGRATION CONTRACT MATCHES GENERATED CLIENTS"
CONTRACT="$("$ROOT_DIR/ailocal" profile state-root)/integration-contract.json"
if [ -f "$CONTRACT" ] && command -v jq >/dev/null 2>&1; then
  cfg_codex=$(jq -r '.compatibility.codex_mcp_lsp.configured' "$CONTRACT")
  check $([ "$cfg_codex" = "false" ] && echo 0 || echo 1) \
    "contract reports codex_mcp_lsp.configured=false (got $cfg_codex)"

  exec_codex=$(jq -r '.compatibility.codex_mcp_lsp.execution' "$CONTRACT")
  check $([ "$exec_codex" = "withheld_client_incompatible" ] && echo 0 || echo 1) \
    "codex MCP is classified as withheld, not as a failure (got $exec_codex)"

  # The contract claim and the artifact must not diverge: zero blocks <=> false.
  blocks=$(grep -cE '^\[mcp_servers' "$GEN" 2>/dev/null || true)
  check $([ "$cfg_codex" = "false" ] && [ "${blocks:-0}" -eq 0 ] && echo 0 || echo 1) \
    "contract and generated codex config agree (configured=$cfg_codex, blocks=${blocks:-0})"

  exec_lsp=$(jq -r '.compatibility.claude_native_lsp.execution' "$CONTRACT")
  check $([ "$exec_lsp" != "failing" ] && echo 0 || echo 1) \
    "claude_native_lsp is not reported as failing while its baseline is gated (got $exec_lsp)"

  vb=$(jq -r '.compatibility.claude_native_lsp.verified_by // ""' "$CONTRACT")
  check $([ -n "$vb" ] && [ -f "$ROOT_DIR/$vb" ] && echo 0 || echo 1) \
    "claude_native_lsp names a real verifier ($vb)"
else
  printf '  \033[33mSKIP\033[0m  contract or jq unavailable\n'
fi


# ── The REAL Cadence consumer, read-only ───────────────────────────────────
# Static shape validation inside ailocal does not establish consumer
# compatibility: this contract exists solely for Cadence, so it is checked
# against Cadence's actual loader.
#
# compose_instructions.py maps `execution` onto a FIXED
# vocabulary in _state_from() -- working | failing | blocked |
# blocked_namespace_dispatch -- and anything else falls through to `configured`.
# An invented value of "verified" therefore produced state='configured'
# ("configured but not verified working"), UNDERSTATING a capability the gate
# proves works. "working" is the word the consumer understands.
#
# Read-only: the contract is copied into a temporary XDG_CONFIG_HOME. Cadence is
# never modified, never installed, and never run against the real config root.
echo
echo "CADENCE CONSUMER (read-only)"
CADENCE_RT="$HOME/.local/share/cadence/runtime/scripts/compose_instructions.py"
if [ -f "$CADENCE_RT" ] && command -v python3 >/dev/null 2>&1; then
  out=$(python3 - "$CADENCE_RT" "$CONTRACT" <<'PY' 2>&1
import importlib.util, os, pathlib, sys, tempfile
loader, contract = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
spec = importlib.util.spec_from_file_location("ci", loader)
ci = importlib.util.module_from_spec(spec); spec.loader.exec_module(ci)
tmp = pathlib.Path(tempfile.mkdtemp()); (tmp / "ailocal").mkdir()
(tmp / "ailocal" / "integration-contract.json").write_text(contract.read_text())
os.environ["XDG_CONFIG_HOME"] = str(tmp)
c, st = ci.read_contract()
if c is None:
    print(f"STATUS={st}"); raise SystemExit
print(f"STATUS={st}")
print(f"SCHEMA={c['schema_version']}")
for k in ("claude_native_lsp", "codex_mcp_lsp"):
    e = c["compatibility"][k]
    print(f"{k.upper()}={ci._state_from(e)}|configured={e['configured']}")
PY
)
  echo "$out" | sed 's/^/        /'
  echo "$out" | grep -q "STATUS=ok"        && check 0 "Cadence accepts the contract"        || check 1 "Cadence accepts the contract"
  echo "$out" | grep -q "SCHEMA=1"          && check 0 "schema_version 1 is accepted unchanged" || check 1 "schema_version 1 is accepted unchanged"
  echo "$out" | grep -q "CLAUDE_NATIVE_LSP=working" \
    && check 0 "Cadence reads claude native LSP as working" \
    || check 1 "Cadence reads claude native LSP as working"
  echo "$out" | grep -q "CODEX_MCP_LSP=.*configured=False" \
    && check 0 "Cadence reads codex MCP as configured=False" \
    || check 1 "Cadence reads codex MCP as configured=False"
  # It must NOT resolve to a state that implies a usable, registrable tool.
  echo "$out" | grep -qE "CODEX_MCP_LSP=(working|visible)" \
    && check 1 "codex MCP does not resolve to a usable state" \
    || check 0 "codex MCP does not resolve to a usable state"
else
  printf '  \033[33mSKIP\033[0m  Cadence runtime not installed — consumer unverified\n'
fi
}

# Executed, not sourced: sourcing this file defines the sections and runs
# nothing, so a caller can source it to reuse a section without emitting checks.
if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  case "${1:-all}" in
    roles) roles_checks ;;
    codex) codex_checks ;;
    all)   roles_checks; codex_checks ;;
    *) echo "unknown section: $1 (expected roles|codex|all)" >&2; exit 2 ;;
  esac
  report || exit 1
fi
