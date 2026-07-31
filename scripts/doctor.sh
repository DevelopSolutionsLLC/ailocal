#!/usr/bin/env bash
# doctor.sh — one-command preflight and health summary for ailocal
# Usage: ailocal doctor
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT_DIR"

has()   { command -v "$1" >/dev/null 2>&1; }
info()  { echo "  ✓ $*"; }
warn()  { echo "  ⚠ $*" >&2; }
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

# Where the models actually live. Nothing verified this, and the failure is silent
# and expensive: with OLLAMA_MODELS unset the daemon quietly uses ~/.ollama, so a
# second account re-downloads tens of gigabytes and the shared store looks fine
# because it still holds the FIRST user's copy. launchctl is the authority here —
# Ollama.app is a GUI process and never reads ~/.zshrc.
MODELS_DIR="$(launchctl getenv OLLAMA_MODELS 2>/dev/null || true)"
if [ -z "$MODELS_DIR" ]; then
  warn "OLLAMA_MODELS unset in launchctl — models go to ~/.ollama, not the shared store"
  warn "  Fix: bash scripts/setup-ollama-env.sh, then restart Ollama"
elif [ ! -d "$MODELS_DIR" ]; then
  warn "OLLAMA_MODELS=$MODELS_DIR does not exist"
elif [ ! -w "$MODELS_DIR" ]; then
  warn "OLLAMA_MODELS=$MODELS_DIR is not writable by $(id -un) — pulls will fail"
else
  info "OLLAMA_MODELS=$MODELS_DIR ($(du -sh "$MODELS_DIR" 2>/dev/null | cut -f1 || echo '?'))"
fi
# Models left behind in the home directory are invisible to a daemon pointed at
# the shared store — they occupy disk while Ollama re-downloads the same tags.
if [ -d "$HOME/.ollama/models" ] && [ -n "$(ls -A "$HOME/.ollama/models" 2>/dev/null)" ] \
   && [ "$MODELS_DIR" != "$HOME/.ollama/models" ]; then
  warn "$HOME/.ollama/models still holds $(du -sh "$HOME/.ollama/models" 2>/dev/null | cut -f1) that Ollama cannot see"
  warn "  Migrate it: bash scripts/setup-ollama-env.sh"
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

  # LIVENESS IS NOT REACHABILITY, and neither is /health/readiness. Measured in
  # scripts/test-readiness-isolated.py against an isolated proxy whose upstream
  # port had nothing listening on it: /health/liveliness returns 200 in ~2ms and
  # /health/readiness returns {"status":"healthy"} in ~1ms. Both describe the
  # proxy's own process, never the backend. So a doctor built on either one
  # prints "healthy" during a total Ollama outage and sends the operator to the
  # wrong layer.
  #
  # `ollama list` above is necessary and still not sufficient: it runs on the
  # HOST. LiteLLM reaches Ollama as host.docker.internal from inside a container,
  # and that path can fail on its own (missing host-gateway mapping, a daemon
  # bound to 127.0.0.1 only, a firewall) while the host CLI keeps working.
  #
  # So probe it the way LiteLLM actually uses it — from inside the container,
  # against $OLLAMA_URL — which is exactly the discipline already applied to
  # SearXNG below.
  if docker exec ailocal-litellm python3 -c "
import json,os,sys,urllib.request
base=os.environ.get('OLLAMA_URL','http://host.docker.internal:11434').rstrip('/')
try:
    d=json.load(urllib.request.urlopen(base+'/api/tags',timeout=10))
except Exception as e:
    print(f'{type(e).__name__}: {e}',file=sys.stderr); sys.exit(1)
sys.exit(0 if isinstance(d.get('models'),list) else 1)" 2>/dev/null; then
    info "Ollama reachable FROM LiteLLM (\$OLLAMA_URL)"
  else
    error "LiteLLM cannot reach Ollama — every capability will fail at request time"
    ok=false
  fi
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
