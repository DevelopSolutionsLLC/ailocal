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
for pkg in git jq yq; do
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
  error "Docker not found. Install Docker Desktop for Mac (Apple Silicon):"
  error "  https://www.docker.com/products/docker-desktop/"
  exit 1
fi
if ! docker ps >/dev/null 2>&1; then
  error "Docker daemon is not running. Start Docker Desktop and re-run this script."
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

# ── Directory structure ────────────────────────────────────────────────────
step "Creating directory structure"
mkdir -p \
  "$ROOT_DIR/data" \
  "$ROOT_DIR/backups" \
  "$ROOT_DIR/logs/caddy" \
  "$ROOT_DIR/config/litellm" \
  "$ROOT_DIR/config/mcp" \
  "$ROOT_DIR/config/caddy" \
  "$ROOT_DIR/config/prometheus" \
  "$ROOT_DIR/config/grafana"
# backups/ holds archives that include .env — lock it down.
chmod 700 "$ROOT_DIR/backups"
info "Directories ready"

# ── .env generation ────────────────────────────────────────────────────────
step "Configuring environment (.env)"

# Shared next-steps block — printed whether .env is kept or newly written.
print_next_steps() {
  echo
  step "Bootstrap complete"
  echo "  Next steps:"
  echo "  1. Start Ollama:           ollama serve  (or open Ollama.app)"
  echo "  2. Pull models:            ./scripts/install-models.sh"
  echo "  3. Start Docker services:  ./scripts/start.sh"
  echo "  4. Check health:           ./scripts/healthcheck.sh"
  echo "  5. Open WebUI:             http://localhost:8081"
  echo "  6. LiteLLM API:            http://localhost:4000"
}

if [ -f "$ENV_FILE" ]; then
  echo "  .env already exists."
  read -r -p "  Re-generate it? Existing values will be overwritten. [y/N]: " REGEN
  if [[ ! "${REGEN:-}" =~ ^[Yy]$ ]]; then
    echo "  Keeping existing .env."
    # Still write the Prometheus bearer token in case it's missing
    EXISTING_KEY=$(grep '^LITELLM_MASTER_KEY=' "$ENV_FILE" | cut -d= -f2)
    if [ -n "$EXISTING_KEY" ]; then
      BEARER_TOKEN_FILE="$ROOT_DIR/config/prometheus/.bearer_token"
      echo -n "$EXISTING_KEY" > "$BEARER_TOKEN_FILE"
      chmod 600 "$BEARER_TOKEN_FILE"
      info "Prometheus bearer token refreshed"
    fi
    print_next_steps
    exit 0
  fi
fi

echo
echo "  Generating secure random secrets for database, cache, and auth..."

POSTGRES_PASSWORD="$(openssl rand -base64 20 | tr -d '/+=' | head -c 24)"
REDIS_PASSWORD="$(openssl rand -base64 16 | tr -d '/+=' | head -c 18)"
JWT_SECRET="$(openssl rand -hex 32)"
ADMIN_PASSWORD="$(openssl rand -base64 14 | tr -d '/+=' | head -c 16)"
# LiteLLM master key — must start with sk- for OpenAI SDK compatibility
LITELLM_MASTER_KEY="sk-$(openssl rand -hex 24)"


# Write the .env file from scratch (no fragile sed replacements)
cat > "$ENV_FILE" <<EOF
# ailocal — generated by scripts/install.sh on $(date)
# Do NOT commit this file to version control.

# ── General ────────────────────────────────────────────────────────────────
AILOCAL_ENV=local
HOST_HTTP_PORT=8080
HOST_PROMETHEUS_PORT=9090
HOST_GRAFANA_PORT=3000

# ── Ollama ─────────────────────────────────────────────────────────────────
OLLAMA_URL=http://host.docker.internal:11434
OLLAMA_QUANTIZATION=q4_K_M

# ── Database ───────────────────────────────────────────────────────────────
POSTGRES_USER=ailocal
POSTGRES_PASSWORD=${POSTGRES_PASSWORD}
POSTGRES_DB=ailocal

# ── Redis ──────────────────────────────────────────────────────────────────
REDIS_PASSWORD=${REDIS_PASSWORD}

# ── LiteLLM proxy key ──────────────────────────────────────────────────────
# Use this as your ANTHROPIC_API_KEY / OPENAI_API_KEY when pointing clients
# at http://localhost:4000 instead of the real cloud APIs.
LITELLM_MASTER_KEY=${LITELLM_MASTER_KEY}

# ── Web UI / Admin ─────────────────────────────────────────────────────────
JWT_SECRET=${JWT_SECRET}
ADMIN_PASSWORD=${ADMIN_PASSWORD}

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
info ".env written with secure random secrets (chmod 600)"

# Write Prometheus bearer token file — Prometheus reads this to auth against LiteLLM /metrics.
# Kept separate from prometheus.yml so that file stays static and committable.
BEARER_TOKEN_FILE="$ROOT_DIR/config/prometheus/.bearer_token"
echo -n "$LITELLM_MASTER_KEY" > "$BEARER_TOKEN_FILE"
chmod 600 "$BEARER_TOKEN_FILE"
info "Prometheus bearer token written (chmod 600)"

echo
echo "  ── Claude Code / Codex integration ───────────────────────────────────"
echo "  After starting services, get your LiteLLM key and configure clients:"
echo
# Print the retrieval command, not the key itself — avoids leaking into
# terminal scrollback or shell history. OWASP A02:2021 (Cryptographic Failures).
echo "  Get your key:  grep LITELLM_MASTER_KEY $ENV_FILE | cut -d= -f2"
echo
echo "  Then:"
echo "    export ANTHROPIC_BASE_URL=http://localhost:4000"
echo "    export ANTHROPIC_API_KEY=<key from above>"
echo
echo "  Or source the helper for your session:"
echo "    source $ROOT_DIR/config/clients/env.sh"
echo

print_next_steps
