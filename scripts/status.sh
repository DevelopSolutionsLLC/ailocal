#!/usr/bin/env bash
# status.sh — live model status by capability.
#
# Reads config/capabilities.generated.json (the declarative registry, generated from
# config/models.yaml by sync-models.py) and Ollama's /api/ps, and shows what is ACTUALLY
# loaded right now, with keep-alive remaining. Real model names are shown, never hidden.
#
#   ./scripts/status.sh          UNIFIED DASHBOARD (ailocal status)
#   ./scripts/status.sh --models verbose per-capability view (the previous default)
#   ./scripts/status.sh --table  compact capability table    (ailocal models)
#
# Only the DEFAULT changed. --table is byte-identical to before, so
# `ailocal models` and any existing muscle memory keep working. The default is
# now the whole stack because "is my local AI working?" previously required four
# separate commands.
#
# Every line in the dashboard is a LIVE probe, never a config read. A config
# saying something is enabled proves nothing about whether it works.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
AILOCAL_STATE="${AILOCAL_STATE:-$("$ROOT_DIR/scripts/profile-config" state-root)}"
CAPS="$ROOT_DIR/config/capabilities.generated.json"
OLLAMA_URL="${OLLAMA_HOST:-http://127.0.0.1:11434}"
MODE="dashboard"
case "${1:-}" in
  --table)  MODE="table" ;;
  --models) MODE="verbose" ;;
  "")       MODE="dashboard" ;;
  *) echo "usage: status.sh [--models|--table]" >&2; exit 1 ;;
esac

if [ "$MODE" = "dashboard" ]; then
  C_OK=$'\033[32m'; C_BAD=$'\033[31m'; C_WARN=$'\033[33m'; C_DIM=$'\033[2m'; C_0=$'\033[0m'
  ok()   { printf '  %s✓%s %s\n' "$C_OK" "$C_0" "$*"; }
  bad()  { printf '  %s✗%s %s\n' "$C_BAD" "$C_0" "$*"; }
  warn() { printf '  %s⚠%s %s\n' "$C_WARN" "$C_0" "$*"; }
  dim()  { printf '  %s—%s %s\n' "$C_DIM" "$C_0" "$*"; }
  hdr()  { printf '\n\033[1m%s\033[0m\n' "$*"; }
  PROXY="${AILOCAL_PROXY:-http://127.0.0.1:4000}"
  TRACES="$AILOCAL_STATE/captures/traces"

  echo "══════════════════════════════════════════════════════════════════════"
  echo " AILOCAL STATUS   $(date '+%Y-%m-%d %H:%M:%S')"
  echo "══════════════════════════════════════════════════════════════════════"

  hdr "Services"
  curl -fsS -m 3 "$OLLAMA_URL/api/tags" >/dev/null 2>&1 \
    && ok "Ollama        $OLLAMA_URL" || bad "Ollama        unreachable"
  if docker ps --format '{{.Names}}' 2>/dev/null | grep -qx ailocal-litellm; then
    H=$(docker inspect ailocal-litellm --format '{{.State.Health.Status}}' 2>/dev/null)
    [ "$H" = healthy ] && ok "LiteLLM       healthy" || warn "LiteLLM       $H"
  else bad "LiteLLM       not running"; fi
  curl -fsS -m 3 "$PROXY/health/liveliness" >/dev/null 2>&1 \
    && ok "Proxy         $PROXY" || bad "Proxy         not responding"
  docker ps --format '{{.Names}}' 2>/dev/null | grep -qx ailocal-searxng \
    && ok "SearXNG       running" || dim "SearXNG       not running"

  hdr "Gateway"
  GW=$(docker exec ailocal-litellm printenv AILOCAL_TOOL_GATEWAY 2>/dev/null || echo "?")
  TN=$(docker exec ailocal-litellm printenv AILOCAL_TASK_NEGOTIATION 2>/dev/null || echo off)
  TR=$(docker exec ailocal-litellm printenv AILOCAL_TRACE_DIR 2>/dev/null || echo "")
  case "$GW" in
    filter) ok "mode          filter — tools removed before the model" ;;
    report) warn "mode          report — measuring only, nothing removed" ;;
    off)    warn "mode          OFF — payloads not reduced. Measured: this is the"
            echo "                  difference between ~90s and <1s to first byte." ;;
    *)      bad "mode          unknown ($GW)" ;;
  esac
  [ "$TN" = on ] && ok "task negot.   on" || dim "task negot.   off"
  [ -n "$TR" ] && ok "tracing       $TR" || dim "tracing       off"
  docker logs --since 2h ailocal-litellm 2>/dev/null \
    | python3 "$ROOT_DIR/scripts/lib/status_gateway.py" 2>/dev/null

  hdr "Clients"
  [ -f "$HOME/.config/ailocal/claude/.claude.json" ] \
    && ok "Claude Code   configured, isolated from ~/.claude" \
    || bad "Claude Code   not installed"
  [ -f "$HOME/.config/ailocal/codex/config.toml" ] \
    && ok "Codex CLI     configured  (MCP registered, NOT reachable: docs/troubleshooting.md)" \
    || bad "Codex CLI     not installed"
  if command -v code >/dev/null 2>&1 && code --list-extensions 2>/dev/null \
       | grep -qix Gethnet.litellm-connector-copilot; then
    ok "VS Code       connector installed  (chat turn unverified — needs GUI)"
  else dim "VS Code       connector not installed"; fi

  hdr "Recent requests"
  if [ -d "$TRACES" ]; then
    python3 "$ROOT_DIR/scripts/lib/status_traces.py" "$TRACES"
  else
    dim "tracing off — set AILOCAL_TRACE_DIR to diagnose intermittent failures"
  fi

  hdr "Models"
fi

[ -f "$CAPS" ] || { echo "capabilities.generated.json missing — run ailocal sync" >&2; exit 1; }

PS_JSON="$(curl -fsS -m 3 "$OLLAMA_URL/api/ps" 2>/dev/null || echo '{"models":[]}')"

CAPS_FILE="$CAPS" PS_DATA="$PS_JSON" MODE="$MODE" python3 - <<'PY'
import json, os, re
from datetime import datetime, timezone

caps = json.load(open(os.environ["CAPS_FILE"]))["capabilities"]
ps   = json.loads(os.environ["PS_DATA"]).get("models", [])
loaded = {m.get("name", ""): m for m in ps}
now = datetime.now(timezone.utc)

def base(tag): return tag.split(":", 1)[0]

def find(backend):
    if backend in loaded: return loaded[backend]
    if backend + ":latest" in loaded: return loaded[backend + ":latest"]
    for name, m in loaded.items():
        if base(name) == base(backend): return m
    return None

def parse_exp(s):
    # Ollama gives RFC3339 with up to 9 fractional digits + offset; trim to 6 for fromisoformat.
    if not s: return None
    s = re.sub(r"(\.\d{6})\d+", r"\1", s)
    try: return datetime.fromisoformat(s)
    except ValueError: return None

def human(delta):
    secs = int(delta.total_seconds())
    if secs <= 0: return "expiring"
    h, m = secs // 3600, (secs % 3600) // 60
    if h and m: return f"{h}h {m}m remaining"
    if h:       return f"{h}h remaining"
    return f"{m}m remaining"

C = lambda s, c: f"\033[{c}m{s}\033[0m"

def state(c):
    """(label, color) — persistent | loaded | idle."""
    m = find(c["backend"])
    if not m:
        return "idle", "33"
    exp = parse_exp(m.get("expires_at"))
    if c.get("persistent") or (exp and (exp.year - now.year) > 5):
        return "persistent", "35"
    return "loaded", "32"

if os.environ.get("MODE") == "table":
    w_cap = max(len("Capability"), *(len(c["name"]) for c in caps))
    w_bk  = max(len("Backend"),    *(len(c["backend"]) for c in caps))
    print(C(f"{'Capability':<{w_cap}}  {'Backend':<{w_bk}}  Status", "1"))
    for c in caps:
        label, color = state(c)
        print(f"{c['name']:<{w_cap}}  {c['backend']:<{w_bk}}  {C(label, color)}")
else:
    print(C("AILOCAL MODEL STATUS", "1"))
    print("─" * 44)
    for c in caps:
        print(C(c["role"], "1;36"))
        print(f"  Model:      {c['backend']}")
        m = find(c["backend"])
        if not m:
            print(f"  Loaded:     {C('No', '33')}")
        else:
            print(f"  Loaded:     {C('Yes', '32')}")
            exp = parse_exp(m.get("expires_at"))
            if c.get("persistent") or (exp and (exp.year - now.year) > 5):
                print(f"  Keep Alive: {C('Persistent', '35')}")
            elif exp:
                print(f"  Keep Alive: {human(exp - now)}")
            print(f"  Context:    {c['context']}")
        print()
PY
