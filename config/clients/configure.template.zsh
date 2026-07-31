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
  # $ANTHROPIC_BASE_URL/v1/models and adds every LiteLLM model (ailocal-architecture,
  # ailocal-implementation, ailocal-review, ailocal-completion, ailocal-embeddings) to
  # the /model picker ("From gateway"), alongside the built-in tier entries. The
  # ANTHROPIC_DEFAULT_*_MODEL vars remap those built-in slots onto real
  # capabilities so the default model AND Claude Code's silent background/summary
  # calls (the "Haiku" slot) resolve to something LiteLLM actually serves.
  # (Requires Claude Code v2.1.129+ for gateway discovery.)
  #
  # The slot block below is GENERATED from config/clients.yaml `claude.slots` by
  # sync-models.py — do not hand-edit it. It used to be maintained by hand and
  # drifted: HAIKU pointed at ailocal-completion (the 4096-token FIM tier), so
  # every background call and every `model: haiku` subagent hard-400'd with
  # "No models have context window large enough" (measured Got=10813, Max=4096).
  # FABLE was missing entirely, so the `reviewer` subagent never reached the
  # review tier. Generating it keeps the wrapper and clients.yaml in lockstep.
  #
  # CLAUDE_CODE_DISABLE_1M_CONTEXT: local backends cap at num_ctx (64K here), not
  # 1M — so let Claude Code request the 1M-context beta and it just overflows.
  # Disabling it keeps sessions inside the window the models actually serve.
  # Slot vars live in an array (not the env-assignment chain) so the generated
  # region can carry its own marker comments — a `#` line inside a `\`
  # continuation would comment out the rest of the command.
  local -a slots
  # >>> BEGIN GENERATED claude slots (sync-models.py) — do not edit <<<
  slots=(
    ANTHROPIC_DEFAULT_OPUS_MODEL="ailocal-architecture"
    ANTHROPIC_DEFAULT_SONNET_MODEL="ailocal-implementation"
    ANTHROPIC_DEFAULT_HAIKU_MODEL="ailocal-fast"
    ANTHROPIC_DEFAULT_FABLE_MODEL="ailocal-review"
  )
  # >>> END GENERATED claude slots <<<
  env "${slots[@]}" \
    CLAUDE_CONFIG_DIR="$cfg/claude" \
    ANTHROPIC_BASE_URL="$base" ANTHROPIC_API_KEY="$key" \
    CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY=1 \
    CLAUDE_CODE_DISABLE_1M_CONTEXT=1 \
    claude "$@"
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
