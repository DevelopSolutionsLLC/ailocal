#!/usr/bin/env bash
# search-health.sh — per-engine observability for the search path.
#
# WHY THIS EXISTS: single-shot search probes lie. A cold query returns 60+
# results and looks perfectly healthy while the general-web engines are one
# burst away from suspension. The failure this catches is volume-triggered, so
# it can only be seen by issuing a realistic burst and counting per engine.
#
# Measured on 2026-07-28, ten agent-style queries, twice:
#   run 1   google cse 10%   duckduckgo 30%   github/stackoverflow 100%
#   run 2   google cse  0%   duckduckgo  0%   API-backed engines 90-100%
# With both scraped engines at 0% the instance still returned 52.1 results/query
# and zero empty queries, which is what justified demoting them to opt-in.
#
# Reports, per engine: served / failed / rate, the failure reasons, results per
# query, zero-result queries, and latency. Also checks the LiteLLM leg, because
# SearXNG being healthy says nothing about whether the proxy can reach it.
#
# Usage:
#   ./scripts/search-health.sh            # 10 default queries
#   ./scripts/search-health.sh --quick    # 3 queries
#   ./scripts/search-health.sh --json     # machine-readable, for baselines
set -uo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SEARXNG="${SEARXNG_URL:-http://127.0.0.1:8080}"
MODE="full"
for a in "$@"; do
  case "$a" in
    --quick) MODE="quick" ;;
    --json)  MODE="json" ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
  esac
done

if ! curl -sf -m 5 "$SEARXNG/search?q=ping&format=json" >/dev/null 2>&1; then
  echo "✗ SearXNG not reachable at $SEARXNG" >&2
  exit 1
fi

MODE="$MODE" SEARXNG="$SEARXNG" ROOT_DIR="$ROOT_DIR" python3 - <<'PY'
import collections, json, os, subprocess, time, urllib.parse, urllib.request

mode, base, root = os.environ["MODE"], os.environ["SEARXNG"], os.environ["ROOT_DIR"]
QUERIES = ["python dataclass", "rust ownership", "docker compose healthcheck",
           "litellm proxy config", "gopls workspace", "bash strict mode",
           "qdrant vector search", "ollama keep_alive",
           "pydantic validation error", "systemd unit file"]
if mode == "quick":
    QUERIES = QUERIES[:3]

served, failed, reasons = collections.Counter(), collections.Counter(), collections.Counter()
counts, lats, zero = [], [], 0

for q in QUERIES:
    url = base + "/search?" + urllib.parse.urlencode({"q": q, "format": "json"})
    t0 = time.time()
    try:
        d = json.load(urllib.request.urlopen(url, timeout=45))
    except Exception as exc:  # noqa: BLE001
        reasons[f"request failed: {exc}"] += 1
        continue
    lats.append(time.time() - t0)
    n = len(d.get("results") or [])
    counts.append(n)
    if n == 0:
        zero += 1
    engines = set()
    for r in d.get("results") or []:
        engines.update(r.get("engines") or [])
    for e in engines:
        served[e] += 1
    for pair in d.get("unresponsive_engines") or []:
        name = pair[0] if isinstance(pair, list) else str(pair)
        why = pair[1] if isinstance(pair, list) and len(pair) > 1 else "?"
        failed[name] += 1
        reasons[f"{name}: {why}"] += 1
    time.sleep(1)

n = len(lats)
mean_lat = sum(lats) / n if n else 0.0
mean_res = sum(counts) / n if n else 0.0

# The LiteLLM leg. SearXNG answering directly proves nothing about the path the
# models actually use — the proxy reaches it by service name over the compose
# network, which is a different route with its own failure modes.
litellm_results = None
try:
    key = ""
    for line in open(os.path.join(root, ".env"), encoding="utf-8"):
        if line.startswith("LITELLM_MASTER_KEY="):
            key = line.split("=", 1)[1].strip().strip('"').strip("'")
    req = urllib.request.Request(
        "http://127.0.0.1:4000/v1/search",
        data=json.dumps({"search_tool_name": "searxng-search",
                         "query": "docker compose healthcheck"}).encode(),
        headers={"Authorization": f"Bearer {key}", "content-type": "application/json"})
    litellm_results = len(json.load(urllib.request.urlopen(req, timeout=90)).get("results") or [])
except Exception:  # noqa: BLE001
    litellm_results = None

doc = {
    "queries": n,
    "mean_results": round(mean_res, 1),
    "zero_result_queries": zero,
    "mean_latency_s": round(mean_lat, 2),
    "max_latency_s": round(max(lats), 2) if lats else 0,
    "engines": {e: {"served": served[e], "failed": failed[e],
                    "rate_pct": round(100 * served[e] / max(served[e] + failed[e], 1))}
                for e in sorted(set(list(served) + list(failed)))},
    "failure_reasons": dict(reasons),
    "litellm_v1_search_results": litellm_results,
}

if mode == "json":
    print(json.dumps(doc, indent=2))
    raise SystemExit(0)

print(f"  queries={doc['queries']}  mean_results={doc['mean_results']}  "
      f"zero_result_queries={doc['zero_result_queries']}")
print(f"  latency mean={doc['mean_latency_s']}s max={doc['max_latency_s']}s")
print()
print(f"  {'engine':16s} {'served':>7s} {'failed':>7s}  rate")
for e, s in doc["engines"].items():
    print(f"  {e:16s} {s['served']:7d} {s['failed']:7d}  {s['rate_pct']:4d}%")
if doc["failure_reasons"]:
    print("\n  failure reasons:")
    for k, v in sorted(doc["failure_reasons"].items(), key=lambda kv: -kv[1]):
        print(f"    {v}x  {k}")
print()
lr = doc["litellm_v1_search_results"]
print(f"  LiteLLM /v1/search: {lr} results" if lr is not None
      else "  LiteLLM /v1/search: UNREACHABLE (the path models actually use)")

# Health verdict. Zero-result queries are the real regression signal; an
# individual scraped engine at 0% is expected and is why they are opt-in.
if doc["zero_result_queries"]:
    print(f"\n  ✗ {doc['zero_result_queries']} query(ies) returned NOTHING — investigate")
    raise SystemExit(1)
if lr is None:
    print("\n  ✗ SearXNG is healthy but LiteLLM cannot reach it")
    raise SystemExit(1)
print("\n  ✓ search healthy")
PY
