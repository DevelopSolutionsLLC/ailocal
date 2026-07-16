#!/usr/bin/env bash
# preload-model.sh — warm an Ollama model at login so the first request is fast.
#
# WHY: Ollama loads a model into memory lazily on the first request (a 20-38 GB
# read = 4-40s cold). This installs a LaunchAgent that, at each login, waits for
# the Ollama server to be up and then sends one tiny request to load the model.
# With OLLAMA_KEEP_ALIVE set (see setup-ollama-env.sh) it then stays resident.
#
# Usage:
#   ./scripts/preload-model.sh              # preload the default backend (coder)
#   ./scripts/preload-model.sh reasoner     # preload a different role's backend
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

# Resolve a role name (or backend tag) to a backend model tag via models.yaml.
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
  step "Warming $model (waiting for Ollama at $OLLAMA_URL)"
  for _ in $(seq 1 60); do
    curl -fsS "$OLLAMA_URL/api/version" >/dev/null 2>&1 && break
    sleep 2
  done
  # keep_alive -1 = keep resident until explicitly stopped or Ollama restarts.
  curl -fsS "$OLLAMA_URL/api/generate" \
    -d "{\"model\":\"$model\",\"prompt\":\"ok\",\"stream\":false,\"keep_alive\":\"24h\"}" \
    >/dev/null 2>&1 && info "$model loaded and resident" \
    || echo "  ⚠ could not warm $model — is it pulled? (ollama pull $model)"
}

case "${1:-}" in
  --uninstall)
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
    rm -f "$PLIST" && info "Removed preload LaunchAgent"
    exit 0 ;;
  --now)
    BACKEND="$(resolve_backend "${2:-coder}")"
    [ -n "$BACKEND" ] || { echo "  ✗ could not resolve model for '${2:-coder}'"; exit 1; }
    warm "$BACKEND"; exit 0 ;;
esac

ROLE="${1:-coder}"
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
