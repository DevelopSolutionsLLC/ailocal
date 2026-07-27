#!/usr/bin/env bash
# audit-installation.sh — report what is installed, what is duplicated, and what
# is stale. READ ONLY. It never deletes, moves, or rewrites anything.
#
# Companion: scripts/cleanup-installation.sh acts on these findings, behind
# --dry-run / --apply, with a backup first.
#
# ── the distinction this script exists to preserve ──
# Two configs for the same client are usually CORRECT here, not a duplicate:
# ailocal deliberately keeps ~/.claude (cloud) separate from
# ~/.config/ailocal/claude (local) so both can coexist. Flagging that pair as a
# duplicate would push someone into deleting a working setup. So every finding is
# classified:
#
#   OK          expected, by design
#   STALE       left over, safe to remove, nothing reads it
#   DUPLICATE   two things that genuinely conflict — one wins unpredictably
#   MISSING     expected and absent
#   UNKNOWN     could not determine; explicitly NOT reported as either state
#
# Exit: 0 nothing actionable, 3 actionable findings, 1 the audit itself failed.
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

FINDINGS_FILE="${AUDIT_FINDINGS:-/tmp/ailocal-audit-findings.txt}"
: > "$FINDINGS_FILE"

C_OK=$'\033[32m'; C_WARN=$'\033[33m'; C_BAD=$'\033[31m'; C_DIM=$'\033[2m'; C_0=$'\033[0m'
actionable=0

hdr()  { printf '\n\033[1m%s\033[0m\n' "$*"; }
line() { printf '  %s\n' "$*"; }
okk()  { printf '  %s✓%s %s\n' "$C_OK" "$C_0" "$*"; }
miss() { printf '  %s—%s %s\n' "$C_DIM" "$C_0" "$*"; }
flag() { # $1=class $2=item $3=location $4=action
  local colour="$C_WARN"
  [ "$1" = DUPLICATE ] && colour="$C_BAD"
  printf '  %s%s%s %s\n' "$colour" "$1" "$C_0" "$2"
  printf '      location: %s\n' "$3"
  printf '      action:   %s\n' "$4"
  printf '%s\t%s\t%s\t%s\n' "$1" "$2" "$3" "$4" >> "$FINDINGS_FILE"
  actionable=$((actionable + 1))
}

echo "══════════════════════════════════════════════════════════════════════"
echo " AILOCAL / CADENCE INSTALLATION AUDIT"
echo " $(date '+%Y-%m-%d %H:%M:%S')   read-only"
echo "══════════════════════════════════════════════════════════════════════"

# ── Clients ─────────────────────────────────────────────────────────────────
hdr "Clients"

CLAUDE_LOCAL="$HOME/.config/ailocal/claude"
line "Claude Code"
if [ -d "$CLAUDE_LOCAL" ]; then
  okk "local root      $CLAUDE_LOCAL ($(du -sh "$CLAUDE_LOCAL" 2>/dev/null | cut -f1))"
  if [ -f "$CLAUDE_LOCAL/.claude.json" ]; then
    okk "local state     .claude.json present (per-root: MCP, history, creds)"
  else
    flag MISSING "claude local .claude.json" "$CLAUDE_LOCAL" \
      "run ./scripts/install-clients.sh claude"
  fi
  if [ -d "$HOME/.claude" ]; then
    okk "cloud root      ~/.claude — SEPARATE BY DESIGN, not a duplicate"
  fi
  # A wrapper that points at the cloud root would silently bill the cloud.
  if grep -q 'CLAUDE_CONFIG_DIR' "$HOME/.config/ailocal/configure.zsh" 2>/dev/null; then
    okk "isolation       configure.zsh sets CLAUDE_CONFIG_DIR"
  else
    flag DUPLICATE "claude-local may share the cloud config root" \
      "$HOME/.config/ailocal/configure.zsh" \
      "ensure CLAUDE_CONFIG_DIR is exported by the wrapper"
  fi
else
  flag MISSING "claude local root" "$CLAUDE_LOCAL" \
    "run ./scripts/install-clients.sh claude"
fi

line ""
line "Codex CLI"
CODEX_LOCAL="$HOME/.config/ailocal/codex"
if [ -f "$CODEX_LOCAL/config.toml" ]; then
  okk "local config    $CODEX_LOCAL/config.toml"
  [ -f "$HOME/.codex/config.toml" ] && \
    okk "cloud config    ~/.codex/config.toml — SEPARATE BY DESIGN"
  N_MCP=$(grep -c '^\[mcp_servers\.' "$CODEX_LOCAL/config.toml" 2>/dev/null || echo 0)
  okk "MCP servers     $N_MCP registered"
  # Duplicate server stanzas make one silently win.
  DUPS=$(grep -o '^\[mcp_servers\.[A-Za-z0-9_-]*\]' "$CODEX_LOCAL/config.toml" 2>/dev/null \
         | sort | uniq -d)
  [ -n "$DUPS" ] && flag DUPLICATE "repeated MCP stanza(s): $(echo "$DUPS" | tr '\n' ' ')" \
    "$CODEX_LOCAL/config.toml" "remove the duplicate [mcp_servers.*] block"
else
  flag MISSING "codex local config" "$CODEX_LOCAL/config.toml" \
    "run ./scripts/install-clients.sh codex"
fi

line ""
line "VS Code"
VSC_USER="$HOME/Library/Application Support/Code/User"
[ -d "$VSC_USER" ] || VSC_USER="$HOME/.config/Code/User"
if command -v code >/dev/null 2>&1; then
  if code --list-extensions 2>/dev/null | grep -qix "Gethnet.litellm-connector-copilot"; then
    okk "extension       litellm-connector-copilot $(code --list-extensions --show-versions 2>/dev/null | grep -i litellm | cut -d@ -f2)"
  else
    flag MISSING "connector extension" "VS Code" "run ./scripts/install-vscode.sh"
  fi
else
  line "$C_DIM—$C_0 code CLI not on PATH; VS Code checks skipped (UNKNOWN, not failing)"
fi
if [ -f "$VSC_USER/chatLanguageModels.json" ]; then
  python3 - "$VSC_USER/chatLanguageModels.json" <<'PY'
import json, sys
try:
    entries = json.load(open(sys.argv[1]))
except Exception as exc:
    print(f"  \033[33mUNKNOWN\033[0m provider file unparseable: {exc}")
    raise SystemExit
by_url = {}
for e in entries:
    if not isinstance(e, dict):
        continue
    by_url.setdefault((e.get("baseUrl"), e.get("vendor")), []).append(e.get("name"))
print(f"  \033[32m✓\033[0m provider groups {len(entries)}")
for (url, vendor), names in by_url.items():
    mark = "\033[31mDUPLICATE\033[0m" if len(names) > 1 else "\033[32m✓\033[0m"
    print(f"      {mark} {vendor} -> {url}  ({', '.join(str(n) for n in names)})")
    if len(names) > 1:
        print("      action:   remove the extra group; the model picker will show "
              "both and one wins unpredictably")
# A native customendpoint group pointed at the same proxy is a known dead end:
# VS Code sends an empty Bearer, which LiteLLM rejects.
dead = [e.get("name") for e in entries
        if isinstance(e, dict) and e.get("vendor") == "customendpoint"]
if dead:
    print(f"  \033[33mSTALE\033[0m customendpoint group(s): {dead}")
    print("      action:   remove — VS Code sends an empty Bearer for this vendor")
PY
else
  flag MISSING "VS Code provider group" "$VSC_USER/chatLanguageModels.json" \
    "run ./scripts/install-vscode.sh"
fi
python3 - "$VSC_USER/settings.json" <<'PY'
import json, os, re, sys
path = sys.argv[1]
try:
    raw = open(path, encoding="utf-8").read()
except FileNotFoundError:
    print("  \033[2m—\033[0m no settings.json"); raise SystemExit
try:
    doc = json.loads(re.sub(r",(\s*[}\]])", r"\1", re.sub(r"//[^\n]*", "", raw)))
except Exception:
    print("  \033[33mUNKNOWN\033[0m settings.json not parseable; not judged")
    raise SystemExit
DEAD = ["litellm-connector.baseUrl", "litellm-connector.backends",
        "github.copilot.chat.customOAIModels",
        "github.copilot.agent.autoApprove",
        "github.copilot.chat.tools.terminal.autoApprove"]
found = [k for k in DEAD if k in doc]
if found:
    print(f"  \033[33mSTALE\033[0m {len(found)} deprecated/inert setting(s): {found}")
    print("      action:   ./scripts/install-vscode.sh (prunes them, backs up first)")
else:
    print("  \033[32m✓\033[0m no deprecated settings")
PY

# ── MCP ─────────────────────────────────────────────────────────────────────
hdr "MCP"
python3 - <<'PY'
import json, os, re
claude = os.path.expanduser("~/.config/ailocal/claude/.claude.json")
codex = os.path.expanduser("~/.config/ailocal/codex/config.toml")
c_servers = set()
try:
    doc = json.load(open(claude))
    def walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                if k == "mcpServers" and isinstance(v, dict):
                    c_servers.update(v)
                walk(v)
        elif isinstance(o, list):
            for i in o: walk(i)
    walk(doc)
except Exception:
    pass
x_servers = set()
try:
    x_servers = set(re.findall(r"\[mcp_servers\.([A-Za-z0-9_-]+)\]", open(codex).read()))
except Exception:
    pass
for name in sorted(c_servers | x_servers):
    where = []
    if name in c_servers: where.append("claude")
    if name in x_servers: where.append("codex")
    print(f"  \033[32m✓\033[0m {name:10} registered in: {', '.join(where)}")
if not (c_servers or x_servers):
    print("  \033[33mMISSING\033[0m no MCP servers registered in either client")
print()
print("  Reachability is a SEPARATE question — registration is not availability.")
print("  Codex: MCP is registered and does NOT reach the model (LiteLLM drops")
print("  namespace tools; flattened names are rejected by Codex's router,")
print("  openai/codex#20652). Run ./scripts/mcp-reachability.sh for the full")
print("  configured -> transmitted -> emitted -> accepted table.")
PY

# ── LiteLLM ─────────────────────────────────────────────────────────────────
hdr "LiteLLM"
if docker ps --format '{{.Names}}' | grep -qx ailocal-litellm; then
  H=$(docker inspect ailocal-litellm --format '{{.State.Health.Status}}')
  okk "container       ailocal-litellm ($H)"
  MOUNTED=$(docker inspect ailocal-litellm --format '{{range .Mounts}}{{.Source}}|{{end}}' \
            | tr '|' '\n' | grep -c "$ROOT" || true)
  okk "mounts          $MOUNTED path(s) from this repo"
  CB=$(grep -cE '^\s+- [a-z_]+\.proxy_handler_instance' config/litellm/config.yaml || echo 0)
  okk "callbacks       $CB hook(s) registered"
  # A hook registered but unimportable takes the proxy down at boot.
  if docker exec -i ailocal-litellm python - >/dev/null 2>&1 <<'PY'
import importlib.util, sys
for n in ("persona_injector","model_registrar","tool_repair","tool_gateway",
          "session_observer","capability_registry"):
    s = importlib.util.spec_from_file_location(n, f"/app/config/{n}.py")
    m = importlib.util.module_from_spec(s); sys.modules[n] = m
    s.loader.exec_module(m)
PY
  then okk "hooks import    all registered hooks load inside the image"
  else flag DUPLICATE "a registered hook does not import" "config/litellm/" \
        "run ./scripts/test-all.sh to see which"
  fi
  MODE=$(docker exec ailocal-litellm printenv AILOCAL_TOOL_GATEWAY 2>/dev/null || echo off)
  okk "gateway mode    $MODE"
else
  flag MISSING "LiteLLM container" "docker" "run ./scripts/start.sh"
fi

# ── stale repo artifacts ────────────────────────────────────────────────────
hdr "Repository artifacts"
for f in config/litellm/config.yaml.backup audit-session.json next-session.md; do
  if [ -e "$f" ]; then
    if git ls-files --error-unmatch "$f" >/dev/null 2>&1; then
      okk "$f (tracked)"
    else
      flag STALE "untracked working file: $f" "$ROOT/$f" \
        "delete, or move under data/ which is gitignored"
    fi
  fi
done
BK=$(ls -1 backups/ 2>/dev/null | wc -l | tr -d ' ')
if [ "${BK:-0}" -gt 20 ]; then
  flag STALE "$BK files in backups/" "$ROOT/backups" "prune the oldest"
else
  okk "backups/        $BK file(s)"
fi
# The generated config must match its sources, or the running proxy and the repo
# silently disagree.
H1=$(md5 -q config/litellm/config.yaml 2>/dev/null || md5sum config/litellm/config.yaml | cut -d' ' -f1)
./scripts/sync-models.sh >/dev/null 2>&1
H2=$(md5 -q config/litellm/config.yaml 2>/dev/null || md5sum config/litellm/config.yaml | cut -d' ' -f1)
if [ "$H1" = "$H2" ]; then
  okk "generated files in sync with their sources"
else
  flag STALE "config.yaml was out of date with its sources" "config/litellm/config.yaml" \
    "it has just been regenerated; review 'git diff' and commit"
fi

# ── launchd ─────────────────────────────────────────────────────────────────
hdr "Login services (launchd)"
for lbl in com.ailocal.ollama com.ailocal.ollama-env com.ailocal.preload com.cadence.grepai-watch; do
  P="$HOME/Library/LaunchAgents/$lbl.plist"
  if [ -f "$P" ]; then
    ST=$(launchctl print "gui/$(id -u)/$lbl" 2>/dev/null | awk -F'= ' '/state =/{print $2; exit}')
    case "$lbl" in
      *-env|*preload)
        # One-shot agents are SUPPOSED to be "not running" after they fire.
        okk "$lbl ${ST:-not loaded} (one-shot: not-running is correct)" ;;
      *)
        if [ "$ST" = running ]; then okk "$lbl running"
        else flag STALE "$lbl is installed but ${ST:-not loaded}" "$P" \
              "launchctl bootstrap gui/$(id -u) '$P'"; fi ;;
    esac
  else
    miss "$lbl not installed"
  fi
done
N_OLLAMA=$(ps ax -o command= | grep -c '[o]llama serve' || true)
if [ "$N_OLLAMA" -eq 1 ]; then
  okk "exactly one 'ollama serve' process"
elif [ "$N_OLLAMA" -eq 0 ]; then
  flag MISSING "no ollama serve process" "launchd" "launchctl kickstart -k gui/$(id -u)/com.ailocal.ollama"
else
  flag DUPLICATE "$N_OLLAMA ollama serve processes" "launchd + Ollama.app" \
    "quit the Ollama menu-bar app; the LaunchAgent owns the daemon"
fi

# ── summary ─────────────────────────────────────────────────────────────────
echo
echo "══════════════════════════════════════════════════════════════════════"
if [ "$actionable" -eq 0 ]; then
  echo " ${C_OK}No actionable findings.${C_0}"
  echo " Nothing was modified. Two configs per client is expected: ailocal keeps"
  echo " cloud and local roots separate on purpose."
  exit 0
fi
echo " ${C_WARN}$actionable actionable finding(s)${C_0} — recorded in $FINDINGS_FILE"
echo
echo " Nothing has been deleted. To act on these:"
echo "     ./scripts/cleanup-installation.sh --dry-run"
echo "     ./scripts/cleanup-installation.sh --apply     # backs up first"
exit 3
