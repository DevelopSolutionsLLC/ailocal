"""judged.py — Tasks A and D: architecture and code review.

These cannot be scored by executing code, so they are scored against a HIDDEN
manifest of keyword signals. That is coarse and it is stated as coarse: it
measures whether the model reached the right conclusion, not whether the prose
is good. Prose polish is explicitly NOT scored.

Why keywords rather than an LLM judge: judging one model's output with another
model measures agreement between models, and would let a strong judge launder a
weak answer into a good score. A fixed manifest is dumber and honest.

LEAKAGE: the manifest (`signals`, `defects`, `noise`) never enters the prompt.
The model sees only `statement`. Expected values are written into the RESULT
after the fact, never into the request.

ARCHITECTURE scoring: +1 per required signal group matched, -1 per anti-signal.
Anti-signals encode the two known wrong turns for the fixture (propose a lock
that constraints.md forbids; rewrite unrelated system) and citing the decoy
deprecated file. Score floors at 0.

REVIEW scoring: precision and recall against seeded defects.
  recall    = seeded defects found / seeded defects
  precision = seeded defects found / (found + speculative claims)
Speculative claims are approximated by counting enumerated findings that match
no seeded defect and no known-noise item. That approximation is imperfect and
is reported as such.
"""
import argparse
import datetime
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C  # noqa: E402

ARCH_PROMPT = "{statement}"
REVIEW_PROMPT = "{statement}"


def match_any(text, aliases):
    low = text.lower()
    return any(a.lower() in low for a in aliases)


def score_architecture(text, task):
    sig = task["signals"]
    hits = [g for g in sig["required"] if match_any(text, g)]
    anti = [g for g in sig["anti"] if match_any(text, g)]
    pts = max(0, len(hits) - len(anti))
    return {
        "points": pts, "max": len(sig["required"]),
        "required_hit": len(hits), "anti_triggered": len(anti),
        "note": "keyword-gated; prose quality deliberately unscored",
    }


ENUM_RE = re.compile(r"(?m)^\s*(?:[-*+]|\d+[.)])\s+")


def score_review(text, task):
    found = [d["id"] for d in task["defects"] if match_any(text, d["aliases"])]
    noise_hit = [n["id"] for n in task["noise"] if match_any(text, n["aliases"])]
    enumerated = len(ENUM_RE.findall(text))
    # Claims that are neither a seeded defect nor known noise. Approximate.
    extra = max(0, enumerated - len(found) - len(noise_hit))
    recall = len(found) / len(task["defects"])
    precision = len(found) / max(1, len(found) + extra)
    crit = [d["id"] for d in task["defects"] if d["severity"] == "critical"]
    crit_found = [c for c in crit if c in found]
    return {
        "points": len(found), "max": len(task["defects"]),
        "recall": round(recall, 3), "precision": round(precision, 3),
        "found": found, "missed": [d["id"] for d in task["defects"] if d["id"] not in found],
        "criticals_found": f"{len(crit_found)}/{len(crit)}",
        "enumerated_claims": enumerated, "unmatched_claims": extra,
        "note": "precision denominator approximates speculative claims by counting "
                "enumerated findings that match neither a seeded defect nor known noise",
    }


def run(args):
    m = C.manifest()
    ollama = m["endpoint"]["ollama"]
    versions = C.software_versions(ollama)
    done = C.completed_keys()
    baseline_swap = C.swap_used_gb()

    suite = args.suite
    with open(os.path.join(C.BENCH, "tasks", f"{suite}.json")) as f:
        tasks = json.load(f)["tasks"]

    tags = [e["tag"] for e in m["models"]]
    if args.model:
        tags = [t for t in tags if t == args.model]

    caps = {}
    cp = os.path.join(C.RESULTS, "capabilities.json")
    if os.path.exists(cp):
        caps = json.load(open(cp)).get("models", {})

    modes = args.reasoning or ["off", "standard"]
    for tag in tags:
        rm = (caps.get(tag) or {}).get("reasoning_modes") or {}
        for mode in modes:
            info = rm.get(mode)
            if info and not info.get("effective"):
                print(f"  SKIP {tag} [{mode}]: "
                      f"{'rejected' if not info.get('accepted') else 'control ignored'}")
                continue
            C.unload_all_except(ollama, keep=(tag,))
            for task in tasks:
                key = "|".join(str(x) for x in (
                    suite, task["id"], tag, 32768, mode, "warm", 0))
                if key in done and not args.force:
                    continue
                if C.safety_check(m["limits"], baseline_swap):
                    print("  ABORT: machine limits"); return 2
                think = None if mode == "default" else m["reasoning_modes"][mode]["think"]
                err = []
                out, t = "", {}
                try:
                    # Architecture/review are reasoning-heavy: Qwen recommends
                    # 32768 output for most queries, so a thinking run gets it
                    # rather than an 8192 ceiling that would truncate the answer
                    # and score it as wrong.
                    opts, prefix = C.model_params(tag, mode, "base",
                                                  thinking=(mode != "off"))
                    opts["num_ctx"] = 32768
                    body = {"model": tag, "stream": False,
                            "messages": C.build_messages(task["statement"], prefix),
                            "options": opts}
                    if think is not None and not prefix:
                        body["think"] = think
                    t0 = time.time()
                    r = C.post(f"{ollama}/api/chat", body, timeout=1800)
                    msg = r.get("message") or {}
                    out = msg.get("content") or ""
                    t = C.timings_from_ollama(r)
                    t["wall_s"] = time.time() - t0
                    t["reasoning_chars"] = len(msg.get("thinking") or "")
                except Exception as e:
                    err = [f"{type(e).__name__}: {str(e)[:180]}"]

                if err:
                    sc = {"points": 0, "max": 1, "note": "request failed"}
                else:
                    sc = (score_architecture(out, task) if suite == "architecture"
                          else score_review(out, task))
                sc["answer_chars"] = len(out)
                sc["reasoning_chars"] = t.get("reasoning_chars")

                C.append_result({
                    "schema_version": C.SCHEMA_VERSION,
                    "run_id": f"{suite}-{tag}-{task['id']}-{mode}-{int(time.time())}",
                    "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    "git_commit": versions.get("git_commit"),
                    "software_versions": versions,
                    "task_suite": suite, "task_id": task["id"],
                    "model_tag": tag,
                    "requested_context_tokens": 32768,
                    "actual_context_tokens": t.get("prompt_eval_count"),
                    "reasoning_mode_requested": mode,
                    "cold_or_warm": "warm", "repetition": 0,
                    "timings": t, "score": sc, "errors": err,
                })
                extra = (f"P={sc.get('precision')} R={sc.get('recall')}"
                         if suite == "review" else
                         f"anti={sc.get('anti_triggered')}")
                print(f"  {tag:<26} {mode:<9} {task['id']:<24} "
                      f"{sc['points']}/{sc['max']}  {extra}  {t.get('wall_s', 0):5.1f}s")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--suite", required=True, choices=("architecture", "review"))
    ap.add_argument("--model")
    ap.add_argument("--reasoning", action="append")
    ap.add_argument("--force", action="store_true")
    sys.exit(run(ap.parse_args()))


if __name__ == "__main__":
    main()
