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
_TIER="$(cat "$ROOT_DIR/config/active-profile" 2>/dev/null || echo 64gb)"
  _PROFILE="$ROOT_DIR/config/profiles/${_TIER}.yaml"
  required_models=($(grep -E '^\s*active:' "$_PROFILE" | sed 's/.*active:[[:space:]]*//'))
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
if docker ps --format '{{.Names}}' | grep -q '^ailocal-litellm$'; then
  health=$(docker inspect --format='{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' ailocal-litellm 2>/dev/null)
  case "$health" in
    healthy|none) info "LiteLLM container running [$health]" ;;
    starting)     warn "LiteLLM container still starting" ;;
    *)            error "LiteLLM container unhealthy [$health]"; ok=false ;;
  esac
else
  error "LiteLLM container is not running"
  ok=false
fi

if docker ps --format '{{.Names}}' | grep -q '^ailocal-searxng$'; then
  health=$(docker inspect --format='{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' ailocal-searxng 2>/dev/null)
  case "$health" in
    healthy|none) info "SearXNG container running [$health]" ;;
    starting)     warn "SearXNG container still starting" ;;
    *)            error "SearXNG container unhealthy [$health]"; ok=false ;;
  esac
else
  # Degraded, not fatal: models still work, only WebSearch stops.
  warn "SearXNG container is not running — local web search unavailable"
fi

# Crash-loop detection (folded in from the old healthcheck.sh).
if docker ps --filter status=restarting --format '{{.Names}}' | grep -q .; then
  error "A container is restart-looping — check: docker logs ailocal-litellm"
  ok=false
fi

step "Service endpoints"
if docker ps --format '{{.Names}}' | grep -q '^ailocal-litellm$'; then
  check_http "LiteLLM" "http://localhost:4000/health/liveliness" 5
else
  echo "  — LiteLLM endpoint skipped (container not running)"
fi

# Probe SearXNG the way LiteLLM actually uses it: by service name, over
# ailocal_net, asking for JSON. A reachable UI proves nothing if json is not in
# settings.yml search.formats.
if docker ps --format '{{.Names}}' | grep -q '^ailocal-searxng$'; then
  if docker exec ailocal-litellm python3 -c "
import urllib.request,json,sys
d=json.load(urllib.request.urlopen('http://searxng:8080/search?q=test&format=json',timeout=20))
sys.exit(0 if d.get('results') else 1)" 2>/dev/null; then
    info "SearXNG JSON API reachable from LiteLLM (http://searxng:8080)"
  else
    warn "LiteLLM cannot get JSON results from SearXNG — WebSearch will fail"
  fi
fi

if [ "$ok" = true ]; then
  echo
  echo "▶ DOCTOR: OK — ailocal looks healthy"
  exit 0
fi

echo
echo "▶ DOCTOR: FAILED — see the issues above" >&2
exit 2
