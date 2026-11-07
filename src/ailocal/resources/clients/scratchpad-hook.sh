#!/usr/bin/env bash
# SessionStart hook: create a per-session scratchpad; arg 1 is claude|codex.
# Quiet, exit 0 always (a failing hook degrades the session); jq optional.
tool="${1:-session}"

payload="$(cat 2>/dev/null || true)"
sid=""
if command -v jq >/dev/null 2>&1; then
  sid="$(printf '%s' "$payload" | jq -r '.session_id // empty' 2>/dev/null || true)"
fi
# The id names a directory: keep it to a path component, never `..` or `/`.
sid="$(printf '%s' "$sid" | tr -cd 'A-Za-z0-9._-' | cut -c1-64)"
case "$sid" in ''|.|..) sid="$(date +%Y%m%d-%H%M%S)-$$" ;; esac
tool="$(printf '%s' "$tool" | tr -cd 'a-z')"

# /tmp is shared: a symlinked or foreign-owned base → fresh mktemp dir (#78).
umask 077
root="${AILOCAL_SCRATCH_ROOT:-/tmp}"  # overridden only by the test suite
base="${root}/ailocal-scratchpad-$(id -u)"
mkdir -m 700 "$base" 2>/dev/null || true
if [ -L "$base" ] || [ ! -d "$base" ] || [ ! -O "$base" ]; then
  base="$(mktemp -d "${root}/ailocal-scratchpad.XXXXXX" 2>/dev/null)" || exit 0
fi
dir="${base}/${tool}-${sid}"
if ! mkdir -m 700 "$dir" 2>/dev/null; then  # a resumed session reuses its own
  { [ -d "$dir" ] && [ ! -L "$dir" ] && [ -O "$dir" ]; } || exit 0
fi

ctx="Session scratchpad: ${dir} (already created and isolated to this session). Use it for all working notes — file/dir summaries, dependency maps, open questions, and implementation plans. Prefer it over holding large context in memory. Do not write scratch files elsewhere in /tmp or in the project."

if command -v jq >/dev/null 2>&1; then
  jq -cn --arg c "$ctx" \
    '{hookSpecificOutput:{hookEventName:"SessionStart",additionalContext:$c}}' 2>/dev/null \
    || printf '%s\n' "$ctx"
else
  # Both clients accept plain stdout as additional context.
  printf '%s\n' "$ctx"
fi
exit 0
