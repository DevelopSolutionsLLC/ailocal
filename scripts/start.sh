#!/usr/bin/env bash
# start.sh — start all ailocal Docker services
# Usage: ./scripts/start.sh [--no-wait]
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT_DIR"

NO_WAIT=false
[[ "${1:-}" == "--no-wait" ]] && NO_WAIT=true

# ── Helpers ────────────────────────────────────────────────────────────────

has()   { command -v "$1" >/dev/null 2>&1; }
info()  { echo "  ✓ $*"; }
warn()  { echo "  ⚠ $*"; }
error() { echo "  ✗ $*" >&2; }
step()  { echo; echo "▶ $*"; }

# ── Pre-flight checks ──────────────────────────────────────────────────────

step "Pre-flight checks"

if [ ! -f ".env" ]; then
  error ".env not found. Run ./scripts/install.sh first."
  exit 1
fi
info ".env present"

if ! docker ps >/dev/null 2>&1; then
  error "Docker daemon is not running. Start Docker Desktop and retry."
  exit 1
fi
info "Docker daemon running"

if ! has ollama; then
  warn "Ollama CLI not found. Install it from https://ollama.ai"
elif ! ollama list >/dev/null 2>&1; then
  warn "Ollama is not running."
  echo "  Start it with: ollama serve   (or open /Applications/Ollama.app)"
  echo "  LiteLLM will start but model requests will fail until Ollama is up."
else
  info "Ollama daemon responding"
fi

# ── Start services ─────────────────────────────────────────────────────────

step "Starting ailocal services"
DOCKER_CLI_HINTS=false docker compose up -d --remove-orphans

if [ "$NO_WAIT" = true ]; then
  info "Services launched (skipping health wait)"
else
  # Wait for LiteLLM process liveness — /health/liveliness returns 200 as soon as
  # the proxy is accepting requests, regardless of whether Ollama is reachable.
  # (The full /health endpoint blocks until all models respond, which fails when
  # Ollama isn't running. Don't use it here.)
  step "Waiting for LiteLLM to become ready"
  attempts=0
  max_attempts=30
  until curl -sSf --max-time 3 http://localhost:4000/health/liveliness >/dev/null 2>&1; do
    attempts=$((attempts + 1))
    if [ $attempts -ge $max_attempts ]; then
      warn "LiteLLM did not become ready after $((max_attempts * 3))s"
      echo "  Check logs: docker logs ailocal_litellm"
      break
    fi
    printf "  Waiting... (%ds)\r" $((attempts * 3))
    sleep 3
  done
  echo ""
fi

# ── Service URLs and client setup ──────────────────────────────────────────

KEY="\$(grep LITELLM_MASTER_KEY '$ROOT_DIR/.env' | cut -d= -f2)"

step "ailocal is running"
echo ""
echo "  ┌─ Services ──────────────────────────────────────────────┐"
echo "  │  Open WebUI      →  http://localhost:8081               │"
echo "  │  LiteLLM API     →  http://localhost:4000               │"
echo "  │  Grafana         →  http://localhost:3000               │"
echo "  └─────────────────────────────────────────────────────────┘"
echo ""
echo "  One-time setup — add to ~/.zprofile to make permanent:"
echo "    source $ROOT_DIR/config/clients/env.sh"
echo ""
echo "  ── Claude Code ───────────────────────────────────────────"
echo "    export ANTHROPIC_BASE_URL=http://localhost:4000"
echo "    export ANTHROPIC_API_KEY=$KEY"
echo "    claude"
echo ""
echo "  ── Codex ─────────────────────────────────────────────────"
echo "    export OPENAI_BASE_URL=http://localhost:4000/v1"
echo "    export OPENAI_API_KEY=$KEY"
echo "    codex"
echo ""
echo "  ── VS Code ───────────────────────────────────────────────"
echo "    source $ROOT_DIR/config/clients/env.sh && code ."
echo "    (Continue + Cline pick up env vars automatically)"
echo ""
echo "  ── Permanent config files ────────────────────────────────"
echo "    Claude Code:  cp config/clients/claude-code.json ~/.claude/settings.json"
echo "    Codex:        cp config/clients/codex-config.yaml ~/.codex/config.yaml"
echo "    Continue:     cp config/clients/vscode-continue.json ~/.continue/config.json"
echo ""
