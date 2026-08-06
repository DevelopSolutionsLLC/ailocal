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
#   ailocal autostart [--model ROLE] [--with-litellm] [--uninstall]
#
# IMPORTANT: if you run ollama via this LaunchAgent, DISABLE the Ollama.app
# "launch at login" (Ollama menubar → Settings) or quit the app — otherwise two
# servers fight over port 11434.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
AILOCAL_STATE="${AILOCAL_STATE:-$(python3 "$ROOT_DIR/lib/profile-config" state-root)}"
LA_DIR="$HOME/Library/LaunchAgents"
LOG_DIR="$HOME/Library/Logs/ailocal"
# Agent-run scripts must live OUTSIDE TCC-protected folders (~/Documents, ~/Desktop,
# ~/Downloads): launchd gets "Operation not permitted" executing scripts there on
# modern macOS. Install self-contained wrappers here instead (and bake in any values
# from the repo at install time, since the repo itself may be under ~/Documents).
APP_SUPPORT="$HOME/Library/Application Support/ailocal"
# Resolve the binary rather than assuming the cask layout. This was hardcoded to
# the app bundle, so a machine where Ollama came from the FORMULA (or where the
# app was moved) got a LaunchAgent pointing at a path that does not exist —
# launchd reports nothing useful and `ollama serve` simply never runs.
# Order: app bundle, then the symlink, then Homebrew, then PATH.
OLLAMA_BIN=""
for _cand in "/Applications/Ollama.app/Contents/Resources/ollama" \
             "/usr/local/bin/ollama" \
             "$(command -v ollama 2>/dev/null || true)"; do
  [ -n "$_cand" ] && [ -x "$_cand" ] && { OLLAMA_BIN="$_cand"; break; }
done
if [ -z "$OLLAMA_BIN" ]; then
  echo "  ✗ ollama binary not found — install Ollama first, then re-run." >&2
  exit 1
fi
MODEL_ROLE="architecture"
WITH_LITELLM=0
UNINSTALL=0

. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/output.sh"

# Resolve a role name to its Ollama backend tag from models.yaml (at install time,
# from the interactive shell — the agent can't read ~/Documents at runtime).
resolve_backend() {
  # Resolve capability -> active Ollama backend via the generator (single source; handles the
  # active/preferred schema). Runs at install time from the interactive shell, where python3 exists.
  python3 "$ROOT_DIR/lib/sync-models.py" --resolve "$1" 2>/dev/null
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

# The README's own Install section says to start Ollama (`ollama serve` or open
# Ollama.app) BEFORE running install.sh, so by the time this script installs the
# launchd-managed 'ollama serve' with OLLAMA_MODELS baked in, an env-less instance
# is usually already bound to :11434 and every model pull silently lands in
# ~/.ollama instead of the shared store. Three things hold the port hostage and
# ALL must be stopped, or the GUI respawns its own 'ollama serve' child within
# ~1s of being killed:
#   1. the Ollama.app GUI process itself (Contents/MacOS/Ollama) — killing only
#      its 'ollama serve' child is not enough; the parent immediately relaunches
#      a fresh one the moment the port is free.
#   2. its embedded Squirrel-framework watchdog LaunchAgent (com.ollama.ollama,
#      registered via Background Task Management from inside the app bundle —
#      distinct from the app's own "launch at login" toggle in its menu, and
#      invisible to `ls ~/Library/LaunchAgents`).
#   3. the app's ordinary login-item registration, if a user enabled it.
# Belt-and-braces: disable the BTM watchdog, quit the GUI, kill anything still
# bound, then hand the port to our launchd agent.
launchctl disable "gui/$(id -u)/com.ollama.ollama" >/dev/null 2>&1 || true
osascript -e 'quit app "Ollama"' >/dev/null 2>&1 || true
pkill -9 -f "/Applications/Ollama.app/Contents/MacOS/Ollama" 2>/dev/null || true
pkill -9 -f "Ollama.app/Contents/Resources/ollama serve" 2>/dev/null || true
for _ in $(seq 1 20); do lsof -i :11434 >/dev/null 2>&1 || break; sleep 0.5; done

# ── 1. ollama serve ──────────────────────────────────────────────────────────
# KEEP_ALIVE=-1 is the GLOBAL DEFAULT and it pins `embed`: grepai calls Ollama
# directly on :11434 (bypassing LiteLLM) and sends no per-request keep_alive, so it
# inherits this default. Generation models go THROUGH LiteLLM, which sends a
# per-role keep_alive (architecture 6h / implementation 20m / review 20m /
# fast 20m / completion 2h) that OVERRIDES this global — nothing generation-side
# is pinned forever; only embeddings is, since it's small (~370 MB) infrastructure
# other tools depend on being resident. MAX_LOADED=5 + NUM_PARALLEL=2: all five
# capabilities can co-reside — embeddings pinned (-1), architecture resident for its
# 6h TTL, implementation/review/fast (20m) and completion (2h) loaded concurrently
# within that window (~46 GB peak, fits the ~48 GB GPU budget).
# MAX_LOADED caps COUNT not size (Ollama refuses an oversized load, never OOMs).
# OLLAMA_MODELS lives on /Users/Shared (out of any one user's home, matches the
# other machines). flash-attn + q8 KV cache = the memory/speed tuning.
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
    <key>OLLAMA_MAX_LOADED_MODELS</key><string>5</string>
    <key>OLLAMA_NUM_PARALLEL</key><string>2</string>
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
info "Ollama.app GUI stopped and its login/watchdog agents disabled — launchd owns :11434 now."

# ── 2. preload the primary model (health-gated, one-shot) ────────────────────
step "Installing com.ailocal.preload ($MODEL_ROLE)"
BACKEND="$(resolve_backend "$MODEL_ROLE")"
[ -n "$BACKEND" ] || { warn "could not resolve '$MODEL_ROLE' in models.yaml — using it as a raw tag"; BACKEND="$MODEL_ROLE"; }
# Resolve the role's actual configured keep_alive rather than hardcoding one here —
# a second hardcoded copy of the profile's TTL is exactly how this previously drifted
# out of sync with profiles/<tier>.yaml (this preload pinned forever via a
# hardcoded -1 even after the profile itself moved to a bounded TTL).
PRELOAD_KEEP_ALIVE="$(python3 "$ROOT_DIR/lib/sync-models.py" --resolve-keep-alive "$MODEL_ROLE" 2>/dev/null)"
[ -n "$PRELOAD_KEEP_ALIVE" ] || PRELOAD_KEEP_ALIVE="-1"
# Ollama's keep_alive field is a Go Duration: -1 must be a bare JSON number
# (Go's duration parser rejects a quoted "-1" — no unit suffix), while an actual
# duration like "6h" must be a quoted JSON string.
if [ "$PRELOAD_KEEP_ALIVE" = "-1" ]; then
  PRELOAD_KEEP_ALIVE_JSON="-1"
else
  PRELOAD_KEEP_ALIVE_JSON="\"$PRELOAD_KEEP_ALIVE\""
fi
# Self-contained wrapper in a non-protected dir. Health-gate → skip if resident →
# empty-prompt load (no inference) → pin for the role's configured TTL. Backend
# tag and keep_alive both baked in at install time (the agent can't read the repo
# under ~/Documents at runtime).
PRELOAD="$APP_SUPPORT/preload.sh"
cat > "$PRELOAD" <<WRAP
#!/bin/sh
O="http://127.0.0.1:11434"
for _ in \$(seq 1 60); do curl -fsS -m 3 "\$O/api/version" >/dev/null 2>&1 && break; sleep 2; done
curl -fsS -m 3 "\$O/api/version" >/dev/null 2>&1 || exit 0   # Ollama never came up — fail gracefully
curl -fsS -m 5 "\$O/api/ps" 2>/dev/null | grep -q '"$BACKEND"' && exit 0   # already resident
curl -fsS -m 300 "\$O/api/generate" -d '{"model":"$BACKEND","keep_alive":$PRELOAD_KEEP_ALIVE_JSON}' >/dev/null 2>&1
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
exec "${LITELLM_BIN:-litellm}" --config "$AILOCAL_STATE/litellm/config.yaml" --port 4000 --host 127.0.0.1
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
  warn "Stop the Docker LiteLLM so they don't both bind :4000 — ailocal stop"
fi

step "Done. Verify:  launchctl list | grep ailocal   •   logs in $LOG_DIR"
echo "  Reload after edits:  ailocal autostart   •   remove: --uninstall"
