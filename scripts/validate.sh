#!/usr/bin/env bash
# validate.sh — end-to-end sanity for the capability platform. Answers: does the active profile
# (or --profile <tier>) actually hang together?
#
#   ./scripts/validate.sh [--profile <tier>]     (or: ailocal validate [--profile <tier>])
#
# Checks, and FAILS (exit 1) on any ✗:
#   Capabilities — every capability declares a backend
#   Backends     — every backend is installed in Ollama
#   Clients      — every clients.yaml mapping targets a real capability
#   Aliases      — every generated model_group_alias resolves to a real capability
#   Generated    — the derived files are in sync (active profile only)
#
# This is the guardrail that catches "aliases point at a capability this tier doesn't define".
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"

TIER=""
[ "${1:-}" = "--profile" ] && TIER="${2:-}"
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

echo
[ "$STATUS" = 0 ] && echo -e "\033[32mVALIDATE: OK\033[0m" || echo -e "\033[31mVALIDATE: FAILED\033[0m"
exit "$STATUS"
