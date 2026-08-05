#!/usr/bin/env bash
# claude-local role alias overrides.
#
# Dry-run only: a stub `claude` on PATH prints the slot variables it was launched
# with, so the EFFECTIVE environment is inspected without any model inference.
# The stub must be a real executable, not a shell function — claude-local calls
# `env "${slots[@]}" ... claude`, and env execs a binary.
set -uo pipefail
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/harness.sh"
CONFIGURE="$("$ROOT_DIR/scripts/profile-config" state-root)/clients/configure.zsh"

STUB="$(mktemp -d)"; trap 'rm -rf "$STUB"' EXIT
cat > "$STUB/claude" <<'EOF'
#!/bin/sh
echo "OPUS=$ANTHROPIC_DEFAULT_OPUS_MODEL"
echo "SONNET=$ANTHROPIC_DEFAULT_SONNET_MODEL"
echo "HAIKU=$ANTHROPIC_DEFAULT_HAIKU_MODEL"
echo "FABLE=$ANTHROPIC_DEFAULT_FABLE_MODEL"
EOF
chmod +x "$STUB/claude"

# Runs claude-local with the stub first on PATH. Prints its output; returns rc.
run() { env "$@" PATH="$STUB:$PATH" zsh -c "source '$CONFIGURE' >/dev/null 2>&1; claude-local --dry" 2>&1; }

echo "client role alias overrides"

out="$(run AILOCAL_UNUSED=1)"
check $([ "$(grep -c '^OPUS=ailocal-architecture$' <<<"$out")" = 1 ] && echo 0 || echo 1) \
  "no override: architecture slot keeps its production alias" "$out"
check $([ "$(grep -c '^SONNET=ailocal-implementation$' <<<"$out")" = 1 ] && echo 0 || echo 1) \
  "no override: implementation slot unchanged" "$out"
check $([ "$(grep -c '^HAIKU=ailocal-fast$' <<<"$out")" = 1 ] && echo 0 || echo 1) \
  "no override: fast slot unchanged" "$out"
check $([ "$(grep -c '^FABLE=ailocal-review$' <<<"$out")" = 1 ] && echo 0 || echo 1) \
  "no override: review slot unchanged" "$out"

# A production alias is used as the "valid" target so the test needs no
# temporary alias and no LiteLLM mutation.
out="$(run AILOCAL_ARCHITECTURE_ALIAS_OVERRIDE=ailocal-review)"
check $([ "$(grep -c '^OPUS=ailocal-review$' <<<"$out")" = 1 ] && echo 0 || echo 1) \
  "valid override reaches the client command environment" "$out"
check $([ "$(grep -c '^SONNET=ailocal-implementation$' <<<"$out")" = 1 ] && echo 0 || echo 1) \
  "overriding architecture leaves implementation untouched" "$out"
check $([ "$(grep -c '^HAIKU=ailocal-fast$' <<<"$out")" = 1 ] && echo 0 || echo 1) \
  "overriding architecture leaves fast untouched" "$out"
check $([ "$(grep -c '^FABLE=ailocal-review$' <<<"$out")" = 1 ] && echo 0 || echo 1) \
  "overriding architecture leaves review untouched" "$out"

# Fail closed. Falling back to production here would silently measure the
# production model while reporting the candidate's name.
out="$(run AILOCAL_ARCHITECTURE_ALIAS_OVERRIDE=bench-does-not-exist)"; rc=$?
check $([ "$rc" != 0 ] && echo 0 || echo 1) \
  "unknown override fails before launching the client" "rc=$rc"
check $([ "$(grep -c '^OPUS=' <<<"$out")" = 0 ] && echo 0 || echo 1) \
  "unknown override never reaches the client at all" "$out"
check $(grep -q "not served by LiteLLM" <<<"$out" && echo 0 || echo 1) \
  "unknown override explains itself on stderr" "$out"

# OUTCOME, not configuration. The previous suite proved the slot variable reached
# the client and passed while nine turns silently served the production model:
# settings.json pins `model`, which OUTRANKS ANTHROPIC_DEFAULT_*. Verified
# precedence (code.claude.com/docs/en/settings):
#   --model > settings.json "model" > ANTHROPIC_DEFAULT_*_MODEL
tpl="$ROOT_DIR/config/clients/configure.template.zsh"
check $(grep -q -- '--model "$AILOCAL_ARCHITECTURE_ALIAS_OVERRIDE"' "$tpl" && echo 0 || echo 1) \
  "architecture override passes --model, the highest-precedence mechanism"
check $(grep -q 'claude "${_model_args\[@\]}" "$@"' "$tpl" && echo 0 || echo 1) \
  "--model args reach the claude invocation"
check $([ -z "$(AILOCAL_ARCHITECTURE_ALIAS_OVERRIDE= zsh -c "source '$CONFIGURE'; typeset -p _model_args" 2>/dev/null)" ] && echo 0 || echo 1) \
  "no override adds no --model argument (defaults untouched)"
# The proxy log is the only authority on which model actually ran. Asserted here
# as a capability; the benchmark calls it live before every candidate.
# Asserted through the IMPORT surface, not by grepping a file: these moved to
# benchmark_clients.py in the module split and the old grep asserted their
# location rather than their existence. Callers reach them via `import
# benchmark`, so that is what is checked.
check $(python3 -c "
import sys, inspect; sys.path.insert(0, '$ROOT_DIR/scripts/lib')
import benchmark as B
assert callable(B.served_models_since)
" >/dev/null 2>&1 && echo 0 || echo 1) \
  "harness can read served aliases from the proxy log"
check $(python3 -c "
import sys, inspect; sys.path.insert(0, '$ROOT_DIR/scripts/lib')
import benchmark as B
assert 'INVALID_ROUTING' in inspect.getsource(B.verify_routing)
" >/dev/null 2>&1 && echo 0 || echo 1) \
  "routing mismatch is classified INVALID_ROUTING, not warned about"

# The override block is hand-maintained and MUST live outside the spliced region,
# or sync-models.py would erase it on the next regeneration.
tpl="$ROOT_DIR/config/clients/configure.template.zsh"
gen_begin=$(grep -n "BEGIN GENERATED claude slots" "$tpl" | cut -d: -f1)
gen_end=$(grep -n "END GENERATED claude slots" "$tpl" | cut -d: -f1)
ovr=$(grep -n "_ailocal_ovr=(" "$tpl" | cut -d: -f1)
check $([ -n "$ovr" ] && [ "$ovr" -gt "$gen_end" ] && echo 0 || echo 1) \
  "override logic sits outside the generated region" "ovr=$ovr gen=$gen_begin-$gen_end"
check $([ "$(grep -c 'AILOCAL_ARCHITECTURE_ALIAS_OVERRIDE' "$CONFIGURE")" -ge 1 ] && echo 0 || echo 1) \
  "override logic survives sync-models.py regeneration"

report || exit 1
