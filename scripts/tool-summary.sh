#!/usr/bin/env bash
# tool-summary.sh — show what the gateway did to the last request(s).
#
# TOOL NEGOTIATION SUMMARY
#   what the client sent, what the model received, what was removed and WHY,
#   and the token cost avoided.
#
# Reads the gateway's own metric stream. Every figure is one the gateway measured
# on a real request; nothing here is estimated except tokens, which are labelled.
#
# Usage:
#   ./scripts/tool-summary.sh              # most recent request
#   ./scripts/tool-summary.sh --all        # every request in the window
#   ./scripts/tool-summary.sh --since 1h
#   ./scripts/tool-summary.sh --watch      # live, as requests arrive
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

CONTAINER="${AILOCAL_LITELLM_CONTAINER:-ailocal-litellm}"
SINCE="30m"; MODE="last"
while [ $# -gt 0 ]; do
  case "$1" in
    --all)   MODE=all;   shift ;;
    --watch) MODE=watch; shift ;;
    --since) SINCE="$2"; shift 2 ;;
    *) echo "unknown option: $1"; exit 1 ;;
  esac
done

docker ps --format '{{.Names}}' | grep -qx "$CONTAINER" || {
  echo "$CONTAINER is not running."; exit 1; }

cat > /tmp/ailocal-tool-summary.py <<'PY'
import json, os, sys

MODE = os.environ.get("SUMMARY_MODE", "last")

def human(n):
    if n is None:
        return "?"
    if n >= 1000:
        return f"{n/1000:.1f} KB" if n >= 10000 else f"{n:,} B"
    return f"{n} B"

def render(d):
    tin = d.get("tools_in") or 0
    tkept = d.get("tools_kept") or 0
    bin_ = d.get("bytes_reachable") or 0
    bkept = d.get("bytes_kept_reachable")
    if bkept is None:
        bkept = d.get("bytes_kept") or 0
    removed_tools = tin - tkept
    removed_bytes = bin_ - bkept
    pct = (100.0 * removed_bytes / bin_) if bin_ else 0.0
    tok_in = d.get("tokens_est_in")
    tok_kept = d.get("tokens_est_kept")
    tok_saved = (tok_in - tok_kept) if (isinstance(tok_in, int)
                                       and isinstance(tok_kept, int)) else None

    print("─" * 66)
    print("TOOL NEGOTIATION SUMMARY")
    print("─" * 66)
    print(f"  Client        {d.get('client')}")
    print(f"  Route         {d.get('route')}")
    print(f"  Model         {d.get('model')}  [{d.get('model_class')}]")
    task = d.get("task_class")
    print(f"  Task          {task if task else '(classification off)'}")
    print(f"  Mode          {d.get('mode')}"
          + ("  APPLIED" if d.get("applied") else "  measured only"))
    if d.get("passthrough"):
        print("  Passthrough   YES — frontier model, forwarded untouched by design")
    print()
    print(f"  Client sent   {tin:3} tools   {human(d.get('bytes_in'))}")
    pre = d.get("bytes_prefiltered_by_litellm") or 0
    if pre:
        # This is the figure that would otherwise be mis-credited to the gateway.
        print(f"  LiteLLM drops {'':3}          {human(pre)}  "
              f"(namespace/shell types — never reach any model)")
    print(f"  Reaches model {tkept:3} tools   {human(bkept)}"
          + (f"   of {human(bin_)}" if bin_ else ""))
    print()
    if removed_tools > 0 or removed_bytes > 0:
        print(f"  REMOVED       {removed_tools:3} tools   {human(removed_bytes)}"
              f"   ({pct:.0f}% of what the model would have seen)")
        by_drop = d.get("bytes_dropped") or 0
        by_rw = d.get("bytes_saved_by_rewrite") or 0
        if by_drop:
            print(f"                  by dropping tools     {human(by_drop)}")
        if by_rw:
            print(f"                  by rewriting schemas  {human(by_rw)}")
        if tok_saved:
            print(f"                  ~{tok_saved:,} tokens avoided "
                  f"(cl100k proxy, calibrated 1.01-1.02 vs real)")
    else:
        claim = d.get("savings_claim")
        print(f"  REMOVED       nothing"
              + (f" — {claim}" if claim else ""))

    groups = d.get("dropped_groups") or []
    names = d.get("dropped_names") or []
    if groups:
        print()
        print("  WHY REMOVED   (group -> not needed for this client/model/task)")
        for g in groups:
            print(f"    - {g}")
    if names:
        print()
        print(f"  DROPPED TOOLS ({len(names)})")
        # Widest first: the reader wants to know what the big wins were.
        largest = {n: b for n, b in (d.get("largest") or [])}
        for n in sorted(names, key=lambda x: -largest.get(x, 0)):
            size = largest.get(n)
            print(f"    - {n}" + (f"   {human(size)}" if size else ""))
    kept_note = d.get("removable_groups")
    if kept_note is not None:
        print()
        print(f"  Removable groups for this pair: {kept_note or '(none)'}")
    ns = d.get("namespaces_expanded")
    if ns:
        print(f"  Namespace bundles expanded: {ns}")
    print(f"  Registry      {d.get('registry')}   "
          f"hook overhead {d.get('overhead_ms')} ms")
    print()

records = []
for line in sys.stdin:
    if "tool_gateway_metric " not in line:
        continue
    try:
        d = json.loads(line.split("tool_gateway_metric ", 1)[1])
    except Exception:
        continue
    if d.get("event"):
        continue
    records.append(d)

if not records:
    print("No gateway records in this window.")
    print()
    print("The gateway is SILENT when AILOCAL_TOOL_GATEWAY=off, which is the")
    print("shipped default. That is not evidence no requests were served — check")
    print("with: docker exec ailocal-litellm printenv AILOCAL_TOOL_GATEWAY")
    raise SystemExit(1)

if MODE == "all":
    for d in records:
        render(d)
    print(f"{len(records)} request(s).")
else:
    # The largest payload is the interesting one: turn 1 carries the full
    # declaration, later turns re-declare a subset and would understate the case.
    render(max(records, key=lambda r: r.get("bytes_in", 0)))
    if len(records) > 1:
        print(f"(largest of {len(records)} requests in this window; "
              f"--all for every one)")
PY

if [ "$MODE" = watch ]; then
  echo "Watching for gateway decisions (Ctrl-C to stop)…"
  docker logs -f --since 1s "$CONTAINER" 2>&1 \
    | grep --line-buffered "tool_gateway_metric" \
    | while IFS= read -r line; do
        printf '%s\n' "$line" | SUMMARY_MODE=last python3 /tmp/ailocal-tool-summary.py
      done
else
  docker logs --since "$SINCE" "$CONTAINER" 2>&1 \
    | SUMMARY_MODE="$MODE" python3 /tmp/ailocal-tool-summary.py
fi
