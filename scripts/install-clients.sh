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
#   codex  → ~/.config/ailocal/codex/config.toml, model_catalog.json
#            (CODEX_HOME for the codex-local wrapper — ~/.codex is NEVER touched)
#   claude → ~/.config/ailocal/claude/settings.json, CLAUDE.md
#            (CLAUDE_CONFIG_DIR for the claude-local wrapper — ~/.claude is NEVER touched)
#
# All targets also (re)install two silent, idempotent lines in ~/.zshrc that
# source config/clients/{configure,finalize}.zsh — these define the
# claude-local / codex-local / ailocal-code wrapper functions and fix the
# VS Code terminal-hang issue. Those two marker-commented lines
# (# ailocal-configure / # ailocal-finalize) are the ONLY footprint this
# installer leaves in ~/.zshrc — everything else lives under
# ~/.config/ailocal/, so uninstalling is just removing those two lines plus
# that directory (see scripts/teardown.sh --clients).
#
# Safe to run multiple times — backs up before touching, skips if already installed.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE="$ROOT_DIR/.env"
AILOCAL_CFG="${XDG_CONFIG_HOME:-$HOME/.config}/ailocal"

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

# ── Shared step: ~/.config/ailocal + the two silent .zshrc source lines ───
# Creates the XDG-style config home for ailocal client state, writes the
# env file the claude-local/codex-local wrappers read, deploys the managed
# configure.zsh/finalize.zsh, and ensures exactly two idempotent lines in
# ~/.zshrc (configure sourced FIRST — before p10k instant prompt — finalize
# sourced last). Runs for every target; it's cheap and target-agnostic.
ensure_ailocal_shell_sourcing() {
  step "Setting up ~/.config/ailocal"

  mkdir -p "$AILOCAL_CFG"
  chmod 700 "$AILOCAL_CFG"

  local env_path="$AILOCAL_CFG/env"
  cat > "$env_path" <<EOF
AILOCAL_BASE_URL=http://localhost:4000
AILOCAL_API_KEY=${LITELLM_KEY}
EOF
  chmod 600 "$env_path"
  info "$env_path written (chmod 600)"

  cp "$ROOT_DIR/config/clients/configure.zsh" "$AILOCAL_CFG/configure.zsh"
  cp "$ROOT_DIR/config/clients/finalize.zsh" "$AILOCAL_CFG/finalize.zsh"
  info "configure.zsh / finalize.zsh deployed to $AILOCAL_CFG"

  local rc="${ZDOTDIR:-$HOME}/.zshrc"
  if [ ! -f "$rc" ]; then
    skip "no ~/.zshrc — skipping source-line injection (functions still available if you source them manually)"
    return 0
  fi

  local configure_line finalize_line
  configure_line='[[ -r "${XDG_CONFIG_HOME:-$HOME/.config}/ailocal/configure.zsh" ]] && source "${XDG_CONFIG_HOME:-$HOME/.config}/ailocal/configure.zsh"  # ailocal-configure'
  finalize_line='[[ -r "${XDG_CONFIG_HOME:-$HOME/.config}/ailocal/finalize.zsh" ]] && source "${XDG_CONFIG_HOME:-$HOME/.config}/ailocal/finalize.zsh"    # ailocal-finalize'

  if already_has "$rc" "# ailocal-configure"; then
    skip "ailocal-configure line already in ~/.zshrc"
  else
    backup "$rc" || true
    printf '%s\n' "$configure_line" | cat - "$rc" > "$rc.tmp" && mv "$rc.tmp" "$rc"
    info "Inserted ailocal-configure as the first line of ~/.zshrc"
  fi

  if already_has "$rc" "# ailocal-finalize"; then
    skip "ailocal-finalize line already in ~/.zshrc"
  else
    backup "$rc" || true
    printf '\n%s\n' "$finalize_line" >> "$rc"
    info "Appended ailocal-finalize to the end of ~/.zshrc"
  fi
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

# ── Shared: install the two silent .zshrc source lines ─────────────────────

ensure_ailocal_shell_sourcing

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

  # Deploy Copilot instruction files to ~/.copilot/instructions/
  # These tell Copilot how to handle terminal commands with local models (detach + log pattern)
  # and provide local stack context. Always overwrite — they are managed files, not user-edited.
  COPILOT_INSTR="$HOME/.copilot/instructions"
  mkdir -p "$COPILOT_INSTR"
  # ailocal.instructions.md gains the shared build checklist at install time
  # (single source: config/clients/claude/references/build-checklist.md).
  # Claude-only blocks (subagent guidance) are stripped for non-Claude clients.
  cat "$ROOT_DIR/config/clients/copilot/ailocal.instructions.md" \
      <(sed '/<!-- claude-only -->/,/<!-- \/claude-only -->/d' \
          "$ROOT_DIR/config/clients/claude/references/build-checklist.md") \
      > "$COPILOT_INSTR/ailocal.instructions.md"
  cp "$ROOT_DIR/config/clients/copilot/session-primer.md" "$COPILOT_INSTR/session-primer.md"
  info "Copilot instruction files deployed to ~/.copilot/instructions/"

  # ── Continue extension (local autocomplete + chat) ────────────────────────
  # Continue gives VS Code local tab-autocomplete (FIM) that Copilot can't. Deploy
  # a managed ~/.continue/config.json: chat/edit through the proxy, autocomplete
  # DIRECT to Ollama (FIM through the proxy is unreliable — continuedev/continue#2907).
  # The user's existing file is backed up first.
  CONTINUE_CFG="$HOME/.continue/config.json"
  mkdir -p "$HOME/.continue"
  backup "$CONTINUE_CFG" || true
  sed "s|__LITELLM_KEY__|${LITELLM_KEY}|g" \
      "$ROOT_DIR/config/clients/continue/config.json" > "$CONTINUE_CFG"
  info "Continue config deployed to ~/.continue/config.json (autocomplete: qwen2.5-coder:3b direct to Ollama)"

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
  echo "  Launcher: run 'ailocal-code [path]' to open the isolated 'ailocal' profile"
  echo "  (defined by configure.zsh — reload your shell first: source ~/.zshrc)."
  echo "  First time only — create that profile from your current one so it inherits"
  echo "  these settings: Cmd+Shift+P → \"Profiles: Create Profile\" → Copy from Current."
fi

# ── Codex CLI ─────────────────────────────────────────────────────────────

if has_target "codex"; then
  step "Installing Codex config (~/.config/ailocal/codex/)"

  CODEX_HOME_DIR="$AILOCAL_CFG/codex"
  mkdir -p "$CODEX_HOME_DIR"

  CODEX_CFG="$CODEX_HOME_DIR/config.toml"
  CODEX_CAT="$CODEX_HOME_DIR/model_catalog.json"

  # config.toml — always overwrite from template (our managed file)
  # This ensures the latest fixes: openai_base_url fallback, sandbox_mode fix, wire_api, etc.
  CODEX_HOME="$CODEX_HOME_DIR" envsubst '${CODEX_HOME}' < "$ROOT_DIR/config/clients/codex/config.toml" > "$CODEX_CFG"
  info "$CODEX_CFG written (from config/clients/codex/config.toml)"

  # model_catalog.json — always update (our managed file, no user customization)
  backup "$CODEX_CAT" || true
  cp "$ROOT_DIR/config/clients/model_catalog.json" "$CODEX_CAT"
  info "$CODEX_CAT written"

  # AGENTS.md — phase protocol + shared build checklist (single source in
  # config/clients/claude/references/), concatenated at install time.
  cat "$ROOT_DIR/config/clients/codex/AGENTS.md" \
      <(sed '/<!-- claude-only -->/,/<!-- \/claude-only -->/d' \
          "$ROOT_DIR/config/clients/claude/references/build-checklist.md") \
      > "$CODEX_HOME_DIR/AGENTS.md"
  info "$CODEX_HOME_DIR/AGENTS.md written (protocol + build checklist)"

  # /local-build prompt + plan/review model profiles — managed, always overwrite.
  mkdir -p "$CODEX_HOME_DIR/prompts"
  cp "$ROOT_DIR/config/clients/codex/prompts/local-build.md" "$CODEX_HOME_DIR/prompts/"
  cp "$ROOT_DIR/config/clients/codex/plan.config.toml" \
     "$ROOT_DIR/config/clients/codex/review.config.toml" "$CODEX_HOME_DIR/"
  info "prompts/local-build.md + plan/review profiles written"

  echo
  echo "  Codex configuration (CODEX_HOME=$CODEX_HOME_DIR):"
  echo "    Config file:      $CODEX_CFG"
  echo "    Model provider:   $(grep '^model_provider' "$CODEX_CFG" | sed 's/.*= *//')"
  echo "    Active model:     $(grep '^model = ' "$CODEX_CFG" | sed 's/.*= *//')"
  echo "    Base URL:         $(grep '^openai_base_url\|^base_url' "$CODEX_CFG" | head -1 | sed 's/.*= *//')"
  echo "    Model catalog:    $CODEX_CAT"
  echo

  if [ -f "$HOME/.codex/config.toml" ]; then
    warn "~/.codex/config.toml still exists — plain 'codex' will keep using it (cloud, unaffected)."
    echo "     Remove it manually if you no longer want it: rm ~/.codex/config.toml"
  fi

  if has codex; then
    info "codex binary found on PATH"
  else
    warn "codex binary not found on PATH — install it, then run: codex-local exec 'say ok'"
  fi
  echo "  Launch with: codex-local exec 'say ok'   (reload your shell first: source ~/.zshrc)"

  echo "  To force-update a config that was skipped, delete it and re-run:"
  echo "    rm $CODEX_CFG && ./scripts/install-clients.sh codex"
fi

# ── Claude Code ───────────────────────────────────────────────────────────

if has_target "claude"; then
  step "Installing Claude Code config (~/.config/ailocal/claude/)"

  CLAUDE_HOME_DIR="$AILOCAL_CFG/claude"
  mkdir -p "$CLAUDE_HOME_DIR"

  CLAUDE_CFG="$CLAUDE_HOME_DIR/settings.json"
  CLAUDE_MD="$CLAUDE_HOME_DIR/CLAUDE.md"
  CLAUDE_JSON="$CLAUDE_HOME_DIR/.claude.json"

  # settings.json — always overwrite (managed file, no secrets — key comes
  # from the claude-local wrapper's process-scoped env, never written to disk here).
  backup "$CLAUDE_CFG" || true
  cp "$ROOT_DIR/config/clients/claude/settings.json" "$CLAUDE_CFG"
  info "$CLAUDE_CFG written"

  # CLAUDE.md — managed file, always overwrite.
  cp "$ROOT_DIR/config/clients/CLAUDE.md" "$CLAUDE_MD"
  info "$CLAUDE_MD written"

  # Local agent trio + /local-build command + checklist — managed, always overwrite.
  for d in agents commands references; do
    rm -rf "${CLAUDE_HOME_DIR:?}/$d"
    cp -R "$ROOT_DIR/config/clients/claude/$d" "$CLAUDE_HOME_DIR/$d"
  done
  info "$CLAUDE_HOME_DIR/{agents,commands,references} written"

  # .claude.json — seed onboarding-complete only if absent, so a real session
  # under this CLAUDE_CONFIG_DIR never gets clobbered.
  if [ -f "$CLAUDE_JSON" ]; then
    skip "$CLAUDE_JSON already exists — left untouched"
  else
    echo '{"hasCompletedOnboarding": true}' > "$CLAUDE_JSON"
    info "$CLAUDE_JSON seeded (skips first-run onboarding)"
  fi

  echo "  Launch with: claude-local   (reload your shell first: source ~/.zshrc)"
  echo "  Plain 'claude' in this or any other shell is untouched — still the cloud session."
fi

# ── Done ───────────────────────────────────────────────────────────────────

echo ""
echo "  ✓ Done. Restart affected tools to pick up changes:"
has_target "vscode"  && echo "    VS Code:      Cmd+Shift+P → \"LiteLLM: Reload Models\" (after the one-time key entry above)"
has_target "codex"   && echo "    Codex:        use 'codex-local' — restart any open codex-local sessions"
has_target "claude"  && echo "    Claude Code:  use 'claude-local' — restart any open claude-local sessions"
echo ""
echo "  New shells pick up claude-local/codex-local/ailocal-code automatically"
echo "  (sourced from ~/.config/ailocal/configure.zsh). For this shell: source ~/.zshrc"
echo ""
echo "  To force-update a config that was skipped, delete it and re-run:"
has_target "codex"   && echo "    rm $AILOCAL_CFG/codex/config.toml"
has_target "claude"  && echo "    rm $AILOCAL_CFG/claude/settings.json"
has_target "vscode"  && echo "    VS Code:  no file to delete — re-enter via \"Chat: Manage Language Models\" (key lives in SecretStorage)"
echo "  Then: ./scripts/install-clients.sh [target]"
echo ""
echo "  Key rotation: after running install.sh, restart the proxy with:"
echo "    ./scripts/start.sh   # LiteLLM reloads LITELLM_MASTER_KEY from .env"
echo "  ...then re-run ./scripts/install-clients.sh to refresh ~/.config/ailocal/env"
