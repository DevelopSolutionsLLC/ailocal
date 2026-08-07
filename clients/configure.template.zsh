# ailocal configure.zsh — sourced at the TOP of ~/.zshrc (before p10k instant
# prompt). Managed file — installed by lib/install-clients.sh, always
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
    echo "claude-local: ${cfg}/env missing or incomplete — run ailocal clients claude" >&2
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
  # The slot block below is GENERATED from profiles/clients.toml `claude.slots` by
  # sync-models.py — do not hand-edit it. Hand-maintained slots drift: pointing
  # HAIKU at a small FIM tier hard-400s every background call, and a missing
  # FABLE silently strands the `reviewer` subagent off the review tier.
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

  # ── Role alias overrides (hand-maintained; OUTSIDE the generated region) ──
  # Point ONE role at an explicit, already-existing LiteLLM alias for this
  # process only. Defaults stay profile-controlled: with no override set, the
  # generated slots above are used verbatim.
  #
  # Exists because the slot names are generated and applied through `env`, which
  # overrides the inherited environment — so there was no supported way to ask
  # "run claude-local, but with the architecture role on THAT model". Needed for
  # client-compatibility testing and model comparisons, where creating a second
  # alias named ailocal-architecture would leave LiteLLM with a duplicate
  # model_name and an ambiguous choice between two backends.
  #
  #   AILOCAL_ARCHITECTURE_ALIAS_OVERRIDE=bench-gemma4-26b-mlx-off-32k claude-local ...
  #
  # FAIL CLOSED: an override naming an alias LiteLLM does not serve aborts the
  # launch. Falling back to production would silently measure the wrong model.
  local -A _ailocal_ovr=(
    ANTHROPIC_DEFAULT_OPUS_MODEL   "${AILOCAL_ARCHITECTURE_ALIAS_OVERRIDE:-}"
    ANTHROPIC_DEFAULT_SONNET_MODEL "${AILOCAL_IMPLEMENTATION_ALIAS_OVERRIDE:-}"
    ANTHROPIC_DEFAULT_HAIKU_MODEL  "${AILOCAL_FAST_ALIAS_OVERRIDE:-}"
    ANTHROPIC_DEFAULT_FABLE_MODEL  "${AILOCAL_REVIEW_ALIAS_OVERRIDE:-}"
  )
  # Precedence (code.claude.com/docs/en/settings):
  #   --model  >  settings.json "model"  >  ANTHROPIC_DEFAULT_*_MODEL
  # Our settings.json pins model=ailocal-architecture, so the slot vars alone are
  # silently outranked — a benchmark override propagated perfectly and served the
  # production model anyway. The architecture override therefore also passes
  # --model, the highest-priority supported mechanism. The slot rewrite below
  # still matters: it redirects the subagent/background tiers.
  local -a _model_args=()
  [[ -n "${AILOCAL_ARCHITECTURE_ALIAS_OVERRIDE:-}" ]] && \
    _model_args=(--model "$AILOCAL_ARCHITECTURE_ALIAS_OVERRIDE")
  local _ailocal_models="" _i _name _val
  for _name in ${(k)_ailocal_ovr}; do
    [[ -n "${_ailocal_ovr[$_name]}" ]] || continue
    # One catalogue fetch, only when an override is actually supplied.
    if [[ -z "$_ailocal_models" ]]; then
      # Bounded retry: the catalogue is served by the same proxy that may be
      # mid-request for someone else, so a single attempt turns transient load
      # into a hard launch failure. Four attempts, ~5s each, then fail closed.
      _ailocal_models=$(curl -fsS --max-time 5 \
        --retry 3 --retry-delay 1 --retry-connrefused --retry-all-errors \
        -H "Authorization: Bearer $key" \
        "$base/v1/models" 2>/dev/null) || {
        echo "claude-local: cannot reach $base/v1/models to validate an alias override" >&2
        return 1
      }
    fi
    _val="${_ailocal_ovr[$_name]}"
    if [[ "$_ailocal_models" != *"\"$_val\""* ]]; then
      echo "claude-local: alias override '$_val' ($_name) is not served by LiteLLM" >&2
      return 1
    fi
    for (( _i = 1; _i <= ${#slots[@]}; _i++ )); do
      [[ "${slots[_i]%%=*}" == "$_name" ]] && slots[_i]="$_name=$_val"
    done
  done

  # API_TIMEOUT_MS matches LiteLLM's own `timeout: 900` (config.template.yaml).
  # This is why the architecture route appeared to "crash" after
  # 10-15 minutes: COLD prompt evaluation on this route is super-linear --
  #   27,791 tok -> 85 s      57,791 tok -> 341 s      87,791 tok -> 789 s
  # (326 -> 170 -> 111 tok/s; it gets SLOWER per token as the prompt grows).
  # A grown session that misses the KV cache therefore stalls for 13+ minutes
  # before its first byte. With no client timeout set, Claude Code gave up on its
  # own undocumented default while LiteLLM waited 900 s and Ollama kept
  # generating -- visible in ollama's log as "aborting completion request due to
  # client closing the connection". Client and proxy now agree on one number, so
  # the wait is bounded and identical at both ends instead of silently mismatched.
  # This is NOT memory: measured at 26-57% free, swap flat, no OOM and no crash.
  env "${slots[@]}" \
    CLAUDE_CONFIG_DIR="$cfg/claude" \
    ANTHROPIC_BASE_URL="$base" ANTHROPIC_API_KEY="$key" \
    CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY=1 \
    CLAUDE_CODE_DISABLE_1M_CONTEXT=1 \
    API_TIMEOUT_MS="${AILOCAL_API_TIMEOUT_MS:-900000}" \
    claude "${_model_args[@]}" "$@"
}

codex-local() {
  local cfg="${XDG_CONFIG_HOME:-$HOME/.config}/ailocal"
  local base key
  base=$(grep '^AILOCAL_BASE_URL=' "$cfg/env" 2>/dev/null | cut -d= -f2-)
  key=$(grep '^AILOCAL_API_KEY=' "$cfg/env" 2>/dev/null | cut -d= -f2-)
  if [[ -z "$base" || -z "$key" ]]; then
    echo "codex-local: ${cfg}/env missing or incomplete — run ailocal clients codex" >&2
    return 1
  fi
  CODEX_HOME="$cfg/codex" OPENAI_API_KEY="$key" OPENAI_BASE_URL="$base/v1" command codex "$@"
}

ailocal-code() { code --profile ailocal "${1:-.}"; }
