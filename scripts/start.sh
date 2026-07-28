#!/usr/bin/env bash
# start.sh — start all ailocal Docker services
# Usage: ./scripts/start.sh [--no-wait]
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT_DIR"

# Single source of truth for how this stack is composed (deploy/litellm + deploy/searxng).
AILOCAL_ROOT="$ROOT_DIR"
. "$(dirname "$0")/lib/compose.sh"

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
  missing_models=()
  _required=()
  _tier="$(cat "$ROOT_DIR/config/active-profile" 2>/dev/null || echo 64gb)"
  while IFS= read -r _m; do _required+=("$_m"); done < <(grep -E '^\s*active:' "$ROOT_DIR/config/profiles/${_tier}.yaml" | sed 's/.*active:[[:space:]]*//')
  for model in "${_required[@]}"; do
    if ! ollama list 2>/dev/null | awk 'NR>1 {print $1}' | grep -Eq "^${model}(:.+)?$"; then
      missing_models+=("$model")
    fi
  done
  if [ ${#missing_models[@]} -gt 0 ]; then
    warn "Missing Ollama models: ${missing_models[*]}"
    echo "  Run ./scripts/install-models.sh to pull the full model set before using the stack."
  else
    info "Required Ollama models present"
  fi
fi

# ── Start services ─────────────────────────────────────────────────────────

# No `docker compose pull` here by design: start (incl. the boot LaunchAgent) must be
# reproducible and offline-safe, so it runs whatever image is on disk. main-stable is a
# moving tag — refresh deliberately via install.sh (initial) or update.sh, not on every boot.
step "Starting ailocal services"

# Was LiteLLM already up BEFORE this run? `dc up -d` is a no-op for an
# already-running container whose compose spec is unchanged — and config.yaml,
# the persona instructions and every hook are BIND-MOUNTED, so editing them
# changes no spec and triggers no restart. LiteLLM parses config.yaml once at
# boot, so the proxy then keeps serving the OLD routing while the file on disk
# says something else, with nothing in the logs to say so.
#
# Measured: after regenerating model_group_alias so claude-haiku-4-5 pointed at
# ailocal-fast, `start.sh` reported success and the proxy still routed haiku to
# ailocal-implementation. Only an explicit `docker restart` picked it up.
WAS_RUNNING=false
docker ps --format '{{.Names}}' 2>/dev/null | grep -q '^ailocal-litellm$' && WAS_RUNNING=true

dc up -d --remove-orphans

# Config fingerprint: the mounted files LiteLLM only reads at boot. If any of
# them changed since the last start, the running process is stale and must be
# restarted explicitly — `up -d` will not do it.
CONFIG_STAMP="$ROOT_DIR/data/.litellm-config.sha"
mkdir -p "$(dirname "$CONFIG_STAMP")"
# registry.yaml is in this list deliberately: it is the tool gateway's capability
# registry, it is mounted, and it is read once at gateway_init. A registry edit
# with no restart leaves the OLD tool policy in force — which is how a change to
# tool-group membership can appear to do nothing at all.
current_sha=$(cat "$ROOT_DIR/config/litellm/config.yaml" \
                  "$ROOT_DIR/config/litellm/registry.yaml" \
                  "$ROOT_DIR"/config/litellm/*.py \
                  "$ROOT_DIR"/config/instructions/*.md 2>/dev/null | shasum -a 256 | cut -d' ' -f1)
previous_sha=$(cat "$CONFIG_STAMP" 2>/dev/null || echo "")

if [ "$WAS_RUNNING" = true ] && [ -n "$previous_sha" ] && [ "$current_sha" != "$previous_sha" ]; then
  step "LiteLLM config changed since last start — restarting to load it"
  docker restart ailocal-litellm >/dev/null
  info "ailocal-litellm restarted (routing/persona/hook changes are now live)"
fi
printf '%s' "$current_sha" > "$CONFIG_STAMP"

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
      echo "  Check logs: docker logs ailocal-litellm"
      break
    fi
    printf "  Waiting... (%ds)\r" $((attempts * 3))
    sleep 3
  done
  echo ""
fi

# ── Service URLs and client setup ──────────────────────────────────────────

KEY="$(grep '^LITELLM_MASTER_KEY=' .env | cut -d= -f2-)"

step "ailocal is running"
echo ""
echo "  ┌─ Service ───────────────────────────────────────────────┐"
echo "  │  LiteLLM API     →  http://localhost:4000               │"
echo "  └─────────────────────────────────────────────────────────┘"
echo ""
echo "  One-time setup — add to ~/.zprofile to make permanent:"
echo "    source \"$ROOT_DIR/config/clients/env.sh\""
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
echo "    Uses the litellm-connector extension (key in SecretStorage):"
echo "    ./scripts/install-clients.sh vscode   # installs extension + prints setup"
echo ""
echo "  ── First-time client setup ───────────────────────────────────"
echo "    ./scripts/install-clients.sh"
echo "    (configures Codex, Claude Code, and VS Code Copilot Chat)"
echo ""
