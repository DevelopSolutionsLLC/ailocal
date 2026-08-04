#!/usr/bin/env python3
"""gateway-metrics.py — aggregate the gateway's own metric stream into a report.

The negotiator emits one `tool_gateway_metric ` JSON line per request. This reads
them (from `docker logs`, or a file) and reports what actually happened: payload
sizes, what was dropped and why, hook overhead, and the distribution across
clients, model classes and task classes.

WHAT IT WILL NOT DO
-------------------
Report an improvement it cannot substantiate. Specifically:

- Every ratio uses `bytes_kept_reachable` over `bytes_reachable` — what the model
  actually received over what the route would have forwarded. Using `bytes_kept`
  or `bytes_in` produces nonsense on /v1/responses, where LiteLLM discards
  namespace tools itself: an earlier version of exactly this calculation reported
  a -133.7% "reduction".
- Records from `off` mode carry no negotiation decision, so they are counted
  separately and excluded from reduction statistics rather than averaged in as
  zeros.
- Token figures are the cl100k proxy and are labelled as such. Calibrated at
  1.009-1.021 against Ollama's real prompt_eval_count, so they under-report
  slightly; re-measure against Ollama's prompt_eval_count after a model change.
- Latency is NOT reported here. These records contain the hook's own overhead,
  not end-to-end request time; presenting hook microseconds next to a model's
  seconds would invite exactly the wrong conclusion. End-to-end latency is
  `ailocal benchmark gateway`, which measures a real client.

Usage:
    scripts/gateway-metrics.py                       # from docker logs
    scripts/gateway-metrics.py --since 30m
    scripts/gateway-metrics.py --file captured.log
    scripts/gateway-metrics.py --json               # machine-readable
"""

import argparse
import collections
import json
import statistics
import subprocess
import sys

PREFIX = "tool_gateway_metric "


def read_docker(container, since):
    cmd = ["docker", "logs"]
    if since:
        cmd += ["--since", since]
    cmd.append(container)
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    return (p.stdout + p.stderr).splitlines()


def parse(lines):
    """Metric records only. Event records (gateway_init, bad_mode, …) are kept
    separately: they are operational signals, not request measurements, and
    averaging them in would corrupt every statistic."""
    records, events, malformed = [], [], 0
    for line in lines:
        idx = line.find(PREFIX)
        if idx < 0:
            continue
        try:
            rec = json.loads(line[idx + len(PREFIX):])
        except Exception:
            malformed += 1
            continue
        (events if rec.get("event") else records).append(rec)
    return records, events, malformed


def pct(part, whole):
    return (100.0 * part / whole) if whole else 0.0


def summarize(records):
    by_mode = collections.Counter(r.get("mode") or "unknown" for r in records)
    negotiated = [r for r in records if (r.get("mode") in ("report", "filter"))]
    applied = [r for r in negotiated if r.get("applied")]
    passthrough = [r for r in negotiated if r.get("passthrough")]

    out = {
        "requests_total": len(records),
        "by_mode": dict(by_mode),
        "negotiated": len(negotiated),
        "filter_applied": len(applied),
        "passthrough": len(passthrough),
        "tokenizer": "cl100k-proxy (calibrated 1.009-1.021 vs real prompt_eval_count)",
    }

    # Reduction statistics: only where a decision was made AND the request was
    # not passthrough. A passthrough request has a reduction of zero by design,
    # and averaging those in would understate the effect on the requests the
    # gateway actually acts on.
    acting = [r for r in negotiated if not r.get("passthrough")
              and r.get("bytes_reachable")]
    if acting:
        ratios = [pct(r["bytes_reachable"] - r.get("bytes_kept_reachable", 0),
                      r["bytes_reachable"]) for r in acting]
        out["acting_requests"] = len(acting)
        out["reduction_pct"] = {
            "min": round(min(ratios), 1),
            "median": round(statistics.median(ratios), 1),
            "max": round(max(ratios), 1),
        }
        out["bytes_reachable_total"] = sum(r["bytes_reachable"] for r in acting)
        out["bytes_delivered_total"] = sum(r.get("bytes_kept_reachable", 0)
                                           for r in acting)
        out["bytes_saved_by_drop"] = sum(r.get("bytes_dropped", 0) for r in acting)
        out["bytes_saved_by_rewrite"] = sum(r.get("bytes_saved_by_rewrite", 0)
                                            for r in acting)
        out["bytes_moot_litellm_already_dropped"] = sum(
            r.get("bytes_prefiltered_by_litellm", 0) for r in acting)
        toks_in = [r["tokens_est_in"] for r in acting
                   if isinstance(r.get("tokens_est_in"), int)]
        toks_kept = [r["tokens_est_kept"] for r in acting
                     if isinstance(r.get("tokens_est_kept"), int)]
        if toks_in and toks_kept:
            out["tokens_est_total_in"] = sum(toks_in)
            out["tokens_est_total_kept"] = sum(toks_kept)
    else:
        out["acting_requests"] = 0
        out["reduction_note"] = ("no request was both negotiated and "
                                 "non-passthrough — nothing to report a "
                                 "reduction over")

    overheads = [r["overhead_ms"] for r in records
                 if isinstance(r.get("overhead_ms"), (int, float))]
    if overheads:
        srt = sorted(overheads)
        out["hook_overhead_ms"] = {
            "median": round(statistics.median(srt), 3),
            "p95": round(srt[min(len(srt) - 1, int(len(srt) * 0.95))], 3),
            "max": round(max(srt), 3),
            "note": "hook only; NOT end-to-end request latency",
        }

    out["clients"] = dict(collections.Counter(
        r.get("client") or "unknown" for r in records))
    out["model_classes"] = dict(collections.Counter(
        r.get("model_class") or "unmatched" for r in records))
    out["routes"] = dict(collections.Counter(
        r.get("route") or "unknown" for r in records))
    out["task_classes"] = dict(collections.Counter(
        str(r.get("task_class")) for r in negotiated))
    out["registry_states"] = dict(collections.Counter(
        r.get("registry") or "unknown" for r in records))

    dropped = collections.Counter()
    dropped_bytes = collections.Counter()
    for r in acting:
        for name in r.get("dropped_names") or []:
            dropped[name] += 1
        for g in r.get("dropped_groups") or []:
            dropped_bytes[g] += 1
    out["most_dropped_tools"] = dropped.most_common(15)
    out["dropped_group_frequency"] = dropped_bytes.most_common()
    return out


def human(s, events, malformed):
    W = 42
    print("=" * 70)
    print("GATEWAY METRICS")
    print("=" * 70)
    print(f"{'requests with a metric record':{W}} {s['requests_total']}")
    print(f"{'  by mode':{W}} {s['by_mode']}")
    print(f"{'  negotiated (report/filter)':{W}} {s['negotiated']}")
    print(f"{'  filter actually applied':{W}} {s['filter_applied']}")
    print(f"{'  passthrough (forwarded intact)':{W}} {s['passthrough']}")
    if malformed:
        print(f"{'  MALFORMED records skipped':{W}} {malformed}")
    if events:
        kinds = collections.Counter(e.get("event") for e in events)
        print(f"{'  operational events (not requests)':{W}} {dict(kinds)}")

    print()
    if not s.get("acting_requests"):
        print(s.get("reduction_note", "No reduction data."))
    else:
        print(f"PAYLOAD, over {s['acting_requests']} negotiated non-passthrough "
              f"request(s)")
        print(f"{'  bytes the route would forward':{W}} {s['bytes_reachable_total']}")
        print(f"{'  bytes the model received':{W}} {s['bytes_delivered_total']}")
        print(f"{'  saved by dropping tools':{W}} {s['bytes_saved_by_drop']}")
        print(f"{'  saved by rewriting schemas':{W}} {s['bytes_saved_by_rewrite']}")
        print(f"{'  (moot: LiteLLM dropped anyway)':{W}} "
              f"{s['bytes_moot_litellm_already_dropped']}")
        r = s["reduction_pct"]
        print(f"{'  reduction min/median/max':{W}} "
              f"{r['min']}% / {r['median']}% / {r['max']}%")
        if "tokens_est_total_in" in s:
            print(f"{'  tokens_est in -> delivered':{W}} "
                  f"{s['tokens_est_total_in']} -> {s['tokens_est_total_kept']}")
        print(f"{'  tokenizer':{W}} {s['tokenizer']}")

    if "hook_overhead_ms" in s:
        o = s["hook_overhead_ms"]
        print()
        print(f"HOOK OVERHEAD  median {o['median']} ms | p95 {o['p95']} ms | "
              f"max {o['max']} ms")
        print(f"  {o['note']} — end-to-end latency is "
              f"ailocal benchmark gateway")

    print()
    for label, key in (("clients", "clients"), ("model classes", "model_classes"),
                       ("routes", "routes"), ("task classes", "task_classes"),
                       ("registry state", "registry_states")):
        print(f"{label:22} {s[key]}")

    if s["most_dropped_tools"]:
        print()
        print("MOST-DROPPED TOOLS")
        for name, n in s["most_dropped_tools"]:
            print(f"    {name:44} x{n}")
        print(f"  by group: {dict(s['dropped_group_frequency'])}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--container", default="ailocal-litellm")
    ap.add_argument("--since", help="docker logs --since value, e.g. 30m")
    ap.add_argument("--file", help="read a saved log instead of docker")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    if args.file:
        with open(args.file, encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    else:
        lines = read_docker(args.container, args.since)

    records, events, malformed = parse(lines)
    if not records:
        print("No gateway metric records found.")
        print("The gateway is silent when AILOCAL_TOOL_GATEWAY=off (the default),"
              " so this is")
        print("NOT evidence that no requests were served. Set it to report or "
              "filter first.")
        return 1

    s = summarize(records)
    if args.json:
        print(json.dumps(s, indent=2))
    else:
        human(s, events, malformed)
    return 0


if __name__ == "__main__":
    sys.exit(main())
