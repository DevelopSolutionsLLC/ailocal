#!/usr/bin/env bash
# setup-startup.sh — production login startup for the ailocal stack via launchd.
#
# Goal: after login, the stack is ready with zero manual steps —
#   1. ollama serve starts (LaunchAgent, env baked in, auto-restart, logs)
#   2. the primary model is preloaded once Ollama is healthy
#   3. LiteLLM starts once Ollama is healthy (native, optional; else use Docker)
#
# WHY a LaunchAgent instead of the Ollama.app + `launchctl setenv`:
#   - launchctl setenv is runtime-only and LOST on reboot; a plist EnvironmentVariables
#     dict is durable and race-free (env is set for the process, not globally-later).
#   - KeepAlive restarts ollama if it crashes; StandardOutPath gives real logs.
#   - No dependency on the GUI app launching first.
#
# launchd has NO native ordering, so ordering is done with health-probe gating:
# the preload and litellm agents WAIT for Ollama's API before acting.
#
# Usage:
#   ./scripts/setup-startup.sh [--model ROLE] [--with-litellm] [--uninstall]
#
# IMPORTANT: if you run ollama via this LaunchAgent, DISABLE the Ollama.app
# "launch at login" (Ollama menubar → Settings) or quit the app — otherwise two
# servers fight over port 11434.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
LA_DIR="$HOME/Library/LaunchAgents"
LOG_DIR="$HOME/Library/Logs/ailocal"
# Agent-run scripts must live OUTSIDE TCC-protected folders (~/Documents, ~/Desktop,
# ~/Downloads): launchd gets "Operation not permitted" executing scripts there on
# modern macOS. Install self-contained wrappers here instead (and bake in any values
# from the repo at install time, since the repo itself may be under ~/Documents).
APP_SUPPORT="$HOME/Library/Application Support/ailocal"
OLLAMA_BIN="/Applications/Ollama.app/Contents/Resources/ollama"
MODEL_ROLE="coder-main"
WITH_LITELLM=0
UNINSTALL=0

info() { echo "  ✓ $*"; }
warn() { echo "  ⚠ $*"; }
step() { echo; echo "▶ $*"; }

# Resolve a role name to its Ollama backend tag from models.yaml (at install time,
# from the interactive shell — the agent can't read ~/Documents at runtime).
resolve_backend() {
  awk -v role="$1" '
    $0 ~ "^"role":" { inrole=1; next }
    inrole && /^[a-z]/ && $0 !~ /^ / { inrole=0 }
    inrole && $1=="backend:" { print $2; exit }
  ' "$ROOT_DIR/config/models.yaml" 2>/dev/null
}

while [ $# -gt 0 ]; do
  case "$1" in
    --model) MODEL_ROLE="$2"; shift 2;;
    --with-litellm) WITH_LITELLM=1; shift;;
    --uninstall) UNINSTALL=1; shift;;
    *) echo "unknown arg: $1"; exit 1;;
  esac
done

load()   { launchctl bootout "gui/$(id -u)/$1" 2>/dev/null || true;
           launchctl bootstrap "gui/$(id -u)" "$LA_DIR/$1.plist" 2>/dev/null \
             || launchctl load "$LA_DIR/$1.plist" 2>/dev/null || true; }
unload() { launchctl bootout "gui/$(id -u)/$1" 2>/dev/null || true; rm -f "$LA_DIR/$1.plist"; }

if [ "$UNINSTALL" = 1 ]; then
  step "Removing ailocal startup LaunchAgents"
  for lbl in com.ailocal.ollama com.ailocal.preload com.ailocal.litellm; do
    unload "$lbl" && info "removed $lbl"
  done
  echo "  Re-enable the Ollama.app 'launch at login' if you want the GUI app back."
  exit 0
fi

[ -x "$OLLAMA_BIN" ] || { warn "Ollama not found at $OLLAMA_BIN — install Ollama.app first"; exit 1; }
mkdir -p "$LA_DIR" "$LOG_DIR" "$APP_SUPPORT" /Users/Shared/ollama/models

# ── 1. ollama serve ──────────────────────────────────────────────────────────
# KEEP_ALIVE=-1 (never unload; -1 is the documented "pin" value). MAX_LOADED=2 +
# NUM_PARALLEL=3 lets a big coder coexist with a small model and serve concurrent
# requests. OLLAMA_MODELS lives on /Users/Shared (out of any one user's home, and
# matches the other machines). flash-attn + q8 KV cache = the memory/speed tuning.
step "Installing com.ailocal.ollama (ollama serve)"
cat > "$LA_DIR/com.ailocal.ollama.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.ailocal.ollama</string>
  <key>ProgramArguments</key>
  <array>
    <string>$OLLAMA_BIN</string>
    <string>serve</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>OLLAMA_HOST</key><string>127.0.0.1:11434</string>
    <key>OLLAMA_MODELS</key><string>/Users/Shared/ollama/models</string>
    <key>OLLAMA_KEEP_ALIVE</key><string>-1</string>
    <key>OLLAMA_MAX_LOADED_MODELS</key><string>2</string>
    <key>OLLAMA_NUM_PARALLEL</key><string>3</string>
    <key>OLLAMA_FLASH_ATTENTION</key><string>1</string>
    <key>OLLAMA_KV_CACHE_TYPE</key><string>q8_0</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>ProcessType</key><string>Interactive</string>
  <key>StandardOutPath</key><string>$LOG_DIR/ollama.log</string>
  <key>StandardErrorPath</key><string>$LOG_DIR/ollama.err.log</string>
</dict>
</plist>
PLIST
load com.ailocal.ollama
info "ollama serve managed by launchd (env baked in, auto-restart, logs in $LOG_DIR)"
warn "Disable Ollama.app 'launch at login' (menubar → Settings) to avoid a port 11434 conflict."

# ── 2. preload the primary model (health-gated, one-shot) ────────────────────
step "Installing com.ailocal.preload ($MODEL_ROLE)"
BACKEND="$(resolve_backend "$MODEL_ROLE")"
[ -n "$BACKEND" ] || { warn "could not resolve '$MODEL_ROLE' in models.yaml — using it as a raw tag"; BACKEND="$MODEL_ROLE"; }
# Self-contained wrapper in a non-protected dir. Health-gate → skip if resident →
# empty-prompt load (no inference) → pin with keep_alive:-1. Backend tag baked in.
PRELOAD="$APP_SUPPORT/preload.sh"
cat > "$PRELOAD" <<WRAP
#!/bin/sh
O="http://127.0.0.1:11434"
for _ in \$(seq 1 60); do curl -fsS -m 3 "\$O/api/version" >/dev/null 2>&1 && break; sleep 2; done
curl -fsS -m 3 "\$O/api/version" >/dev/null 2>&1 || exit 0   # Ollama never came up — fail gracefully
curl -fsS -m 5 "\$O/api/ps" 2>/dev/null | grep -q '"$BACKEND"' && exit 0   # already resident
curl -fsS -m 300 "\$O/api/generate" -d '{"model":"$BACKEND","keep_alive":-1}' >/dev/null 2>&1
WRAP
chmod +x "$PRELOAD"
cat > "$LA_DIR/com.ailocal.preload.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.ailocal.preload</string>
  <key>ProgramArguments</key><array><string>$PRELOAD</string></array>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>$LOG_DIR/preload.log</string>
  <key>StandardErrorPath</key><string>$LOG_DIR/preload.err.log</string>
</dict>
</plist>
PLIST
load com.ailocal.preload
info "primary model '$MODEL_ROLE' ($BACKEND) preloads at login once Ollama is healthy"

# ── 3. LiteLLM (optional native agent; otherwise Docker keeps managing it) ────
if [ "$WITH_LITELLM" = 1 ]; then
  step "Installing com.ailocal.litellm (native, health-gated)"
  LITELLM_BIN="$(command -v litellm || true)"
  [ -n "$LITELLM_BIN" ] || { warn "litellm not on PATH — install with: uv tool install litellm  (or pipx install 'litellm[proxy]')"; }
  WRAP="$APP_SUPPORT/litellm-run.sh"
  cat > "$WRAP" <<WRAP
#!/usr/bin/env bash
set -euo pipefail
# Wait for Ollama, then run LiteLLM natively (no Docker). Env from .env.
cd "$ROOT_DIR"
set -a; . "$ROOT_DIR/.env"; set +a
export OLLAMA_URL="http://127.0.0.1:11434"
for _ in \$(seq 1 60); do curl -fsS -m 3 http://127.0.0.1:11434/api/version >/dev/null 2>&1 && break; sleep 2; done
exec "${LITELLM_BIN:-litellm}" --config "$ROOT_DIR/config/litellm/config.yaml" --port 4000 --host 127.0.0.1
WRAP
  chmod +x "$WRAP"
  cat > "$LA_DIR/com.ailocal.litellm.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.ailocal.litellm</string>
  <key>ProgramArguments</key><array><string>$WRAP</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>ProcessType</key><string>Interactive</string>
  <key>StandardOutPath</key><string>$LOG_DIR/litellm.log</string>
  <key>StandardErrorPath</key><string>$LOG_DIR/litellm.err.log</string>
</dict>
</plist>
PLIST
  load com.ailocal.litellm
  info "LiteLLM runs natively via launchd (waits for Ollama; no Docker needed)"
  warn "Stop the Docker LiteLLM so they don't both bind :4000 — ./scripts/stop.sh"
fi

step "Done. Verify:  launchctl list | grep ailocal   •   logs in $LOG_DIR"
echo "  Reload after edits:  ./scripts/setup-startup.sh   •   remove: --uninstall"
