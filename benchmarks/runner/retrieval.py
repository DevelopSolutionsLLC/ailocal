"""retrieval.py — the cheapest real ACCURACY signal: can the model use the
context it accepted?

Throughput proves a model ingested 64K tokens. It says nothing about whether the
model could find anything in them. This suite asks a question only answerable
from a fact planted inside the fixture, and grades the answer exactly.

Two placements, because they fail differently:
  * "late"        — the fact sits near the end. A model that only attends to the
                    tail still passes.
  * "distributed" — the fact is split in two halves, one mid-context and one at
                    the end, and must be JOINED to answer. Tail-attention fails.

Scoring is exact-match on a planted token, not a judged opinion: the answer is
either the string that was in the prompt or it is not. The model is told to
reply with the value alone, so verbosity cannot earn partial credit.

Deliberately NOT here: architecture, implementation, review, self-repair. Those
need real repositories, hidden tests and defect manifests (Tasks A-F). This is
one honest measurement, not a stand-in for them.
"""
import argparse
import datetime
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C          # noqa: E402
import fixtures as F        # noqa: E402

QUESTION = ("\n\nQUESTION: what is the effective retry budget in production, "
            "as stated in the operational note? Reply with the value ONLY, "
            "nothing else.")


def grade(answer, secret):
    """Exact containment of the planted token. Case-insensitive, whitespace
    tolerant; everything else is a miss. A model that explains at length but
    never names the value scores 0, which is correct -- the task was to find it."""
    if not answer:
        return 0, "empty"
    a = " ".join(answer.split()).upper()
    if secret.upper() in a:
        # Reward finding it; note verbosity separately rather than in the score.
        return 1, ("exact" if len(a) <= len(secret) + 24 else "found-but-verbose")
    return 0, "not-found"


def run(args):
    m = C.manifest()
    ollama = m["endpoint"]["ollama"]
    limits = m["limits"]
    baseline_swap = C.swap_used_gb()
    cache = F.load_cache(C.BENCH)
    done = C.completed_keys()
    versions = C.software_versions(ollama)

    tags = [e["tag"] for e in m["models"]]
    if args.model:
        tags = [t for t in tags if t == args.model]
    for x in (getattr(args, "exclude", None) or []):
        tags = [t for t in tags if t != x]
    targets = args.context or m["context_targets"]
    variants = [args.variant] if args.variant else ["late", "distributed"]

    for tag in tags:
        for target in targets:
            for variant in variants:
                key = "|".join(str(x) for x in (
                    "retrieval", variant, tag, target, args.reasoning, "warm", 0))
                if key in done and not args.force:
                    continue
                reason = C.safety_check(limits, baseline_swap)
                if reason:
                    print(f"  ABORT: {reason}")
                    return 2

                ckey = f"{tag}|{target}|{variant}"
                C.unload_all_except(ollama, keep=(tag,))
                if ckey not in cache:
                    print(f"  SKIP {tag} @{target} {variant}: no calibration "
                          f"(run the throughput suite first)")
                    continue
                text, secret = F.build(target, variant, scale=cache[ckey]["scale"])

                t0 = time.time()
                err = []
                answer = ""
                timings = {}
                try:
                    opts, prefix = C.model_params(tag, args.reasoning, "coding")
                    opts.update({"num_ctx": target, "num_predict": 512})
                    body = {"model": tag, "stream": False,
                            "messages": C.build_messages(text + QUESTION, prefix),
                            "options": opts}
                    if args.reasoning != "default" and not prefix:
                        body["think"] = m["reasoning_modes"][args.reasoning]["think"]
                    r = C.post(f"{ollama}/api/chat", body, timeout=1800)
                    answer = (r.get("message") or {}).get("content") or ""
                    timings = C.timings_from_ollama(r)
                    timings["wall_s"] = time.time() - t0
                    timings["reasoning_chars"] = len((r.get("message") or {}).get("thinking") or "")
                except Exception as e:
                    err = [f"{type(e).__name__}: {str(e)[:200]}"]

                pts, note = grade(answer, secret) if not err else (None, "error")
                rec = {
                    "schema_version": C.SCHEMA_VERSION,
                    "run_id": f"ret-{tag}-{target}-{variant}-{int(time.time())}",
                    "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    "git_commit": versions.get("git_commit"),
                    "software_versions": versions,
                    "task_suite": "retrieval",
                    "task_id": variant,
                    "model_tag": tag,
                    "requested_context_tokens": target,
                    "actual_context_tokens": cache[ckey]["measured"],
                    "reasoning_mode_requested": args.reasoning,
                    "cold_or_warm": "warm",
                    "repetition": 0,
                    "timings": timings,
                    # The scoring manifest is NEVER in the prompt; the expected
                    # value is recorded only in the result, after the fact.
                    "score": {"points": pts, "max": 1, "note": note,
                              "expected": secret,
                              "answer_chars": len(answer)},
                    "memory_metrics": {"after": C.machine_snapshot()},
                    "errors": err,
                }
                C.append_result(rec)
                mark = "PASS" if pts else ("ERR " if err else "FAIL")
                print(f"  {tag:<26} @{target:<6} {variant:<12} {mark}  "
                      f"({note}, {len(answer)} chars, {timings.get('wall_s', 0):.0f}s)")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model")
    ap.add_argument("--exclude", action="append",
                    help="skip a model tag; repeatable")
    ap.add_argument("--context", type=int, action="append")
    ap.add_argument("--variant", choices=("late", "distributed"))
    ap.add_argument("--reasoning", default="default")
    ap.add_argument("--force", action="store_true")
    sys.exit(run(ap.parse_args()))


if __name__ == "__main__":
    main()
