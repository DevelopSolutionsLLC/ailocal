#!/usr/bin/env bash
# status.sh — live model status by capability.
#
# Reads config/capabilities.generated.json (the declarative registry, generated from
# config/models.yaml by sync-models.py) and Ollama's /api/ps, and shows what is ACTUALLY
# loaded right now, with keep-alive remaining. Real model names are shown, never hidden.
#
#   ./scripts/status.sh          verbose, per-capability   (ailocal status)
#   ./scripts/status.sh --table  compact one-line-per-capability table (ailocal models)
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
CAPS="$ROOT_DIR/config/capabilities.generated.json"
OLLAMA_URL="${OLLAMA_HOST:-http://127.0.0.1:11434}"
MODE="verbose"; [ "${1:-}" = "--table" ] && MODE="table"

[ -f "$CAPS" ] || { echo "capabilities.generated.json missing — run ./scripts/sync-models.sh" >&2; exit 1; }

PS_JSON="$(curl -fsS -m 3 "$OLLAMA_URL/api/ps" 2>/dev/null || echo '{"models":[]}')"

CAPS_FILE="$CAPS" PS_DATA="$PS_JSON" MODE="$MODE" python3 - <<'PY'
import json, os, re
from datetime import datetime, timezone

caps = json.load(open(os.environ["CAPS_FILE"]))["capabilities"]
ps   = json.loads(os.environ["PS_DATA"]).get("models", [])
loaded = {m.get("name", ""): m for m in ps}
now = datetime.now(timezone.utc)

def base(tag): return tag.split(":", 1)[0]

def find(backend):
    if backend in loaded: return loaded[backend]
    if backend + ":latest" in loaded: return loaded[backend + ":latest"]
    for name, m in loaded.items():
        if base(name) == base(backend): return m
    return None

def parse_exp(s):
    # Ollama gives RFC3339 with up to 9 fractional digits + offset; trim to 6 for fromisoformat.
    if not s: return None
    s = re.sub(r"(\.\d{6})\d+", r"\1", s)
    try: return datetime.fromisoformat(s)
    except ValueError: return None

def human(delta):
    secs = int(delta.total_seconds())
    if secs <= 0: return "expiring"
    h, m = secs // 3600, (secs % 3600) // 60
    if h and m: return f"{h}h {m}m remaining"
    if h:       return f"{h}h remaining"
    return f"{m}m remaining"

C = lambda s, c: f"\033[{c}m{s}\033[0m"

def state(c):
    """(label, color) — persistent | loaded | idle."""
    m = find(c["backend"])
    if not m:
        return "idle", "33"
    exp = parse_exp(m.get("expires_at"))
    if c.get("persistent") or (exp and (exp.year - now.year) > 5):
        return "persistent", "35"
    return "loaded", "32"

if os.environ.get("MODE") == "table":
    w_cap = max(len("Capability"), *(len(c["name"]) for c in caps))
    w_bk  = max(len("Backend"),    *(len(c["backend"]) for c in caps))
    print(C(f"{'Capability':<{w_cap}}  {'Backend':<{w_bk}}  Status", "1"))
    for c in caps:
        label, color = state(c)
        print(f"{c['name']:<{w_cap}}  {c['backend']:<{w_bk}}  {C(label, color)}")
else:
    print(C("AILOCAL MODEL STATUS", "1"))
    print("─" * 44)
    for c in caps:
        print(C(c["role"], "1;36"))
        print(f"  Model:      {c['backend']}")
        m = find(c["backend"])
        if not m:
            print(f"  Loaded:     {C('No', '33')}")
        else:
            print(f"  Loaded:     {C('Yes', '32')}")
            exp = parse_exp(m.get("expires_at"))
            if c.get("persistent") or (exp and (exp.year - now.year) > 5):
                print(f"  Keep Alive: {C('Persistent', '35')}")
            elif exp:
                print(f"  Keep Alive: {human(exp - now)}")
            print(f"  Context:    {c['context']}")
        print()
PY
