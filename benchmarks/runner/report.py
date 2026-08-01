"""report.py — JSONL -> CSV + Markdown.

Statistical rules enforced here:
  * report MEDIAN with min/max and dispersion, never a bare mean;
  * flag a cell as UNSTABLE rather than presenting a tidy number;
  * report numerator/denominator, so 1/1 never reads like 100%;
  * keep failed and unsupported runs separate from a score of zero.
"""
import argparse
import csv
import json
import os
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C  # noqa: E402


def supersede(runs):
    """Keep only the LATEST record per (cell, phase, repetition).

    Results are append-only, so re-running a cell with --force ADDS records
    rather than replacing them. Without this, a cell re-run on a clean machine
    would be averaged together with the contaminated samples it was re-run to
    replace -- hiding exactly the problem the re-run was meant to fix. Later
    timestamp wins; superseded records stay on disk as history.
    """
    latest = {}
    for r in runs:
        k = C.run_key(r)
        prev = latest.get(k)
        if prev is None or (r.get("timestamp") or "") > (prev.get("timestamp") or ""):
            latest[k] = r
    return list(latest.values())


def load(name="runs.jsonl"):
    p = C.results_path(name)
    if not os.path.exists(p):
        return []
    out = []
    with open(p) as f:
        for line in f:
            try:
                out.append(json.loads(line))
            except Exception:
                continue
    return out


def flatten(r):
    t = r.get("timings") or {}
    mem = (r.get("memory_metrics") or {})
    return {
        "run_id": r.get("run_id"), "timestamp": r.get("timestamp"),
        "git_commit": r.get("git_commit"), "suite": r.get("task_suite"),
        "task_id": r.get("task_id"), "model": r.get("model_tag"),
        "requested_ctx": r.get("requested_context_tokens"),
        "actual_ctx": r.get("actual_context_tokens"),
        "within_1pct": r.get("fixture_within_1pct"),
        "reasoning": r.get("reasoning_mode_requested"),
        "phase": r.get("cold_or_warm"), "rep": r.get("repetition"),
        "prompt_tok_s": t.get("prompt_tok_s"), "gen_tok_s": t.get("gen_tok_s"),
        "load_s": t.get("load_duration_s"), "wall_s": t.get("wall_s"),
        "total_s": t.get("total_duration_s"),
        "prompt_tokens": t.get("prompt_eval_count"), "gen_tokens": t.get("eval_count"),
        "reasoning_chars": t.get("reasoning_chars"), "answer_chars": t.get("answer_chars"),
        "truncated": t.get("truncated"),
        "swap_before": (mem.get("before") or {}).get("swap_used_gb"),
        "swap_after": (mem.get("after") or {}).get("swap_used_gb"),
        "free_mem_after": (mem.get("after") or {}).get("free_memory_pct"),
        "points": (r.get("score") or {}).get("points"),
        "max_points": (r.get("score") or {}).get("max"),
        "errors": "; ".join(r.get("errors") or []),
    }


def agg(vals):
    vals = [v for v in vals if isinstance(v, (int, float))]
    if not vals:
        return None
    med = statistics.median(vals)
    spread = (max(vals) - min(vals)) / med if med else 0
    return {"median": med, "min": min(vals), "max": max(vals),
            "n": len(vals), "spread": spread, "unstable": spread > 0.25}


def fmt(a, unit=""):
    if not a:
        return "—"
    s = f"{a['median']:,.0f}{unit}" if a["median"] >= 10 else f"{a['median']:,.1f}{unit}"
    if a["unstable"]:
        s += f" ⚠±{a['spread']*100:.0f}%"
    return s


def markdown(rows):
    ok = [r for r in rows if not r["errors"]]
    models = sorted({r["model"] for r in ok})
    ctxs = sorted({r["requested_ctx"] for r in ok if r["requested_ctx"]})
    L = ["# ailocal model benchmark", "",
         f"Runs: **{len(rows)}** total, **{len(rows)-len(ok)}** errored.",
         "Median of warm repetitions unless stated. `⚠` marks >25% spread —",
         "an unstable cell, not a precise figure.", ""]

    L += ["## Prompt throughput (tok/s, warm, native counters)", ""]
    L.append("| model | " + " | ".join(f"{c//1024}K" for c in ctxs) + " |")
    L.append("|---|" + "---|" * len(ctxs))
    for m in models:
        cells = []
        for c in ctxs:
            a = agg([r["prompt_tok_s"] for r in ok
                     if r["model"] == m and r["requested_ctx"] == c and r["phase"] == "warm"])
            cells.append(fmt(a))
        L.append(f"| `{m}` | " + " | ".join(cells) + " |")

    L += ["", "## Generation throughput (tok/s, warm)", ""]
    L.append("| model | " + " | ".join(f"{c//1024}K" for c in ctxs) + " |")
    L.append("|---|" + "---|" * len(ctxs))
    for m in models:
        cells = []
        for c in ctxs:
            a = agg([r["gen_tok_s"] for r in ok
                     if r["model"] == m and r["requested_ctx"] == c and r["phase"] == "warm"])
            cells.append(fmt(a))
        L.append(f"| `{m}` | " + " | ".join(cells) + " |")

    L += ["", "## Cold load and first-response latency", "",
          "| model | cold load (s) | cold wall (s) | warm wall (s) |", "|---|---|---|---|"]
    for m in models:
        cl = agg([r["load_s"] for r in ok if r["model"] == m and r["phase"] == "cold"])
        cw = agg([r["wall_s"] for r in ok if r["model"] == m and r["phase"] == "cold"])
        ww = agg([r["wall_s"] for r in ok if r["model"] == m and r["phase"] == "warm"])
        L.append(f"| `{m}` | {fmt(cl)} | {fmt(cw)} | {fmt(ww)} |")

    # Context fidelity: a cell whose fixture missed +/-1% is not comparable.
    bad = [r for r in ok if r["within_1pct"] is False]
    if bad:
        L += ["", "## Fixture fidelity warnings", "",
              "These cells did NOT hit the ±1% token target and are not directly",
              "comparable across models:", ""]
        for r in sorted({(r["model"], r["requested_ctx"], r["actual_ctx"]) for r in bad}):
            L.append(f"- `{r[0]}` requested {r[1]}, actual {r[2]}")

    # ── accuracy AT each context size, beside throughput ────────────────────
    # Ingestion speed alone answers "can it load 64K", not "can it USE 64K".
    # The retrieval suite plants a fact at a controlled position and grades the
    # answer exactly, so this table pairs tok/s with whether the context was
    # actually usable at that size.
    ret = [r for r in rows if r["suite"] == "retrieval"]
    if ret:
        L += ["", "## Context USABILITY (retrieval accuracy per size)", "",
              "`late` = fact near the end. `distributed` = fact split across",
              "positions and must be joined. A model that accepts 64K but only",
              "attends to the tail passes `late` and fails `distributed`.", ""]
        rctx = sorted({r["requested_ctx"] for r in ret})
        L.append("| model | variant | " + " | ".join(f"{c//1024}K" for c in rctx) + " |")
        L.append("|---|---|" + "---|" * len(rctx))
        for mdl in sorted({r["model"] for r in ret}):
            for v in ("late", "distributed"):
                cells = []
                for c in rctx:
                    hits = [r for r in ret if r["model"] == mdl and r["task_id"] == v
                            and r["requested_ctx"] == c]
                    if not hits:
                        cells.append("—")
                    else:
                        got = sum(1 for h in hits if h.get("points"))
                        cells.append(f"{got}/{len(hits)}")
                if any(c != "—" for c in cells):
                    L.append(f"| `{mdl}` | {v} | " + " | ".join(cells) + " |")

    # ── per-area leaderboard: who is best, and how fast ─────────────────────
    areas = [("fastcode", "Fast coding"), ("cruxeval-o", "Code understanding"),
             ("architecture", "Architecture"), ("retrieval", "Long-context retrieval")]
    have = [(k, n) for k, n in areas if any(r["suite"] == k for r in rows)]
    if have:
        L += ["", "## Per-area standings (correctness first, then speed)", "",
              "Ranked by score. Latency is the median for that area — a fast",
              "wrong answer never outranks a correct one.", ""]
        for key, name in have:
            sub = [r for r in rows if r["suite"] == key]
            L += ["", f"### {name}", "",
                  "| model | reasoning | score | median latency |", "|---|---|---|---|"]
            agg_rows = {}
            for r in sub:
                k = (r["model"], r["reasoning"])
                a = agg_rows.setdefault(k, {"pts": 0, "max": 0, "lat": []})
                a["pts"] += r.get("points") or 0
                a["max"] += r.get("max_points") or 0
                if r["wall_s"]:
                    a["lat"].append(r["wall_s"])
            for (mdl, mode), a in sorted(
                    agg_rows.items(),
                    key=lambda kv: (-(kv[1]["pts"] / kv[1]["max"] if kv[1]["max"] else 0),
                                    statistics.median(kv[1]["lat"]) if kv[1]["lat"] else 1e9)):
                pct = f"{a['pts']}/{a['max']}" + (
                    f" ({a['pts']/a['max']*100:.0f}%)" if a["max"] else "")
                lat = f"{statistics.median(a['lat']):.1f}s" if a["lat"] else "—"
                L.append(f"| `{mdl}` | {mode} | {pct} | {lat} |")

    errs = [r for r in rows if r["errors"]]
    if errs:
        L += ["", "## Errors (reported separately, NOT scored as zero)", ""]
        for r in errs[:20]:
            L.append(f"- `{r['model']}` @{r['requested_ctx']} {r['phase']}: {r['errors'][:120]}")
    return "\n".join(L) + "\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--score", action="store_true")
    a = ap.parse_args()
    runs = load()
    superseded = len(runs)
    runs = supersede(runs)
    superseded -= len(runs)
    if not runs:
        print("no runs recorded yet"); return 1
    rows = [flatten(r) for r in runs]
    os.makedirs(C.RESULTS, exist_ok=True)
    csv_p = os.path.join(C.RESULTS, "runs.csv")
    with open(csv_p, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader(); w.writerows(rows)
    md_p = os.path.join(C.RESULTS, "report.md")
    with open(md_p, "w") as f:
        f.write(markdown(rows))
    if superseded:
        print(f"  {superseded} superseded record(s) excluded (re-runs replace older samples)")
    print(f"  {len(rows)} runs -> {csv_p}")
    print(f"                 -> {md_p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
