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
# Keep this many timestamped backups per file. Every run of this script backed up
# unconditionally and never pruned, so the deployed roots accumulated 43 stale
# copies (212 KB of model_catalog.json alone) across a single day of iteration.
# Rollback only ever reaches for a recent one; the rest is landfill.
BACKUP_KEEP="${BACKUP_KEEP:-5}"

backup() {
  local file="$1"
  if [ -f "$file" ]; then
    local ts backup
    ts=$(date +%Y%m%d_%H%M%S)
    backup="${file}.bak.${ts}"
    cp "$file" "$backup"
    warn "Backed up: $(basename "$file") → $(basename "$backup")"
    prune_backups "$file"
    return 0
  fi
  return 1
}

# Drop all but the newest $BACKUP_KEEP backups of "$file". Sorted by name, which
# is chronological because the suffix is YYYYmmdd_HHMMSS. Uses a null-delimited
# read so paths with spaces survive.
prune_backups() {
  local file="$1" old n=0
  while IFS= read -r old; do
    n=$((n + 1))
    [ "$n" -gt "$BACKUP_KEEP" ] && rm -f "$old"
  done < <(ls -1t "${file}".bak.* 2>/dev/null)
}

# Returns 0 if file exists and contains the marker string.
already_has() {
  local file="$1" marker="$2"
  [ -f "$file" ] && grep -qF "$marker" "$file" 2>/dev/null
}

# ── Composing with Cadence ───────────────────────────────────────────────
# Cadence (github.com/DevelopSolutionsLLC/cadence) deploys into this same config root and owns
# two things inside directories ailocal also writes:
#
#   1. a <!-- cadence:start -->…<!-- cadence:end --> block appended INTO our own agent files
#   2. symlinks for agents that have no ailocal counterpart (e.g. repository-health.md)
#
# This directory used to be replaced wholesale with `rm -rf`, which destroyed both — silently,
# and only in one install order (ailocal after cadence). We already reason this way about
# .claude.json, which we preserve rather than clobber; the same applies here.
#
# ailocal remains the owner of the files it ships. It is not the owner of the directory.
CADENCE_START='<!-- cadence:start -->'
CADENCE_END='<!-- cadence:end -->'
MANIFEST_NAME=".ailocal-managed"

# Copy a directory of managed files WITHOUT taking ownership of the directory itself.
# Preserves foreign files, preserves symlinks it does not own, and carries any Cadence block
# in a destination file across the overwrite.
install_managed_dir() {
  local src="$1" dst="$2" f base carried manifest="$2/$MANIFEST_NAME"
  mkdir -p "$dst"

  # Prune files we shipped previously but no longer ship. Without this the old `rm -rf` behaviour
  # of clearing stale files would be lost. Only ever touches names in OUR manifest.
  if [ -f "$manifest" ]; then
    while IFS= read -r base; do
      [ -n "$base" ] || continue
      [ -e "$src/$base" ] && continue          # still shipped
      [ -L "$dst/$base" ] && continue          # someone else's symlink now — not ours to delete
      rm -f "$dst/$base"
    done < "$manifest"
  fi

  : > "$manifest"
  for f in "$src"/*; do
    [ -e "$f" ] || continue
    base="$(basename "$f")"
    echo "$base" >> "$manifest"

    if [ -d "$f" ]; then
      rm -rf "${dst:?}/$base"; cp -R "$f" "$dst/$base"; continue
    fi

    # Capture any Cadence block already present so overwriting our file does not drop it.
    carried=""
    if [ -f "$dst/$base" ] && [ ! -L "$dst/$base" ] \
       && grep -qF "$CADENCE_START" "$dst/$base" 2>/dev/null; then
      carried="$(sed -n "/$(printf '%s' "$CADENCE_START" | sed 's/[][\.*^$/]/\\&/g')/,/$(printf '%s' "$CADENCE_END" | sed 's/[][\.*^$/]/\\&/g')/p" "$dst/$base")"
    fi

    # Never write THROUGH a symlink — that would edit the linked source file in its own repo.
    [ -L "$dst/$base" ] && rm -f "$dst/$base"
    cp "$f" "$dst/$base"

    if [ -n "$carried" ]; then
      printf '\n%s\n' "$carried" >> "$dst/$base"
      info "  preserved Cadence block in $base"
    fi
  done
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
  # Shared SessionStart hook (claude-local + codex-local) — per-session scratchpad.
  cp "$ROOT_DIR/config/clients/scratchpad-hook.sh" "$AILOCAL_CFG/scratchpad-hook.sh"
  chmod +x "$AILOCAL_CFG/scratchpad-hook.sh"
  info "configure.zsh / finalize.zsh / scratchpad-hook.sh deployed to $AILOCAL_CFG"

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

  # The provider group and the deprecated-settings cleanup live in ONE place:
  # scripts/install-vscode.sh. It is the per-client installer, matching the
  # config/clients/vscode/ layout and the install-*.sh naming used elsewhere.
  # Delegating rather than duplicating means the researched details (which
  # settings VS Code still honours, and that the SecretStorage apiKey reference
  # must be preserved) are not maintained in two files that can drift.
  if [ -x "$ROOT_DIR/scripts/install-vscode.sh" ]; then
    "$ROOT_DIR/scripts/install-vscode.sh" || warn "install-vscode.sh reported a problem"
  else
    warn "scripts/install-vscode.sh missing — provider group not configured"
  fi

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

  # AGENTS.md — operating protocol (config/clients/codex/AGENTS.md.template, a TRACKED source)
  # + the shared build checklist, concatenated at install time. The template carries the .template
  # extension so the /AGENTS.md gitignore rule cannot swallow it (that bare pattern once did).
  cat "$ROOT_DIR/config/clients/codex/AGENTS.md.template" \
      <(sed '/<!-- claude-only -->/,/<!-- \/claude-only -->/d' \
          "$ROOT_DIR/config/clients/claude/references/build-checklist.md") \
      > "$CODEX_HOME_DIR/AGENTS.md"
  info "$CODEX_HOME_DIR/AGENTS.md written (protocol + build checklist)"

  # /local-build prompt + plan/review model profiles — managed, always overwrite.
  mkdir -p "$CODEX_HOME_DIR/prompts"
  cp "$ROOT_DIR/config/clients/codex/prompts/"*.md "$CODEX_HOME_DIR/prompts/"
  cp "$ROOT_DIR/config/clients/codex/plan.config.toml" \
     "$ROOT_DIR/config/clients/codex/review.config.toml" "$CODEX_HOME_DIR/"
  info "prompts/ ($(ls "$ROOT_DIR/config/clients/codex/prompts/" | tr '\n' ' ')) + plan/review profiles written"

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

  # Local agent trio + /local-build command + checklist — ailocal owns these FILES, but not the
  # directories they live in. Cadence deploys into the same directories; see install_managed_dir.
  for d in agents commands references; do
    install_managed_dir "$ROOT_DIR/config/clients/claude/$d" "$CLAUDE_HOME_DIR/$d"
  done
  info "$CLAUDE_HOME_DIR/{agents,commands,references} written (Cadence overlays preserved)"

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

# ── Native LSP (the officially supported path) ─────────────────────────────
# Claude Code ships native LSP: one `LSP` tool exposing goToDefinition,
# findReferences, goToImplementation, hover, rename, documentSymbol,
# workspaceSymbol, incomingCalls and outgoingCalls, plus automatic diagnostics
# after every edit. It is off by default and needs TWO things: env
# ENABLE_LSP_TOOL (carried in the deployed settings.json) and a language server
# plugin per language.
#
# This replaced the mcpls MCP bridge for Claude clients. Measured on 2.1.220:
# native is 1 tool / 2,224 B against the bridge's 20 tools / 10,021 B for the
# same operations, and the payload dropped 49 -> 26 tools once the bridge was
# descoped. The bridge remains for codex-local, which has no native LSP.
#
# Language servers themselves are NOT installed here — the plugin only wires up
# a binary the user already has (pyright, typescript-language-server, gopls,
# clangd). A plugin whose binary is missing simply reports no server for that
# file type, which is the same fail-quiet shape we avoid elsewhere; hence the
# presence check below.
install_claude_lsp_plugins() {
  local root="$1" p bin
  command -v claude >/dev/null 2>&1 || { skip "claude not on PATH — skipping LSP plugins"; return 0; }
  CLAUDE_CONFIG_DIR="$root" claude plugin marketplace update claude-plugins-official \
    >/dev/null 2>&1 || true
  # plugin:binary — only install a plugin whose language server is actually present.
  for p in pyright-lsp:pyright-langserver \
           typescript-lsp:typescript-language-server \
           gopls-lsp:gopls \
           clangd-lsp:clangd; do
    bin="${p#*:}"; p="${p%%:*}"
    if ! command -v "$bin" >/dev/null 2>&1; then
      skip "$p — $bin not installed"
      continue
    fi
    if CLAUDE_CONFIG_DIR="$root" claude plugin install "${p}@claude-plugins-official" \
         >/dev/null 2>&1; then
      info "$p enabled"
    else
      warn "$p install failed (native LSP for that language will be unavailable)"
    fi
  done
}

if has_target "claude"; then
  step "Native LSP plugins (Claude Code)"
  install_claude_lsp_plugins "$CLAUDE_HOME_DIR"
  # The cloud root benefits identically and shares the same official mechanism —
  # this is the one place ailocal touches ~/.claude, and it only adds plugins.
  if [ "${AILOCAL_LSP_CLOUD:-1}" = "1" ] && [ -d "$HOME/.claude" ]; then
    install_claude_lsp_plugins "$HOME/.claude"
    echo "     (set AILOCAL_LSP_CLOUD=0 to leave the cloud root alone)"
  fi
fi

# ── Re-apply Cadence-owned MCP registrations ───────────────────────────────
# Codex's config.toml is rewritten wholesale from our template above, which
# DESTROYS the [mcp_servers.*] blocks Cadence appends (grepai, lsp). That made
# every run of this script silently strip codex-local's MCP servers — the
# failure was invisible because Codex simply starts with no tools rather than
# erroring. The documented workaround was "remember to re-run cadence
# afterwards", which is exactly the kind of manual step that gets forgotten.
#
# MCP ownership stays with Cadence (single authoritative implementation); we
# just re-invoke it so the ordering constraint is enforced by code, not memory.
# Never fatal: ailocal must stay installable on a machine without Cadence.
if command -v cadence >/dev/null 2>&1; then
  if cadence mcp sync >/tmp/ailocal-mcp-sync.log 2>&1; then
    info "Cadence MCP registrations re-applied (grepai/lsp survive this install)"
  else
    warn "cadence mcp sync failed — codex-local/claude-local may have no MCP servers."
    warn "  See /tmp/ailocal-mcp-sync.log; re-run 'cadence mcp sync' by hand."
  fi
else
  skip "cadence not on PATH — skipping MCP re-sync (no MCP servers to restore)"
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
