#!/usr/bin/env bash
# scratchpad-hook.sh — shared SessionStart hook for claude-local and codex-local.
# Deployed to ~/.config/ailocal/ by `ailocal clients` and registered in both
# claude/settings.json ("hooks".SessionStart) and codex/config.toml ([[hooks.SessionStart]]).
#
# Both clients pass the hook a JSON payload on stdin containing `session_id`, and
# both read back `hookSpecificOutput.additionalContext` as extra model context.
# This creates a per-session scratchpad directory on the HOST (isolated per session)
# and tells the model to use it — so /tmp/scratchpad never mixes work between
# sessions or tools. Arg 1 is the tool label (claude|codex).
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
# Fallback id if the payload had none (keeps isolation, just not client-session-keyed).
[ -n "$sid" ] || sid="$(date +%Y%m%d-%H%M%S)-$$"

dir="/tmp/scratchpad/${tool}-${sid}"
mkdir -p "$dir" 2>/dev/null || true

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
