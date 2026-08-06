#!/usr/bin/env bash
# lifecycle.sh — the Docker stack's lifecycle, as one implementation.
#
#   lifecycle.sh start | stop | update | teardown
#
# Reached through `ailocal <command>`; not a public entry point. The four
# operations differ in what they do but share the same setup — repo root,
# Compose composition, output helpers — which is why they live together
# rather than in four files each re-deriving it.
#
# Bodies are NOT indented: several use heredocs, whose terminators must stay
# at column 0.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT_DIR"

# Single source of truth for how this stack is composed (deploy/litellm + deploy/searxng).
AILOCAL_ROOT="$ROOT_DIR"
. "$(dirname "$0")/compose.sh"
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/output.sh"

cmd_start() {
NO_WAIT=false
[[ "${1:-}" == "--no-wait" ]] && NO_WAIT=true

# ── Helpers ────────────────────────────────────────────────────────────────

. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/output.sh"

# ── Pre-flight checks ──────────────────────────────────────────────────────

step "Pre-flight checks"

if [ ! -f ".env" ]; then
  error ".env not found. Run ./install.sh first."
  exit 1
fi
info ".env present"

if ! docker ps >/dev/null 2>&1; then
  error "Docker daemon is not running. Start Docker Desktop and retry."
  exit 1
fi
info "Docker daemon running"

if ! has ollama; then
  warn "Ollama CLI not found. Install it from https://ollama.ai"
elif ! ollama list >/dev/null 2>&1; then
  warn "Ollama is not running."
  echo "  Start the MANAGED service (not the GUI app, which competes for :11434):"
  echo "    launchctl kickstart -k gui/$(id -u)/com.ailocal.ollama"
  echo "  LiteLLM will start but model requests will fail until Ollama is up."
else
  info "Ollama daemon responding"
  missing_models=()
  _required=()
  # Fail closed: no implicit tier. A suppressed read falling through to a
  # hardcoded 64gb is what silently installs the wrong model set on a
  # smaller machine. Resolve once, here, and reuse.
  _tier="$(python3 "$ROOT_DIR/lib/profile-config" active-tier)" || {
    echo "  ✗ cannot resolve the active profile (see error above)" >&2; exit 1; }
  # Model list from the GENERATED artifact, not by grepping YAML. jq is already
  # a hard install dependency; a grep|sed over a heavily-commented profile is
  # exactly the fragile parsing this architecture removes.
  while IFS= read -r _m; do _required+=("$_m"); done < <(
    python3 "$ROOT_DIR/lib/profile-config" profile-summary \
      | jq -r '.roles[].model' | sort -u)
  for model in "${_required[@]}"; do
    if ! ollama list 2>/dev/null | awk 'NR>1 {print $1}' | grep -Eq "^${model}(:.+)?$"; then
      missing_models+=("$model")
    fi
  done
  if [ ${#missing_models[@]} -gt 0 ]; then
    warn "Missing Ollama models: ${missing_models[*]}"
    echo "  Run ailocal models-install to pull the full model set before using the stack."
  else
    info "Required Ollama models present"
  fi
fi

# ── Start services ─────────────────────────────────────────────────────────

# No `docker compose pull` here by design: start (incl. the boot LaunchAgent) must be
# reproducible and offline-safe, so it runs whatever image is on disk. main-stable is a
# moving tag — refresh deliberately via install.sh (initial) or update.sh, not on every boot.
step "Starting ailocal services"

# Was LiteLLM already up BEFORE this run? `dc up -d` is a no-op for an
# already-running container whose compose spec is unchanged — and config.yaml,
# the persona instructions and every hook are BIND-MOUNTED, so editing them
# changes no spec and triggers no restart. LiteLLM parses config.yaml once at
# boot, so the proxy then keeps serving the OLD routing while the file on disk
# says something else, with nothing in the logs to say so.
#
# Measured: after regenerating model_group_alias so claude-haiku-4-5 pointed at
# ailocal-fast, `start.sh` reported success and the proxy still routed haiku to
# ailocal-implementation. Only an explicit `docker restart` picked it up.
WAS_RUNNING=false
docker ps --format '{{.Names}}' 2>/dev/null | grep -q '^ailocal-litellm$' && WAS_RUNNING=true

dc up -d --remove-orphans

# Config fingerprint: the mounted files LiteLLM only reads at boot. If any of
# them changed since the last start, the running process is stale and must be
# restarted explicitly — `up -d` will not do it.
CONFIG_STAMP="$AILOCAL_STATE/litellm-config.sha"
mkdir -p "$(dirname "$CONFIG_STAMP")"
# registry.yaml is in this list deliberately: it is the tool gateway's capability
# registry, it is mounted, and it is read once at gateway_init. A registry edit
# with no restart leaves the OLD tool policy in force — which is how a change to
# tool-group membership can appear to do nothing at all.
current_sha=$(cat "$AILOCAL_STATE/litellm/config.yaml" \
                  "$ROOT_DIR/deploy/litellm/registry.yaml" \
                  "$ROOT_DIR"/deploy/litellm/hooks/*.py \
                  "$ROOT_DIR"/deploy/litellm/instructions/*.md 2>/dev/null | shasum -a 256 | cut -d' ' -f1)
previous_sha=$(cat "$CONFIG_STAMP" 2>/dev/null || echo "")

if [ "$WAS_RUNNING" = true ] && [ -n "$previous_sha" ] && [ "$current_sha" != "$previous_sha" ]; then
  step "LiteLLM config changed since last start — restarting to load it"
  docker restart ailocal-litellm >/dev/null
  info "ailocal-litellm restarted (routing/persona/hook changes are now live)"
fi
printf '%s' "$current_sha" > "$CONFIG_STAMP"

if [ "$NO_WAIT" = true ]; then
  info "Services launched (skipping health wait)"
else
  step "Waiting for LiteLLM to become ready"
  if ! ailocal_wait_ready 30 progress; then
    warn "LiteLLM did not become ready after 90s"
    echo "  Check logs: docker logs ailocal-litellm"
  fi
  echo ""
fi

# ── Service URLs and client setup ──────────────────────────────────────────

KEY="$(grep '^LITELLM_MASTER_KEY=' .env | cut -d= -f2-)"

step "ailocal is running"
echo ""
echo "  ┌─ Service ───────────────────────────────────────────────┐"
echo "  │  LiteLLM API     →  http://localhost:4000               │"
echo "  └─────────────────────────────────────────────────────────┘"
echo ""
echo "  One-time setup — add to ~/.zprofile to make permanent:"
echo "    source \"$ROOT_DIR/clients/env.sh\""
echo ""
echo "  ── Claude Code ───────────────────────────────────────────"
echo "    export ANTHROPIC_BASE_URL=http://localhost:4000"
echo "    export ANTHROPIC_API_KEY=$KEY"
echo "    claude"
echo ""
echo "  ── Codex ─────────────────────────────────────────────────"
echo "    export OPENAI_BASE_URL=http://localhost:4000/v1"
echo "    export OPENAI_API_KEY=$KEY"
echo "    codex"
echo ""
echo "  ── VS Code ───────────────────────────────────────────────"
echo "    Uses the litellm-connector extension (key in SecretStorage):"
echo "    ailocal vscode   # installs extension + prints setup"
echo ""
echo "  ── First-time client setup ───────────────────────────────────"
echo "    ailocal clients"
echo "    (configures Codex, Claude Code, and VS Code Copilot Chat)"
echo ""
}

cmd_stop() {
REMOVE_VOLUMES=false
if [[ "${1:-}" == "--volumes" ]]; then
  REMOVE_VOLUMES=true
  warn "--volumes flag set: all Docker volumes will be removed."
  read -r -p "  This destroys all database and cache data. Are you sure? [y/N]: " confirm
  [[ "${confirm:-}" =~ ^[Yy]$ ]] || { echo "Aborted."; exit 0; }
fi

# ── Stop services ──────────────────────────────────────────────────────────

step "Stopping ailocal services"

if [ "$REMOVE_VOLUMES" = true ]; then
  dc down --volumes --remove-orphans
  info "Services stopped and volumes removed."
else
  dc down --remove-orphans
  info "Services stopped. Data volumes preserved."
  echo "  To also remove volumes: ailocal stop --volumes"
fi
}

cmd_update() {
SKIP_MODELS=false
[[ "${1:-}" == "--skip-models" ]] && SKIP_MODELS=true

# ── Snapshot .env first ─────────────────────────────────────────────────────
# The only non-git, non-regenerable state is .env (the master key). Config is
# in git and Ollama models re-pull, so a one-line snapshot is the whole backup.

step "Snapshotting .env before update"
if [ -f "$ROOT_DIR/.env" ]; then
  mkdir -p "$AILOCAL_STATE/backups"
  SNAP="$AILOCAL_STATE/backups/.env.$(date -u +%Y%m%dT%H%M%SZ)"
  cp "$ROOT_DIR/.env" "$SNAP" && chmod 600 "$SNAP"
  info "Saved $SNAP"
else
  warn "No .env found — nothing to snapshot."
fi

# ── Pull updated images ────────────────────────────────────────────────────

# NOT an image upgrade. Every image is digest-pinned, so this re-fetches the
# SAME digests and exists only to repair a locally deleted layer. `ailocal update`
# advances Ollama models, never container images -- replacing an image is a
# validated, human-approved change: ailocal security --check-updates
step "Pulling pinned Docker images (digests unchanged by design)"
dc pull

# ── Update Ollama models ───────────────────────────────────────────────────

if [ "$SKIP_MODELS" = false ]; then
  step "Updating Ollama models"
  bash "$ROOT_DIR/lib/install-models.sh" || warn "Model update had warnings — services will still restart."
fi

# ── Regenerate model config (single source of truth) ──────────────────────
# Regenerate config.yaml / model_catalog.json / docs from models.yaml.
# Client configs are NOT auto-redeployed here — that would rewrite the user's
# ~/.codex and ~/.claude files on every update. Redeploy explicitly when you
# want to:  ailocal clients [claude|codex|vscode]

step "Regenerating model config (sync-models)"
python3 "$ROOT_DIR/lib/sync-models.py"

# ── Rolling restart (dependency order) ────────────────────────────────────
# Restart infrastructure first, then dependents.

step "Restarting services"
dc up -d --remove-orphans
# Ensure LiteLLM reloads the regenerated model_info (config-only changes are
# not picked up by `up -d` when the image is unchanged).
dc restart litellm searxng

# ── Post-update health check ───────────────────────────────────────────────

step "Validating health post-update"
# Wait for LiteLLM to accept requests, then run doctor (the single health script).
ailocal_wait_ready 20 || true
if python3 "$ROOT_DIR/lib/checks/run.py" doctor; then
  step "Update complete — LiteLLM healthy."
else
  warn "Health check reported issues after update."
  echo "  Check logs: docker logs ailocal-litellm --tail=50"
  echo "  To roll back: git checkout the previous config, then ailocal start"
  exit 1
fi
}

cmd_teardown() {
REMOVE_IMAGES=false
REMOVE_CLIENTS=false
for arg in "$@"; do
  case "$arg" in
    --images)  REMOVE_IMAGES=true ;;
    --clients) REMOVE_CLIENTS=true ;;
  esac
done

# ── Confirmation ───────────────────────────────────────────────────────────

step "ailocal teardown"
echo ""
echo "  This will permanently remove:"
echo "    • All ailocal containers"
echo "    • The ailocal Docker network"
[ "$REMOVE_IMAGES" = true ] && echo "    • All pulled Docker images"
if [ "$REMOVE_CLIENTS" = true ]; then
  echo "    • The claude-local/codex-local shell integration (~/.zshrc lines + ~/.config/ailocal/)"
fi
echo ""
echo "  Your .env and repo config files will NOT be touched."
echo "  Re-run ./install.sh + ailocal start to rebuild."
echo ""
read -r -p "  Proceed? [y/N]: " confirm
[[ "${confirm:-}" =~ ^[Yy]$ ]] || { echo "Aborted."; exit 0; }

# ── Stop containers and remove volumes ────────────────────────────────────

step "Stopping containers and removing volumes"
dc down --volumes --remove-orphans 2>/dev/null || true

# ── Remove Docker network (compose may have already done this) ─────────────

if docker network ls --format "{{.Name}}" | grep -q "^ailocal_net$"; then
  step "Removing Docker network"
  docker network rm ailocal_net 2>/dev/null || true
fi

# ── Optionally remove images ───────────────────────────────────────────────

if [ "$REMOVE_IMAGES" = true ]; then
  step "Removing Docker images"
  # Extract image names from the deploy/ compose files and remove them
  grep -h '^\s*image:' deploy/litellm/compose.yaml deploy/searxng/compose.yaml \
    | awk '{print $2}' \
    | while read -r img; do
        if docker image inspect "$img" >/dev/null 2>&1; then
          docker rmi "$img" && info "removed $img" || warn "could not remove $img (may be in use elsewhere)"
        fi
      done
fi

# ── Optionally uninstall claude-local/codex-local shell integration ───────

if [ "$REMOVE_CLIENTS" = true ]; then
  step "Removing claude-local/codex-local shell integration"

  RC="${ZDOTDIR:-$HOME}/.zshrc"
  if [ -f "$RC" ] && grep -qE '# ailocal-configure|# ailocal-finalize' "$RC" 2>/dev/null; then
    ts=$(date +%Y%m%d_%H%M%S)
    cp "$RC" "${RC}.bak.${ts}"
    info "Backed up: $(basename "$RC") → $(basename "$RC").bak.${ts}"
    python3 - "$RC" <<'PY'
import re, sys
p = sys.argv[1]
s = open(p, encoding="utf-8").read()
lines = s.splitlines(keepends=True)
lines = [l for l in lines if '# ailocal-configure' not in l and '# ailocal-finalize' not in l]
s = ''.join(lines)
s = re.sub(r'\n{3,}', '\n\n', s)
s = s.rstrip('\n') + '\n'
open(p, "w", encoding="utf-8").write(s)
PY
    info "Removed ailocal-configure/ailocal-finalize lines from ~/.zshrc"
  else
    warn "No ailocal-configure/ailocal-finalize lines found in ~/.zshrc — nothing to remove"
  fi

  AILOCAL_CFG="${XDG_CONFIG_HOME:-$HOME/.config}/ailocal"
  if [ -d "$AILOCAL_CFG" ]; then
    if [ -f "$AILOCAL_CFG/env" ]; then
      ts=$(date +%Y%m%d_%H%M%S)
      cp "$AILOCAL_CFG/env" "$AILOCAL_CFG/env.bak.${ts}"
      cp "$AILOCAL_CFG/env.bak.${ts}" "$HOME/ailocal-env.bak.${ts}" 2>/dev/null || true
      info "Backed up $AILOCAL_CFG/env → ~/ailocal-env.bak.${ts}"
    fi
    rm -rf "$AILOCAL_CFG"
    info "Removed $AILOCAL_CFG"
  else
    warn "$AILOCAL_CFG does not exist — nothing to remove"
  fi

  echo "  ~/.claude and ~/.codex were never touched by ailocal — nothing to revert there."
fi

step "Teardown complete."
echo ""
echo "  To fully reset and start fresh:"
echo "    ./install.sh     # re-generate .env if needed"
echo "    ailocal start       # rebuild and start"
}

case "${1:-}" in
  start)    shift; cmd_start "$@" ;;
  stop)     shift; cmd_stop "$@" ;;
  update)   shift; cmd_update "$@" ;;
  teardown) shift; cmd_teardown "$@" ;;
  *) echo "usage: lifecycle.sh <start|stop|update|teardown> [options]" >&2; exit 2 ;;
esac
