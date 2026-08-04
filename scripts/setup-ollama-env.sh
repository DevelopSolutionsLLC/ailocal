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
# KEEP_ALIVE is the GLOBAL DEFAULT. It governs only direct-to-Ollama callers that
# send no per-request keep_alive — in practice `embed` (grepai on :11434, bypassing
# LiteLLM), which we WANT pinned as infrastructure. Generation models go through
# LiteLLM, which sends a per-role keep_alive (architecture 6h / implementation 20m /
# review 20m / fast 20m / completion 2h) that overrides this.
# -1 = never evict on idle: embed (~370 MB) is infrastructure Cadence's index depends
# on, so keep it truly resident. Matches OLLAMA_KEEP_ALIVE=-1 in setup-startup.sh.
KEEP_ALIVE="${OLLAMA_KEEP_ALIVE:--1}"     # global default; pins embed persistently (-1)
MAX_LOADED="${OLLAMA_MAX_LOADED_MODELS:-5}" # 5 = all capabilities can be resident at once: embeddings pinned (keep_alive -1), architecture resident for its 6h TTL, implementation/review/fast (20m) and completion (2h) loaded concurrently within that window. Peak ~46 GB (weights+KV at 32K/16K/16K/4K/8K) fits the ~48 GB GPU budget on 64 GB. MAX_LOADED caps COUNT not size — Ollama refuses a model that won't fit, so it never OOMs the machine.
NUM_PARALLEL="${OLLAMA_NUM_PARALLEL:-2}"  # concurrent requests per model (GLOBAL — Ollama has no per-model setting). 2 balances snappy multi-request against KV growth (KV = num_ctx x NUM_PARALLEL per loaded model).
FLASH_ATTN="${OLLAMA_FLASH_ATTENTION:-1}" # faster attention + lower memory, no quality loss
KV_CACHE="${OLLAMA_KV_CACHE_TYPE:-q8_0}" # quantize KV cache to 8-bit, halves memory at large contexts
MODELS_DIR="${OLLAMA_MODELS:-/Users/Shared/ollama/models}" # store models outside any one user's home

. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/output.sh"

# /Users/Shared exists so the model store is not inside one user's home — a 43 GB
# download should not be re-fetched per account, and Cadence's embedder reads the
# same daemon. A plain `mkdir -p` leaves it 755 and owned by whoever ran the
# installer, so a SECOND user can read the models but cannot pull: `ollama pull`
# fails with a permission error naming a path they did not choose.
#
# Group-writable + setgid so files created by any member inherit the group. Best
# effort: on a single-user machine the chmod is a no-op, and if the directory is
# owned by someone else the installer must not fail over a permission it cannot
# change — it says so instead.
mkdir -p "$MODELS_DIR"
if [ -w "$MODELS_DIR" ]; then
  chmod g+rwxs "$MODELS_DIR" 2>/dev/null || true
else
  echo "  ⚠ $MODELS_DIR is not writable by $(id -un) — models will not pull here." >&2
  echo "    Fix: sudo chgrp -R staff '$MODELS_DIR' && sudo chmod -R g+rwXs '$MODELS_DIR'" >&2
fi

# ── Migrate an existing model store ────────────────────────────────────────
#
# Pointing OLLAMA_MODELS at the shared store ORPHANS whatever is already in
# ~/.ollama/models: Ollama looks in the new location, finds nothing, and
# re-downloads everything. On a machine that has been used for a while that is
# tens of gigabytes silently pulled again, with the originals still on disk.
#
# ~ and /Users/Shared are the same volume, so `mv` is a rename — instant, and it
# never needs twice the disk. Entries are moved one at a time so an interruption
# leaves a resumable state rather than a half-copied blob.
HOME_MODELS="$HOME/.ollama/models"
migrate_home_models() {
  [ -d "$HOME_MODELS" ] || return 0
  [ -n "$(ls -A "$HOME_MODELS" 2>/dev/null)" ] || return 0
  # Same directory (or symlinked to it) — nothing to do.
  [ "$(cd "$HOME_MODELS" && pwd -P)" = "$(cd "$MODELS_DIR" 2>/dev/null && pwd -P)" ] && return 0

  local size; size="$(du -sh "$HOME_MODELS" 2>/dev/null | cut -f1)"
  step "Migrating existing models to the shared store"
  echo "  from: $HOME_MODELS ($size)"
  echo "  to:   $MODELS_DIR"

  # Models cannot be moved while the daemon has them open.
  if pgrep -qx ollama 2>/dev/null || pgrep -qf "Ollama.app" 2>/dev/null; then
    echo "  Stopping Ollama first..."
    osascript -e 'quit app "Ollama"' 2>/dev/null || true
    pkill -x ollama 2>/dev/null || true
    sleep 2
  fi

  # $HOME_MODELS and $MODELS_DIR both always have top-level blobs/ and manifests/
  # dirs (setup-startup.sh mkdir -p's the shared store on every run), so a
  # shallow top-level compare always finds them "already present" and silently
  # skips merging the actual content underneath — models vanish instead of
  # migrating. Walk files, not top-level entries.
  local moved=0 kept=0 entry rel dest
  while IFS= read -r -d '' entry; do
    rel="${entry#"$HOME_MODELS"/}"
    dest="$MODELS_DIR/$rel"
    if [ -e "$dest" ]; then
      kept=$((kept + 1))            # already present in the shared store
      continue
    fi
    mkdir -p "$(dirname "$dest")"
    mv "$entry" "$dest" && moved=$((moved + 1))
  done < <(find "$HOME_MODELS" -type f -print0)

  info "moved $moved entr$([ "$moved" = 1 ] && echo y || echo ies)$([ "$kept" -gt 0 ] && echo ", $kept already present")"
  find "$HOME_MODELS" -type d -empty -delete 2>/dev/null || true
  if [ -z "$(ls -A "$HOME_MODELS" 2>/dev/null)" ]; then
    rmdir "$HOME_MODELS" 2>/dev/null || true
    info "$HOME_MODELS is empty and was removed"
  else
    warn "$HOME_MODELS still has entries that already existed in the shared store —"
    warn "  review and delete it yourself: rm -rf '$HOME_MODELS'"
  fi
}
migrate_home_models

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
