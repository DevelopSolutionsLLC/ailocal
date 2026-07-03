#!/usr/bin/env bash
# doctor.sh — one-command preflight and health summary for ailocal
# Usage: ./scripts/doctor.sh
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT_DIR"

has()   { command -v "$1" >/dev/null 2>&1; }
info()  { echo "  ✓ $*"; }
warn()  { echo "  ⚠ $*"; }
error() { echo "  ✗ $*" >&2; }
step()  { echo; echo "▶ $*"; }

ok=true

check_http() {
  local name="$1"
  local url="$2"
  local timeout="${3:-5}"
  if curl -sSf --max-time "$timeout" "$url" >/dev/null 2>&1; then
    info "$name reachable ($url)"
  else
    echo "  ✗ $name not reachable ($url)" >&2
    ok=false
  fi
}

step "Pre-flight checks"

if [ ! -f ".env" ]; then
  error ".env not found. Run ./scripts/install.sh first."
  ok=false
else
  info ".env present"
fi

if ! has docker; then
  error "Docker CLI not found"
  ok=false
else
  info "Docker CLI present"
fi

if docker ps >/dev/null 2>&1; then
  info "Docker daemon responding"
else
  error "Docker daemon is not running"
  ok=false
fi

if ! has ollama; then
  error "Ollama CLI not found"
  ok=false
else
  info "Ollama CLI present"
fi

if has ollama && ollama list >/dev/null 2>&1; then
  info "Ollama daemon responding"
else
  error "Ollama daemon is not responding"
  ok=false
fi

if has docker && docker compose version >/dev/null 2>&1; then
  info "docker compose available"
else
  error "docker compose is unavailable"
  ok=false
fi

# Derive required models from the model manifest — single source of truth.
required_models=($(grep '^\s*backend:' "$ROOT_DIR/config/models.yaml" | sed 's/.*backend:[[:space:]]*//'))
if has ollama && ollama list >/dev/null 2>&1; then
  installed_models=$(ollama list 2>/dev/null | awk 'NR>1 {print $1}')
  missing_models=()
  for model in "${required_models[@]}"; do
    if ! echo "$installed_models" | grep -Eq "^${model}(:.+)?$"; then
      missing_models+=("$model")
    fi
  done
  if [ ${#missing_models[@]} -gt 0 ]; then
    warn "Missing Ollama models: ${missing_models[*]}"
    ok=false
  else
    info "Required Ollama models present"
  fi
fi

step "Compose status"
if docker ps --format '{{.Names}}' | grep -q '^ailocal_litellm$'; then
  info "LiteLLM container is running"
else
  warn "LiteLLM container is not running"
fi

step "Service endpoints"
if docker ps --format '{{.Names}}' | grep -q '^ailocal_litellm$'; then
  check_http "LiteLLM" "http://localhost:4000/health/liveliness" 5
else
  echo "  — LiteLLM endpoint skipped (container not running)"
fi

if [ "$ok" = true ]; then
  echo
  echo "▶ DOCTOR: OK — ailocal looks healthy"
  exit 0
fi

echo
echo "▶ DOCTOR: FAILED — see the issues above" >&2
exit 2
