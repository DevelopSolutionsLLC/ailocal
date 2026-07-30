#!/usr/bin/env bash
# lib/compose.sh — the ONE definition of how ailocal invokes Docker Compose.
#
# Source this, then call `dc` instead of `docker compose`:
#
#   . "$(dirname "$0")/lib/compose.sh"
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
# This file lives at <root>/scripts/lib/compose.sh, so the root is two levels up.
# ${BASH_SOURCE[0]:-$0} keeps this correct when sourced from zsh too, where
# BASH_SOURCE is empty and would otherwise resolve two levels above $PWD.
: "${AILOCAL_ROOT:=$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/../.." && pwd)}"

# Mutable runtime state lives OUTSIDE the checkout, under XDG state. A working
# copy is source, not storage: benchmark output, e2e results, captured request
# payloads and the config fingerprint all used to accumulate in ./data and
# ./backups, where they were invisible to `git status` and travelled with the
# repository. Exported so the compose files can mount it by absolute path.
export AILOCAL_STATE="${AILOCAL_STATE:-${XDG_STATE_HOME:-$HOME/.local/state}/ailocal}"
mkdir -p "$AILOCAL_STATE"

AILOCAL_COMPOSE_FILES=(
  -f "$AILOCAL_ROOT/deploy/litellm/docker-compose.yml"
  -f "$AILOCAL_ROOT/deploy/searxng/docker-compose.yml"
)

dc() {
  DOCKER_CLI_HINTS=false docker compose \
    --project-directory "$AILOCAL_ROOT" \
    "${AILOCAL_COMPOSE_FILES[@]}" \
    "$@"
}

# Space-separated form for scripts that need to echo the command to a user.
ailocal_compose_cmd() {
  echo "docker compose --project-directory \"$AILOCAL_ROOT\" -f deploy/litellm/docker-compose.yml -f deploy/searxng/docker-compose.yml"
}
