"""export_lmeval.py — emit results in the lm-evaluation-harness shape.

Our per-run JSONL is already the equivalent of harness `samples_<task>_*.jsonl`
(one record per instance, with the prompt-independent metadata). What was
missing is the AGGREGATE `results_*.json` that standard tooling actually reads:

    config            run/model metadata
    configs           per-task settings (the sampling actually used)
    results           task -> metric values
    higher_is_better  metric directionality
    n-samples         numerator/denominator per task
    date, versions

Reference: https://github.com/EleutherAI/lm-evaluation-harness
(docs/task_guide.md; results_*.json + samples_*.jsonl convention)

This is a FORMAT ADAPTER, not a claim of harness compatibility: these tasks are
not registered harness tasks and the numbers are not produced by harness code.
It makes the output machine-readable by conventional tooling and comparable
across our own runs.
"""
import datetime
import json
import os
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C  # noqa: E402

HIGHER_IS_BETTER = {
    "acc": True, "pass_rate": True, "precision": True, "recall": True,
    "prompt_tok_s": True, "gen_tok_s": True, "latency_s": False,
    "reasoning_chars": False,
}


def build(runs):
    m = C.manifest()
    by = {}
    for r in runs:
        if r.get("errors"):
            continue
        suite, tag = r.get("task_suite"), r.get("model_tag")
        mode = r.get("reasoning_mode_requested")
        by.setdefault((tag, mode), {}).setdefault(suite, []).append(r)

    out = {}
    for (tag, mode), suites in by.items():
        results, nsamples, configs = {}, {}, {}
        for suite, rows in suites.items():
            pts = [(r.get("score") or {}).get("points") for r in rows]
            mx = [(r.get("score") or {}).get("max") for r in rows]
            pts = [p for p in pts if isinstance(p, (int, float))]
            mx = [x for x in mx if isinstance(x, (int, float))]
            lat = [(r.get("timings") or {}).get("wall_s") for r in rows]
            lat = [x for x in lat if isinstance(x, (int, float))]
            entry = {}
            if pts and mx and sum(mx):
                entry["acc"] = round(sum(pts) / sum(mx), 4)
            if lat:
                entry["latency_s"] = round(statistics.median(lat), 2)
            pe = [(r.get("timings") or {}).get("prompt_tok_s") for r in rows]
            pe = [x for x in pe if isinstance(x, (int, float))]
            if pe:
                entry["prompt_tok_s"] = round(statistics.median(pe), 1)
            ge = [(r.get("timings") or {}).get("gen_tok_s") for r in rows]
            ge = [x for x in ge if isinstance(x, (int, float))]
            if ge:
                entry["gen_tok_s"] = round(statistics.median(ge), 1)
            # review carries its own precision/recall
            prec = [(r.get("score") or {}).get("precision") for r in rows]
            prec = [x for x in prec if isinstance(x, (int, float))]
            if prec:
                entry["precision"] = round(statistics.mean(prec), 3)
            rec = [(r.get("score") or {}).get("recall") for r in rows]
            rec = [x for x in rec if isinstance(x, (int, float))]
            if rec:
                entry["recall"] = round(statistics.mean(rec), 3)
            results[suite] = entry
            nsamples[suite] = {"effective": len(rows),
                               "points": sum(pts) if pts else None,
                               "max": sum(mx) if mx else None}
            opts, prefix = C.model_params(tag, mode, "coding",
                                          thinking=(mode != "off"))
            configs[suite] = {"sampling": opts, "system_prefix": prefix,
                              "reasoning_mode": mode,
                              "num_ctx": rows[0].get("requested_context_tokens")}
        out[(tag, mode)] = {
            "config": {"model": tag, "model_args": f"ollama_chat/{tag}",
                       "reasoning_mode": mode,
                       "batch_size": 1, "device": "apple-silicon-metal",
                       "limit": None, "bootstrap_iters": 0,
                       "gen_kwargs": C.model_params(tag, mode, "coding")[0]},
            "configs": configs,
            "results": results,
            "higher_is_better": {k: {m2: HIGHER_IS_BETTER.get(m2)
                                     for m2 in v} for k, v in results.items()},
            "n-samples": nsamples,
            "date": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "git_hash": C.git_commit(),
            "versions": (runs[0].get("software_versions") if runs else {}),
            "format_note": "lm-evaluation-harness-SHAPED, not harness-produced. "
                           "Tasks are not registered harness tasks; see "
                           "benchmarks/README.md for adaptations.",
        }
    return out


def main():
    p = C.results_path()
    if not os.path.exists(p):
        print("no runs yet"); return 1
    runs = [json.loads(l) for l in open(p) if l.strip()]
    built = build(runs)
    os.makedirs(C.RESULTS, exist_ok=True)
    n = 0
    for (tag, mode), doc in built.items():
        safe = tag.replace(":", "_").replace("/", "_")
        fp = os.path.join(C.RESULTS, f"results_{safe}_{mode}.json")
        with open(fp, "w") as f:
            json.dump(doc, f, indent=2)
        n += 1
    print(f"  wrote {n} results_*.json (lm-eval-harness shape) in {C.RESULTS}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
