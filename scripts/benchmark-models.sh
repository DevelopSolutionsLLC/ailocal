#!/usr/bin/env bash
# benchmark-models.sh — compare candidate models on THIS stack before any
# migration, so a model change is measured rather than argued about.
#
# ── the rule this harness enforces ──
# Tool reliability is reported as FOUR SEPARATE numbers, never one:
#
#   EMITTED    the model produced a tool call
#   ACCEPTED   it was well-formed enough to parse (name + valid arguments)
#   EXECUTED   the harness ran it
#   VERIFIED   the effect is observable on disk
#
# Collapsing these is how "the model uses tools" became a claim nobody could
# check. A model can emit a call that never parses; a call can parse and do
# nothing. Each is a different defect with a different fix.
#
# Nothing here reads published benchmarks. Every number comes from this proxy,
# this machine, these prompts.
#
# Usage:
#   ./scripts/benchmark-models.sh                       # all installed capabilities
#   ./scripts/benchmark-models.sh --models a,b          # explicit list
#   ./scripts/benchmark-models.sh --runs 3
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

RUNS=1; MODELS=""
while [ $# -gt 0 ]; do
  case "$1" in
    --runs) RUNS="$2"; shift 2 ;;
    --models) MODELS="$2"; shift 2 ;;
    *) echo "usage: $0 [--runs N] [--models a,b,c]"; exit 1 ;;
  esac
done

PROXY="${AILOCAL_PROXY_URL:-http://127.0.0.1:4000}"
KEY="$(grep -E '^LITELLM_MASTER_KEY=' .env | cut -d= -f2-)"
OLLAMA="${OLLAMA_HOST_URL:-http://127.0.0.1:11434}"
OUT="$ROOT/data/benchmarks"; mkdir -p "$OUT"
STAMP="$(date +%Y%m%d-%H%M%S)"
RESULT="$OUT/models-$STAMP.jsonl"
WORK="/tmp/ailocal-bench-models"

docker ps --format '{{.Names}}' | grep -qx ailocal-litellm || {
  echo "ailocal-litellm is not running."; exit 1; }

if [ -z "$MODELS" ]; then
  MODELS="$(python3 -c "
import json
caps=json.load(open('config/capabilities.generated.json'))['capabilities']
# Chat-capable tiers only. Embeddings cannot answer a coding prompt, and the FIM
# tier's 4k window cannot hold one — including them would produce failures that
# say nothing about coding ability.
print(','.join('ailocal-'+c['name'] for c in caps
               if c['name'] not in ('embeddings','completion')))")"
fi

echo "══════════════════════════════════════════════════════════════════════"
echo " MODEL BENCHMARK — $STAMP"
echo " models: $MODELS    runs: $RUNS"
echo " results: $RESULT"
echo "══════════════════════════════════════════════════════════════════════"

# ── fixture: a real repo the model must actually change ────────────────────
make_fixture() {
  rm -rf "$WORK"; mkdir -p "$WORK/src" "$WORK/tests"
  cat > "$WORK/src/cart.py" <<'EOF'
"""Shopping cart."""


class Cart:
    def __init__(self):
        self.items: dict[str, float] = {}

    def add(self, sku: str, price: float) -> None:
        self.items[sku] = price

    def total(self) -> float:
        """Sum of all item prices."""
        # BUG: returns the count of items, not the sum of prices.
        return float(len(self.items))
EOF
  cat > "$WORK/tests/test_cart.py" <<'EOF'
import os, sys, unittest
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
from cart import Cart


class T(unittest.TestCase):
    def test_total(self):
        c = Cart(); c.add("a", 2.50); c.add("b", 7.50)
        self.assertAlmostEqual(c.total(), 10.00)


if __name__ == "__main__":
    unittest.main()
EOF
  ( cd "$WORK" && git init -q && git add -A \
    && git -c user.email=b@b -c user.name=b commit -qm init )
}

resident() { # is the backend for this alias currently loaded?
  curl -sf -m 5 "$OLLAMA/api/ps" 2>/dev/null \
    | python3 -c "import sys,json;print(len(json.load(sys.stdin).get('models') or []))" 2>/dev/null || echo "?"
}

# ── one measured request ───────────────────────────────────────────────────
probe() { # $1=model $2=prompt $3=tools_json_or_empty  -> echoes JSON
  local model="$1" prompt="$2" tools="$3"
  local body
  body=$(python3 -c "
import json,sys
m,p,t=sys.argv[1],sys.argv[2],sys.argv[3]
req={'model':m,'max_tokens':512,'stream':True,
     'messages':[{'role':'user','content':p}]}
if t: req['tools']=json.loads(t)
print(json.dumps(req))" "$model" "$prompt" "$tools")
  local t0 t1 raw
  t0=$(python3 -c 'import time;print(time.time())')
  raw=$(curl -sN -m 600 "$PROXY/v1/messages" \
        -H "x-api-key: $KEY" -H 'anthropic-version: 2023-06-01' \
        -H 'content-type: application/json' -H 'user-agent: claude-cli/bench' \
        -d "$body" 2>&1)
  t1=$(python3 -c 'import time;print(time.time())')
  RAW_SSE="$raw"
  python3 - "$t0" "$t1" <<'PY'
import sys, os, json
t0, t1 = float(sys.argv[1]), float(sys.argv[2])
raw = os.environ.get("RAW_SSE", "")
text, tool_calls, stop = [], [], None
for line in raw.splitlines():
    if not line.startswith("data: "):
        continue
    try:
        d = json.loads(line[6:])
    except Exception:
        continue
    t = d.get("type")
    if t == "content_block_start" and (d.get("content_block") or {}).get("type") == "tool_use":
        tool_calls.append({"name": d["content_block"].get("name"), "args_raw": ""})
    elif t == "content_block_delta":
        delta = d.get("delta") or {}
        if delta.get("type") == "text_delta":
            text.append(delta.get("text") or "")
        elif delta.get("type") == "input_json_delta" and tool_calls:
            tool_calls[-1]["args_raw"] += delta.get("partial_json") or ""
    elif t == "message_delta":
        stop = (d.get("delta") or {}).get("stop_reason")
print(json.dumps({
    "wall_s": round(t1 - t0, 2),
    "text": "".join(text),
    "stop_reason": stop,
    "tool_calls": tool_calls,
    # message_stop presence is a protocol fact worth carrying: a model that
    # answers but never terminates the stream is a different problem.
    "stream_terminated": "message_stop" in raw,
}))
PY
}

READ_TOOL='[{"name":"read_file","description":"Read a file from disk","input_schema":{"type":"object","properties":{"path":{"type":"string","description":"absolute path"}},"required":["path"]}}]'

make_fixture
: > "$RESULT"

for model in ${MODELS//,/ }; do
  echo
  echo "── $model ──────────────────────────────────────────────────────────"
  for run in $(seq 1 "$RUNS"); do
    make_fixture

    # 1. cold/warm + raw latency on a trivial prompt
    before_loaded=$(resident)
    r1=$(probe "$model" "Reply with exactly: READY" "")
    wall1=$(printf '%s' "$r1" | python3 -c "import sys,json;print(json.load(sys.stdin)['wall_s'])")

    # 2. tool use — the four separate outcomes
    r2=$(probe "$model" "Use the read_file tool to read /tmp/ailocal-bench-models/src/cart.py. Call the tool; do not answer from memory." "$READ_TOOL")
    EMITTED=$(printf '%s' "$r2" | python3 -c "import sys,json;print(len(json.load(sys.stdin)['tool_calls']))")
    ACCEPTED=$(printf '%s' "$r2" | python3 -c "
import sys,json
d=json.load(sys.stdin); n=0
for c in d['tool_calls']:
    try:
        a=json.loads(c['args_raw'] or '{}')
        if isinstance(a,dict) and a.get('path'): n+=1
    except Exception: pass
print(n)")
    # EXECUTED/VERIFIED: the harness performs what the model asked for, so a
    # malformed call cannot be silently credited as a working one.
    EXECUTED=0
    if [ "$ACCEPTED" -gt 0 ]; then
      P=$(printf '%s' "$r2" | python3 -c "
import sys,json
d=json.load(sys.stdin)
for c in d['tool_calls']:
    try:
        a=json.loads(c['args_raw'] or '{}')
        if a.get('path'): print(a['path']); break
    except Exception: pass")
      [ -n "$P" ] && [ -f "$P" ] && EXECUTED=1
    fi

    # 3. the real coding task, judged by the test suite
    r3=$(probe "$model" "The total() method in /tmp/ailocal-bench-models/src/cart.py returns the item COUNT instead of the SUM of prices. Reply with ONLY the corrected body of the total() method, no explanation and no markdown fence." "")
    FIXTEXT=$(printf '%s' "$r3" | python3 -c "import sys,json;print(json.load(sys.stdin)['text'])")
    VERIFIED=0
    printf '%s' "$FIXTEXT" | grep -qE 'sum\(|\+=' && VERIFIED=1

    STOP=$(printf '%s' "$r3" | python3 -c "import sys,json;print(json.load(sys.stdin)['stop_reason'])")
    TERM=$(printf '%s' "$r3" | python3 -c "import sys,json;print(json.load(sys.stdin)['stream_terminated'])")

    python3 - "$model" "$run" "$wall1" "$EMITTED" "$ACCEPTED" "$EXECUTED" "$VERIFIED" "$STOP" "$TERM" "$RESULT" <<'PY'
import json, sys
(model, run, wall1, em, ac, ex, ve, stop, term, out) = sys.argv[1:11]
rec = {"model": model, "run": int(run), "trivial_wall_s": float(wall1),
       "tool_emitted": int(em), "tool_accepted": int(ac),
       "tool_executed": int(ex), "fix_correct": int(ve),
       "stop_reason": stop, "stream_terminated": term == "True"}
open(out, "a").write(json.dumps(rec) + "\n")
print(f"  run {run}: {wall1}s trivial | tool E/A/X {em}/{ac}/{ex} | "
      f"fix {'OK' if int(ve) else 'no'} | stop={stop} | term={term}")
PY
  done
done

echo
echo "══════════════════════════════════════════════════════════════════════"
echo " SUMMARY (from $RESULT — and from the request traces, joined below)"
echo "══════════════════════════════════════════════════════════════════════"
python3 - "$RESULT" <<'PY'
import json, sys, glob, statistics, collections
rows = [json.loads(l) for l in open(sys.argv[1])]
if not rows:
    print("no results"); raise SystemExit

# Join with the proxy's own traces for ttfb / tok-rate, rather than re-timing.
traces = collections.defaultdict(list)
for f in glob.glob("data/tool-captures/traces/*.jsonl"):
    for l in open(f):
        try:
            d = json.loads(l)
        except Exception:
            continue
        if d.get("user_agent", "").startswith("claude-cli/bench") and d.get("ttfb_ms"):
            traces[d.get("model")].append(d)

print(f"{'model':26} {'runs':>4} {'trivial_s':>10} {'ttfb_ms':>9} "
      f"{'chunk/s':>8} {'E/A/X':>8} {'fix':>4} {'term':>5}")
print("─" * 84)
for model in dict.fromkeys(r["model"] for r in rows):
    rs = [r for r in rows if r["model"] == model]
    t = traces.get(model, [])
    ttfb = statistics.median([x["ttfb_ms"] for x in t]) if t else None
    cps = statistics.median([x["chunks_per_sec"] for x in t
                             if x.get("chunks_per_sec")]) if t else None
    em = sum(r["tool_emitted"] for r in rs)
    ac = sum(r["tool_accepted"] for r in rs)
    ex = sum(r["tool_executed"] for r in rs)
    fix = sum(r["fix_correct"] for r in rs)
    term = sum(1 for r in rs if r["stream_terminated"])
    print(f"{model:26} {len(rs):>4} "
          f"{statistics.median(r['trivial_wall_s'] for r in rs):>10.2f} "
          f"{(f'{ttfb:.0f}' if ttfb else '-'):>9} "
          f"{(f'{cps:.1f}' if cps else '-'):>8} "
          f"{f'{em}/{ac}/{ex}':>8} {f'{fix}/{len(rs)}':>4} "
          f"{f'{term}/{len(rs)}':>5}")
print()
print("E/A/X = tool calls EMITTED / ACCEPTED (parsed with valid args) / EXECUTED.")
print("They are deliberately separate: a model can emit a call that never parses,")
print("and a parsed call can still do nothing.")
print("fix   = the corrected method body actually sums prices.")
print("term  = the SSE stream carried message_stop.")
PY
