#!/usr/bin/env bash
# validate.sh — end-to-end sanity for the capability platform. Answers: does the active profile
# (or --profile <tier>) actually hang together?
#
#   ./scripts/validate.sh [--profile <tier>] [--runtime]   (or: ailocal validate [...])
#
# STATIC checks (always), FAIL (exit 1) on any ✗:
#   Capabilities — every capability declares a backend
#   Backends     — every backend is installed in Ollama
#   Clients      — every clients.yaml mapping targets a real capability
#   Aliases      — every generated model_group_alias resolves to a real capability
#   Generated    — the derived files are in sync (active profile only)
#
# RUNTIME checks (--runtime) — live against the running proxy + Ollama:
#   /v1/models   — the proxy actually serves every ailocal-<capability>
#   /model/info  — the ADVERTISED max_input_tokens matches the profile context (catches a
#                  proxy running stale config — the exact failure that broke VS Code chat,
#                  where clients trust the advertised window, not the model's real limit)
#   Embeddings   — the embeddings backend is loaded in Ollama (grepai infrastructure)
#
# This is the guardrail that catches "aliases point at a capability this tier doesn't define"
# and "the deployed proxy advertises a different context than the source of truth".
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"

TIER=""; RUNTIME=0
while [ $# -gt 0 ]; do
  case "$1" in
    --profile) TIER="${2:-}"; shift 2 ;;
    --runtime) RUNTIME=1; shift ;;
    *) shift ;;
  esac
done
ACTIVE="$(cat "$ROOT_DIR/config/active-profile" 2>/dev/null || echo 64gb)"
CHECK_GENERATED=1
[ -n "$TIER" ] && [ "$TIER" != "$ACTIVE" ] && CHECK_GENERATED=0   # generated files reflect the ACTIVE tier

OLLAMA_LIST="$(ollama list 2>/dev/null || true)"

ROOT_DIR="$ROOT_DIR" TIER="$TIER" OLLAMA_LIST="$OLLAMA_LIST" python3 - <<'PY'
import os, sys, importlib.util
root = os.environ["ROOT_DIR"]; tier = os.environ["TIER"] or None
spec = importlib.util.spec_from_file_location("sm", root + "/scripts/sync-models.py")
sm = importlib.util.module_from_spec(spec); spec.loader.exec_module(sm)

path    = sm.profile_path(explicit=tier)
models  = sm.load_models_yaml(path)
clients = sm.load_clients_yaml()
installed_bases = {ln.split()[0].split(":")[0]
                   for ln in os.environ["OLLAMA_LIST"].splitlines()[1:] if ln.strip()}

C = lambda s, c: f"\033[{c}m{s}\033[0m"
YES, NO = C("✓", "32"), C("✗", "31")
fails = 0
def check(cond, msg):
    global fails
    print(f"  {YES if cond else NO} {msg}")
    if not cond: fails += 1

print(C(f"Profile: {path.stem}", "1"))

print(C("Capabilities", "1;36"))
for name, info in models.items():
    b = sm.backend_of(info)
    enabled = "" if sm.truthy(info.get("enabled", "true")) else "  (enabled: false on this tier)"
    check(bool(b), f"{name} → {b or '(no backend!)'}{enabled}")

print(C("Backends installed in Ollama", "1;36"))
if not installed_bases:
    print(f"  {NO} could not read `ollama list` (is Ollama running?)"); fails += 1
else:
    for b in sorted({sm.backend_of(i) for i in models.values() if sm.backend_of(i)}):
        check(b.split(":")[0] in installed_bases, f"{b}")

print(C("Client mappings → real capabilities", "1;36"))
caps = set(models)
def check_targets(label, targets):
    bad = [t for t in targets if t not in caps]
    check(not bad, f"{label}" + (f" — unknown: {', '.join(bad)}" if bad else ""))
cl = clients
check_targets("claude.launch_default", [cl.get("claude", {}).get("launch_default", "")] if cl.get("claude", {}).get("launch_default") else [])
check_targets("claude.slots", list((cl.get("claude", {}).get("slots") or {}).values()))
check_targets("codex.default", [cl.get("codex", {}).get("default", "")] if cl.get("codex", {}).get("default") else [])
check_targets("codex.profiles", list((cl.get("codex", {}).get("profiles") or {}).values()))
cont = cl.get("continue", {})
check_targets("continue.chat", cont.get("chat", []))
check_targets("continue.autocomplete/embeddings", [x for x in (cont.get("autocomplete"), cont.get("embeddings")) if x])
check_targets("compat aliases", list((cl.get("compat") or {}).values()))

print(C("No duplicate alias keys", "1;36"))
# Raw scan: the dict loader silently keeps the LAST of a duplicated key, which hides copy-paste
# doubling (and masks other checks). Flag any alias name declared more than once under compat.
import collections
sect, counts = None, collections.Counter()
for ln in open(root + "/config/clients.yaml").read().splitlines():
    if ln and not ln[0].isspace() and ln.rstrip().endswith(":"):
        sect = ln.rstrip()[:-1]
    elif sect == "compat" and ln.startswith(" ") and ":" in ln:
        counts[ln.strip().split(":", 1)[0]] += 1
dupes = [k for k, n in counts.items() if n > 1]
check(not dupes, "compat keys unique" + (f" — DUPLICATED: {', '.join(dupes)}" if dupes else ""))

print(C("Generated aliases resolve", "1;36"))
cfg = (root + "/config/litellm/config.yaml")
import re
try:
    txt = open(cfg).read()
    block = re.search(r"model_group_alias:(.*?)# >>> END GENERATED model_group_alias", txt, re.S)
    targets = re.findall(r"^\s+\S+:\s*(\S+)\s*$", block.group(1), re.M) if block else []
    listed = set(re.findall(r"model_name:\s*(\S+)", txt))
    bad = sorted({t for t in targets if t not in listed})
    check(bool(targets) and not bad, f"{len(targets)} aliases → model_list" + (f" — unresolved: {', '.join(bad)}" if bad else ""))
    if os.environ.get("_note"): pass
except FileNotFoundError:
    print(f"  {NO} config/litellm/config.yaml missing"); fails += 1

sys.exit(1 if fails else 0)
PY
STATUS=$?

if [ "$CHECK_GENERATED" = 1 ]; then
  echo -e "\033[1;36mGenerated files in sync\033[0m"
  if OUT="$("$ROOT_DIR/scripts/sync-models.sh" --check 2>&1)"; then echo "  ✓ $OUT"; else echo -e "  \033[31m✗\033[0m drift — run ./scripts/sync-models.sh && commit"; STATUS=1; fi
else
  echo "  (generated-files check skipped — --profile $TIER differs from active $ACTIVE)"
fi

if [ "$RUNTIME" = 1 ]; then
  KEY="$(grep '^LITELLM_MASTER_KEY=' "$ROOT_DIR/.env" 2>/dev/null | cut -d= -f2-)"
  BASE="http://localhost:4000"
  MODELS_JSON="$(curl -fsS -m 5 -H "Authorization: Bearer $KEY" "$BASE/v1/models" 2>/dev/null || echo '')"
  INFO_JSON="$(curl -fsS -m 5 -H "Authorization: Bearer $KEY" "$BASE/model/info" 2>/dev/null || echo '')"
  PS_JSON="$(curl -fsS -m 3 "${OLLAMA_HOST:-http://127.0.0.1:11434}/api/ps" 2>/dev/null || echo '{"models":[]}')"
  if ! ROOT_DIR="$ROOT_DIR" TIER="$TIER" MODELS_JSON="$MODELS_JSON" INFO_JSON="$INFO_JSON" PS_JSON="$PS_JSON" python3 - <<'PY'
import os, sys, json, importlib.util
root = os.environ["ROOT_DIR"]; tier = os.environ["TIER"] or None
spec = importlib.util.spec_from_file_location("sm", root + "/scripts/sync-models.py")
sm = importlib.util.module_from_spec(spec); spec.loader.exec_module(sm)
models = sm.load_models_yaml(sm.profile_path(explicit=tier))

C = lambda s, c: f"\033[{c}m{s}\033[0m"
YES, NO = C("✓", "32"), C("✗", "31")
fails = 0
def check(cond, msg):
    global fails
    print(f"  {YES if cond else NO} {msg}")
    if not cond: fails += 1

print(C("Runtime — proxy /v1/models", "1;36"))
mj = os.environ["MODELS_JSON"]
check(bool(mj), "proxy reachable at localhost:4000")
served = {m["id"] for m in json.loads(mj).get("data", [])} if mj else set()
for cap in models:
    check(f"ailocal-{cap}" in served, f"serves ailocal-{cap}")

print(C("Runtime — advertised context (/model/info) matches profile", "1;36"))
ij = os.environ["INFO_JSON"]
adv = {}
if ij:
    for m in json.loads(ij).get("data", []):
        adv[m.get("model_name")] = (m.get("model_info") or {}).get("max_input_tokens")
for cap, info in models.items():
    if cap == "embeddings":
        continue
    want, got = sm.ctx_of(info), adv.get(f"ailocal-{cap}")
    check(got == want, f"ailocal-{cap}: advertises max_input={got} (profile {want})"
          + ("" if got == want else " — proxy running STALE config; restart it"))

print(C("Runtime — embeddings loaded in Ollama", "1;36"))
ps = json.loads(os.environ["PS_JSON"]).get("models", [])
emb = sm.backend_of(models.get("embeddings", {}))
base = lambda t: t.split(":", 1)[0]
check(any(base(m.get("name", "")) == base(emb) for m in ps),
      f"embeddings backend '{emb}' resident (grepai depends on it)")

sys.exit(1 if fails else 0)
PY
  then STATUS=1; fi
fi

echo
[ "$STATUS" = 0 ] && echo -e "\033[32mVALIDATE: OK\033[0m" || echo -e "\033[31mVALIDATE: FAILED\033[0m"
exit "$STATUS"
