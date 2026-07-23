#!/usr/bin/env bash
# preload-model.sh — warm an Ollama model at login so the first request is fast.
#
# WHY: Ollama loads a model into memory lazily on the first request (a 20-38 GB
# read = 4-40s cold). This installs a LaunchAgent that, at each login, waits for
# the Ollama server to be up and then sends one tiny request to load the model.
# With OLLAMA_KEEP_ALIVE set (see setup-ollama-env.sh) it then stays resident.
#
# Usage:
#   ./scripts/preload-model.sh              # preload the default backend (coder-main)
#   ./scripts/preload-model.sh deep-think    # preload a different role's backend
#   ./scripts/preload-model.sh --now        # just warm now, don't install the agent
#   ./scripts/preload-model.sh --uninstall  # remove the LaunchAgent
#
# Note: this warms the raw Ollama BACKEND model (from config/models.yaml), not a
# LiteLLM role alias — preloading talks to Ollama directly (11434), not the proxy.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
MODELS_YAML="$ROOT_DIR/config/models.yaml"
OLLAMA_URL="${OLLAMA_HOST:-http://localhost:11434}"
LABEL="com.ailocal.ollama-preload"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

info() { echo "  ✓ $*"; }
step() { echo; echo "▶ $*"; }

# Resolve a role name (or backend tag) to the raw Ollama backend tag via
# models.yaml. LiteLLM serves the base model directly (personas are injected by
# the LiteLLM hook, not baked into an overlay), so the base tag is what runs.
resolve_backend() {
  local want="$1"
  # If it already looks like a tag (has a colon), use as-is.
  case "$want" in *:*) echo "$want"; return;; esac
  awk -v role="$want" '
    $0 ~ "^"role":" { inrole=1; next }
    inrole && /^[a-z]/ && $0 !~ /^ / { inrole=0 }
    inrole && $1=="backend:" { print $2; exit }
  ' "$MODELS_YAML"
}

warm() {
  local model="$1"
  # Health-gate: wait (bounded) for the Ollama API before touching it. Fails
  # gracefully if Ollama never comes up — no hang, no crash loop.
  step "Warming $model (waiting for Ollama at $OLLAMA_URL)"
  local up=0 i
  for i in $(seq 1 60); do
    if curl -fsS -m 3 "$OLLAMA_URL/api/version" >/dev/null 2>&1; then up=1; break; fi
    sleep 2
  done
  [ "$up" = 1 ] || { echo "  ⚠ Ollama not reachable after 120s — skipping preload"; return 1; }
  # Skip if already resident (avoid a redundant reload).
  if curl -fsS -m 5 "$OLLAMA_URL/api/ps" 2>/dev/null | grep -q "\"$model\""; then
    info "$model already resident — nothing to do"; return 0
  fi
  # Preload via EMPTY prompt: loads weights into memory WITHOUT running inference
  # (the documented warm-up call). keep_alive:-1 pins it until Ollama restarts.
  curl -fsS -m 300 "$OLLAMA_URL/api/generate" \
    -d "{\"model\":\"$model\",\"keep_alive\":-1}" \
    >/dev/null 2>&1 && info "$model loaded and pinned (keep_alive=-1)" \
    || echo "  ⚠ could not warm $model — is it pulled? (ollama pull $model)"
}

case "${1:-}" in
  --uninstall)
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
    rm -f "$PLIST" && info "Removed preload LaunchAgent"
    exit 0 ;;
  --now)
    BACKEND="$(resolve_backend "${2:-coder-main}")"
    [ -n "$BACKEND" ] || { echo "  ✗ could not resolve model for '${2:-coder-main}'"; exit 1; }
    warm "$BACKEND"; exit 0 ;;
esac

ROLE="${1:-coder-main}"
BACKEND="$(resolve_backend "$ROLE")"
[ -n "$BACKEND" ] || { echo "  ✗ could not resolve backend for role '$ROLE' in models.yaml"; exit 1; }

# Warm immediately, then install the login agent.
warm "$BACKEND"

step "Installing login preload LaunchAgent ($LABEL)"
mkdir -p "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$ROOT_DIR/scripts/preload-model.sh</string>
    <string>--now</string>
    <string>$ROLE</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
</dict>
</plist>
PLISTEOF
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST" 2>/dev/null || launchctl load "$PLIST" 2>/dev/null || true
info "Installed: $PLIST (preloads '$ROLE' → $BACKEND at every login)"
echo
echo "  Change model:   ./scripts/preload-model.sh <role>"
echo "  Remove:         ./scripts/preload-model.sh --uninstall"
