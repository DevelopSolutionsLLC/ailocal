#!/usr/bin/env bash
# setup-ollama-env.sh — make Ollama's runtime env vars actually reach the server.
#
# WHY: The Ollama macOS app is a GUI/launchd process. It does NOT read ~/.zshrc
# or ~/.zprofile — those only apply to interactive terminal shells. So setting
# OLLAMA_KEEP_ALIVE / OLLAMA_MAX_LOADED_MODELS in your shell rc has no effect on
# the server; it keeps using defaults (5-minute keep-alive). The GUI app reads
# its environment from launchctl, so we set them there and persist via a
# LaunchAgent that re-applies them at every login.
#
# After running this, QUIT Ollama fully (menubar → Quit) and reopen it — the
# server only reads these variables at startup.
set -euo pipefail

# Desired values (edit here if you want different behavior).
KEEP_ALIVE="${OLLAMA_KEEP_ALIVE:-24h}"    # keep models resident for 24 hours of idle
MAX_LOADED="${OLLAMA_MAX_LOADED_MODELS:-3}" # 3 = e.g. coder-fast (~2 GB) + one big coder + one reasoner can co-reside. MAX_LOADED caps COUNT, not size — Ollama refuses a model that won't fit, so two big models still never thrash. Bounded by the retuned num_ctx (64K coders / 32K supervisor) so KV stays small enough for 3-way residency on 64 GB.
NUM_PARALLEL="${OLLAMA_NUM_PARALLEL:-2}"  # concurrent requests per model (GLOBAL — Ollama has no per-model setting). 2 balances snappy multi-request against KV growth (KV = num_ctx x NUM_PARALLEL per loaded model).
FLASH_ATTN="${OLLAMA_FLASH_ATTENTION:-1}" # faster attention + lower memory, no quality loss
KV_CACHE="${OLLAMA_KV_CACHE_TYPE:-q8_0}" # quantize KV cache to 8-bit, halves memory at large contexts
MODELS_DIR="${OLLAMA_MODELS:-/Users/Shared/ollama/models}" # store models outside any one user's home

info() { echo "  ✓ $*"; }
step() { echo; echo "▶ $*"; }

mkdir -p "$MODELS_DIR"

step "Setting Ollama env vars for the current login session (launchctl)"
launchctl setenv OLLAMA_KEEP_ALIVE "$KEEP_ALIVE"
launchctl setenv OLLAMA_MAX_LOADED_MODELS "$MAX_LOADED"
launchctl setenv OLLAMA_NUM_PARALLEL "$NUM_PARALLEL"
launchctl setenv OLLAMA_FLASH_ATTENTION "$FLASH_ATTN"
launchctl setenv OLLAMA_KV_CACHE_TYPE "$KV_CACHE"
launchctl setenv OLLAMA_MODELS "$MODELS_DIR"
info "OLLAMA_KEEP_ALIVE=$KEEP_ALIVE"
info "OLLAMA_MAX_LOADED_MODELS=$MAX_LOADED"
info "OLLAMA_NUM_PARALLEL=$NUM_PARALLEL"
info "OLLAMA_FLASH_ATTENTION=$FLASH_ATTN"
info "OLLAMA_KV_CACHE_TYPE=$KV_CACHE"
info "OLLAMA_MODELS=$MODELS_DIR"

step "Installing a LaunchAgent so these persist across reboots/logins"
PLIST="$HOME/Library/LaunchAgents/com.ailocal.ollama-env.plist"
mkdir -p "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.ailocal.ollama-env</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/sh</string>
    <string>-c</string>
    <string>launchctl setenv OLLAMA_KEEP_ALIVE $KEEP_ALIVE; launchctl setenv OLLAMA_MAX_LOADED_MODELS $MAX_LOADED; launchctl setenv OLLAMA_NUM_PARALLEL $NUM_PARALLEL; launchctl setenv OLLAMA_FLASH_ATTENTION $FLASH_ATTN; launchctl setenv OLLAMA_KV_CACHE_TYPE $KV_CACHE; launchctl setenv OLLAMA_MODELS $MODELS_DIR</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
</dict>
</plist>
PLISTEOF
# Reload (bootout is fine to fail if not loaded yet).
launchctl bootout "gui/$(id -u)/com.ailocal.ollama-env" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST" 2>/dev/null || launchctl load "$PLIST" 2>/dev/null || true
info "LaunchAgent installed: $PLIST"

echo
echo "  ▶ Now QUIT Ollama (menubar icon → Quit Ollama) and reopen it."
echo "    Then verify:  ollama ps   (the UNTIL column should read hours, not minutes)"
echo "    Or:  launchctl getenv OLLAMA_KEEP_ALIVE"
