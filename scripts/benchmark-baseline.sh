#!/usr/bin/env bash
# benchmark-baseline.sh — repeatable baseline for the whole stack, after upgrades.
#
# PURPOSE: establish baselines, NOT to optimize scores. A number moving is a
# signal to investigate, not a target to tune. Do not "improve" a result by
# changing the scenario.
#
# Complements, does not replace:
#   benchmark-models.sh        candidate model comparison before a migration
#   benchmark-tool-gateway.sh  A/B of gateway report vs filter mode
#   validate-deployment.sh     pass/fail health, not measurement
# This one measures the CLIENT-VISIBLE behaviour those three do not: which tools
# a real session actually calls, how routing and classification resolved, and
# whether the repository-intelligence answers were correct.
#
# Everything runs through the production path — real CLI, real proxy, real
# models on Ollama. Nothing is staged.
#
# RUN IT IDLE. Local inference competes with itself; contention has produced
# phantom failures in this project three separate times. The script refuses to
# start if another claude/test-all run is active.
#
# Usage:
#   ./scripts/benchmark-baseline.sh              # run + write a timestamped baseline
#   ./scripts/benchmark-baseline.sh --compare    # run + diff against the newest baseline
#   ./scripts/benchmark-baseline.sh --list       # list stored baselines
#
# Output: data/benchmarks/baseline-<stamp>.json  (+ a table on stdout)
set -uo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
OUT_DIR="$ROOT_DIR/data/benchmarks"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
RESULT="$OUT_DIR/baseline-$STAMP.json"
CFG="${XDG_CONFIG_HOME:-$HOME/.config}/ailocal"
COMPARE=false

for a in "$@"; do
  case "$a" in
    --compare) COMPARE=true ;;
    --list) ls -1t "$OUT_DIR"/baseline-*.json 2>/dev/null || echo "(no baselines yet)"; exit 0 ;;
    -h|--help) sed -n '2,30p' "$0"; exit 0 ;;
  esac
done

mkdir -p "$OUT_DIR"

# ── preconditions ───────────────────────────────────────────────────────────
# Presence is not capability: check the proxy answers before measuring anything.
if ! curl -sf -m 5 http://127.0.0.1:4000/health/liveliness >/dev/null 2>&1; then
  echo "✗ LiteLLM not reachable — start the stack first (./scripts/start.sh)" >&2
  exit 1
fi
# pgrep patterns must not match this script's own command line (that bug cost a
# wasted run once: an `until ! pgrep -f 'claude -p'` loop matched itself).
if pgrep -f 'test-all\.sh' >/dev/null 2>&1; then
  echo "✗ test-all.sh is running — run this idle, or results are noise" >&2
  exit 1
fi

BASE=$(grep '^AILOCAL_BASE_URL=' "$CFG/env" 2>/dev/null | cut -d= -f2-)
KEY=$(grep '^AILOCAL_API_KEY=' "$CFG/env" 2>/dev/null | cut -d= -f2-)
if [ -z "$BASE" ] || [ -z "$KEY" ]; then
  echo "✗ $CFG/env missing — run ./scripts/install-clients.sh" >&2
  exit 1
fi

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# A tiny fixture repo, so quality assertions have a known-correct answer and do
# not drift when this repository changes.
mkdir -p "$WORK/repo/src"
cat > "$WORK/repo/src/ledger.py" <<'PY'
def compute_balance(entries):
    """Sum signed entries."""
    return sum(e["amount"] for e in entries)


def post_entry(ledger, amount):
    ledger.append({"amount": amount})
    return compute_balance(ledger)
PY
(cd "$WORK/repo" && git init -q . 2>/dev/null && git add -A && git commit -qm fixture 2>/dev/null) || true

# ── scenarios ───────────────────────────────────────────────────────────────
# id | model | expected task_class | expect_tool (empty = expect NO tools) | prompt
# Deliberately small and cheap; this must be affordable after every upgrade.
run_scenario() {
  local id="$1" model="$2" prompt="$3"
  local mark start end
  mark=$(date -u +%s)
  start=$(python3 -c 'import time;print(time.time())')
  ( cd "$WORK/repo" && env \
      CLAUDE_CONFIG_DIR="$CFG/claude" ANTHROPIC_BASE_URL="$BASE" ANTHROPIC_API_KEY="$KEY" \
      CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY=1 CLAUDE_CODE_DISABLE_1M_CONTEXT=1 \
      timeout 1200 claude -p "$prompt" --model "$model" \
        --permission-mode bypassPermissions --output-format stream-json --verbose \
        > "$WORK/$id.jsonl" 2>/dev/null </dev/null )
  end=$(python3 -c 'import time;print(time.time())')
  echo "$mark $start $end"
}

echo "▶ baseline $STAMP"
echo "  (each scenario is a real local inference; expect several minutes)"
echo

SCEN_IDS=(); SCEN_JSON=()

measure() {
  local id="$1" model="$2" expect_class="$3" expect_tool="$4" expect_text="$5" prompt="$6"
  printf '  %-14s ' "$id"
  local t mark start end
  t=$(run_scenario "$id" "$model" "$prompt")
  mark=$(echo "$t" | cut -d' ' -f1); start=$(echo "$t" | cut -d' ' -f2); end=$(echo "$t" | cut -d' ' -f3)

  # Gateway view: classification + tool accounting for this window.
  docker logs --since "$mark" ailocal-litellm 2>&1 \
    | grep -o 'tool_gateway_metric {.*}' > "$WORK/$id.gw" 2>/dev/null || true
  docker logs --since "$mark" ailocal-litellm 2>&1 \
    | grep -o 'request_trace {.*}' > "$WORK/$id.tr" 2>/dev/null || true

  python3 - "$id" "$WORK" "$expect_class" "$expect_tool" "$expect_text" "$start" "$end" <<'PY' >> "$WORK/results.jsonl"
import json, sys, os
sid, work, exp_class, exp_tool, exp_text, start, end = sys.argv[1:8]
rec = {"id": sid, "wall_s": round(float(end) - float(start), 1)}

tools, result, models, usage = [], "", {}, {}
p = os.path.join(work, sid + ".jsonl")
if os.path.exists(p):
    for line in open(p):
        try: e = json.loads(line)
        except Exception: continue
        c = (e.get("message") or {}).get("content")
        if isinstance(c, list):
            for b in c:
                if isinstance(b, dict) and b.get("type") == "tool_use":
                    tools.append(b.get("name"))
        if e.get("type") == "result":
            result = e.get("result") or ""
            models = e.get("modelUsage") or {}
            usage = e.get("usage") or {}

rec["tools_called"] = tools
rec["tool_count"] = len(tools)
# Repository-intelligence usage, the thing no other harness records.
rec["grepai_calls"] = sum(1 for t in tools if str(t).startswith("mcp__grepai__"))
rec["lsp_calls"] = sum(1 for t in tools if str(t).startswith("mcp__lsp__"))
rec["delegations"] = sum(1 for t in tools if t == "Agent")
rec["search_calls"] = sum(1 for t in tools if t in ("WebSearch", "web_search"))
rec["models_used"] = sorted(models.keys())
rec["input_tokens"] = usage.get("input_tokens")
rec["output_tokens"] = usage.get("output_tokens")

gw = os.path.join(work, sid + ".gw")
classes, kept, sent = set(), None, None
if os.path.exists(gw):
    for line in open(gw):
        try: d = json.loads(line[len("tool_gateway_metric "):])
        except Exception: continue
        if d.get("client") != "claude-code":
            continue
        classes.add(str(d.get("task_class")))
        kept, sent = d.get("tools_kept"), d.get("tools_in")
rec["task_classes"] = sorted(classes)
rec["tools_sent_by_client"] = sent
rec["tools_kept_by_gateway"] = kept

tr = os.path.join(work, sid + ".tr")
routes = set()
if os.path.exists(tr):
    for line in open(tr):
        try: d = json.loads(line[len("request_trace "):])
        except Exception: continue
        if d.get("model"):
            routes.add(f"{d['model']}->{d.get('capability')}")
rec["routing"] = sorted(routes)

# ── quality assertions ──────────────────────────────────────────────────────
checks = {}
if exp_class:
    checks["routing_class"] = exp_class in rec["task_classes"]
if exp_tool:
    checks["tool_used"] = exp_tool in tools
else:
    # A conversational scenario must call NOTHING. This is the assertion that
    # catches the gateway regressing back to handing out file tools.
    checks["no_tools"] = len(tools) == 0
if exp_text:
    checks["answer_correct"] = exp_text.lower() in (result or "").lower()
rec["checks"] = checks
rec["passed"] = all(checks.values()) if checks else None
print(json.dumps(rec))
PY
  local ok
  ok=$(tail -1 "$WORK/results.jsonl" | python3 -c 'import sys,json;r=json.load(sys.stdin);print("PASS" if r.get("passed") else "FAIL")' 2>/dev/null || echo "?")
  local secs
  secs=$(tail -1 "$WORK/results.jsonl" | python3 -c 'import sys,json;print(json.load(sys.stdin)["wall_s"])' 2>/dev/null || echo "?")
  echo "$ok  (${secs}s)"
}

: > "$WORK/results.jsonl"

# 1. Conversational: must answer with NO tools. Guards the gateway's
#    task-classification, the single biggest behavioural lever.
measure conversational ailocal-architecture conversational "" "" \
  "show me an example of hello world in c++"

# 2. LSP: exact symbol lookup through mcpls -> pyright.
measure lsp-symbol ailocal-architecture "" mcp__lsp__get_document_symbols compute_balance \
  "Use the mcp__lsp__get_document_symbols tool on src/ledger.py and name the functions it returns."

# 3. grepai: semantic retrieval through Qdrant.
measure grepai-search ailocal-architecture "" mcp__grepai__grepai_search persona_injector \
  "Use the mcp__grepai__grepai_search tool to find where persona injection is implemented in the ailocal project. Report the file path."

# 4. Delegation: parent -> subagent, and the subagent must land on the review tier.
measure delegation ailocal-architecture "" Agent "" \
  "Delegate a security review of src/ledger.py to the reviewer subagent, then summarise its findings."

# 5. Implementation routing: a plain edit stays on the coding tier.
measure simple-edit ailocal-implementation simple_edit "" "" \
  "fix the typo in the docstring of compute_balance in src/ledger.py: change 'Sum signed entries.' to 'Sum the signed entries.'"

# ── assemble ────────────────────────────────────────────────────────────────
python3 - "$RESULT" "$STAMP" "$WORK/results.jsonl" <<'PY'
import json, subprocess, sys
out, stamp, src = sys.argv[1:4]
rows = [json.loads(l) for l in open(src) if l.strip()]
def sh(c):
    try: return subprocess.run(c, shell=True, capture_output=True, text=True).stdout.strip()
    except Exception: return ""
doc = {
    "stamp": stamp,
    "versions": {
        "claude_code": sh("claude --version 2>/dev/null"),
        "codex": sh("codex --version 2>/dev/null"),
        "litellm_image": sh("docker inspect ailocal-litellm --format '{{.Config.Image}}' 2>/dev/null"),
        "ollama": sh("ollama --version 2>/dev/null | head -1"),
    },
    "scenarios": rows,
    "summary": {
        "passed": sum(1 for r in rows if r.get("passed")),
        "total": len(rows),
        "total_wall_s": round(sum(r.get("wall_s") or 0 for r in rows), 1),
        "grepai_calls": sum(r.get("grepai_calls") or 0 for r in rows),
        "lsp_calls": sum(r.get("lsp_calls") or 0 for r in rows),
        "delegations": sum(r.get("delegations") or 0 for r in rows),
    },
}
json.dump(doc, open(out, "w"), indent=2)
s = doc["summary"]
print()
print(f"  {s['passed']}/{s['total']} scenarios passed in {s['total_wall_s']}s"
      f"  (grepai {s['grepai_calls']} · lsp {s['lsp_calls']} · delegations {s['delegations']})")
print(f"  baseline: {out}")
PY

if [ "$COMPARE" = true ]; then
  PREV=$(ls -1t "$OUT_DIR"/baseline-*.json 2>/dev/null | sed -n 2p)
  if [ -n "$PREV" ]; then
    echo
    echo "▶ vs $(basename "$PREV")"
    python3 - "$RESULT" "$PREV" <<'PY'
import json, sys
new = json.load(open(sys.argv[1])); old = json.load(open(sys.argv[2]))
o = {r["id"]: r for r in old["scenarios"]}
for r in new["scenarios"]:
    p = o.get(r["id"])
    if not p:
        print(f"  {r['id']:<14} NEW"); continue
    d = (r.get("wall_s") or 0) - (p.get("wall_s") or 0)
    flag = ""
    if p.get("passed") and not r.get("passed"): flag = "  <-- REGRESSION"
    elif r.get("passed") and not p.get("passed"): flag = "  <-- now passing"
    print(f"  {r['id']:<14} {'PASS' if r.get('passed') else 'FAIL':<5} "
          f"{r.get('wall_s')}s ({d:+.1f}s){flag}")
print("\n  Timing drift is expected; a PASS->FAIL flip is the signal worth chasing.")
PY
  else
    echo "  (no previous baseline to compare against)"
  fi
fi
