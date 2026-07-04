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
MAX_LOADED="${OLLAMA_MAX_LOADED_MODELS:-2}" # allow 2 models resident at once (e.g. coder + supervisor)

info() { echo "  ✓ $*"; }
step() { echo; echo "▶ $*"; }

step "Setting Ollama env vars for the current login session (launchctl)"
launchctl setenv OLLAMA_KEEP_ALIVE "$KEEP_ALIVE"
launchctl setenv OLLAMA_MAX_LOADED_MODELS "$MAX_LOADED"
info "OLLAMA_KEEP_ALIVE=$KEEP_ALIVE"
info "OLLAMA_MAX_LOADED_MODELS=$MAX_LOADED"

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
    <string>launchctl setenv OLLAMA_KEEP_ALIVE $KEEP_ALIVE; launchctl setenv OLLAMA_MAX_LOADED_MODELS $MAX_LOADED</string>
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
