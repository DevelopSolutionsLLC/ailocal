"""throughput.py — Layer A: raw inference, through the endpoint clients use.

Measures the model and proxy without agent overhead.

Two separations this file refuses to blur:

  COLD vs WARM. A cold run pays model load; folding that into decode throughput
  makes a big model look slower than it is at steady state. The model is
  explicitly evicted and its absence verified before a cold run, and
  load_duration is reported on its own.

  NATIVE vs WALL-CLOCK. tok/s comes from prompt_eval_count/prompt_eval_duration
  and eval_count/eval_duration. Wall-clock is recorded separately as
  request-level latency, never substituted for throughput.
"""
import argparse
import os
import sys
import time
import statistics
import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C          # noqa: E402
import fixtures as F        # noqa: E402


def measure_tokens(ollama, tag, text):
    """Backend's own count for a candidate fixture — 4 predicted tokens keeps
    calibration cheap while still returning prompt_eval_count."""
    r = C.post(f"{ollama}/api/chat", {
        "model": tag, "stream": False,
        "messages": [{"role": "user", "content": text}],
        "options": {"num_ctx": 262144, "num_predict": 4, "temperature": 0},
    }, timeout=1800)
    return r.get("prompt_eval_count")


def one_run(ollama, tag, text, num_ctx, completion_limit, think, npredict):
    body = {
        "model": tag, "stream": False,
        "messages": [{"role": "user", "content": text}],
        "options": {"num_ctx": num_ctx, "num_predict": npredict,
                    "temperature": 0, "top_p": 1.0, "seed": 42},
    }
    if think is not None:
        body["think"] = think
    t0 = time.time()
    r = C.post(f"{ollama}/api/chat", body, timeout=1800)
    wall = time.time() - t0
    t = C.timings_from_ollama(r)
    msg = r.get("message") or {}
    t["wall_s"] = wall
    t["reasoning_chars"] = len(msg.get("thinking") or "")
    t["answer_chars"] = len(msg.get("content") or "")
    t["truncated"] = r.get("done_reason") == "length"
    return t


def run(args):
    m = C.manifest()
    ollama = m["endpoint"]["ollama"]
    limits = m["limits"]
    baseline_swap = C.swap_used_gb()
    cache = F.load_cache(C.BENCH)
    done = C.completed_keys()
    caps_path = os.path.join(C.RESULTS, "capabilities.json")
    caps = {}
    if os.path.exists(caps_path):
        import json
        caps = json.load(open(caps_path)).get("models", {})

    tags = [e["tag"] for e in m["models"]]
    if args.model:
        tags = [t for t in tags if t == args.model]
    targets = args.context or m["context_targets"]
    versions = C.software_versions(ollama)

    planned = skipped = executed = 0
    for tag in tags:
        cap = caps.get(tag, {})
        if cap.get("available") is False:
            print(f"  SKIP {tag}: not installed")
            continue
        for target in targets:
            planned += 1
            # Skip cells the probe proved impossible, with a machine-readable reason.
            ctx_probe = (cap.get("context") or {}).get(str(target))
            if ctx_probe and ctx_probe.get("accepted") is False:
                print(f"  SKIP {tag} @{target}: probe says unsupported")
                skipped += 1
                continue

            reason = C.safety_check(limits, baseline_swap)
            if reason:
                print(f"  ABORT: {reason}")
                return 2

            # ── calibrate a fixture to +/-1% of target, per model ────────────
            ckey = f"{tag}|{target}|{args.variant}"
            C.unload_all_except(ollama, keep=(tag,))
            if ckey in cache:
                scale = cache[ckey]["scale"]
                text, _ = F.build(target, args.variant, scale=scale)
                measured = cache[ckey]["measured"]
            else:
                try:
                    text, measured, scale, iters = F.calibrate(
                        lambda t: measure_tokens(ollama, tag, t), target, args.variant)
                    cache[ckey] = {"scale": scale, "measured": measured,
                                   "iterations": iters,
                                   "fingerprint": F.fingerprint(text)}
                    F.save_cache(C.BENCH, cache)
                except Exception as e:
                    print(f"  FAIL {tag} @{target}: calibration {type(e).__name__}")
                    skipped += 1
                    continue
            off = abs(measured - target) / target
            label = "ok" if off <= 0.01 else f"OFF-TARGET {off*100:.1f}%"

            for phase, reps in (("cold", m["runs"]["cold"]), ("warm", m["runs"]["warm"])):
                samples = []
                for rep in range(reps):
                    key = "|".join(str(x) for x in (
                        "throughput", args.variant, tag, target, args.reasoning,
                        phase, rep))
                    if key in done and not args.force:
                        continue
                    # A NOVEL prompt per repetition, at the same calibrated size.
                    # Without this, warm reps hit the KV cache and report absurd
                    # throughput (measured: 837,866 tok/s) instead of real work.
                    nonce = (0 if phase == "cold" else rep + 1)
                    run_text, _ = F.build(target, args.variant,
                                          scale=cache[ckey]["scale"], nonce=nonce)
                    if phase == "cold" and rep == 0:
                        if not C.unload(ollama, tag):
                            print(f"  WARN {tag}: could not evict before cold run")
                    snap_before = C.machine_snapshot()
                    try:
                        t = one_run(ollama, tag, run_text, target,
                                    m["completion_limit"],
                                    None if args.reasoning == "default" else
                                    m["reasoning_modes"][args.reasoning]["think"],
                                    args.npredict)
                        err = []
                    except Exception as e:
                        t = {}
                        err = [f"{type(e).__name__}: {str(e)[:200]}"]
                    snap_after = C.machine_snapshot()
                    rec = {
                        "schema_version": C.SCHEMA_VERSION,
                        "run_id": f"{tag}-{target}-{phase}-{rep}-{int(time.time())}",
                        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                        "git_commit": versions.get("git_commit"),
                        "software_versions": versions,
                        "task_suite": "throughput",
                        "task_id": args.variant,
                        "model_tag": tag,
                        "requested_context_tokens": target,
                        "actual_context_tokens": measured,
                        "fixture_within_1pct": off <= 0.01,
                        "completion_limit": m["completion_limit"],
                        "reasoning_mode_requested": args.reasoning,
                        "cold_or_warm": phase,
                        "repetition": rep,
                        "timings": t,
                        "memory_metrics": {"before": snap_before, "after": snap_after},
                        "errors": err,
                    }
                    C.append_result(rec)
                    executed += 1
                    if err:
                        print(f"  {tag:<28} @{target:<6} {phase} rep{rep}  ERROR {err[0][:50]}")
                    else:
                        samples.append(t)
                        print(f"  {tag:<28} @{target:<6} {phase} rep{rep}  "
                              f"pe {t['prompt_tok_s'] or 0:7.0f}/s  "
                              f"gen {t['gen_tok_s'] or 0:6.1f}/s  "
                              f"load {t['load_duration_s']:5.1f}s  [{label}]")
                if phase == "warm" and len(samples) >= 2:
                    pes = [s["prompt_tok_s"] for s in samples if s.get("prompt_tok_s")]
                    if len(pes) >= 2:
                        spread = (max(pes) - min(pes)) / max(statistics.median(pes), 1)
                        if spread > 0.25:
                            print(f"      UNSTABLE: warm prompt tok/s spread {spread*100:.0f}%")
    print(f"\n  planned {planned} cells, skipped {skipped}, runs executed {executed}")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model")
    ap.add_argument("--context", type=int, action="append")
    ap.add_argument("--reasoning", default="default")
    ap.add_argument("--variant", default="late", choices=("late", "distributed"))
    ap.add_argument("--npredict", type=int, default=64,
                    help="generation ceiling for throughput cells; the "
                         "saturation suite uses the full 8192")
    ap.add_argument("--force", action="store_true")
    sys.exit(run(ap.parse_args()))


if __name__ == "__main__":
    main()
