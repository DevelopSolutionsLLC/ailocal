#!/usr/bin/env bash
# validate-deployment.sh — prove the ailocal deployment is real, not just present.
#
#   0 = all checks passed
#   1 = at least one check failed
#
# Design note: never report a capability as working on the strength of a file
# existing. Every check either probes the running system or is a static check
# of the file the runtime actually loads.
set -uo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT_DIR"
AILOCAL_ROOT="$ROOT_DIR"
. "$ROOT_DIR/scripts/lib/compose.sh"

FAILS=0
ok()   { echo "  ✓ $*"; }
bad()  { echo "  ✗ $*" >&2; FAILS=$((FAILS+1)); }
step() { echo; echo "▶ $*"; }

LITELLM_IMAGE="ghcr.io/berriai/litellm:main-stable"
# Parse YAML with the LiteLLM image's own Python: the host python3 has no
# pyyaml, and the previous version of this script mistook that for invalid YAML.
ypy() { docker run --rm --entrypoint python3 -v "$ROOT_DIR:/w:ro" "$LITELLM_IMAGE" -c "$1"; }

# ── 1. Deployment layout (static) ──────────────────────────────────────────
step "Deployment layout"

for f in deploy/litellm/docker-compose.yml deploy/searxng/docker-compose.yml \
         deploy/searxng/settings.yml config/litellm/config.yaml scripts/lib/compose.sh; do
  [ -f "$f" ] && ok "$f" || bad "missing $f"
done

if [ -f docker-compose.yml ]; then
  bad "root docker-compose.yml exists — the stack must be defined only under deploy/"
else
  ok "no root docker-compose.yml (deploy/ is canonical)"
fi

strays=$(find deploy \( -name '*.backup' -o -name '*.bak' -o -name 'config.yaml*' \
         -o -name '__pycache__' \) 2>/dev/null)
[ -z "$strays" ] && ok "no stale duplicates under deploy/" \
                 || bad "stale duplicates under deploy/: $(echo "$strays" | tr '\n' ' ')"

# ── 2. Compose validity + single project (static) ──────────────────────────
step "Compose configuration"

if dc config >/dev/null 2>&1; then
  ok "merged compose config is valid"
else
  bad "merged compose config is INVALID:"; dc config 2>&1 | tail -5 >&2
fi

cfgjson=$(dc config --format json 2>/dev/null)

proj=$(echo "$cfgjson" | python3 -c 'import sys,json;print(json.load(sys.stdin).get("name",""))' 2>/dev/null)
[ "$proj" = "ailocal" ] && ok "single compose project: ailocal" \
                        || bad "compose project is '${proj:-?}', expected 'ailocal'"

# Both services must sit on the same network or service discovery breaks.
nets=$(echo "$cfgjson" | python3 -c '
import sys,json
svcs=json.load(sys.stdin).get("services",{})
for n in ("litellm","searxng"):
    print(n, ",".join(sorted((svcs.get(n) or {}).get("networks",{}) or {})))' 2>/dev/null)
if [ -n "$nets" ] && [ "$(echo "$nets" | awk '{print $2}' | sort -u | wc -l)" -eq 1 ]; then
  ok "litellm and searxng share network: $(echo "$nets" | head -1 | awk '{print $2}')"
else
  bad "litellm and searxng are NOT on a shared network: $(echo "$nets" | tr '\n' ' ')"
fi

# The api_base LiteLLM is configured with must match SearXNG's service name.
svcname=$(echo "$cfgjson" | python3 -c 'import sys,json;print("searxng" if "searxng" in json.load(sys.stdin).get("services",{}) else "")' 2>/dev/null)
if grep -q 'api_base: *http://searxng:8080' config/litellm/config.yaml && [ "$svcname" = searxng ]; then
  ok "search api_base matches the searxng compose service name"
else
  bad "search api_base and the searxng service name do not agree"
fi

# Localhost-only binding.
pubs=$(echo "$cfgjson" | python3 -c '
import sys,json
for n,s in json.load(sys.stdin).get("services",{}).items():
    for p in s.get("ports",[]) or []:
        print(n, p.get("host_ip","0.0.0.0"), p.get("published"))' 2>/dev/null)
if [ -n "$pubs" ] && echo "$pubs" | grep -qv '127\.0\.0\.1'; then
  bad "service(s) published beyond localhost:"; echo "$pubs" | grep -v '127\.0\.0\.1' >&2
else
  ok "all published ports bound to 127.0.0.1"
fi

# ── 3. LiteLLM config semantics (static, parsed) ───────────────────────────
step "LiteLLM config.yaml"

TMP=$(mktemp)
ypy '
import yaml
class S(yaml.SafeLoader): pass
def nodup(l,n,deep=False):
    seen=set()
    for k,_ in n.value:
        key=l.construct_object(k,deep=deep)
        if key in seen: print("DUP",key)
        seen.add(key)
    return l.construct_mapping(n,deep)
S.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,nodup)
d=yaml.load(open("/w/config/litellm/config.yaml"),S)
ls=d.get("litellm_settings") or {}
print("MODELS", ",".join(sorted({m["model_name"] for m in d.get("model_list",[])})))
print("CALLBACKS", ",".join(ls.get("callbacks") or []))
# search_tools MUST be top-level: proxy_server.parse_search_tools() reads
# config["search_tools"] then general_settings — never litellm_settings.
print("SEARCHTOOLS_TOP", bool(d.get("search_tools")))
print("SEARCHTOOLS_NESTED", bool(ls.get("search_tools")))
print("WSI", bool(ls.get("websearch_interception_params")))
' >"$TMP" 2>"$TMP.err"

if [ -s "$TMP" ]; then
  grep -q '^DUP' "$TMP" && bad "duplicate YAML keys: $(grep '^DUP' "$TMP" | tr '\n' ' ')" \
                        || ok "no duplicate YAML keys"
  for m in ailocal-architecture ailocal-implementation ailocal-review ailocal-completion ailocal-embeddings; do
    grep -q "$m" "$TMP" && ok "model_list has $m" || bad "model_list missing $m"
  done
  grep -q 'SEARCHTOOLS_TOP True' "$TMP" && ok "search_tools is top-level" \
    || bad "search_tools NOT top-level — LiteLLM will register ZERO search tools"
  grep -q 'SEARCHTOOLS_NESTED True' "$TMP" \
    && bad "search_tools also nested under litellm_settings (ignored — remove it)" \
    || ok "search_tools not misplaced under litellm_settings"
  grep -q 'WSI True' "$TMP" && ok "websearch_interception_params present" \
                            || bad "websearch_interception_params missing"
  grep -q 'CALLBACKS.*websearch_interception' "$TMP" && ok "websearch_interception callback enabled" \
                            || bad "websearch_interception not in callbacks"
  grep -q 'CALLBACKS.*persona_injector' "$TMP" && ok "persona_injector callback enabled" \
                            || bad "persona_injector not in callbacks"
  grep -q 'CALLBACKS.*model_registrar' "$TMP" && ok "model_registrar callback enabled" \
                            || bad "model_registrar not in callbacks"
  grep -q 'CALLBACKS.*tool_repair' "$TMP" && ok "tool_repair callback enabled" \
                            || bad "tool_repair not in callbacks (malformed tool calls will stall agents)"
else
  bad "could not parse config/litellm/config.yaml"; cat "$TMP.err" >&2
fi
rm -f "$TMP" "$TMP.err"

# Clients must never see raw backend tags as a model_name.
if grep -qE '^\s*-\s*model_name:\s*(ollama_chat|ollama)/' config/litellm/config.yaml; then
  bad "config exposes a raw backend tag as model_name (use ailocal-* capabilities)"
else
  ok "no raw backend tags exposed as model_name"
fi

# ── 4. Runtime ─────────────────────────────────────────────────────────────
step "Runtime"

running=$(dc ps --services --filter status=running 2>/dev/null | tr '\n' ' ')
echo "$running" | grep -q litellm && ok "litellm running" || bad "litellm NOT running"
echo "$running" | grep -q searxng && ok "searxng running" || bad "searxng NOT running"

for c in ailocal-litellm ailocal-searxng; do
  h=$(docker inspect -f '{{.State.Health.Status}}' "$c" 2>/dev/null)
  [ "$h" = healthy ] && ok "$c healthy" || bad "$c health=${h:-unknown}"
done

# The mounted config must BE the file validated above, not a stale copy.
# Hash via python3: the LiteLLM image has no `test`/`md5sum` binaries (cap_drop
# ALL + a slim base), so shell-builtin-style checks give false failures here.
hh=$(docker exec ailocal-litellm python3 -c \
  "import hashlib;print(hashlib.md5(open('/app/config/config.yaml','rb').read()).hexdigest())" 2>/dev/null)
lh=$(md5 -q config/litellm/config.yaml 2>/dev/null || md5sum config/litellm/config.yaml | awk '{print $1}')
if [ -z "$hh" ]; then
  bad "/app/config/config.yaml is not readable in the container"
elif [ "$hh" = "$lh" ]; then
  ok "container is running the repo's config.yaml"
else
  bad "MOUNT DRIFT: container config.yaml != config/litellm/config.yaml"
fi

# The search tool must actually have registered at boot.
# NOTE: use a bash pattern match, NOT a pipe into `grep -q`. Under `pipefail`
# any `<producer> | grep -q` is a latent SIGPIPE bug: grep -q exits on first
# match and the producer dies with 141, failing the pipeline despite the match.
# Capturing to a variable first does NOT fix it — it just moves the SIGPIPE to
# `echo` once the log outgrows the pipe buffer (this bit us at ~188KB).
bootlog=$(docker logs ailocal-litellm 2>&1)
if [[ "$bootlog" == *searxng-search* ]]; then
  ok "LiteLLM registered search tool searxng-search at boot"
else
  bad "LiteLLM did NOT register searxng-search (check search_tools placement)"
fi

# ── 5. Connectivity ────────────────────────────────────────────────────────
step "Connectivity"

curl -sSf --max-time 5 http://127.0.0.1:4000/health/liveliness >/dev/null 2>&1 \
  && ok "LiteLLM /health/liveliness" || bad "LiteLLM not answering on :4000"

curl -sSf --max-time 5 http://127.0.0.1:11434/api/tags >/dev/null 2>&1 \
  && ok "Ollama reachable from host" || bad "Ollama not reachable on :11434"

# Ollama is host-native; the container reaches it via host.docker.internal.
docker exec ailocal-litellm python3 -c "
import urllib.request;urllib.request.urlopen('http://host.docker.internal:11434/api/tags',timeout=5)" 2>/dev/null \
  && ok "Ollama reachable FROM the litellm container" \
  || bad "litellm container cannot reach Ollama — all model calls will fail"

docker exec ailocal-litellm python3 -c "
import urllib.request,json,sys
d=json.load(urllib.request.urlopen('http://searxng:8080/search?q=test&format=json',timeout=20))
sys.exit(0 if d.get('results') else 1)" 2>/dev/null \
  && ok "litellm -> http://searxng:8080 returns JSON results" \
  || bad "litellm cannot get JSON from searxng (settings.yml search.formats must include json)"

# ── 6. End to end through the proxy ────────────────────────────────────────
step "End-to-end through the proxy"

KEY=$(grep '^LITELLM_MASTER_KEY=' .env 2>/dev/null | cut -d= -f2- | tr -d '"'\'' ')
if [ -z "$KEY" ]; then
  bad "LITELLM_MASTER_KEY not found in .env — skipping authenticated checks"
else
  models=$(curl -s --max-time 10 -H "Authorization: Bearer $KEY" http://127.0.0.1:4000/v1/models 2>/dev/null)
  for m in ailocal-architecture ailocal-implementation ailocal-review ailocal-completion ailocal-embeddings; do
    [[ "$models" == *"\"$m\""* ]] && ok "/v1/models exposes $m" || bad "/v1/models missing $m"
  done
  # Compat aliases the clients hard-code.
  for m in claude-sonnet-4-6 gpt-4o; do
    [[ "$models" == *"\"$m\""* ]] && ok "/v1/models exposes alias $m" || bad "/v1/models missing alias $m"
  done

  resp=$(curl -s --max-time 300 http://127.0.0.1:4000/v1/chat/completions \
    -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
    -d '{"model":"ailocal-implementation","messages":[{"role":"user","content":"Reply with exactly: OK"}],"max_tokens":16}')
  [[ "$resp" == *'"content"'* ]] && ok "chat completion via ailocal-implementation" \
                                     || bad "chat completion failed: $(echo "$resp" | head -c 200)"

  # BEHAVIORAL test that context-window validation is actually running.
  # Checking that the "isn't mapped yet" warning is absent is NOT sufficient —
  # when the lookup fails, router._pre_call_checks swallows the exception and
  # SKIPS max_input_tokens enforcement, forwarding oversized prompts to the
  # backend to be silently truncated. Verified A/B: with model_registrar
  # disabled this same request returns 200 with a garbage answer.
  # ailocal-completion caps at 4096 tokens, so this is cheap.
  big=$(python3 -c "
import json
print(json.dumps({'model':'ailocal-completion','max_tokens':16,
 'messages':[{'role':'user','content':'the quick brown fox jumps over the lazy dog. '*6000}]}))")
  over=$(curl -s --max-time 120 http://127.0.0.1:4000/v1/chat/completions \
    -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' -d "$big")
  if echo "$over" | grep -q 'ContextWindowExceededError'; then
    ok "context-window validation ENFORCED (oversized prompt rejected)"
  else
    bad "context-window validation NOT enforced — oversized prompt was accepted. \
model_registrar likely failed; local models are being silently truncated."
  fi

  s=$(curl -s --max-time 60 http://127.0.0.1:4000/v1/search \
    -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
    -d '{"search_tool_name":"searxng-search","query":"ollama apple silicon"}')
  [[ "$s" == *'"results"'* ]] && ok "/v1/search returns results via searxng-search" \
                                  || bad "/v1/search failed: $(echo "$s" | head -c 200)"
fi

# ── Summary ────────────────────────────────────────────────────────────────
echo
if [ "$FAILS" -eq 0 ]; then
  echo "▶ PASS — deployment validated"
  exit 0
fi
echo "▶ FAIL — $FAILS check(s) failed" >&2
exit 1
