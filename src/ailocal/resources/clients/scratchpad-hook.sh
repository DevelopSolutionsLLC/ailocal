#!/usr/bin/env bash
# scratchpad-hook.sh — shared SessionStart hook for claude-local and codex-local.
# Deployed to ~/.config/ailocal/ by `ailocal clients` and registered in both
# claude/settings.json ("hooks".SessionStart) and codex/config.toml ([[hooks.SessionStart]]).
#
# Both clients pass the hook a JSON payload on stdin containing `session_id`, and
# both read back `hookSpecificOutput.additionalContext` as extra model context.
# This creates a per-session scratchpad directory on the HOST (isolated per session)
# and tells the model to use it — so scratch notes never mix between sessions
# or tools. Arg 1 is the tool label (claude|codex).
#
# Must stay quiet on stderr and exit 0 even on partial failure — a noisy or failing
# SessionStart hook degrades the session.
#
# jq is OPTIONAL here and is not an ailocal prerequisite. Every use below is
# guarded: without it the scratchpad is keyed by timestamp+pid instead of the
# client's session_id (still isolated per session), and the context is emitted
# as plain stdout — a supported SessionStart form, not a fallback that merely
# happens to work. Verified on a PATH containing no jq: exit 0, clean stderr,
# context delivered, directory created.
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

# /tmp is shared and writable by every account; codex-local's workspace-write
# sandbox allows it. The notes stay private to this user (0700) and an
# attacker-created base is never used: a base that is a symlink or not owned by
# us is replaced by a fresh mktemp directory (#78).
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
  # No jq: plain stdout is also accepted as additional context by both clients.
  printf '%s\n' "$ctx"
fi
exit 0
