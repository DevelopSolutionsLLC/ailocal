#!/usr/bin/env bash
# install-clients.sh — install AI client configs to their destinations
#
# Usage:
#   ./scripts/install-clients.sh              # install all three
#   ./scripts/install-clients.sh vscode       # VS Code Copilot Chat only
#   ./scripts/install-clients.sh codex        # Codex CLI only
#   ./scripts/install-clients.sh claude       # Claude Code only
#   ./scripts/install-clients.sh codex claude # multiple targets
#
# Destinations:
#   vscode → installs the litellm-connector extension + prints one-time setup
#            (the key lives in VS Code SecretStorage — no file is written)
#   codex  → ~/.codex/config.toml, ~/.codex/model_catalog.json
#   claude → ~/.claude/settings.json, ~/.claude/CLAUDE.md
#
# Safe to run multiple times — backs up before touching, skips if already installed.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE="$ROOT_DIR/.env"

has()  { command -v "$1" >/dev/null 2>&1; }
info() { echo "  ✓ $*"; }
warn() { echo "  ⚠ $*"; }
skip() { echo "  — $*"; }
step() { echo; echo "▶ $*"; }

# Backup a file if it exists.
backup() {
  local file="$1"
  if [ -f "$file" ]; then
    local ts backup
    ts=$(date +%Y%m%d_%H%M%S)
    backup="${file}.bak.${ts}"
    cp "$file" "$backup"
    warn "Backed up: $(basename "$file") → $(basename "$backup")"
    return 0
  fi
  return 1
}

# Returns 0 if file exists and contains the marker string.
already_has() {
  local file="$1" marker="$2"
  [ -f "$file" ] && grep -qF "$marker" "$file" 2>/dev/null
}

# Make VS Code shell integration reliable under a heavy zsh prompt (oh-my-zsh +
# powerlevel10k). ROOT CAUSE of "agent runs a terminal command, it finishes, but
# the spinner never stops": p10k's *instant prompt* runs at the top of ~/.zshrc
# and corrupts VS Code's OSC 633 command-completion markers, so the client never
# sees the command exit. Fix idempotently: (1) skip instant prompt inside VS Code,
# (2) source the shell-integration script explicitly (more robust than VS Code's
# automatic injection when a custom prompt is present).
ensure_shell_integration() {
  local rc="${ZDOTDIR:-$HOME}/.zshrc"
  [ -f "$rc" ] || { skip "no ~/.zshrc — shell-integration step skipped"; return 0; }
  local changed=0
  if grep -q 'p10k-instant-prompt' "$rc" && ! grep -q 'ailocal: p10k instant prompt' "$rc"; then
    backup "$rc" || true
    python3 - "$rc" <<'PY'
import re, sys
p = sys.argv[1]; s = open(p).read()
s2 = re.sub(
    r'if \[\[ -r (.*p10k-instant-prompt.*) \]\]; then',
    r'# ailocal: p10k instant prompt corrupts VS Code OSC 633 markers; skip in VS Code\n'
    r'if [[ "$TERM_PROGRAM" != "vscode" && -r \1 ]]; then',
    s, count=1)
if s2 != s: open(p, "w").write(s2); print("guarded")
PY
    changed=1
  fi
  if ! grep -q 'ailocal: VS Code shell integration' "$rc"; then
    backup "$rc" || true
    cat >> "$rc" <<'EOF'

# ailocal: VS Code shell integration — explicit sourcing keeps OSC 633
# command-completion markers reliable under a custom prompt. Without it, agent
# terminal commands finish but the client's spinner never stops.
if [[ "$TERM_PROGRAM" == "vscode" ]] && command -v code >/dev/null 2>&1; then
  . "$(code --locate-shell-integration-path zsh)" 2>/dev/null
fi
EOF
    changed=1
  fi
  [ "$changed" = 1 ] && info "Shell integration fixed in ~/.zshrc (p10k VS Code guard + explicit SI sourcing)" \
                     || skip "Shell integration already configured in ~/.zshrc"
}

# ── Target selection ───────────────────────────────────────────────────────

TARGETS=()
for arg in "$@"; do
  case "$arg" in
    vscode|codex|claude) TARGETS+=("$arg") ;;
    *) echo "  ✗ Unknown target: '$arg'. Valid targets: vscode  codex  claude"; exit 1 ;;
  esac
done
[ ${#TARGETS[@]} -eq 0 ] && TARGETS=("vscode" "codex" "claude")

has_target() { local t; for t in "${TARGETS[@]}"; do [ "$t" = "$1" ] && return 0; done; return 1; }

echo "Targets: ${TARGETS[*]}"

# ── Sync models.yaml → all derived files before deploying ─────────────────
python3 "$ROOT_DIR/scripts/sync-models.py"

# ── Validate pre-conditions ────────────────────────────────────────────────

if [ ! -f "$ENV_FILE" ]; then
  echo "  ✗ .env not found — run ./scripts/install.sh first"
  exit 1
fi

LITELLM_KEY=$(grep '^LITELLM_MASTER_KEY=' "$ENV_FILE" | cut -d= -f2-)
if [ -z "$LITELLM_KEY" ]; then
  echo "  ✗ LITELLM_MASTER_KEY not set in .env — run ./scripts/install.sh first"
  exit 1
fi

# ── VS Code / Copilot Chat ─────────────────────────────────────────────────

if has_target "vscode"; then
  step "Configuring VS Code Copilot Chat"

  # VS Code connects through the litellm-connector-copilot extension, which
  # stores the Base URL + API key in VS Code's encrypted SecretStorage. That is
  # a security boundary no script/file can write — the key must be entered once
  # via the extension's UI. The old chatLanguageModels.json (vendor
  # "customendpoint") approach is a dead end: VS Code ignores its apiKey and
  # sends an empty Bearer, which LiteLLM rejects ("Ensure Key has Bearer prefix").
  #
  # This step therefore (1) removes that stale/broken entry if present,
  # (2) auto-installs the extension when the `code` CLI is available, and
  # (3) prints the one-time manual key entry.

  EXT_ID="Gethnet.litellm-connector-copilot"
  VSCODE_USER="$HOME/Library/Application Support/Code/User"
  COPILOT_CFG="$VSCODE_USER/chatLanguageModels.json"

  if [ ! -d "$VSCODE_USER" ]; then
    warn "VS Code user directory not found — is VS Code installed?"
  else
    # Clean up the broken customendpoint "ailocal (LiteLLM)" entry (and any
    # direct Ollama entries) so it stops colliding in the model picker.
    if [ -f "$COPILOT_CFG" ] && grep -qF '"ailocal (LiteLLM)"' "$COPILOT_CFG" 2>/dev/null; then
      backup "$COPILOT_CFG" || true
      python3 - "$COPILOT_CFG" <<'PYEOF'
import json, sys
cfg = sys.argv[1]
with open(cfg) as f:
    data = json.load(f)
data = [p for p in data
        if p.get('vendor') != 'ollama'
        and p.get('name') != 'ailocal (LiteLLM)']
with open(cfg, 'w') as f:
    json.dump(data, f, indent=2)
PYEOF
      info "Removed stale customendpoint entry from chatLanguageModels.json"
    else
      skip "No stale customendpoint entry to remove"
    fi
  fi

  # Auto-install the extension if the `code` CLI is on PATH.
  if has code; then
    if code --list-extensions 2>/dev/null | grep -qix "$EXT_ID"; then
      skip "Extension $EXT_ID already installed"
    else
      if code --install-extension "$EXT_ID" >/dev/null 2>&1; then
        info "Installed VS Code extension: $EXT_ID"
      else
        warn "Could not auto-install $EXT_ID — install it from the Marketplace"
      fi
    fi
  else
    warn "'code' CLI not on PATH — install the extension manually: $EXT_ID"
    echo "     (VS Code → Cmd+Shift+P → 'Shell Command: Install code command in PATH')"
  fi

  # Apply recommended connector settings for local models. Non-destructive:
  # each key is added to settings.json ONLY if absent, so a user's own choices
  # and comments are preserved. inactivityTimeout=300 matters most (a 35B model
  # cold-loads ~30 GB with no tokens, which trips the 60s default watchdog);
  # the other two are just the extension defaults, pinned defensively.
  SETTINGS="$VSCODE_USER/settings.json"
  if [ -d "$VSCODE_USER" ]; then
    backup "$SETTINGS" 2>/dev/null || true
    python3 - "$SETTINGS" <<'PYEOF'
import json, sys, os
path = sys.argv[1]
recommended = {
    "litellm-connector.inactivityTimeout": 300,
    "litellm-connector.enableResponsesApi": False,
    "litellm-connector.disableCaching": True,
    # BYOK utility-model fix (VS Code 1.128+ regression): keep title/summary
    # "utility" calls on the selected local model instead of failing with
    # "No utility model is configured for 'copilot-utility-small'".
    "chat.byokUtilityModelDefault": "mainAgent",
    "github.copilot.chat.codeGeneration.useInstructionFiles": True,
    "chat.instructionsFilesLocations": {"~/.copilot/instructions": True},
    "chat.editing.autoAcceptDelay": 0,
    "github.copilot.chat.agent.runTasks": True,
    "github.copilot.chat.agent.autoFix": True,
    # The ONLY valid global auto-approve key (per the VS Code AI settings
    # reference). The old chat.tools.autoApprove / github.copilot.agent.autoApprove
    # / github.copilot.chat.tools.terminal.autoApprove variants are not real
    # settings — VS Code silently ignores them, so they only added confusion.
    "chat.tools.global.autoApprove": True,
    # Terminal: auto-approve everything EXCEPT broad process kills and rm -rf.
    # pkill/kill of node in the integrated terminal takes down VS Code's own
    # extension host and the litellm-connector, dropping the model connection.
    "chat.tools.terminal.autoApprove": {
        "/^.*/": True,
        "/\\b(pkill|kill|killall)\\b/": False,
        "/\\brm\\s+-rf\\b/": False,
    },
}
text = open(path).read() if os.path.exists(path) else "{}"
missing = {k: v for k, v in recommended.items() if f'"{k}"' not in text}
if missing:
    if "{" not in text:
        text = "{}"
    i = text.index("{")
    ins = "".join(f'\n    "{k}": {json.dumps(v)},' for k, v in missing.items())
    text = text[:i+1] + ins + text[i+1:]
    open(path, "w").write(text)
    print("added:", ", ".join(missing))
else:
    print("already present")
PYEOF
    info "Recommended connector settings ensured (added only if missing)"
  fi

  # Fix the terminal-command hang (p10k instant prompt vs VS Code OSC 633 markers).
  ensure_shell_integration

  # Deploy Copilot instruction files to ~/.copilot/instructions/
  # These tell Copilot how to handle terminal commands with local models (detach + log pattern)
  # and provide local stack context. Always overwrite — they are managed files, not user-edited.
  COPILOT_INSTR="$HOME/.copilot/instructions"
  mkdir -p "$COPILOT_INSTR"
  cp "$ROOT_DIR/config/clients/copilot/ailocal.instructions.md" "$COPILOT_INSTR/ailocal.instructions.md"
  cp "$ROOT_DIR/config/clients/copilot/session-primer.md" "$COPILOT_INSTR/session-primer.md"
  info "Copilot instruction files deployed to ~/.copilot/instructions/"

  # Install the `ailocal-code` launcher — opens VS Code in a dedicated "ailocal"
  # profile so the YOLO auto-approve settings stay isolated from your other work.
  # The path is optional: `ailocal-code` opens the current dir; `ailocal-code ~/foo`
  # opens that path.
  RC="${ZDOTDIR:-$HOME}/.zshrc"
  LAUNCHER_MARKER="# ailocal-code launcher"
  if already_has "$RC" "$LAUNCHER_MARKER"; then
    skip "ailocal-code launcher already in $(basename "$RC")"
  else
    {
      echo ""
      echo "$LAUNCHER_MARKER"
      echo 'ailocal-code() { code --profile ailocal "${1:-.}"; }'
    } >> "$RC"
    info "Added ailocal-code launcher to $RC (reload: source $RC)"
  fi

  # Put the key on the clipboard so it's a one-paste into the Manage Models dialog.
  if command -v pbcopy >/dev/null 2>&1; then
    printf '%s' "$LITELLM_KEY" | pbcopy && KEY_HINT="(copied to clipboard — just paste)" || KEY_HINT=""
  fi

  echo
  echo "  Final step — enter the key ONCE (encrypted SecretStorage, unscriptable):"
  echo "    1. Copilot Chat → model-picker dropdown → \"Manage Models…\""
  echo "       (or Cmd+Shift+P → \"Chat: Manage Language Models\")"
  echo "    2. Pick \"LiteLLM Connector\" and enter:"
  echo "         Base URL:  http://localhost:4000"
  echo "         API Key:   ${LITELLM_KEY}  ${KEY_HINT:-}"
  echo "    3. Cmd+Shift+P → \"LiteLLM: Reload Models\""
  echo "  Models + capabilities (vision/tools/ctx) are auto-discovered from LiteLLM."
  echo
  echo "  Launcher: run 'ailocal-code [path]' to open the isolated 'ailocal' profile."
  echo "  First time only — create that profile from your current one so it inherits"
  echo "  these settings: Cmd+Shift+P → \"Profiles: Create Profile\" → Copy from Current."
fi

# ── Codex CLI ─────────────────────────────────────────────────────────────

if has_target "codex"; then
  step "Installing Codex config (~/.codex/)"
  mkdir -p "$HOME/.codex"

  CODEX_CFG="$HOME/.codex/config.toml"
  CODEX_CAT="$HOME/.codex/model_catalog.json"

  # config.toml — always overwrite from template (our managed file)
  # This ensures the latest fixes: openai_base_url fallback, sandbox_mode fix, wire_api, etc.
  envsubst '${HOME}' < "$ROOT_DIR/config/clients/codex-config.toml" > "$CODEX_CFG"
  info "~/.codex/config.toml written (from config/clients/codex-config.toml)"

  # model_catalog.json — always update (our managed file, no user customization)
  backup "$CODEX_CAT" || true
  cp "$ROOT_DIR/config/clients/model_catalog.json" "$CODEX_CAT"
  info "~/.codex/model_catalog.json written"

  # ── Validate the installation ───────────────────────────────────────────
  echo
  echo "  Codex configuration:"
  echo "    Config file:      $CODEX_CFG"
  echo "    Model provider:   $(grep '^model_provider' "$CODEX_CFG" | sed 's/.*= *//')"
  echo "    Active model:     $(grep '^model = ' "$CODEX_CFG" | sed 's/.*= *//')"
  echo "    Base URL:         $(grep '^openai_base_url\|^base_url' "$CODEX_CFG" | head -1 | sed 's/.*= *//')"
  echo "    Model catalog:    $CODEX_CAT"
  echo

  # Try codex doctor or config show if available for validation
  if has codex; then
    echo "  Running codex self-check..."
    if codex --strict-config codex --help >/dev/null 2>&1; then
      info "codex binary is functional and accepts config"
    elif codex --help >/dev/null 2>&1; then
      info "codex binary is present (no strict-config flag — older version)"
    fi
    # Try 'codex exec' with a trivial prompt to verify env vars are loadable
    if codex exec "echo ok" 2>/dev/null | grep -q "ok"; then
      info "codex exec test passed (model responses are reachable)"
    else
      warn "codex exec test returned no output — check LITELLM_KEY and LiteLLM status"
      echo "     Run manually: codex --model coder 'hello'"
      echo "     Check logs:   ~/ailocal/logs/litellm/proxy*"
    fi
  else
    warn "codex binary not found on PATH — cannot validate runtime"
    echo "     After installing codex, run: ./scripts/install-clients.sh codex"
    echo "     Then test:                   codex --model coder 'hello'"
  fi

  echo "  To force-update a config that was skipped, delete it and re-run:"
  echo "    rm $CODEX_CFG && ./scripts/install-clients.sh codex"
fi

# ── Claude Code ───────────────────────────────────────────────────────────

if has_target "claude"; then
  step "Installing Claude Code config (~/.claude/)"
  mkdir -p "$HOME/.claude"

  CLAUDE_CFG="$HOME/.claude/settings.json"
  CLAUDE_MD="$HOME/.claude/CLAUDE.md"

  # settings.json — skip if already pointing at our proxy
  if already_has "$CLAUDE_CFG" "localhost:4000"; then
    skip "~/.claude/settings.json already points at localhost:4000"
  else
    backup "$CLAUDE_CFG" || true
    sed "s|<LITELLM_MASTER_KEY>|${LITELLM_KEY}|g" \
      "$ROOT_DIR/config/clients/claude-code.json" \
      > "$CLAUDE_CFG"
    chmod 600 "$CLAUDE_CFG"
    info "~/.claude/settings.json written (chmod 600)"
  fi

  # CLAUDE.md — skip if exists (user may have customized it); back up and write on first install
  if [ -f "$CLAUDE_MD" ]; then
    backup "$CLAUDE_MD"
    skip "~/.claude/CLAUDE.md already exists — backed up, not overwritten"
    echo "     To update: cp $ROOT_DIR/config/clients/CLAUDE.md $CLAUDE_MD"
  else
    cp "$ROOT_DIR/config/clients/CLAUDE.md" "$CLAUDE_MD"
    info "~/.claude/CLAUDE.md written (first install)"
  fi
fi

# ── Shell profile auto-sourcing of env.sh ────────────────────────────────────
# Add 'source ~/ailocal/config/clients/env.sh' to ~/.zshrc (or ~/.bashrc) so the
# user doesn't need to remember to source it manually. Idempotent: skip if already present.
ENV_SH="$ROOT_DIR/config/clients/env.sh"
SOURCE_MARKER="# ailocal env.sh — generated by install-clients.sh"

for PROFILE in "$HOME/.zshrc" "$HOME/.bashrc" "$HOME/.profile"; do
  [ -f "$PROFILE" ] && {
    if already_has "$PROFILE" "$SOURCE_MARKER"; then
      skip "env.sh sourcing already in $(basename "$PROFILE")"
    else
      {
        echo ""
        echo "$SOURCE_MARKER"
        echo "source \"$ENV_SH\""
      } >> "$PROFILE"
      info "Added env.sh sourcing to $(basename "$PROFILE")"
    fi
    break  # First existing profile wins; stop searching
  }
done

# ── Done ───────────────────────────────────────────────────────────────────

echo ""
echo "  ✓ Done. Restart affected tools to pick up changes:"
has_target "vscode"  && echo "    VS Code:      Cmd+Shift+P → \"LiteLLM: Reload Models\" (after the one-time key entry above)"
has_target "codex"   && echo "    Codex:        restart any open Codex sessions"
has_target "claude"  && echo "    Claude Code:  restart any open claude sessions"
echo ""
echo "  To force-update a config that was skipped, delete it and re-run:"
has_target "codex"   && echo "    rm ~/.codex/config.toml"
has_target "claude"  && echo "    rm ~/.claude/settings.json"
has_target "vscode"  && echo "    VS Code:  no file to delete — re-enter via \"Chat: Manage Language Models\" (key lives in SecretStorage)"
echo "  Then: ./scripts/install-clients.sh [target]"
echo ""
echo "  Key rotation: after running install.sh, restart the proxy with:"
echo "    ./scripts/start.sh   # LiteLLM reloads LITELLM_MASTER_KEY from .env"
