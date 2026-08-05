#!/usr/bin/env bash
# .githooks/commit-msg — rejects attribution/session metadata, allows product words.
#
# ISOLATED BY CONSTRUCTION. Every commit here happens in a throwaway repository
# under $TMPDIR with its own core.hooksPath and its own user identity. The real
# repository's git configuration is never read for policy and never written --
# a test that mutates the working repo to prove a hook works is not a test, it
# is a second way to break the thing it is checking.
#
# The distinction under test is TRAILER SHAPE vs PRODUCT WORD. This repository
# legitimately discusses claude-local, Claude Code, the Anthropic API and
# OpenAI-compatible routes in commit messages; it must never publish a
# Co-Authored-By naming an assistant, or a session URL.
set -uo pipefail
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/harness.sh"
HOOK="$ROOT_DIR/.githooks/commit-msg"

echo "COMMIT-MSG HOOK"
check $([ -f "$HOOK" ] && echo 0 || echo 1) "the versioned hook exists (.githooks/commit-msg)"
check $([ -x "$HOOK" ] && echo 0 || echo 1) "the hook is executable"
[ -f "$HOOK" ] || { report || true; exit 1; }

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
git -C "$WORK" init -q .
git -C "$WORK" config user.name  "Test Person"
git -C "$WORK" config user.email "test@example.invalid"
git -C "$WORK" config core.hooksPath "$ROOT_DIR/.githooks"
echo seed > "$WORK/f.txt"; git -C "$WORK" add f.txt

# Verified through a REAL `git commit`, not by invoking the hook directly: the
# thing that matters is whether git actually refuses to create the object.
attempt() {   # attempt <message>  -> 0 committed, 1 rejected
  echo "$RANDOM" >> "$WORK/f.txt"; git -C "$WORK" add f.txt
  git -C "$WORK" commit -q -m "$1" >/dev/null 2>&1 && echo 0 || echo 1
}

echo
echo "  ACCEPTED — legitimate product references"
accept() { check "$(attempt "$1")" "accepts: $2"; }
accept "fix(vscode): route claude-local through claude_native_lsp" "claude-local / claude_native_lsp"
accept "docs: describe Claude Code tool filtering" "Claude Code"
accept "feat: talk to the Anthropic API via ANTHROPIC_BASE_URL" "Anthropic API"
accept "feat: OpenAI-compatible /v1/responses route" "OpenAI-compatible"
accept "$(printf 'fix: multiline body\n\nExplains the change in detail.\nMentions .claude.json and code.claude.com/docs.\n\nRefs: #12')" \
       "ordinary multiline body with product words"

echo
echo "  REJECTED — attribution and session metadata"
reject() {
  r="$(attempt "$1")"
  check "$([ "$r" = 1 ] && echo 0 || echo 1)" "rejects: $2"
}
reject "$(printf 'fix: thing\n\nCo-Authored-By: Claude Opus 5 <noreply@anthropic.com>')" \
       "Claude Co-Authored-By trailer"
reject "$(printf 'fix: thing\n\nClaude-Session: https://claude.ai/code/session_01ABCDEF')" \
       "Claude-Session trailer"
reject "$(printf 'fix: thing\n\nsee https://claude.ai/code/session_01ABCDEF for context')" \
       "claude.ai session URL anywhere in the body"
reject "$(printf 'fix: thing\n\nCo-Authored-By: Someone <noreply@anthropic.com>')" \
       "Anthropic noreply co-author address"
reject "$(printf 'fix: thing\n\nGenerated with Claude Code')" \
       "Generated-with-assistant attribution"
reject "$(printf 'fix: thing\n\nCo-Authored-By: ChatGPT <x@openai.com>')" \
       "ChatGPT Co-Authored-By trailer"

echo
echo "  the real repository's config was not touched"
real="$(git -C "$ROOT_DIR" config --local --get core.hooksPath || true)"
check $([ "$real" = ".githooks" ] || [ -z "$real" ] && echo 0 || echo 1) \
  "core.hooksPath in this repo is '.githooks' or unset (got '${real:-unset}')"
check $([ -z "$(git -C "$ROOT_DIR" config --global --get core.hooksPath || true)" ] && echo 0 || echo 1) \
  "no GLOBAL core.hooksPath was set"

report || exit 1
