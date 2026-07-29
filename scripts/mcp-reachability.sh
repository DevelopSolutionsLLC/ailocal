#!/usr/bin/env bash
# mcp-reachability.sh — answer "is this MCP tool actually available to the model?"
# for every client, at four distinct levels.
#
#   CONFIGURED   the client has the MCP server registered and can spawn it
#   TRANSMITTED  the tool's schema actually reaches the model (survives LiteLLM)
#   EMITTED      the model tried to call it
#   ACCEPTED     the client actually dispatched it and a result came back
#
# EMITTED and ACCEPTED are separate because they diverged in practice: with
# namespace expansion on, qwen3-coder emitted mcp__lsp__workspace_symbol_search
# and Codex's router answered "unsupported call". A report that collapsed those
# two would have shown that tool as working.
#
# These are four different facts and the gap between them is where this stack
# has hidden its worst surprises. Codex has grepai and lsp CONFIGURED perfectly,
# and until namespace expansion existed neither was TRANSMITTED, because LiteLLM
# discards namespace-typed tools. Nothing looked broken: the servers started, the
# config was right, and the model simply never knew the tools existed.
#
# So this reports all four columns and never collapses them. "Configured" is
# never printed as availability.
#
# Usage: ./scripts/mcp-reachability.sh
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

LEDGERS="$ROOT/data/tool-captures/sessions"
CAPTURES="$ROOT/data/tool-captures"

echo "══════════════════════════════════════════════════════════════════════"
echo " MCP reachability: configured -> transmitted -> emitted -> accepted"
echo "══════════════════════════════════════════════════════════════════════"

# ── level 1: CONFIGURED ─────────────────────────────────────────────────────
echo
echo "1. CONFIGURED (the client has the server registered)"
python3 - <<'PY'
import json, os, re

claude_cfg = os.path.expanduser("~/.config/ailocal/claude/.claude.json")
codex_cfg = os.path.expanduser("~/.config/ailocal/codex/config.toml")

found = {"claude-local": set(), "codex-local": set()}

try:
    doc = json.load(open(claude_cfg))
    def walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                if k == "mcpServers" and isinstance(v, dict):
                    found["claude-local"].update(v.keys())
                walk(v)
        elif isinstance(o, list):
            for i in o:
                walk(i)
    walk(doc)
except Exception as exc:
    print(f"   claude-local: could not read config ({type(exc).__name__})")

try:
    src = open(codex_cfg).read()
    found["codex-local"].update(re.findall(r"\[mcp_servers\.([A-Za-z0-9_-]+)\]", src))
except Exception as exc:
    print(f"   codex-local: could not read config ({type(exc).__name__})")

for client in ("claude-local", "codex-local"):
    servers = sorted(found[client])
    print(f"   {client:14} {', '.join(servers) if servers else '<none>'}")
print("   vscode         (separate connector; see Phase 7)")
print()
print("   Registration says nothing about whether the model can use them.")
PY

# ── level 2: TRANSMITTED ────────────────────────────────────────────────────
echo
echo "2. TRANSMITTED (the schema survives to the model)"
echo "   Read from real captured payloads in data/tool-captures/."
python3 - "$CAPTURES" <<'PY'
import glob, json, os, sys

caps = sys.argv[1]
files = glob.glob(os.path.join(caps, "*.json"))
if not files:
    print("   no captures — run a real client with AILOCAL_TOOL_GATEWAY_CAPTURE set")
    raise SystemExit

# Largest capture per client/route: the turn that carries the full declaration.
best = {}
for f in files:
    try:
        d = json.load(open(f))
    except Exception:
        continue
    r = d.get("report") or {}
    key = (r.get("client"), r.get("route"))
    if key[0] is None:
        continue
    if key not in best or r.get("bytes_in", 0) > best[key][1].get("bytes_in", 0):
        best[key] = (d, r)

DROPPED_TYPES = {"namespace", "shell", "computer_use", "image_generation"}

for (client, route), (doc, rep) in sorted(best.items(), key=lambda kv: str(kv[0])):
    print(f"\n   {client}  {route}")
    flat, bundled = {}, {}
    for t in doc.get("tools") or []:
        if not isinstance(t, dict):
            continue
        name = t.get("name") or (t.get("function") or {}).get("name") or ""
        ttype = t.get("type")
        if ttype in DROPPED_TYPES:
            subs = [s.get("name") for s in (t.get("tools") or [])
                    if isinstance(s, dict)]
            bundled[name or f"<{ttype}>"] = subs
        elif name.startswith("mcp__"):
            flat[name] = True
    if flat:
        servers = sorted({n.split("__")[1] for n in flat if n.count("__") >= 2})
        print(f"     TRANSMITTED as flat tools: {len(flat)} tools "
              f"from servers {servers}")
    if bundled:
        for bname, subs in sorted(bundled.items()):
            print(f"     NOT TRANSMITTED: bundle '{bname}' holding {len(subs)} "
                  f"tools — LiteLLM discards this type before the backend")
    if not flat and not bundled:
        print("     no MCP tools present in this payload at all")
PY

# ── level 3: EXECUTED ───────────────────────────────────────────────────────
echo
echo "3. EMITTED (the model tried to call it) — NOT proof of success"
echo "   Read from session ledgers written by the proxy, not from model prose."
echo "   A ledger entry proves the model ASKED for the tool. Whether the client"
echo "   dispatched it is level 4."
python3 - "$LEDGERS" <<'PY'
import glob, json, os, sys
from collections import Counter

d = sys.argv[1]
files = glob.glob(os.path.join(d, "*.json"))
if not files:
    print("   no session ledgers — set AILOCAL_SESSION_LEDGER and run a session")
    raise SystemExit

calls = Counter()
sessions = 0
for f in files:
    try:
        led = json.load(open(f))
    except Exception:
        continue
    sessions += 1
    for name in led.get("tool_call_sequence") or []:
        if str(name).startswith("mcp__"):
            calls[name] += 1

print(f"   across {sessions} ledger(s)")
if not calls:
    print("   no MCP tool was executed in any recorded session.")
    print("   NOTE: ledgers are wiped between harness tasks, so this reflects the")
    print("   most recent runs only — it is not evidence MCP has never worked.")
else:
    for name, n in calls.most_common():
        print(f"     {name:44} x{n}")
    errored = sum(json.load(open(f)).get("tool_results_errored") or 0
                  for f in files if os.path.getsize(f) > 2)
    if errored:
        print(f"   {errored} tool result(s) in these sessions came back as errors —")
        print("   an emitted call is not a successful one.")
PY

echo
echo "══════════════════════════════════════════════════════════════════════"
echo "4. ACCEPTED (the client dispatched it) — check the client's own log"
echo "   The proxy cannot see this: dispatch happens inside the client, after the"
echo "   response leaves LiteLLM. For Codex, grep its run log for"
echo "   'codex_core::tools::router' — that is where 'unsupported call' appears."
echo
echo " MEASURED [REAL] as of codex-cli 0.146.0 (re-verified 2026-07-29):"
echo "   claude-local  configured -> transmitted -> emitted -> ACCEPTED   (works)"
echo "   codex-local   configured -> NOT transmitted                      (bundles dropped)"
echo "   codex-local   with expansion: transmitted -> emitted -> REJECTED (codex#20652)"
echo
echo " A server can be CONFIGURED and not TRANSMITTED, TRANSMITTED and never"
echo " EMITTED, and EMITTED yet REJECTED. Only the last column proves the chain."
