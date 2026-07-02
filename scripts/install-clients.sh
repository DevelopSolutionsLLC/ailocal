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
#   vscode → ~/Library/Application Support/Code/User/chatLanguageModels.json
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
  step "Installing VS Code Copilot Chat config"

  VSCODE_USER="$HOME/Library/Application Support/Code/User"
  COPILOT_CFG="$VSCODE_USER/chatLanguageModels.json"
  COPILOT_TPL="$ROOT_DIR/config/clients/vscode-copilot.json"

  if [ ! -d "$VSCODE_USER" ]; then
    warn "VS Code user directory not found — is VS Code installed?"
  else
    # Always re-merge so key rotation and template changes are picked up on every run.
    backup "$COPILOT_CFG" || true
    if [ -f "$COPILOT_CFG" ]; then
      # Existing file: merge ailocal provider into the array, replacing direct Ollama entry.
      # Removes the bare {"vendor":"ollama"} entry that bypasses LiteLLM, then appends our provider.
      python3 - "$COPILOT_CFG" "$COPILOT_TPL" "$LITELLM_KEY" <<'PYEOF'
import json, sys
cfg_path, tpl_path, api_key = sys.argv[1], sys.argv[2], sys.argv[3]
with open(cfg_path) as f:
    existing = json.load(f)
with open(tpl_path) as f:
    template = json.load(f)
# Inject API key (template placeholder already includes "Bearer " prefix)
for provider in template:
    provider['apiKey'] = provider['apiKey'].replace('<LITELLM_MASTER_KEY>', api_key)
# Drop direct Ollama entries (vendor=ollama) and stale ailocal entries, then append ours
filtered = [p for p in existing
            if p.get('vendor') != 'ollama'
            and p.get('name') != 'ailocal (LiteLLM)']
filtered.extend(template)
with open(cfg_path, 'w') as f:
    json.dump(filtered, f, indent=2)
PYEOF
      info "chatLanguageModels.json merged (ailocal provider updated)"
    else
      # No existing file: write template with key substituted
      sed "s|<LITELLM_MASTER_KEY>|${LITELLM_KEY}|g" "$COPILOT_TPL" > "$COPILOT_CFG"
      info "chatLanguageModels.json written"
    fi
    echo "  → Reload VS Code to see coder/reasoner/supervisor/router in the model picker"
    echo "    (Cmd+Shift+P → Developer: Reload Window)"
  fi
fi

# ── Codex CLI ─────────────────────────────────────────────────────────────

if has_target "codex"; then
  step "Installing Codex config (~/.codex/)"
  mkdir -p "$HOME/.codex"

  CODEX_CFG="$HOME/.codex/config.toml"
  CODEX_CAT="$HOME/.codex/model_catalog.json"

  # config.toml — additive merge: inject ailocal settings without replacing existing config
  if already_has "$CODEX_CFG" 'model_provider = "ailocal"'; then
    skip "~/.codex/config.toml already configured for ailocal"
    # Still ensure model_catalog_json is present
    if ! already_has "$CODEX_CFG" "model_catalog_json"; then
      backup "$CODEX_CFG"
      sed -i'' -e '/^model[[:space:]]*=[[:space:]]*"coder"/a\
model_catalog_json = "'"$HOME"'/.codex/model_catalog.json"' "$CODEX_CFG"
      info "Injected missing model_catalog_json into existing config"
    else
      skip "model_catalog_json already present"
    fi
  elif [ -f "$CODEX_CFG" ]; then
    # Existing file without ailocal config: prepend top-level keys, append provider block.
    # Preserves computer-use, marketplace, plugin, and MCP entries the user may have added.
    backup "$CODEX_CFG"
    TMPFILE=$(mktemp)
    cat > "$TMPFILE" <<'AILOCAL_HEADER'
# ── ailocal provider — injected by install-clients.sh ──────────────────────
AILOCAL_HEADER
    cat >> "$TMPFILE" <<AILOCAL_KEYS
model_provider = "ailocal"
model = "coder"
model_catalog_json = "${HOME}/.codex/model_catalog.json"
approval_policy = "never"
sandbox_policy = "danger-full-access"
model_reasoning_effort = "medium"
model_supports_reasoning_summaries = false

AILOCAL_KEYS
    cat "$CODEX_CFG" >> "$TMPFILE"
    if ! grep -qF '[model_providers.ailocal]' "$CODEX_CFG"; then
      printf '\n[model_providers.ailocal]\nname = "ailocal (LiteLLM)"\nbase_url = "http://localhost:4000/v1"\nenv_key = "LITELLM_KEY"\n' >> "$TMPFILE"
    fi
    mv "$TMPFILE" "$CODEX_CFG"
    info "~/.codex/config.toml merged (existing config preserved, ailocal provider added)"
  else
    # No existing file: write the full template
    envsubst '${HOME}' < "$ROOT_DIR/config/clients/codex-config.toml" > "$CODEX_CFG"
    info "~/.codex/config.toml written"
  fi

  # model_catalog.json — always update (our managed file, no user customization)
  backup "$CODEX_CAT" || true
  cp "$ROOT_DIR/config/clients/model_catalog.json" "$CODEX_CAT"
  info "~/.codex/model_catalog.json written"
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

# ── Done ───────────────────────────────────────────────────────────────────

echo ""
echo "  ✓ Done. Restart affected tools to pick up changes:"
has_target "vscode"  && echo "    VS Code:      Cmd+Shift+P → Developer: Reload Window"
has_target "codex"   && echo "    Codex:        restart any open Codex sessions"
has_target "claude"  && echo "    Claude Code:  restart any open claude sessions"
echo ""
echo "  To force-update a config that was skipped, delete it and re-run:"
has_target "vscode"  && echo "    rm \"$HOME/Library/Application Support/Code/User/chatLanguageModels.json\""
has_target "codex"   && echo "    rm ~/.codex/config.toml"
has_target "claude"  && echo "    rm ~/.claude/settings.json"
echo "  Then: ./scripts/install-clients.sh [target]"
echo ""
echo "  Key rotation: after running install.sh, restart Docker services with:"
echo "    ./scripts/start.sh"
echo "  Caddy picks up the new LITELLM_MASTER_KEY automatically — no need to re-run this script."
