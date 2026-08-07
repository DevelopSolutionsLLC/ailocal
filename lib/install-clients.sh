#!/usr/bin/env bash
# install-clients.sh — install AI client configs to their destinations
#
# Usage:
#   ailocal clients              # install all three
#   ailocal vscode       # VS Code Copilot Chat only
#   ailocal clients codex        # Codex CLI only
#   ailocal clients claude       # Claude Code only
#   ailocal clients codex claude # multiple targets
#
# Destinations:
#   vscode → installs the litellm-connector extension + prints one-time setup
#            (the key lives in VS Code SecretStorage — no file is written)
#   codex  → ~/.config/ailocal/codex/config.toml, model_catalog.json
#            (CODEX_HOME for the codex-local wrapper — ~/.codex is NEVER touched)
#   claude → ~/.config/ailocal/claude/settings.json (instruction policy not written here)
#            (CLAUDE_CONFIG_DIR for the claude-local wrapper — ~/.claude is NEVER touched)
#
# All targets also (re)install two silent, idempotent lines in ~/.zshrc that
# source clients/{configure,finalize}.zsh — these define the
# claude-local / codex-local / ailocal-code wrapper functions and fix the
# VS Code terminal-hang issue. Those two marker-commented lines
# (# ailocal-configure / # ailocal-finalize) are the ONLY footprint this
# installer leaves in ~/.zshrc — everything else lives under
# ~/.config/ailocal/, so uninstalling is just removing those two lines plus
# that directory (see `ailocal teardown --clients`).
#
# Safe to run multiple times — backs up before touching, skips if already installed.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
AILOCAL_STATE="${AILOCAL_STATE:-$(python3 "$ROOT_DIR/lib/profile-config" state-root)}"
ENV_FILE="$(python3 "$ROOT_DIR/lib/profile-config" config-root)/.env"
AILOCAL_CFG="${XDG_CONFIG_HOME:-$HOME/.config}/ailocal"

. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/output.sh"
skip() { echo "  — $*"; }

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

# ── Composition by other tools ────────────────────────────────────────────
# ailocal provisions an isolated client home and owns the files it ships there.
# It does NOT own the directory: a user or another tool may add content to an
# installed home, in two shapes ailocal must not destroy:
#
#   1. a marked block appended INTO a file ailocal also writes
#   2. symlinks for entries that have no ailocal counterpart
#
# Replacing such a directory wholesale destroys both, silently, and only in one
# install order. Same reasoning as .claude.json, which is preserved rather than
# clobbered. Reinstalling ailocal restores the ailocal baseline; it does not
# rebuild anything another tool contributed, and does not assume anything will.
#
# The marker below is a literal external interface: the string is fixed because
# another tool writes it, not because ailocal depends on that tool existing.
#: Literal marker pair another tool appends into files ailocal also writes.
#: The strings are fixed because that tool writes them; ailocal only has to
#: recognise and carry the block, and works unchanged if nothing ever writes one.
OVERLAY_START='<!-- cadence:start -->'
OVERLAY_END='<!-- cadence:end -->'
MANIFEST_NAME=".ailocal-managed"

# Copy a directory of managed files WITHOUT taking ownership of the directory itself.
# Preserves foreign files, preserves symlinks it does not own, and carries any marked block
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

    # Capture any marked block already present so overwriting our file does not drop it.
    carried=""
    if [ -f "$dst/$base" ] && [ ! -L "$dst/$base" ] \
       && grep -qF "$OVERLAY_START" "$dst/$base" 2>/dev/null; then
      carried="$(sed -n "/$(printf '%s' "$OVERLAY_START" | sed 's/[][\.*^$/]/\\&/g')/,/$(printf '%s' "$OVERLAY_END" | sed 's/[][\.*^$/]/\\&/g')/p" "$dst/$base")"
    fi

    # Never write THROUGH a symlink — that would edit the linked source file in its own repo.
    [ -L "$dst/$base" ] && rm -f "$dst/$base"
    cp "$f" "$dst/$base"

    if [ -n "$carried" ]; then
      printf '\n%s\n' "$carried" >> "$dst/$base"
      info "  preserved external block in $base"
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

  cp "$AILOCAL_STATE/clients/configure.zsh" "$AILOCAL_CFG/configure.zsh"
  cp "$ROOT_DIR/clients/finalize.zsh" "$AILOCAL_CFG/finalize.zsh"
  # Shared SessionStart hook (claude-local + codex-local) — per-session scratchpad.
  # Client-invoked hooks: the client execs these, so the deployed copy must be
  # executable regardless of the mode the file carries in the checkout.
  for _hook in scratchpad-hook.sh compact-hook.sh; do
    cp "$ROOT_DIR/clients/$_hook" "$AILOCAL_CFG/$_hook"
    chmod +x "$AILOCAL_CFG/$_hook"
  done
  info "configure.zsh / finalize.zsh / scratchpad-hook.sh / compact-hook.sh deployed to $AILOCAL_CFG"

  # The integration contract — the published description of this runtime for any
  # external consumer. Deployed to a stable path so a consumer never has to know where the
  # ailocal repo lives, and never parses our generated Markdown for a fact.
  if [ -f "$AILOCAL_STATE/integration-contract.json" ]; then
    cp "$AILOCAL_STATE/integration-contract.json" "$AILOCAL_CFG/integration-contract.json"
    info "$AILOCAL_CFG/integration-contract.json published (runtime schema)"
  else
    warn "integration-contract.json missing — run ailocal sync"
  fi

  local rc="${ZDOTDIR:-$HOME}/.zshrc"
  if [ ! -f "$rc" ]; then
    # A bare Mac (zsh is the default shell) simply has no rc file yet — skipping
    # injection here left claude-local/codex-local/ailocal-code permanently
    # unavailable, and every "reload your shell: source ~/.zshrc" message below
    # this point would then fail outright (no such file). Create it instead.
    : > "$rc"
    info "Created $rc (none existed)"
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
python3 "$ROOT_DIR/lib/sync-models.py"

# ── Validate pre-conditions ────────────────────────────────────────────────

if [ ! -f "$ENV_FILE" ]; then
  echo "  ✗ .env not found — run ./ailocal install first"
  exit 1
fi

LITELLM_KEY=$(grep '^LITELLM_MASTER_KEY=' "$ENV_FILE" | cut -d= -f2-)
if [ -z "$LITELLM_KEY" ]; then
  echo "  ✗ LITELLM_MASTER_KEY not set in .env — run ./ailocal install first"
  exit 1
fi

# ── Shared: install the two silent .zshrc source lines ─────────────────────

ensure_ailocal_shell_sourcing

# ── VS Code / Copilot Chat ─────────────────────────────────────────────────

if has_target "vscode"; then
  step "Configuring VS Code Copilot Chat"

  # The provider group and the deprecated-settings cleanup live in ONE place:
  # lib/install-vscode.sh. It is the per-client installer, matching the
  # clients/vscode/ layout and the install-*.sh naming used elsewhere.
  # Delegating rather than duplicating means the researched details (which
  # settings VS Code still honours, and that the SecretStorage apiKey reference
  # must be preserved) are not maintained in two files that can drift.
  if [ -f "$ROOT_DIR/lib/install-vscode.sh" ]; then
    bash "$ROOT_DIR/lib/install-vscode.sh" || warn "install-vscode.sh reported a problem"
  else
    warn "lib/install-vscode.sh missing — provider group not configured"
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
  # (single source: clients/claude/references/build-checklist.md).
  # Claude-only blocks (subagent guidance) are stripped for non-Claude clients.
  cat "$ROOT_DIR/clients/copilot/ailocal.instructions.md" \
      <(sed '/<!-- claude-only -->/,/<!-- \/claude-only -->/d' \
          "$ROOT_DIR/clients/claude/references/build-checklist.md") \
      > "$COPILOT_INSTR/ailocal.instructions.md"
  cp "$ROOT_DIR/clients/copilot/session-primer.md" "$COPILOT_INSTR/session-primer.md"
  info "Copilot instruction files deployed to ~/.copilot/instructions/"

  # Repo-level Copilot instructions. GENERATED, not tracked: hand-maintaining
  # this in .github/ lets its capability table drift from the profile, and a VS
  # Code agent then follows stale rows and dead paths. Source is the generated
  # copilot instructions. .github/ is gitignored for this name.
  mkdir -p "$ROOT_DIR/.github"
  cp "$AILOCAL_STATE/clients/copilot/repo-instructions.md" \
     "$ROOT_DIR/.github/copilot-instructions.md"
  info ".github/copilot-instructions.md generated (from clients/copilot/)"

  # ── Continue extension (local autocomplete + chat) ────────────────────────
  # Continue gives VS Code local tab-autocomplete (FIM) that Copilot can't. Deploy
  # a managed ~/.continue/config.json: chat/edit through the proxy, autocomplete
  # DIRECT to Ollama (FIM through the proxy is unreliable — continuedev/continue#2907).
  # The user's existing file is backed up first.
  # CONDITIONAL ON THE EXTENSION BEING INSTALLED. Writing a keyed config for
  # absent software is dead output and a needless place for a secret to sit, and
  # it accumulates a timestamped backup on every repair of a file nothing reads.
  # SUPPORTED-IF-PRESENT, not deleted: the FIM route is real (Copilot cannot do
  # local autocomplete) and `AILOCAL_CONTINUE=1` opts in before installing the
  # extension. Never installs Continue on the user's behalf.
  CONTINUE_CFG="$HOME/.continue/config.json"
  _continue_present=0
  if [ -n "${AILOCAL_CONTINUE:-}" ]; then
    _continue_present=1
  elif command -v code >/dev/null 2>&1 && \
       code --list-extensions 2>/dev/null | grep -qi '^continue\.continue$'; then
    _continue_present=1
  elif [ -f "$CONTINUE_CFG" ]; then
    # Already managed here previously — keep it current rather than stranding a
    # stale key in a file we wrote.
    _continue_present=1
  fi

  if [ "$_continue_present" = "1" ]; then
    mkdir -p "$HOME/.continue"
    backup "$CONTINUE_CFG" || true
    sed "s|__LITELLM_KEY__|${LITELLM_KEY}|g" \
        "$AILOCAL_STATE/clients/continue/config.json" > "$CONTINUE_CFG"
    info "Continue config deployed to ~/.continue/config.json (autocomplete: qwen2.5-coder:3b direct to Ollama)"
  else
    info "Continue extension not installed — skipping ~/.continue/config.json"
    info "  install 'continue.continue' then re-run, or set AILOCAL_CONTINUE=1 to force"
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
  CODEX_GEN="$AILOCAL_STATE/clients/codex"
  CODEX_HOME="$CODEX_HOME_DIR" envsubst '${CODEX_HOME}' < "$CODEX_GEN/config.toml" > "$CODEX_CFG"
  info "$CODEX_CFG written (from $CODEX_GEN/config.toml)"

  # model_catalog.json — always update (our managed file, no user customization)
  backup "$CODEX_CAT" || true
  cp "$AILOCAL_STATE/clients/model_catalog.json" "$CODEX_CAT"
  info "$CODEX_CAT written"

  # AGENTS.md — operating protocol (clients/codex/AGENTS.md.template, a TRACKED source)
  # + the shared build checklist, concatenated at install time. The template carries the .template
  # extension so the /AGENTS.md gitignore rule cannot swallow it (that bare pattern once did).
  cat "$ROOT_DIR/clients/codex/AGENTS.md.template" \
      <(sed '/<!-- claude-only -->/,/<!-- \/claude-only -->/d' \
          "$ROOT_DIR/clients/claude/references/build-checklist.md") \
      > "$CODEX_HOME_DIR/AGENTS.md"
  info "$CODEX_HOME_DIR/AGENTS.md written (protocol + build checklist)"

  # /local-build prompt + plan/review model profiles — managed, always overwrite.
  mkdir -p "$CODEX_HOME_DIR/prompts"
  cp "$ROOT_DIR/clients/codex/prompts/"*.md "$CODEX_HOME_DIR/prompts/"
  cp "$CODEX_GEN/plan.config.toml" "$CODEX_GEN/review.config.toml" "$CODEX_HOME_DIR/"
  info "prompts/ ($(ls "$ROOT_DIR/clients/codex/prompts/" | tr '\n' ' ')) + plan/review profiles written"

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
  echo "    rm $CODEX_CFG && ailocal clients codex"
fi

# ── Claude Code ───────────────────────────────────────────────────────────

if has_target "claude"; then
  step "Installing Claude Code config (~/.config/ailocal/claude/)"

  CLAUDE_HOME_DIR="$AILOCAL_CFG/claude"
  mkdir -p "$CLAUDE_HOME_DIR"

  CLAUDE_CFG="$CLAUDE_HOME_DIR/settings.json"
  CLAUDE_MD="$CLAUDE_HOME_DIR/AGENTS.md"
  CLAUDE_JSON="$CLAUDE_HOME_DIR/.claude.json"

  # settings.json — always overwrite (managed file, no secrets — key comes
  # from the claude-local wrapper's process-scoped env, never written to disk here).
  backup "$CLAUDE_CFG" || true
  cp "$AILOCAL_STATE/clients/claude/settings.json" "$CLAUDE_CFG"
  info "$CLAUDE_CFG written"

  # No instruction-policy file is written into this root. ailocal publishes the
  # integration contract above and stops there; composing client instruction
  # policy is outside its ownership. Writing one here would fight whatever
  # composes this root and re-create the drift that removal fixed.

  # Local agent trio + /local-build command + checklist — ailocal owns these FILES, but not the
  # directories they live in. Other tools may deploy into them too; see install_managed_dir.
  for d in agents commands references; do
    install_managed_dir "$ROOT_DIR/clients/claude/$d" "$CLAUDE_HOME_DIR/$d"
  done
  info "$CLAUDE_HOME_DIR/{agents,commands,references} written (external overlays preserved)"

  # .claude.json — seed onboarding-complete only if absent, so a real session
  # under this CLAUDE_CONFIG_DIR never gets clobbered.
  if [ -f "$CLAUDE_JSON" ]; then
    skip "$CLAUDE_JSON already exists — left untouched"
  else
    echo '{"hasCompletedOnboarding": true}' > "$CLAUDE_JSON"
    info "$CLAUDE_JSON seeded (skips first-run onboarding)"
  fi

  echo "  Launch with: claude-local   (reload your shell first: source ~/.zshrc)"
  echo "  Plain 'claude' still talks to Anthropic's cloud — model/base URL/keys are untouched."
  echo "  It does get the same Python LSP baseline (pyright-lsp) installed below."
fi

# ── Minimum Python LSP baseline (ailocal-owned) ────────────────────────────
# ailocal provides the minimum local-client compatibility baseline required by
# the isolated profiles it creates. Anything broader is owned elsewhere:
# broader language tooling, cross-client integration, and policy.
#
# The generated settings.json sets ENABLE_LSP_TOOL=1, but a plugin is what puts a
# language server behind that tool. Delegating all plugin provisioning elsewhere
# leaves an ailocal-only machine advertising a capability that cannot answer,
# which is worse than leaving the tool off.
#
# ONE LANGUAGE, DELIBERATELY. Python, because this repository is Python and its
# own agents edit it. Everything else — TypeScript, Go, C — is owned elsewhere, so
# there is exactly one owner per layer and no duplicate plugin management.
#
# A plugin only wires up a binary the user already has, so this checks for the
# server before installing the plugin rather than advertising a dead tool.
# Applied to BOTH the isolated claude-local root AND the user's own ~/.claude
# (plain cloud `claude`). Earlier this only touched claude-local, on the theory
# that ailocal owns only the local-model workflow — but the LSP baseline isn't
# LiteLLM/routing behavior, it's a Claude Code plugin wiring up a binary the
# user already has, and there is no reason for the exact same fix to not apply
# to a client's cloud session too. ailocal's other guarantee — that client
# CONFIG (models, base URL, keys) for cloud sessions is never touched — still
# holds; this only installs+enables one plugin, same as a user could do by hand.
lsp_baseline() {
  local root="$1" label="$2"
  command -v claude >/dev/null 2>&1 || { skip "claude not on PATH — no LSP baseline ($label)"; return 0; }
  [ -d "$root" ] || { skip "$root missing — no LSP baseline ($label)"; return 0; }

  if ! command -v pyright-langserver >/dev/null 2>&1; then
    warn "pyright-langserver not installed — $label has NO Python LSP."
    warn "  Install it, then re-run this script:  npm i -g pyright"
    return 0
  fi

  # Idempotent: the marketplace update and install are both no-ops when current,
  # but checking first keeps a re-run quiet and fast. IMPORTANT: `claude plugin
  # install` does NOT enable the plugin — a plugin can be present but disabled,
  # and `claude plugin list` still lists it either way. Checking only for
  # presence (not the "enabled" status line) let a disabled plugin report as
  # "already present" on every future re-run, permanently hiding the gap:
  # claude-local silently had no working LSP with no error anywhere. Check the
  # actual enabled state and fix it forward rather than treating install as done.
  local plugin_status
  plugin_status="$(CLAUDE_CONFIG_DIR="$root" claude plugin list 2>/dev/null)"
  if printf '%s' "$plugin_status" | grep -q 'pyright-lsp@claude-plugins-official'; then
    if printf '%s' "$plugin_status" | grep -A2 'pyright-lsp@claude-plugins-official' | grep -q 'enabled'; then
      info "Python LSP baseline already present and enabled ($label)"
      return 0
    fi
    warn "pyright-lsp plugin present but disabled ($label) — enabling"
    # A live `claude` session already open on this same root can race the
    # plugin-state write (its own background settings sync competes with this
    # enable call), producing a transient failure that clears on retry. Two
    # attempts with a short pause covers that without masking a real failure.
    local _try
    for _try in 1 2; do
      if CLAUDE_CONFIG_DIR="$root" claude plugin enable \
           pyright-lsp@claude-plugins-official >/dev/null 2>&1; then
        info "Python LSP baseline enabled ($label)"
        return 0
      fi
      [ "$_try" = 1 ] && sleep 2
    done
    warn "pyright-lsp enable failed — $label has no working Python LSP"
    warn "  If a live 'claude' session has $root open, close it and re-run."
    return 0
  fi

  CLAUDE_CONFIG_DIR="$root" claude plugin marketplace update \
    claude-plugins-official >/dev/null 2>&1 || true
  if CLAUDE_CONFIG_DIR="$root" claude plugin install \
       pyright-lsp@claude-plugins-official >/dev/null 2>&1 \
     && CLAUDE_CONFIG_DIR="$root" claude plugin enable \
       pyright-lsp@claude-plugins-official >/dev/null 2>&1; then
    info "Python LSP baseline installed and enabled ($label)"
  else
    warn "pyright-lsp install/enable failed — $label has no Python LSP"
  fi
}

if has_target "claude"; then
  step "Python LSP baseline (ailocal-owned minimum)"
  lsp_baseline "$HOME/.config/ailocal/claude" "claude-local"
  lsp_baseline "$HOME/.claude" "claude (cloud)"
fi

# ── Broader language servers: not ailocal's to install ────────────────────
# ailocal provisions ONLY the Python baseline its own agents need. Installing
# language servers it does not own inverts the dependency: a capability owned
# elsewhere would be provisioned only when ailocal happened to run. Anything
# beyond the baseline is installed by whatever owns it. We only report state.
if has_target "claude"; then
  info "language servers: Python baseline only — anything broader is provisioned by its own owner"
fi

# ── Codex MCP: WITHHELD BY POLICY, not restored ────────────────────────────
#   codex-local is intended to have NO grepai MCP, NO LSP MCP, NO GitHub MCP
#   and no namespace tools. Codex cannot dispatch namespaced tool names, so an
#   MCP server there advertises a surface it cannot drive.
#
# An empty [mcp_servers.*] section is therefore the CORRECT outcome, not a
# condition to warn about or restore. Never invoke another tool's global MCP
# sync from here: it re-adds what this policy withholds and mutates other
# clients as a side effect of installing this one. claude-local needs no
# restoration — its MCP registrations live in .claude.json, which this script
# preserves rather than rewriting.
info "Codex MCP intentionally withheld (Codex cannot dispatch namespaced tools)"
info "  claude-local MCP registrations in .claude.json are preserved."

# ── External agent overlays ───────────────────────────────────────────────
# install_managed_dir carries any marked block in a destination file across the
# overwrite, and leaves foreign files and symlinks it does not own alone. That
# is the whole contract: ailocal restores its own baseline and preserves what it
# did not write. It does not detect, warn about, or re-invoke another product's
# installer.

# ── ailocal on PATH ─────────────────────────────────────────────────────────
# This used to generate a shim in ~/.local/bin that read the checkout location
# from ~/.config/ailocal/repo. Its stated reason was that "the repo IS the
# runtime" because LiteLLM bind-mounted deploy/litellm straight out of it.
# `ailocal provision` ended that: assets are installed into the data root and
# Compose points there, so the command is a normal console script and the
# indirection has nothing left to solve (ADR 009).
warn_if_not_on_path() {
  command -v ailocal >/dev/null 2>&1 && return 0
  warn "ailocal is not on PATH. Install the command:  pipx install ."
  warn "Until then use ./ailocal from the checkout."
}

warn_if_not_on_path

echo "  New shells pick up claude-local/codex-local/ailocal-code automatically"
echo "  (sourced from ~/.config/ailocal/configure.zsh). For this shell: source ~/.zshrc"
echo ""
echo "  To force-update a config that was skipped, delete it and re-run:"
has_target "codex"   && echo "    rm $AILOCAL_CFG/codex/config.toml"
has_target "claude"  && echo "    rm $AILOCAL_CFG/claude/settings.json"
has_target "vscode"  && echo "    VS Code:  no file to delete — re-enter via \"Chat: Manage Language Models\" (key lives in SecretStorage)"
echo "  Then: ailocal clients [target]"
echo ""
echo "  Key rotation: after running `ailocal install`, restart the proxy with:"
echo "    ailocal start   # LiteLLM reloads LITELLM_MASTER_KEY from .env"
echo "  ...then re-run ailocal clients to refresh ~/.config/ailocal/env"
