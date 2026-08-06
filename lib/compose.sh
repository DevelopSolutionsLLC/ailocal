#!/usr/bin/env bash
# lib/compose.sh — the ONE definition of how ailocal invokes Docker Compose.
#
# Source this, then call `dc` instead of `docker compose`:
#
#   . "$(dirname "$0")/compose.sh"
#   dc up -d --remove-orphans
#
# Why a helper at all: the stack is split across two files under deploy/ so
# LiteLLM and SearXNG are configured in their own locations, but they must come
# up as ONE Compose project ("ailocal") sharing ONE network so LiteLLM can reach
# SearXNG at http://searxng:8080. A bare `docker compose` in the repo root would
# find no compose file and silently operate on nothing.
#
# --project-directory pins path resolution AND .env discovery to the repo root,
# so every relative path inside both compose files is repo-root-relative and the
# root .env is auto-loaded. Do not drop it.

# AILOCAL_ROOT must already be set by the caller, or we derive it from this file.
# This file lives at <root>/lib/compose.sh, so the root is one level up.
# ${BASH_SOURCE[0]:-$0} keeps this correct when sourced from zsh too, where
# BASH_SOURCE is empty and would otherwise resolve two levels above $PWD.
: "${AILOCAL_ROOT:=$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/.." && pwd)}"

# Mutable runtime state lives OUTSIDE the checkout, under XDG state. A working
# copy is source, not storage: benchmark output, e2e results, captured request
# payloads and the config fingerprint all used to accumulate in ./data and
# ./backups, where they were invisible to `git status` and travelled with the
# repository. Exported so the compose files can mount it by absolute path.
# policy.py is the one implementation of this path; shell asks rather than
# repeating the default expression.
export AILOCAL_PROXY="${AILOCAL_PROXY:-http://127.0.0.1:4000}"
export AILOCAL_STATE="${AILOCAL_STATE:-$(python3 "$AILOCAL_ROOT/lib/profile-config" state-root)}"
mkdir -p "$AILOCAL_STATE"

AILOCAL_COMPOSE_FILES=(
  -f "$AILOCAL_ROOT/deploy/litellm/compose.yaml"
  -f "$AILOCAL_ROOT/deploy/searxng/compose.yaml"
)

# Rendered SearXNG settings. SearXNG has NO environment interpolation for an
# engine's api_key -- settings_defaults.py exposes only a fixed allow-list
# (SEARXNG_SECRET, SEARXNG_PORT, ...) and settings_loader.py has no ${VAR}
# substitution and no file layering. So the key cannot be passed the way
# SEARXNG_SECRET is, and the tracked settings.yml must stay secret-free.
#
# The rendered copy therefore lives under AILOCAL_STATE, OUTSIDE the checkout,
# which is what makes committing it impossible rather than merely discouraged.
export AILOCAL_SEARXNG_SETTINGS="$AILOCAL_STATE/searxng/settings.yml"

# Render deploy/searxng/settings.yml -> $AILOCAL_SEARXNG_SETTINGS, substituting
# the Brave key. Fails closed; never prints the key.
ailocal_render_searxng_settings() {
  local src="$AILOCAL_ROOT/deploy/searxng/settings.yml"
  local out="$AILOCAL_SEARXNG_SETTINGS"
  local dir; dir="$(dirname "$out")"
  local placeholder='__BRAVE_API_KEY__'

  [ -f "$src" ] || { echo "BRAVE_SETTINGS_GENERATION_FAILED: missing $src" >&2; return 1; }
  mkdir -p "$dir" && chmod 700 "$dir" || {
    echo "BRAVE_SETTINGS_GENERATION_FAILED: cannot create $dir" >&2; return 1; }

  # No placeholder => Brave is intentionally not configured. Render a plain
  # copy and require nothing: disabling Brave must not break the deployment.
  if ! grep -q "$placeholder" "$src"; then
    ( umask 077; cp "$src" "$out.tmp.$$" ) && mv -f "$out.tmp.$$" "$out" || {
      rm -f "$out.tmp.$$"
      echo "BRAVE_SETTINGS_GENERATION_FAILED: copy failed" >&2; return 1; }
    return 0
  fi

  local key=""
  if [ -f "$AILOCAL_ROOT/.env" ]; then
    # Values may be quoted; strip one layer. Never echoed.
    key="$(sed -n 's/^BRAVE_API=//p' "$AILOCAL_ROOT/.env" | head -n1 \
           | sed -e 's/^"\(.*\)"$/\1/' -e "s/^'\(.*\)'$/\1/")"
  fi
  if [ -z "$key" ]; then
    echo "BRAVE_KEY_MISSING: braveapi is configured in deploy/searxng/settings.yml" >&2
    echo "  but BRAVE_API is unset or empty in .env. Set it, or remove braveapi" >&2
    echo "  from keep_only to disable Brave." >&2
    return 1
  fi

  # Atomic: write restricted, verify, then rename. A partially written or
  # world-readable settings file must never be mountable.
  # Cleanup is explicit on every failure path: `trap ... RETURN` is a bashism
  # and this file is deliberately sourceable from zsh (see the header), where
  # it aborts with "undefined signal: RETURN" and leaves the temp file behind.
  local tmp="$out.tmp.$$"
  ( umask 077
    # awk, not sed: the key is arbitrary text and must not be reinterpreted as
    # a sed replacement (& and \1 would corrupt or truncate it).
    awk -v k="$key" -v ph="$placeholder" '{
      i = index($0, ph)
      if (i) $0 = substr($0,1,i-1) k substr($0,i+length(ph))
      print
    }' "$src" > "$tmp"
  ) || { rm -f "$tmp"; echo "BRAVE_SETTINGS_GENERATION_FAILED: render error" >&2; return 1; }

  if grep -q "$placeholder" "$tmp"; then
    rm -f "$tmp"
    echo "BRAVE_PLACEHOLDER_MISSING: placeholder survived substitution" >&2; return 1
  fi
  # The tracked source must be untouched, and must never gain the secret.
  if grep -qF -- "$key" "$src" 2>/dev/null; then
    rm -f "$tmp"
    echo "BRAVE_SETTINGS_SECRET_LEAK: key present in TRACKED $src -- remove it" >&2
    return 1
  fi
  chmod 600 "$tmp" && mv -f "$tmp" "$out" || {
    rm -f "$tmp"
    echo "BRAVE_SETTINGS_GENERATION_FAILED: atomic replace failed" >&2; return 1; }
  return 0
}

# Wait until the proxy accepts requests. /health/liveliness returns 200 as soon
# as LiteLLM is listening; the full /health endpoint blocks on every model and
# fails when Ollama is down, so it is the wrong signal here.
#
#   ailocal_wait_ready [max_attempts] [progress]
# Returns non-zero if it never became ready; callers decide how loud to be.
ailocal_wait_ready() {
  local max="${1:-30}" progress="${2:-}" attempts=0
  until curl -sSf --max-time 3 "$AILOCAL_PROXY/health/liveliness" >/dev/null 2>&1; do
    attempts=$((attempts + 1))
    [ "$attempts" -ge "$max" ] && return 1
    [ -n "$progress" ] && printf "  Waiting... (%ds)\r" $((attempts * 3))
    sleep 3
  done
  [ -n "$progress" ] && printf "                    \r"
  return 0
}

dc() {
  # Any subcommand that starts or recreates containers needs the rendered
  # settings to exist first -- the SearXNG service mounts it by absolute path.
  case "${1:-}" in
    up|start|restart|create|run)
      ailocal_render_searxng_settings || return 1 ;;
  esac
  DOCKER_CLI_HINTS=false docker compose \
    --project-directory "$AILOCAL_ROOT" \
    "${AILOCAL_COMPOSE_FILES[@]}" \
    "$@"
}

# Space-separated form for scripts that need to echo the command to a user.
