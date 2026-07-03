#!/usr/bin/env bash
# install.sh — bootstrap host tools, generate .env, verify Docker & Ollama
# Idempotent: safe to run multiple times.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE="$ROOT_DIR/.env"

# ── Helpers ────────────────────────────────────────────────────────────────

has() { command -v "$1" >/dev/null 2>&1; }

info()  { echo "  ✓ $*"; }
warn()  { echo "  ⚠ $*"; }
error() { echo "  ✗ $*" >&2; }
step()  { echo; echo "▶ $*"; }

# Prompt for a value; shows default in brackets; returns default if user hits Enter.
# Usage: prompt_value "Prompt text" "default_value"  → sets $REPLY
prompt_value() {
  local prompt="$1"
  local default="${2:-}"
  if [ -n "$default" ]; then
    read -r -p "  $prompt [$default]: " REPLY
    REPLY="${REPLY:-$default}"
  else
    read -r -p "  $prompt: " REPLY
  fi
}

# Prompt for a secret (no echo); skips prompt if already set in env.
prompt_secret() {
  local varname="$1"
  local prompt="$2"
  read -r -s -p "  $prompt (leave blank to auto-generate): " REPLY
  echo
}

# ── Homebrew ───────────────────────────────────────────────────────────────
step "Checking Homebrew"
if ! has brew; then
  echo "  Homebrew not found — installing..."
  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
  echo 'eval "$(/opt/homebrew/bin/brew shellenv)"' >> ~/.zprofile
  eval "$(/opt/homebrew/bin/brew shellenv)"
else
  info "Homebrew present"
fi

# ── CLI tools ──────────────────────────────────────────────────────────────
step "Checking CLI tools"
for pkg in git jq; do
  if ! has "$pkg"; then
    echo "  Installing $pkg..."
    brew install "$pkg"
  else
    info "$pkg present"
  fi
done

# ── Docker ─────────────────────────────────────────────────────────────────
step "Checking Docker"
if ! has docker; then
  echo "  Docker not found — installing Docker Desktop via Homebrew..."
  brew install --cask docker 2>/dev/null || {
    error "Could not install Docker Desktop automatically."
    echo "  Install manually: https://www.docker.com/products/docker-desktop/" >&2
    exit 1
  }
  echo "  Docker Desktop installed."
  echo "  ▶ Open Docker Desktop, accept the license, complete first-run setup,"
  echo "    then re-run this script."
  exit 0
fi
if ! docker ps >/dev/null 2>&1; then
  error "Docker daemon is not running. Open Docker Desktop and re-run this script."
  exit 1
fi
info "Docker present and running"

# ── Ollama ─────────────────────────────────────────────────────────────────
step "Checking Ollama"
if ! has ollama; then
  echo "  Ollama not found — installing via Homebrew cask..."
  brew install --cask ollama 2>/dev/null || brew install ollama 2>/dev/null || {
    warn "Could not install Ollama via Homebrew."
    echo "  Install manually from: https://ollama.ai/download"
  }
fi
if has ollama; then
  info "Ollama CLI present"
  if ! ollama list >/dev/null 2>&1; then
    warn "Ollama daemon is not running."
    echo "  Start it with: ollama serve   (or open /Applications/Ollama.app)"
    echo "  Models will be pulled by scripts/install-models.sh after Ollama is running."
  else
    info "Ollama daemon is responding"
  fi
else
  warn "Ollama CLI not found after install attempt — proceed manually."
fi

# ── Ollama runtime env (keep-alive + parallel models) ─────────────────────
# IMPORTANT: the Ollama macOS app is a GUI/launchd process — it does NOT read
# ~/.zshrc. Env vars must be set via launchctl (persisted with a LaunchAgent).
# scripts/setup-ollama-env.sh does that. Checking the shell env here would be
# misleading, so we check launchctl (what the app actually sees).
step "Configuring Ollama runtime env (launchctl, not ~/.zshrc)"
CUR_KA=$(launchctl getenv OLLAMA_KEEP_ALIVE 2>/dev/null || true)
if [ -n "$CUR_KA" ]; then
  info "OLLAMA_KEEP_ALIVE=$CUR_KA  OLLAMA_MAX_LOADED_MODELS=$(launchctl getenv OLLAMA_MAX_LOADED_MODELS 2>/dev/null || echo '?')"
else
  warn "Ollama env not set where the app can see it (launchctl) — models would unload after 5 min."
  bash "$ROOT_DIR/scripts/setup-ollama-env.sh" \
    || warn "Could not configure Ollama env — run ./scripts/setup-ollama-env.sh manually."
  echo "  ▶ Restart Ollama (menubar → Quit, then reopen) for it to take effect."
fi

# ── Hardware profile selection ─────────────────────────────────────────────
step "Detecting hardware profile"

RAM_BYTES=$(sysctl -n hw.memsize 2>/dev/null || echo 0)
RAM_GB=$((RAM_BYTES / 1024 / 1024 / 1024))

if   [ "$RAM_GB" -ge 96 ]; then RAM_TIER="128gb"
elif [ "$RAM_GB" -ge 48 ]; then RAM_TIER="64gb"
elif [ "$RAM_GB" -ge 24 ]; then RAM_TIER="32gb"
else                             RAM_TIER="16gb"
fi

PROFILE_SRC="$ROOT_DIR/config/profiles/${RAM_TIER}.yaml"
MODELS_YAML="$ROOT_DIR/config/models.yaml"

info "Detected ${RAM_GB} GB RAM → profile: ${RAM_TIER}"

if [ -f "$MODELS_YAML" ]; then
  ACTIVE=$(grep '^# profile:' "$MODELS_YAML" | awk '{print $3}' || echo "unknown")
  if [ "$ACTIVE" = "$RAM_TIER" ]; then
    info "models.yaml already on $RAM_TIER profile — keeping existing"
  else
    warn "models.yaml is on '$ACTIVE' profile — switching to $RAM_TIER"
    read -r -p "  Apply $RAM_TIER profile? This overwrites any custom model changes. [y/N]: " APPLY
    if [[ "${APPLY:-}" =~ ^[Yy]$ ]]; then
      cp "$PROFILE_SRC" "$MODELS_YAML"
      info "models.yaml updated to $RAM_TIER profile"
    else
      info "Keeping existing models.yaml"
    fi
  fi
else
  cp "$PROFILE_SRC" "$MODELS_YAML"
  info "models.yaml written from $RAM_TIER profile"
fi

# ── Directory structure ────────────────────────────────────────────────────
step "Creating directory structure"
mkdir -p \
  "$ROOT_DIR/data" \
  "$ROOT_DIR/backups" \
  "$ROOT_DIR/config/litellm" \
  "$ROOT_DIR/config/mcp"
# backups/ holds archives that include .env — lock it down.
chmod 700 "$ROOT_DIR/backups"
info "Directories ready"

# ── .env generation ────────────────────────────────────────────────────────
step "Configuring environment (.env)"

# Run the service stack, client install, and healthcheck automatically.
run_next_steps() {
  # Sync models.yaml → litellm config so LiteLLM sees the latest model choices.
  if has python3 && [ -f "$ROOT_DIR/scripts/sync-models.py" ]; then
    echo
    step "Syncing model config"
    python3 "$ROOT_DIR/scripts/sync-models.py" || true
  fi

  echo
  step "Starting Docker services"
  bash "$ROOT_DIR/scripts/start.sh" --no-wait

  # Always restart LiteLLM so it picks up any config or model changes.
  echo
  step "Reloading LiteLLM"
  docker compose restart litellm
  attempts=0
  until curl -sSf --max-time 3 http://localhost:4000/health/liveliness >/dev/null 2>&1; do
    attempts=$((attempts + 1))
    if [ $attempts -ge 30 ]; then
      warn "LiteLLM did not become ready — check: docker logs ailocal_litellm"
      break
    fi
    printf "  Waiting... (%ds)\r" $((attempts * 3))
    sleep 3
  done
  echo ""
  info "LiteLLM ready"

  echo
  step "Pulling Ollama models"
  bash "$ROOT_DIR/scripts/install-models.sh"

  echo
  step "Checking health"
  bash "$ROOT_DIR/scripts/healthcheck.sh" || true

  # Client configs are OPT-IN — installing them rewrites/merges existing
  # Claude Code / Codex / VS Code settings, which can disrupt a customized
  # setup. Ask instead of doing it automatically.
  echo
  step "Client configs (optional)"
  echo "  ailocal can point Claude Code, Codex, and VS Code at the local proxy."
  echo "  ⚠ This backs up, then rewrites/merges existing client configs."
  echo "    Choose: all | claude | codex | vscode  (space-separated) — or Enter to skip"
  read -r -p "  Install which client configs? [skip]: " CLIENTS
  CLIENTS="${CLIENTS:-skip}"
  case "$CLIENTS" in
    skip|"")
      info "Skipped — run later with:  ./scripts/install-clients.sh [all|claude|codex|vscode]" ;;
    all)
      bash "$ROOT_DIR/scripts/install-clients.sh" || warn "Client install reported issues." ;;
    *)
      # shellcheck disable=SC2086
      bash "$ROOT_DIR/scripts/install-clients.sh" $CLIENTS \
        || warn "Client install reported issues — check target names (claude codex vscode)." ;;
  esac

  echo
  step "Done"
  echo "  LiteLLM proxy is ready at http://localhost:4000"
  echo "  Verify a real request:  ./scripts/smoke-test.sh"
}

if [ -f "$ENV_FILE" ]; then
  echo "  .env already exists."
  read -r -p "  Re-generate it? Existing values will be overwritten. [y/N]: " REGEN
  if [[ ! "${REGEN:-}" =~ ^[Yy]$ ]]; then
    echo "  Keeping existing .env."
    run_next_steps
    exit 0
  fi
fi

echo
echo "  Generating the LiteLLM master key..."

# LiteLLM master key — must start with sk- for OpenAI SDK compatibility
LITELLM_MASTER_KEY="sk-$(openssl rand -hex 24)"


# Write the .env file from scratch (no fragile sed replacements)
cat > "$ENV_FILE" <<EOF
# ailocal — generated by scripts/install.sh on $(date)
# Do NOT commit this file to version control.

# ── General ────────────────────────────────────────────────────────────────
AILOCAL_ENV=local

# ── Ollama ─────────────────────────────────────────────────────────────────
OLLAMA_URL=http://host.docker.internal:11434

# ── LiteLLM proxy key ──────────────────────────────────────────────────────
# Use this as your ANTHROPIC_API_KEY / OPENAI_API_KEY when pointing clients
# at http://localhost:4000 instead of the real cloud APIs.
LITELLM_MASTER_KEY=${LITELLM_MASTER_KEY}

# ── Cloud fallbacks (disabled by default) ─────────────────────────────────
# Set ENABLE_CLOUD=true and uncomment the relevant model block in
# config/litellm/config.yaml to enable cloud fallback for a specific model.
ENABLE_CLOUD=false
# To enable cloud fallback: add your key here, set ENABLE_CLOUD=true,
# and uncomment the relevant model block in config/litellm/config.yaml.
# Then: docker compose restart litellm
ANTHROPIC_API_KEY=
OPENAI_API_KEY=
EOF

chmod 600 "$ENV_FILE"
info ".env written with the LiteLLM master key (chmod 600)"

run_next_steps
