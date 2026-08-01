"""ruler.py — RULER-style long-context tasks. The context is REQUIRED, not decoration.

Source:  https://github.com/NVIDIA/RULER  (arXiv:2404.06654)
         "RULER: What's the Real Context Size of Your Long-Context Language Models?"

WHY THIS REPLACES THE PREVIOUS APPROACH. The earlier `compete` wrapper padded a
task with repo-like filler and asked the model to solve the task. That measured
NOISE TOLERANCE, not long-context capability: "write chunk(items, size)" is
answerable while ignoring every one of the 32,768 surrounding tokens. RULER's
tasks are unanswerable without reading the context, which is the entire point of
the benchmark.

RULER is SYNTHETIC and generated at a configured sequence length, so the task
formats are reimplemented here rather than a dataset being vendored. Implemented
subset (RULER task names in brackets):

  niah_single    [niah_single_1]  one needle, one value, retrieve it
  niah_multikey  [niah_multikey]  many needles, retrieve ONE by its key
  vt             [vt]             variable tracking: X1=val, X2=X1, X3=X2 ...
                                  report every variable bound to a value.
                                  MULTI-HOP -- tail attention alone fails.
  cwe            [cwe]            common-word extraction: aggregate over the
                                  WHOLE context, not one span.

ADAPTATION, stated because it affects comparability: upstream RULER uses its own
generators, prompt templates and a 500-example-per-task default. This uses the
same task DEFINITIONS at a smaller sample count with our prompt wording, scored
by exact match. Internally comparable across our models; NOT comparable to the
published RULER leaderboard.

COLD BY DEFAULT. Every run unloads the model first, so each measurement pays a
real prefill. Reusing a warm model made `off` look 3x slower than `deep` purely
because `off` ran first -- a cache artifact reported as a mode difference.
"""
import argparse
import datetime
import json
import os
import random
import zlib
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C  # noqa: E402

WORDS = ("apple river tiger cloud stone maple ocean amber quartz willow ember "
         "harbor lantern meadow pillar canyon thistle beacon cobalt drift").split()


def _filler(rng, n_words):
    return " ".join(rng.choice(WORDS) for _ in range(n_words))


def gen_niah_single(rng, approx_tokens):
    key = f"K{rng.randint(10000, 99999)}"
    val = f"{rng.randint(1000000, 9999999)}"
    n = max(50, int(approx_tokens * 0.65))
    body = _filler(rng, n)
    pos = len(body) // 2
    needle = f" The special magic number for {key} is {val}. "
    text = body[:pos] + needle + body[pos:]
    q = (f"{text}\n\nWhat is the special magic number for {key}? "
         f"Reply with the number ONLY.")
    return q, val


def gen_niah_multikey(rng, approx_tokens, n_needles=8):
    keys = [f"K{rng.randint(10000, 99999)}" for _ in range(n_needles)]
    vals = [f"{rng.randint(1000000, 9999999)}" for _ in range(n_needles)]
    n = max(50, int(approx_tokens * 0.62))
    body = _filler(rng, n).split()
    step = max(1, len(body) // (n_needles + 1))
    for i, (k, v) in enumerate(zip(keys, vals)):
        at = step * (i + 1)
        body[at:at] = f"The special magic number for {k} is {v}.".split()
    tgt = rng.randrange(n_needles)
    q = (" ".join(body) + f"\n\nWhat is the special magic number for {keys[tgt]}? "
         f"Reply with the number ONLY.")
    return q, vals[tgt]


def gen_vt(rng, approx_tokens, chain=4):
    """Variable tracking — MULTI-HOP. X1=val, X2=X1, X3=X2... The answer requires
    following the chain, so retrieving the tail is not enough."""
    val = f"{rng.randint(100000, 999999)}"
    names = [f"VAR_{rng.randint(1000, 9999)}" for _ in range(chain)]
    stmts = [f"{names[0]} = {val}"] + [
        f"{names[i+1]} = {names[i]}" for i in range(chain - 1)]
    n = max(50, int(approx_tokens * 0.6))
    body = _filler(rng, n).split()
    step = max(1, len(body) // (len(stmts) + 1))
    for i, s in enumerate(stmts):
        body[step * (i + 1):step * (i + 1)] = s.split()
    q = (" ".join(body) + f"\n\nFind all variables assigned the value {val}, "
         f"directly or through a chain of assignments. Reply with the variable "
         f"names ONLY, comma-separated.")
    return q, names


def gen_cwe(rng, approx_tokens, top=3):
    """Common-word extraction — AGGREGATION over the whole context. A model that
    reads only one span cannot answer; it must count across everything."""
    n = max(80, int(approx_tokens * 0.6))
    common = [f"zeta{rng.randint(100,999)}" for _ in range(top)]
    words = []
    for _ in range(n):
        words.append(rng.choice(WORDS))
    # inject the common words far more often than any filler word
    for c in common:
        for _ in range(max(6, n // 40)):
            words.insert(rng.randrange(len(words)), c)
    rng.shuffle(common)
    q = (" ".join(words) + f"\n\nWhich {top} words appear most frequently in the "
         f"text above? Reply with the words ONLY, comma-separated.")
    return q, common


TASKS = {
    "niah_single": (gen_niah_single, "exact"),
    "niah_multikey": (gen_niah_multikey, "exact"),
    "vt": (gen_vt, "set"),
    "cwe": (gen_cwe, "set"),
}


def score(kind, answer, truth):
    if not answer:
        return 0, 1, "empty"
    a = answer.strip().lower()
    if kind == "exact":
        return (1 if str(truth).lower() in a else 0), 1, "exact-match"
    got = {w.strip(" .,:;\"'`*").lower() for w in a.replace("\n", ",").split(",")}
    want = {str(x).lower() for x in truth}
    hit = len(want & got)
    return hit, len(want), f"{hit}/{len(want)} of the required items"


def run(args):
    m = C.manifest()
    ollama = m["endpoint"]["ollama"]
    versions = C.software_versions(ollama)
    done = C.completed_keys()
    baseline_swap = C.swap_used_gb()

    tags = [e["tag"] for e in m["models"]]
    if args.model:
        tags = [t for t in tags if t == args.model]
    for x in (args.exclude or []):
        tags = [t for t in tags if t != x]
    targets = args.context or m["context_targets"]
    names = args.task or list(TASKS)

    caps = {}
    cp = os.path.join(C.RESULTS, "capabilities.json")
    if os.path.exists(cp):
        caps = json.load(open(cp)).get("models", {})

    for tag in tags:
        rm = (caps.get(tag) or {}).get("reasoning_modes") or {}
        modes = args.reasoning or [k for k, v in rm.items() if v.get("effective")] or ["standard"]
        for target in targets:
            for mode in modes:
                for name in names:
                    key = "|".join(str(x) for x in (
                        "ruler", name, tag, target, mode, "cold", 0))
                    if key in done and not args.force:
                        continue
                    if C.safety_check(m["limits"], baseline_swap):
                        print("  ABORT: machine limits"); return 2

                    # COLD every run. Each measurement pays a real prefill and a
                    # real load; nothing is served from a previous run's cache.
                    C.unload_all_except(ollama)

                    gen, kind = TASKS[name]
                    # STABLE seed. Python SALTS hash() for strings per process,
                    # so `hash(name)` generated a DIFFERENT task instance on every
                    # invocation -- models were being compared on tasks that were
                    # not the same tasks, which is the one thing a cross-model
                    # benchmark must never do. zlib.crc32 is stable across
                    # processes and machines.
                    rng = random.Random(args.seed + target
                                        + zlib.crc32(name.encode()))
                    prompt, truth = gen(rng, target)
                    opts, prefix = C.model_params(tag, mode, "base",
                                                  thinking=(mode != "off"))
                    opts["num_ctx"] = target
                    opts["num_predict"] = 2048   # answers are short by construction
                    err, out, t = [], "", {}
                    try:
                        body = {"model": tag, "stream": False,
                                "messages": C.build_messages(prompt, prefix),
                                "options": opts}
                        if mode != "default" and not prefix:
                            body["think"] = m["reasoning_modes"][mode]["think"]
                        t0 = time.time()
                        r = C.post(f"{ollama}/api/chat", body, timeout=2700)
                        msg = r.get("message") or {}
                        out = msg.get("content") or ""
                        t = C.timings_from_ollama(r)
                        t["wall_s"] = time.time() - t0
                        t["reasoning_chars"] = len(msg.get("thinking") or "")
                        t["truncated"] = r.get("done_reason") == "length"
                    except Exception as e:
                        err = [f"{type(e).__name__}: {str(e)[:160]}"]

                    if err:
                        pts, mx, note = 0, 1, "request failed"
                    else:
                        pts, mx, note = score(kind, out, truth)
                    C.append_result({
                        "schema_version": C.SCHEMA_VERSION,
                        "run_id": f"ruler-{tag}-{name}-{target}-{mode}-{int(time.time())}",
                        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                        "git_commit": versions.get("git_commit"),
                        "software_versions": versions,
                        "task_suite": "ruler", "task_id": name,
                        "benchmark_source": {
                            "url": "https://github.com/NVIDIA/RULER",
                            "paper": "arXiv:2404.06654",
                            "adaptation": "RULER task DEFINITIONS reimplemented "
                                          "(synthetic generators); our prompt wording, "
                                          "fewer samples. NOT comparable to the "
                                          "published RULER leaderboard."},
                        "model_tag": tag,
                        "requested_context_tokens": target,
                        "actual_context_tokens": t.get("prompt_eval_count"),
                        "reasoning_mode_requested": mode,
                        "sampling": opts, "system_prefix": prefix,
                        "cold_or_warm": "cold", "repetition": 0,
                        "timings": t,
                        "score": {"points": pts, "max": mx, "note": note,
                                  "expected": truth,
                                  "answer_chars": len(out),
                                  "reasoning_chars": t.get("reasoning_chars"),
                                  "truncated": t.get("truncated")},
                        "errors": err,
                    })
                    print(f"  {tag:<24} {target//1024:>4}K {mode:<9} {name:<14} "
                          f"{pts}/{mx}  {t.get('wall_s', 0):7.1f}s  "
                          f"pe={t.get('prompt_eval_count') or 0}")
        C.unload_all_except(ollama)
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model")
    ap.add_argument("--exclude", action="append")
    ap.add_argument("--task", action="append", choices=list(TASKS))
    ap.add_argument("--context", type=int, action="append")
    ap.add_argument("--reasoning", action="append")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--force", action="store_true")
    sys.exit(run(ap.parse_args()))


if __name__ == "__main__":
    main()
