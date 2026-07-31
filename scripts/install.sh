#!/usr/bin/env bash
# install.sh — bootstrap host tools, generate .env, verify Docker & Ollama
# Idempotent: safe to run multiple times.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE="$ROOT_DIR/.env"

# Single source of truth for how this stack is composed (deploy/litellm + deploy/searxng).
AILOCAL_ROOT="$ROOT_DIR"
. "$ROOT_DIR/scripts/lib/compose.sh"

# ── Helpers ────────────────────────────────────────────────────────────────

has() { command -v "$1" >/dev/null 2>&1; }

info()  { echo "  ✓ $*"; }
warn()  { echo "  ⚠ $*" >&2; }
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
echo "  Two ways to run Ollama:"
echo "    [1] Production autostart — a launchd LaunchAgent runs 'ollama serve' at"
echo "        login (auto-restart, logs, env baked in incl. OLLAMA_MODELS on"
echo "        /Users/Shared) and preloads the coder model. Disables Ollama.app"
echo "        'launch at login' to avoid a port 11434 conflict. (scripts/setup-startup.sh)"
echo "    [2] Env-only — keep using Ollama.app; just set runtime env vars via"
echo "        launchctl so models don't unload. (scripts/setup-ollama-env.sh)"
read -r -p "  Set up production autostart? [y/N]: " AUTOSTART
if [[ "${AUTOSTART:-}" =~ ^[Yy]$ ]]; then
  bash "$ROOT_DIR/scripts/setup-startup.sh" --model coder \
    || warn "Could not set up autostart — run ./scripts/setup-startup.sh manually."
else
  CUR_KA=$(launchctl getenv OLLAMA_KEEP_ALIVE 2>/dev/null || true)
  if [ -n "$CUR_KA" ]; then
    info "OLLAMA_KEEP_ALIVE=$CUR_KA  OLLAMA_MAX_LOADED_MODELS=$(launchctl getenv OLLAMA_MAX_LOADED_MODELS 2>/dev/null || echo '?')"
  else
    warn "Ollama env not set where the app can see it (launchctl) — models would unload after 5 min."
    bash "$ROOT_DIR/scripts/setup-ollama-env.sh" \
      || warn "Could not configure Ollama env — run ./scripts/setup-ollama-env.sh manually."
    echo "  ▶ Restart Ollama (menubar → Quit, then reopen) for it to take effect."
  fi
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
ACTIVE_PROFILE="$ROOT_DIR/config/active-profile"

info "Detected ${RAM_GB} GB RAM → profile: ${RAM_TIER}"

# Record the active tier as a one-line marker (machine-specific, gitignored). sync-models.py reads
# config/profiles/<tier>.yaml directly — there is no intermediate models.yaml copy to drift or to
# edit-then-lose. Edit the tracked profile itself to change models.
if [ ! -f "$PROFILE_SRC" ]; then
  warn "profile $RAM_TIER not found at $PROFILE_SRC — cannot continue"
  exit 1
fi
CURRENT="$(cat "$ACTIVE_PROFILE" 2>/dev/null || echo none)"
if [ "$CURRENT" = "$RAM_TIER" ]; then
  info "active profile already $RAM_TIER"
else
  [ "$CURRENT" != "none" ] && warn "active profile was '$CURRENT' — switching to $RAM_TIER"
  echo "$RAM_TIER" > "$ACTIVE_PROFILE"
  info "active profile set to $RAM_TIER (config/active-profile)"
fi
[ "$RAM_TIER" != "64gb" ] && warn "profile '$RAM_TIER' is marked status: unverified — validate with 'ailocal validate' before relying on it"

# ── Directory structure ────────────────────────────────────────────────────
step "Creating directory structure"
# Runtime state lives outside the checkout (see scripts/lib/compose.sh). Only the
# backup directory needs pre-creating, because it holds .env snapshots from
# update.sh and must be locked down before anything writes one.
mkdir -p "${AILOCAL_STATE:-${XDG_STATE_HOME:-$HOME/.local/state}/ailocal}/backups"
chmod 700 "${AILOCAL_STATE:-${XDG_STATE_HOME:-$HOME/.local/state}/ailocal}/backups"
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
  # Initial setup pulls the latest image so a fresh install starts on current LiteLLM
  # (bug fixes land in main-stable frequently). Routine start.sh never auto-pulls —
  # that keeps a reboot reproducible; use update.sh (or `./scripts/update.sh`) to refresh.
  step "Pulling latest Docker images"
  dc pull

  echo
  step "Starting Docker services"
  bash "$ROOT_DIR/scripts/start.sh" --no-wait

  # Always restart LiteLLM so it picks up any config or model changes.
  echo
  step "Reloading LiteLLM"
  dc restart litellm searxng
  attempts=0
  until curl -sSf --max-time 3 http://localhost:4000/health/liveliness >/dev/null 2>&1; do
    attempts=$((attempts + 1))
    if [ $attempts -ge 30 ]; then
      warn "LiteLLM did not become ready — check: docker logs ailocal-litellm"
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
  bash "$ROOT_DIR/scripts/doctor.sh" || true

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
      info "Skipped — run later with:  ailocal clients [all|claude|codex|vscode]" ;;
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
echo "  Generating the LiteLLM master key and SearXNG secret..."

# LiteLLM master key — must start with sk- for OpenAI SDK compatibility
LITELLM_MASTER_KEY="sk-$(openssl rand -hex 24)"

# SearXNG refuses to start without a real secret. Generated per install so the
# placeholder in deploy/searxng/settings.yml is never used.
SEARXNG_SECRET="$(openssl rand -hex 32)"


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

# ── SearXNG (local web search backend) ─────────────────────────────────────
# Consumed by deploy/searxng/docker-compose.yml. LiteLLM reaches SearXNG over
# the ailocal_net bridge at http://searxng:8080 — no API key, nothing external.
SEARXNG_SECRET=${SEARXNG_SECRET}

# ── Cloud fallbacks (disabled by default) ─────────────────────────────────
# Set ENABLE_CLOUD=true and uncomment the relevant model block in
# config/litellm/config.yaml to enable cloud fallback for a specific model.
ENABLE_CLOUD=false
# To enable cloud fallback: add your key here, set ENABLE_CLOUD=true,
# and uncomment the relevant model block in config/litellm/config.yaml.
# Then: ailocal start  (or: dc restart litellm)
ANTHROPIC_API_KEY=
OPENAI_API_KEY=
EOF

chmod 600 "$ENV_FILE"
info ".env written with the LiteLLM master key (chmod 600)"

run_next_steps
