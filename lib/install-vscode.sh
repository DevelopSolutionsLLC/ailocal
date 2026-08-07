#!/usr/bin/env bash
# install-vscode.sh — configure VS Code for the local stack WITHOUT hand-editing
# anything in the UI, the same way claude-local and codex-local are configured.
#
# DEPRECATED SETTINGS — do not reintroduce. The extension replaced
# litellm-connector.baseUrl/.backends with VS Code's Language Models provider
# groups plus SecretStorage; VS Code deprecated github.copilot.chat.customOAIModels
# and replaced the "OpenAI Compatible" provider with "Custom Endpoint".
#   https://github.com/gethnet/litellm-connector-copilot/
#   https://code.visualstudio.com/docs/agent-customization/language-models
#
# THE AUTOMATABLE SURFACE. The provider group is a real file
# (~/Library/Application Support/Code/User/chatLanguageModels.json) and can be
# written from a script. Only the API key VALUE lives in SecretStorage and
# cannot be seeded from outside VS Code; the file holds a reference to it
# (${input:chat.lm.secret.<id>}), and an existing reference is PRESERVED here so
# a key entered once is never re-entered.
#
# Individual model entries are NOT written here: the connector discovers them
# from the proxy's /model/info at runtime, which is why that endpoint is probed
# below.
#
# Usage: ailocal vscode [--dry-run]
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

DRY=""
[ "${1:-}" = "--dry-run" ] && DRY=1

USER_DIR="$HOME/Library/Application Support/Code/User"
[ -d "$USER_DIR" ] || USER_DIR="$HOME/.config/Code/User"      # Linux
MODELS_JSON="$USER_DIR/chatLanguageModels.json"
SETTINGS_JSON="$USER_DIR/settings.json"
BASE_URL="${AILOCAL_BASE_URL:-http://localhost:4000}"

. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/output.sh"
ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }

command -v code >/dev/null || { echo "the 'code' CLI is not on PATH"; exit 1; }
[ -d "$USER_DIR" ] || { echo "VS Code user dir not found: $USER_DIR"; exit 1; }

banner "VS Code $(code --version | head -1), user dir: $USER_DIR"

# ── 1. the connector extension ──────────────────────────────────────────────
EXT_ID="Gethnet.litellm-connector-copilot"
if code --list-extensions 2>/dev/null | grep -qix "$EXT_ID"; then
  ok "$EXT_ID already installed ($(code --list-extensions --show-versions 2>/dev/null | grep -i litellm | cut -d@ -f2))"
else
  if [ -n "$DRY" ]; then
    echo "  would install $EXT_ID"
  else
    banner "installing $EXT_ID"
    code --install-extension "$EXT_ID" --force >/dev/null 2>&1 \
      && ok "installed" || warn "install failed — check the Marketplace manually"
  fi
fi

# ── 1b. language servers, VS Code's own way ─────────────────────────────────
# VS Code is deliberately excluded from the mcpls MCP bridge on the grounds that
# it "has native language servers". AUDITED 2026-07-28: that was only ever true
# for TypeScript/JavaScript, which ship in VS Code core. The install had FOUR
# extensions total and no Python or Go support at all — so the justification held
# while the capability did not.
#
# Fixed the client-native way rather than by handing VS Code the bridge: these
# are the official extensions, they are what a VS Code user would install anyway,
# and Copilot Chat's agent mode consumes their language features directly. Adding
# the bridge instead would have duplicated TS/JS and introduced a second symbol
# path — the thing ADR 007/008 exist to avoid.
#
# Shell has no entry for the same reason it has none under native Claude LSP:
# there is no first-party shell language server. Not claimed, not faked.
for EXT in ms-python.python golang.go; do
  if code --list-extensions 2>/dev/null | grep -qix "$EXT"; then
    ok "$EXT already installed"
  elif [ -n "$DRY" ]; then
    echo "  would install $EXT"
  else
    banner "installing $EXT"
    code --install-extension "$EXT" --force >/dev/null 2>&1 \
      && ok "installed" || warn "$EXT install failed — language intelligence for it will be absent"
  fi
done

# ── 2. provider group (automatable; preserves the SecretStorage reference) ──
banner "provider group -> $(basename "$MODELS_JSON")"
python3 - "$MODELS_JSON" "$BASE_URL" "${DRY:-}" <<'PY'
import json, os, sys

path, base_url, dry = sys.argv[1], sys.argv[2], sys.argv[3]

existing = []
try:
    with open(path, encoding="utf-8") as f:
        existing = json.load(f) or []
except FileNotFoundError:
    pass
except Exception as exc:
    print(f"  WARN existing file unparseable ({type(exc).__name__}); it will be "
          f"replaced, and any API key reference in it lost")
    existing = []

VENDOR = "litellm-connector"
NAME = "LiteLLM"

# Preserve an existing apiKey reference. It points into SecretStorage, which this
# script cannot write; discarding it would force the user to re-enter the key for
# no reason. That is the whole difference between "automated" and "automated
# except for the annoying part".
carried_key = None
for entry in existing:
    if isinstance(entry, dict) and entry.get("vendor") == VENDOR:
        if isinstance(entry.get("apiKey"), str):
            carried_key = entry["apiKey"]
        break

group = {"name": NAME, "vendor": VENDOR, "baseUrl": base_url}
if carried_key:
    group["apiKey"] = carried_key

others = [e for e in existing
          if not (isinstance(e, dict) and e.get("vendor") == VENDOR)]
merged = others + [group]

if dry:
    print("  would write:")
    print("   ", json.dumps(merged))
else:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(merged, f, indent="\t")
    os.replace(tmp, path)

if carried_key:
    print(f"  \033[32m✓\033[0m provider group written, existing API key reference "
          f"preserved ({carried_key[:28]}…)")
else:
    print("  \033[1;33mACTION NEEDED\033[0m no API key reference found. The key "
          "value lives in VS Code's")
    print("     SecretStorage (Keychain) and cannot be written from a script.")
    print("     Enter it ONCE:  Command Palette -> 'Chat: Manage Language Models'")
    print("     -> LiteLLM -> paste LITELLM_MASTER_KEY from .env")
    print("     Everything else is already configured; this is the only manual step.")
PY

# ── 3. remove settings VS Code no longer honours ────────────────────────────
banner "pruning deprecated settings from settings.json"
python3 - "$SETTINGS_JSON" "${DRY:-}" <<'PY'
import json, os, re, sys

path, dry = sys.argv[1], sys.argv[2]

# Each of these was written by the old installer and is no longer used. Leaving
# them is not harmless: they make the config look configured while doing nothing,
# which is how the baseUrl=None state went unnoticed.
DEPRECATED = [
    "litellm-connector.baseUrl",              # -> provider groups
    "litellm-connector.backends",             # -> provider groups
    "github.copilot.chat.customOAIModels",    # -> Custom Endpoint provider
    "github.copilot.agent.autoApprove",       # never a real setting
    "github.copilot.chat.tools.terminal.autoApprove",   # never a real setting
]

try:
    raw = open(path, encoding="utf-8").read()
except FileNotFoundError:
    print("  no settings.json; nothing to prune")
    raise SystemExit

# settings.json permits comments and trailing commas. Parse a stripped copy to
# INSPECT, but only rewrite when there is something to remove, and rewrite from
# the parsed structure — losing a user's comments is a real cost, so say so.
stripped = re.sub(r"//[^\n]*", "", raw)
stripped = re.sub(r",(\s*[}\]])", r"\1", stripped)
try:
    doc = json.loads(stripped)
except Exception as exc:
    print(f"  WARN could not parse settings.json ({type(exc).__name__}); "
          f"leaving it untouched rather than risk mangling it")
    raise SystemExit

present = [k for k in DEPRECATED if k in doc]
if not present:
    print("  \033[32m✓\033[0m no deprecated keys present")
    raise SystemExit

if dry:
    for k in present:
        print(f"  would remove {k}")
    raise SystemExit

for k in present:
    doc.pop(k, None)

backup = path + ".ailocal-bak"
if not os.path.exists(backup):
    open(backup, "w", encoding="utf-8").write(raw)
tmp = path + ".tmp"
with open(tmp, "w", encoding="utf-8") as f:
    json.dump(doc, f, indent=2, sort_keys=True)
os.replace(tmp, path)
for k in present:
    print(f"  \033[32m✓\033[0m removed {k}")
print(f"  NOTE settings.json was rewritten from parsed JSON, so any COMMENTS in "
      f"it are gone.")
print(f"       Original saved at {os.path.basename(backup)}")
PY

# ── 4. report what is verifiable and what is not ───────────────────────────
echo
banner "verification"
KEY="$(grep -E '^LITELLM_MASTER_KEY=' "$(python3 "$ROOT_DIR/lib/profile-config" config-root)/.env" 2>/dev/null | cut -d= -f2-)"
if curl -sf -m 10 "$BASE_URL/model/info" -H "Authorization: Bearer $KEY" \
     -o /tmp/ailocal-modelinfo.json 2>/dev/null; then
  N=$(python3 -c "import json;print(len(json.load(open('/tmp/ailocal-modelinfo.json')).get('data') or []))")
  ok "the proxy's /model/info answers with $N models — this is the endpoint the"
  echo "     connector reads to populate the model picker"
else
  warn "$BASE_URL/model/info did not answer; start the stack (ailocal start)"
fi

echo
echo "  Automated here:"
echo "    - connector extension installed"
echo "    - provider group written to chatLanguageModels.json (API key reference preserved)"
echo "    - deprecated settings removed"
echo
echo "  NOT verifiable from a script, and not claimed:"
echo "    Whether a VS Code CHAT TURN reaches the model. That needs the GUI, so"
echo "    this script does not assert it works. Run ailocal validate e2e vscode"
echo "    after opening a chat to check the proxy actually saw the request."
