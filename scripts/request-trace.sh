#!/usr/bin/env bash
# request-trace.sh — the per-request timeline. Answers "which component, and when".
#
#   ./scripts/request-trace.sh              recent requests
#   ./scripts/request-trace.sh --failures   only failures
#   ./scripts/request-trace.sh --slow 5000  only requests slower than N ms
#   ./scripts/request-trace.sh --id <id>    one request in full
#
# Reads the JSONL written by config/litellm/request_trace.py.
#
# READ THE COLUMNS LITERALLY:
#   ttfb_ms   time to the FIRST STREAMED CHUNK. It is a PROXY for prompt-eval
#             time, not a measurement of it — Ollama's prompt_eval_duration does
#             not survive into the LiteLLM response (verified: `usage` carries
#             only token counts).
#   outcome   what the PROXY saw. A client that timed out at 60 s while the proxy
#             streamed happily still shows `streamed` here. A large ttfb_ms next
#             to a client-reported error IS that evidence — but the client's
#             disconnect is not observable from inside the proxy and is never
#             recorded as though it were.
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
DIR="${AILOCAL_TRACE_HOST_DIR:-${AILOCAL_STATE:-$HOME/.local/state/ailocal}/captures/traces}"

MODE=recent; ARG=""
while [ $# -gt 0 ]; do
  case "$1" in
    --failures) MODE=failures; shift ;;
    --slow) MODE=slow; ARG="$2"; shift 2 ;;
    --id) MODE=id; ARG="$2"; shift 2 ;;
    *) echo "usage: $0 [--failures|--slow MS|--id ID]"; exit 1 ;;
  esac
done

if [ ! -d "$DIR" ] || [ -z "$(ls -A "$DIR" 2>/dev/null)" ]; then
  echo "No traces in $DIR."
  echo
  echo "Tracing is OFF unless AILOCAL_TRACE_DIR is set in .env. That is not"
  echo "evidence that no requests were served — check with:"
  echo "    docker exec ailocal-litellm printenv AILOCAL_TRACE_DIR"
  exit 1
fi

MODE="$MODE" ARG="$ARG" DIR="$DIR" python3 - <<'PY'
import glob, json, os, time

mode, arg, d = os.environ["MODE"], os.environ["ARG"], os.environ["DIR"]
rows = []
for f in sorted(glob.glob(os.path.join(d, "*.jsonl"))):
    for line in open(f, encoding="utf-8", errors="replace"):
        try:
            rows.append(json.loads(line))
        except Exception:
            continue

if mode == "failures":
    rows = [r for r in rows if r.get("outcome") in ("failure", "empty_stream")]
elif mode == "slow":
    try:
        n = float(arg)
    except ValueError:
        n = 5000.0
    rows = [r for r in rows if (r.get("total_ms") or 0) >= n]
elif mode == "id":
    rows = [r for r in rows if str(r.get("request_id", "")).startswith(arg)]

if not rows:
    print(f"No matching traces ({mode}).")
    print("An empty result here means NO REQUEST MATCHED — not that the system")
    print("is healthy and not that nothing was served.")
    raise SystemExit(0)

if mode == "id":
    for r in rows:
        print("=" * 66)
        for k in sorted(r):
            v = r[k]
            if k == "traceback" and v:
                print(f"  {k}:")
                for ln in str(v).splitlines()[-12:]:
                    print(f"      {ln}")
            else:
                print(f"  {k:18} {v}")
    raise SystemExit(0)

print("─" * 96)
print(f"{'when':9} {'id':17} {'model':24} {'ttfb':>8} {'total':>9} "
      f"{'tools':>5} {'msgs':>5}  outcome")
print("─" * 96)
for r in rows[-40:]:
    ts = r.get("ts")
    when = time.strftime("%H:%M:%S", time.localtime(ts)) if ts else "-"
    ttfb = r.get("ttfb_ms")
    outcome = str(r.get("outcome"))
    if r.get("error_type"):
        outcome = f"{outcome}: {r['error_type']}"
    # Flag the shape that caused the Claude failures: a long silence before the
    # first byte, with everything else reporting success.
    flag = ""
    if isinstance(ttfb, (int, float)) and ttfb > 60000:
        flag = "  <-- >60s SILENCE before first byte; a client timeout here " \
               "looks like an 'API error' with no failing component"
    # Precomputed, not inlined: a nested quote inside an f-string expression is
    # a SyntaxError, and it bit twice in this project.
    tot = r.get("total_ms")
    ttfb_s = f"{ttfb:.0f}ms" if isinstance(ttfb, (int, float)) else "-"
    tot_s = f"{tot:.0f}ms" if isinstance(tot, (int, float)) else "-"
    rid = str(r.get("request_id"))[:16]
    mdl = str(r.get("model"))[:23]
    print(f"{when:9} {rid:17} {mdl:24} {ttfb_s:>8} {tot_s:>9} "
          f"{str(r.get('tools_declared') or '-'):>5} "
          f"{str(r.get('messages') or '-'):>5}  {outcome}{flag}")

fails = [r for r in rows if r.get("outcome") == "failure"]
slow = [r for r in rows if isinstance(r.get("ttfb_ms"), (int, float))
        and r["ttfb_ms"] > 60000]
print("─" * 96)
print(f"{len(rows)} trace record(s), {len(fails)} failure(s), "
      f"{len(slow)} with >60s time-to-first-byte")
if fails:
    print("\nInspect one in full:  ./scripts/request-trace.sh --id "
          f"{str(fails[-1].get('request_id'))[:8]}")
PY
