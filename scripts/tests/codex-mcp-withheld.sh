#!/usr/bin/env bash
# codex-local has NO MCP servers, by policy — and ailocal must not restore them.
#
# THE REGRESSION THIS GUARDS. install-clients.sh used to run `cadence mcp sync`
# immediately after rewriting codex/config.toml, explicitly to put Cadence's
# [mcp_servers.*] blocks (grepai, lsp) BACK, and warned when Codex ended up with
# none. That predates the settled client policy:
#
#   codex-local intentionally has no grepai MCP, no LSP MCP, no GitHub MCP and
#   no namespace tools. Codex cannot dispatch namespaced tool names, so an MCP
#   server there advertises a surface it cannot drive.
#
# So an empty MCP section is the CORRECT outcome, and a global `cadence mcp
# sync` is doubly wrong: it re-adds what policy withholds, and it mutates other
# clients as a side effect of installing this one.
#
# Cadence keeps MCP ownership. ailocal simply stops undoing and redoing its work.
# claude-local is unaffected: its registrations live in .claude.json, which
# install-clients.sh preserves rather than rewriting.
#
# Static + fixture based: it must not depend on Cadence actually being installed,
# and it must never mutate the real deployed config.
set -uo pipefail
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/harness.sh"

echo "CODEX MCP IS WITHHELD"

GEN="$("$ROOT_DIR/scripts/profile-config" state-root)/clients/codex/config.toml"
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
IC="$ROOT_DIR/scripts/install-clients.sh"
inv=$(grep -cE '^[^#]*cadence[[:space:]]+mcp[[:space:]]+sync' "$IC" 2>/dev/null || true)
check $([ "${inv:-0}" -eq 0 ] && echo 0 || echo 1) \
  "install-clients.sh does not invoke 'cadence mcp sync' (found ${inv:-0})"

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
  "install-clients.sh still preserves .claude.json (claude-local MCP survives)"



# ── Integration contract agrees with the generated clients ─────────────────
# Cadence reads config/integration-contract.json to decide whether a tool is
# usable. It previously published claude_native_lsp.execution="failing" (while
# the LSP baseline was green) and codex_mcp_lsp.configured=true (while Codex
# shipped zero MCP servers) -- historical experiment outcomes, not deployed
# state. A stale fact here makes Cadence apply the wrong policy, so the contract
# is checked AGAINST the generated configuration rather than on its own.
echo
echo "INTEGRATION CONTRACT MATCHES GENERATED CLIENTS"
CONTRACT="$ROOT_DIR/config/integration-contract.json"
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
# MEASURED 2026-08-04: compose_instructions.py maps `execution` onto a FIXED
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
  out=$(python3 - "$CADENCE_RT" "$ROOT_DIR/config/integration-contract.json" <<'PY' 2>&1
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

report || exit 1