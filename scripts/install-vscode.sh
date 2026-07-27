#!/usr/bin/env bash
# install-vscode.sh — configure VS Code for the local stack WITHOUT hand-editing
# anything in the UI, the same way claude-local and codex-local are configured.
#
# WHAT WAS WRONG BEFORE (researched, not guessed)
# The previous VS Code setup wrote settings that VS Code and the connector no
# longer use. Three layers of deprecation:
#
#   litellm-connector.baseUrl / .backends   deprecated by the extension in favour
#                                           of VS Code's Language Models provider
#                                           groups + SecretStorage
#   github.copilot.chat.customOAIModels     deprecated by VS Code
#   "OpenAI Compatible" provider            deprecated, replaced by "Custom
#                                           Endpoint" (Chat Completions /
#                                           Responses / Messages API types)
#
# Sources:
#   https://github.com/gethnet/litellm-connector-copilot/
#   https://code.visualstudio.com/docs/agent-customization/language-models
#
# THE AUTOMATABLE SURFACE
# The provider group is a real file:
#   ~/Library/Application Support/Code/User/chatLanguageModels.json
# so it can be written from a script. Only the API key VALUE lives in
# SecretStorage (Keychain-backed) and cannot be seeded from outside VS Code — but
# the file holds a *reference* to it (${input:chat.lm.secret.<id>}), and an
# existing reference is PRESERVED here, so a key entered once never has to be
# entered again.
#
# Individual model entries are NOT written here. The connector discovers them
# from the proxy's /model/info at runtime, which is why that endpoint is probed
# below. (An earlier draft of this header claimed the entries were generated from
# capabilities.generated.json — they are not, and the claim is removed rather
# than left to mislead.)
#
# Usage: ./scripts/install-vscode.sh [--dry-run]
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

info() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mWARN\033[0m %s\n' "$*"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }

command -v code >/dev/null || { echo "the 'code' CLI is not on PATH"; exit 1; }
[ -d "$USER_DIR" ] || { echo "VS Code user dir not found: $USER_DIR"; exit 1; }

info "VS Code $(code --version | head -1), user dir: $USER_DIR"

# ── 1. the connector extension ──────────────────────────────────────────────
EXT_ID="Gethnet.litellm-connector-copilot"
if code --list-extensions 2>/dev/null | grep -qix "$EXT_ID"; then
  ok "$EXT_ID already installed ($(code --list-extensions --show-versions 2>/dev/null | grep -i litellm | cut -d@ -f2))"
else
  if [ -n "$DRY" ]; then
    echo "  would install $EXT_ID"
  else
    info "installing $EXT_ID"
    code --install-extension "$EXT_ID" --force >/dev/null 2>&1 \
      && ok "installed" || warn "install failed — check the Marketplace manually"
  fi
fi

# ── 2. provider group (automatable; preserves the SecretStorage reference) ──
info "provider group -> $(basename "$MODELS_JSON")"
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
info "pruning deprecated settings from settings.json"
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
info "verification"
KEY="$(grep -E '^LITELLM_MASTER_KEY=' .env 2>/dev/null | cut -d= -f2-)"
if curl -sf -m 10 "$BASE_URL/model/info" -H "Authorization: Bearer $KEY" \
     -o /tmp/ailocal-modelinfo.json 2>/dev/null; then
  N=$(python3 -c "import json;print(len(json.load(open('/tmp/ailocal-modelinfo.json')).get('data') or []))")
  ok "the proxy's /model/info answers with $N models — this is the endpoint the"
  echo "     connector reads to populate the model picker"
else
  warn "$BASE_URL/model/info did not answer; start the stack (./scripts/start.sh)"
fi

echo
echo "  Automated here:"
echo "    - connector extension installed"
echo "    - provider group written to chatLanguageModels.json (API key reference preserved)"
echo "    - deprecated settings removed"
echo
echo "  NOT verifiable from a script, and not claimed:"
echo "    Whether a VS Code CHAT TURN reaches the model. That needs the GUI, so"
echo "    this script does not assert it works. Run ./scripts/validate-vscode-e2e.sh"
echo "    after opening a chat to check the proxy actually saw the request."
