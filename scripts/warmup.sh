#!/usr/bin/env bash
# warmup.sh — pay initialization cost before the user's first request, and
# MEASURE what that actually buys.
#
# Warmup is the kind of optimisation that invites unmeasured claims ("hides
# startup latency"), so every target reports two real timings and the delta
# between them. A target whose second call is no faster is reported as such
# rather than quietly kept.
#
# READ THE COLUMNS LITERALLY. They are CALL1 and CALL2, not "cold" and "warm".
# CALL1 is only genuinely cold when the model has actually been evicted — after
# a proxy restart, after keep_alive expiry, or after `ollama stop`. Run this
# twice in a row and CALL1 is already warm, which is why the first run of this
# script showed architecture 3842->1364 ms (a real cold load) and the second
# showed 1353->1321 ms (both warm). Labelling those 32 ms as a warmup "saving"
# would have been a measurement artefact.
#
# Targets are declared in config/litellm/registry.yaml (model_classes with
# routing_hints.warmup: true, and clients with a `warmup:` list), so adding one
# is a registry edit — this script asks what to warm, it does not decide.
#
# WHAT THIS CANNOT WARM, AND WHY
#   LSP (mcp__lsp__*)  The language server is spawned BY THE CLIENT as an MCP
#                      subprocess, not by the proxy. Nothing server-side can
#                      pre-start it: a warm pyright in this script's process
#                      tree is not the one Claude Code will talk to. Declared in
#                      the registry as a client warmup hint so the intent is
#                      recorded, but this script reports it as unwarmable rather
#                      than pretending.
#
# Usage: ./scripts/warmup.sh [--quiet]
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
QUIET=""
[ "${1:-}" = "--quiet" ] && QUIET=1

OLLAMA="${OLLAMA_HOST_URL:-http://127.0.0.1:11434}"
PROXY="${AILOCAL_PROXY_URL:-http://127.0.0.1:4000}"

say() { [ -n "$QUIET" ] || printf '%s\n' "$*"; }
info() { [ -n "$QUIET" ] || printf '\033[1;34m==>\033[0m %s\n' "$*"; }

now_ms() { python3 -c 'import time;print(int(time.time()*1000))'; }

# Millisecond timing around a command, ignoring its output. Returns "-1" when the
# command fails, so a failed probe can never be reported as a fast one.
#
# Every curl below MUST pass --fail. Without it curl exits 0 on HTTP 400/500, so
# an erroring probe is timed as a very fast success — which is how an embeddings
# model probed against /v1/chat/completions first reported "124 ms warm" instead
# of "FAIL". The guarantee in the line above was false until --fail was added.
timed() {
  local start end
  start=$(now_ms)
  if "$@" >/dev/null 2>&1; then
    end=$(now_ms); echo $((end - start))
  else
    echo "-1"
  fi
}

KEY="$(grep -E '^LITELLM_MASTER_KEY=' .env 2>/dev/null | cut -d= -f2-)"

# ── which models does the registry want warm? ───────────────────────────────
# Asked of the registry inside the container, so this script holds no model list.
warm_models() {
  # -i is REQUIRED: `docker exec` without it does not forward stdin, so a
  # heredoc is silently discarded and the script sees empty output — which reads
  # as "the registry is unavailable" rather than "I forgot a flag".
  docker exec -i ailocal-litellm python - <<'PY' 2>/dev/null
import importlib.util, sys, json
s = importlib.util.spec_from_file_location(
    "capability_registry", "/app/config/capability_registry.py")
m = importlib.util.module_from_spec(s); sys.modules["capability_registry"] = m
s.loader.exec_module(m)
reg = m.Registry(path="/app/config/registry.yaml",
                 caps_json="/app/ailocal-config/capabilities.generated.json",
                 config_path="/app/config/config.yaml")
out = []
for name, spec in (reg.doc.get("model_classes") or {}).items():
    if not (spec.get("routing_hints") or {}).get("warmup"):
        continue
    for cap in spec.get("match_capabilities") or []:
        out.append("ailocal-" + cap)
print(json.dumps(sorted(set(out))))
PY
}

MODELS_JSON="$(warm_models)"
if [ -z "$MODELS_JSON" ]; then
  echo "Could not ask the registry which models to warm (is ailocal-litellm up?)."
  echo "Refusing to guess a model list — that is exactly what the registry exists"
  echo "to prevent."
  exit 1
fi
MODELS="$(printf '%s' "$MODELS_JSON" | python3 -c 'import sys,json;print(" ".join(json.load(sys.stdin)))')"

printf '%-34s %9s %9s %9s\n' TARGET CALL1_ms CALL2_ms DELTA_ms
printf '%-34s %9s %9s %9s\n' "----------------------------------" --------- --------- ---------

total_saved=0

# ── Ollama: is the model resident ───────────────────────────────────────────
for model in $MODELS; do
  # Embeddings have their own endpoint and are probed separately below. Sending
  # them to /v1/chat/completions produced a duplicate row AND (before --fail) a
  # bogus fast timing for a request that had actually 400ed.
  case "$model" in *embeddings*) continue ;; esac
  # Route through the PROXY, not straight at Ollama: that warms the whole path
  # the client will use — model residency, the router, the persona hook, the
  # gateway's tiktoken encoding — not just the model file.
  cold=$(timed curl -sf -m 300 "$PROXY/v1/chat/completions" \
    -H "Authorization: Bearer $KEY" -H 'content-type: application/json' \
    -d "{\"model\":\"$model\",\"max_tokens\":1,\"messages\":[{\"role\":\"user\",\"content\":\"warm\"}]}")
  warm=$(timed curl -sf -m 300 "$PROXY/v1/chat/completions" \
    -H "Authorization: Bearer $KEY" -H 'content-type: application/json' \
    -d "{\"model\":\"$model\",\"max_tokens\":1,\"messages\":[{\"role\":\"user\",\"content\":\"warm\"}]}")
  if [ "$cold" = "-1" ] || [ "$warm" = "-1" ]; then
    printf '%-34s %9s %9s %9s\n' "$model" "FAIL" "FAIL" "-"
    continue
  fi
  saved=$((cold - warm))
  printf '%-34s %9s %9s %9s\n' "$model" "$cold" "$warm" "$saved"
  [ "$saved" -gt 0 ] && total_saved=$((total_saved + saved))
done

# ── embeddings (Cadence's index depends on this staying resident) ────────────
EMB_MODEL="$(docker exec ailocal-litellm python -c "
import json;print(json.load(open('/app/ailocal-config/capabilities.generated.json'))['capabilities'][-1]['name'])" 2>/dev/null)"
if [ -n "$EMB_MODEL" ]; then
  cold=$(timed curl -sf -m 120 "$PROXY/v1/embeddings" \
    -H "Authorization: Bearer $KEY" -H 'content-type: application/json' \
    -d "{\"model\":\"ailocal-$EMB_MODEL\",\"input\":\"warm\"}")
  warm=$(timed curl -sf -m 120 "$PROXY/v1/embeddings" \
    -H "Authorization: Bearer $KEY" -H 'content-type: application/json' \
    -d "{\"model\":\"ailocal-$EMB_MODEL\",\"input\":\"warm\"}")
  if [ "$cold" != "-1" ] && [ "$warm" != "-1" ]; then
    printf '%-34s %9s %9s %9s\n' "ailocal-$EMB_MODEL" "$cold" "$warm" "$((cold - warm))"
  else
    printf '%-34s %9s %9s %9s\n' "ailocal-$EMB_MODEL" "FAIL" "FAIL" "-"
  fi
fi

# ── grepai semantic index ───────────────────────────────────────────────────
if command -v grepai >/dev/null 2>&1; then
  cold=$(timed grepai search "warmup probe" -n 1)
  warm=$(timed grepai search "warmup probe" -n 1)
  if [ "$cold" != "-1" ]; then
    printf '%-34s %9s %9s %9s\n' "grepai index" "$cold" "$warm" "$((cold - warm))"
  else
    printf '%-34s %9s %9s %9s\n' "grepai index" "FAIL" "FAIL" "-"
  fi
else
  printf '%-34s %9s %9s %9s\n' "grepai index" "absent" "-" "-"
fi

# ── what cannot be warmed from here ─────────────────────────────────────────
printf '%-34s %9s %9s %9s\n' "LSP (client-spawned MCP)" "n/a" "n/a" "n/a"

echo
say "LSP is spawned by the CLIENT as an MCP subprocess. Nothing server-side can"
say "pre-start it — a warm language server in this process tree is not the one"
say "the client will talk to. Reported as n/a rather than claimed as warmed."
echo
say "Observed on a genuinely cold run (models evicted):"
say "  ailocal-architecture  3842 -> 1364 ms   (2478 ms of model load avoided)"
say "  ailocal-completion    1701 ->  317 ms   (1384 ms)"
say "  grepai index           164 ->   47 ms   ( 117 ms)"
say "On an already-warm run the deltas collapse to noise, as they should."
echo
say "The proxy's own first-request cost (~42 ms, tiktoken loading its encoding"
say "lazily) is inside CALL1 and is real but small next to model residency."
