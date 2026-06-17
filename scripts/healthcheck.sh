#!/usr/bin/env bash
# healthcheck.sh — verify Ollama, Docker services, and HTTP endpoints
# Usage: ./scripts/healthcheck.sh
#
# Exits 0 if all critical checks pass, 2 if any critical check fails.
# Callers (update.sh) rely on the non-zero exit code.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=../.env
source "$ROOT_DIR/.env" 2>/dev/null || true

# ── Helpers ────────────────────────────────────────────────────────────────

has()   { command -v "$1" >/dev/null 2>&1; }
info()  { echo "  ✓ $*"; }
warn()  { echo "  ⚠ $*"; }
error() { echo "  ✗ $*" >&2; }
step()  { echo; echo "▶ $*"; }

ok=true

# ── Domain-specific helpers ────────────────────────────────────────────────

check_container() {
  local name="$1"
  local tier="$2"   # critical | optional

  if docker ps --format "{{.Names}}" | grep -q "^${name}$"; then
    local health
    health=$(docker inspect --format='{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$name" 2>/dev/null)
    case "$health" in
      healthy)  info "$name  [healthy]" ;;
      none)     info "$name  [running, no healthcheck]" ;;
      starting) warn "$name  [starting — still initialising]" ;;
      *)        echo "  ✗ $name  [$health]" >&2
                [ "$tier" = "critical" ] && ok=false ;;
    esac
  else
    if docker ps -a --format "{{.Names}}" | grep -q "^${name}$"; then
      echo "  ✗ $name  [stopped/crashed]" >&2
    else
      echo "  ✗ $name  [not created]" >&2
    fi
    [ "$tier" = "critical" ] && ok=false
  fi
}

check_service() {
  local name="$1"
  local url="$2"
  local timeout="${3:-5}"
  if curl -sSf --max-time "$timeout" "$url" >/dev/null 2>&1; then
    info "$name  ($url)"
  else
    echo "  ✗ $name  ($url) — not reachable" >&2
    ok=false
  fi
}

# ── Ollama ─────────────────────────────────────────────────────────────────

step "Ollama (host native)"
if has ollama; then
  if ollama list >/dev/null 2>&1; then
    info "Ollama daemon responding"
    echo "  Loaded models:"
    ollama list | tail -n +2 | awk '{print "    •", $1}' || true
  else
    echo "  ✗ Ollama daemon not responding — run: ollama serve" >&2
    ok=false
  fi
else
  echo "  ✗ Ollama CLI not installed" >&2
  ok=false
fi

# ── Docker containers ──────────────────────────────────────────────────────

step "Critical containers"
check_container "ailocal_postgres"  "critical"
check_container "ailocal_redis"     "critical"
check_container "ailocal_litellm"   "critical"
check_container "ailocal_openwebui" "critical"
check_container "ailocal_caddy"     "critical"

step "Optional containers"
check_container "ailocal_prometheus"  "optional"
check_container "ailocal_grafana"     "optional"

# Catch containers stuck in restart loop (real problem, not just stopped)
restarting=$(docker ps --filter "status=restarting" --format "{{.Names}}" | head -10)
if [ -n "$restarting" ]; then
  echo ""
  warn "RESTART LOOP detected — these containers are crash-looping:"
  echo "$restarting" | awk '{print "    •", $0}'
  echo "  Run: docker logs <container_name>  to see the error."
  ok=false
fi

# ── HTTP endpoints ─────────────────────────────────────────────────────────

step "HTTP endpoints"
check_service "LiteLLM API"  "http://localhost:4000/health/liveliness"  10
check_service "Open WebUI"   "http://localhost:8081/"                    10
check_service "Prometheus"   "http://localhost:9090/-/ready"              5
check_service "Grafana"      "http://localhost:3000/api/health"           5

# ── Summary ────────────────────────────────────────────────────────────────

echo ""
if [ "$ok" = true ]; then
  echo "▶ HEALTHCHECK: OK — all critical services operational"
  exit 0
else
  echo "▶ HEALTHCHECK: FAILED — one or more critical checks failed" >&2
  echo "  Run 'docker logs <container>' to investigate." >&2
  exit 2
fi
