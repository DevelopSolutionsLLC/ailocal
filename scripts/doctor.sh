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
# because it still holds the FIRST user's copy.
#
# Two valid configurations exist: the production-autostart LaunchAgent (default,
# `install.sh --yes`) bakes OLLAMA_MODELS into its own EnvironmentVariables dict
# and deliberately never calls `launchctl setenv` (setenv is session-only, lost on
# reboot — see setup-startup.sh). The env-only path (setup-ollama-env.sh) DOES use
# `launchctl setenv`. Checking setenv alone false-positives on every autostart
# install, so ask the actual running daemon process what it sees instead — correct
# under either configuration — and fall back to setenv only if no daemon is up.
OLLAMA_PID="$(lsof -ti :11434 2>/dev/null | head -1)"
MODELS_DIR=""
[ -n "$OLLAMA_PID" ] && MODELS_DIR="$(ps eww -p "$OLLAMA_PID" 2>/dev/null | tr ' ' '\n' | sed -n 's/^OLLAMA_MODELS=//p')"
[ -z "$MODELS_DIR" ] && MODELS_DIR="$(launchctl getenv OLLAMA_MODELS 2>/dev/null || true)"
if [ -z "$MODELS_DIR" ]; then
  warn "OLLAMA_MODELS unset — models go to ~/.ollama, not the shared store"
  warn "  Fix: bash scripts/setup-startup.sh (autostart) or scripts/setup-ollama-env.sh, then restart Ollama"
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
# Resolved ONCE for the whole script, fail closed. doctor previously re-read the
# marker five times and parsed the profile YAML with sed, which breaks on any
# comment or reordering -- and profiles are heavily commented.
_TIER="$("$ROOT_DIR/scripts/profile-config" active-tier)" || {
  echo "  ✗ cannot resolve the active profile — refusing to report on an assumed tier" >&2
  exit 1; }
_PROFILE_JSON="$("$ROOT_DIR/scripts/profile-config" profile-summary --tier "$_TIER")" || {
  echo "  ✗ active profile is invalid — refusing to report on an assumed tier" >&2
  exit 1; }
# jq is already a hard install dependency (install.sh preflight), so the
# summary is queried with it rather than with a bespoke extractor.
_pf() { printf '%s' "$_PROFILE_JSON" | jq -r "$1"; }
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

# ── Architecture route ──────────────────────────────────────────────────────
# The questions an operator actually has when a long session stalls. Every value
# here is MEASURED; nothing is estimated. The recurring "architecture API outage"
# was not a crash and not memory -- it was cold prompt evaluation on a large
# context exceeding the client's timeout, so these are the numbers that identify
# it while it is happening.
step "Architecture route"

ARCH_MODEL="$(_pf ".roles.architecture.model")"
if [ -n "$ARCH_MODEL" ]; then
  PS_JSON="$(curl -s --max-time 5 http://127.0.0.1:11434/api/ps 2>/dev/null || echo '{}')"
  if echo "$PS_JSON" | grep -q "$ARCH_MODEL"; then
    RESIDENT="$(echo "$PS_JSON" | python3 -c "
import json,sys
try: ms=json.load(sys.stdin).get('models',[])
except Exception: ms=[]
for m in ms:
    if m['name'].startswith('$ARCH_MODEL'.split(':')[0]):
        print(f\"{m['size']/2**30:.1f} GB resident, ctx={m.get('context_length')}\"); break" 2>/dev/null)"
    info "model loaded: $ARCH_MODEL — ${RESIDENT:-size unknown}"
  else
    warn "model NOT loaded: $ARCH_MODEL — the next request pays a cold load AND a cold prompt eval"
  fi
fi

# In-flight generation. Ollama exposes no request API, so this reads its own log:
# a slot with a `new prompt` and no later `release` is still working. That is the
# difference between "the queue is blocked" and "the backend is dead".
OLOG="$HOME/.ollama/logs/server.log"
if [ -f "$OLOG" ]; then
  # Take the most recent task mentioned on ANY line, not the last `new prompt`.
  # Matching only `new prompt` reported task 1620 as current while task 2455 was
  # actually mid-evaluation: a long prompt eval emits progress lines for minutes
  # after its own `new prompt` line has scrolled past other slots' activity.
  LASTTASK="$(grep -aoE 'task [0-9]+' "$OLOG" | tail -1 | grep -oE '[0-9]+')"
  if [ -n "$LASTTASK" ] && ! grep -aq "task $LASTTASK | stop processing" "$OLOG"; then
    AGE="$(grep -a "task $LASTTASK |" "$OLOG" | grep -oE 't = [0-9.]+ s' | tail -1 | grep -oE '[0-9.]+')"
    warn "task $LASTTASK is STILL GENERATING${AGE:+ (${AGE}s of prompt eval so far)} — a disconnected client does NOT stop it"
    echo "      it holds the KV slot; a new request queues behind it or evicts its cache"
    echo "      if abandoned:  ailocal stop && ailocal start    (otherwise let it finish)"
  else
    info "no generation left running from a disconnected client"
  fi
fi

# Parallelism and the KV consequence. num_ctx is allocated PER SLOT, so this is
# the single setting that decides both memory and cache locality.
NPAR="$(launchctl getenv OLLAMA_NUM_PARALLEL 2>/dev/null || echo '')"
ARCH_CTX="$(_pf ".roles.architecture.context")"
[ -n "$NPAR" ] && [ -n "$ARCH_CTX" ] && \
  info "OLLAMA_NUM_PARALLEL=$NPAR, architecture context=$ARCH_CTX (KV is allocated per slot)"

# Memory and swap. Swap GROWTH is the signal, not swap presence.
FREEPCT="$(memory_pressure 2>/dev/null | sed -n 's/.*free percentage: *\([0-9]*\)%.*/\1/p' | head -1)"
SWAPUSED="$(sysctl -n vm.swapusage 2>/dev/null | sed -n 's/.*used = \([0-9.]*\)M.*/\1/p')"
if [ -n "$FREEPCT" ]; then
  if [ "$FREEPCT" -lt 10 ]; then error "memory free ${FREEPCT}% — critical"; ok=false
  elif [ "$FREEPCT" -lt 25 ]; then warn "memory free ${FREEPCT}% — tight"
  else info "memory free ${FREEPCT}%"; fi
fi
[ -n "$SWAPUSED" ] && {
  if [ "${SWAPUSED%%.*}" -gt 4096 ]; then warn "swap in use ${SWAPUSED} MB — paging degrades prompt eval badly"
  else info "swap in use ${SWAPUSED} MB"; fi; }

# Client-native compaction is what keeps a session OUT of the danger zone. If the
# deployed client config disagrees with the active profile, the client compacts on
# a threshold this repository never chose -- silently, with no error anywhere.
CC_WIN="$(python3 -c "
import json,os
try: print(json.load(open(os.path.expanduser('~/.config/ailocal/claude/settings.json')))['env'].get('CLAUDE_CODE_AUTO_COMPACT_WINDOW',''))
except Exception: print('')" 2>/dev/null)"
PROF_WIN="$(_pf ".compaction.window")"
PROF_PCT="$(_pf ".compaction.pct")"
if [ -z "$CC_WIN" ]; then
  warn "claude-local has NO auto-compaction window — long sessions will grow into the stall"
  echo "      fix:  ailocal clients"
elif [ "$CC_WIN" = "$PROF_WIN" ]; then
  info "auto-compaction: ${PROF_WIN} x ${PROF_PCT}% = $((PROF_WIN * PROF_PCT / 100)) tokens (matches profile)"
else
  warn "auto-compaction window $CC_WIN disagrees with the active profile ($PROF_WIN) — run: ailocal clients"
fi

echo "      Cold prompt eval is super-linear on this route [REAL, measured]:"
echo "        ~28K -> 85 s    ~58K -> 341 s    ~88K -> 789 s (13 min)"
echo "      A cache MISS at large context is what exceeds the client timeout."
echo "      See docs/troubleshooting.md, 'architecture route stalls'."

if [ "$ok" = true ]; then
  echo
  echo "▶ DOCTOR: OK — ailocal looks healthy"
  exit 0
fi

echo
echo "▶ DOCTOR: FAILED — see the issues above" >&2
exit 2
