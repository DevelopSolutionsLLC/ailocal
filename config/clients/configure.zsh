# ailocal configure.zsh — sourced at the TOP of ~/.zshrc (before p10k instant
# prompt). Managed file — installed by scripts/install-clients.sh, always
# overwritten. Must produce ZERO stdout/stderr: this runs on every interactive
# shell startup, and any output here corrupts p10k instant prompt / VS Code's
# OSC 633 command markers.

# ── VS Code terminal detection ──────────────────────────────────────────────
# TERM_PROGRAM=vscode covers local terminals; VSCODE_INJECTION covers
# devcontainers/SSH remotes where TERM_PROGRAM isn't propagated.
if [[ "$TERM_PROGRAM" == "vscode" || -n "$VSCODE_INJECTION" ]]; then
  _AILOCAL_VSCODE=1
  # oh-my-zsh + p10k rewrite the prompt at instant-prompt time, which corrupts
  # VS Code's OSC 633 command-completion markers → agent terminal commands hang.
  typeset -g POWERLEVEL9K_INSTANT_PROMPT=off
fi

# ── claude-local / codex-local wrappers ─────────────────────────────────────
# Process-scoped env only — never sourced into the calling shell, so plain
# `claude` / `codex` in this same terminal stay pointed at the cloud.

claude-local() {
  local cfg="${XDG_CONFIG_HOME:-$HOME/.config}/ailocal"
  local base key
  base=$(grep '^AILOCAL_BASE_URL=' "$cfg/env" 2>/dev/null | cut -d= -f2-)
  key=$(grep '^AILOCAL_API_KEY=' "$cfg/env" 2>/dev/null | cut -d= -f2-)
  if [[ -z "$base" || -z "$key" ]]; then
    echo "claude-local: ${cfg}/env missing or incomplete — run ./scripts/install-clients.sh claude" >&2
    return 1
  fi
  # CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY: on launch, Claude Code GETs
  # $ANTHROPIC_BASE_URL/v1/models and adds every LiteLLM role (coder-main,
  # coder-agent, coder-fast, deep-think, deep-think-more, supervisor) to the
  # /model picker ("From gateway"), alongside the built-in Opus/Sonnet/Haiku
  # entries. The three ANTHROPIC_DEFAULT_*_MODEL vars remap those built-in slots
  # onto real local roles so the default model AND Claude Code's silent
  # background/summary calls (the "Haiku" slot) resolve to something LiteLLM
  # actually serves. (Requires Claude Code v2.1.129+ for gateway discovery.)
  #   Opus  → deep-think-more (deepest reasoning tier)
  #   Sonnet→ coder-main      (primary heavy coder, the daily driver)
  #   Haiku → coder-fast      (fast, for background/summary calls)
  CLAUDE_CONFIG_DIR="$cfg/claude" \
  ANTHROPIC_BASE_URL="$base" ANTHROPIC_API_KEY="$key" \
  CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY=1 \
  ANTHROPIC_DEFAULT_OPUS_MODEL="deep-think-more" \
  ANTHROPIC_DEFAULT_SONNET_MODEL="coder-main" \
  ANTHROPIC_DEFAULT_HAIKU_MODEL="coder-fast" \
  command claude "$@"
}

codex-local() {
  local cfg="${XDG_CONFIG_HOME:-$HOME/.config}/ailocal"
  local base key
  base=$(grep '^AILOCAL_BASE_URL=' "$cfg/env" 2>/dev/null | cut -d= -f2-)
  key=$(grep '^AILOCAL_API_KEY=' "$cfg/env" 2>/dev/null | cut -d= -f2-)
  if [[ -z "$base" || -z "$key" ]]; then
    echo "codex-local: ${cfg}/env missing or incomplete — run ./scripts/install-clients.sh codex" >&2
    return 1
  fi
  CODEX_HOME="$cfg/codex" OPENAI_API_KEY="$key" OPENAI_BASE_URL="$base/v1" command codex "$@"
}

ailocal-code() { code --profile ailocal "${1:-.}"; }
