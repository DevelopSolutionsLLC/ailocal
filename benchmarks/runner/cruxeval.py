"""cruxeval.py — CRUXEval-O (output prediction) against the REAL upstream dataset.

Source:  https://github.com/facebookresearch/cruxeval  (MIT, (c) 2023 Meta)
Data:    data/cruxeval.jsonl — 799 samples, fields: id, code, input, output
Paper:   https://crux-eval.github.io/paper/cruxeval.pdf

NOT VENDORED. The dataset is fetched at run time and cached under
benchmarks/fixtures/ (git-ignored). Copying 799 upstream samples into this
repository would republish someone else's dataset; the harness records the exact
source URL, the sample IDs used and the selection seed instead, which is what
makes a run reproducible.

TASK: given `code` and `input`, predict the exact output of `f(input)`.
Scoring is EXACT MATCH after literal evaluation, so `[1, 2]` and `[1,2]`
compare equal while a plausible-looking wrong answer scores zero. Nothing is
judged; there is a ground-truth string.

ADAPTATION, stated because it changes comparability with published numbers:
upstream evaluates with a few-shot prompt and pass@k over multiple samples. This
runs ZERO-shot, one sample, temperature 0, and reports pass@1 on a SUBSET. These
figures are therefore internally comparable across the models measured here, and
are NOT directly comparable to the published CRUXEval leaderboard.
"""
import argparse
import ast
import datetime
import json
import os
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C  # noqa: E402

SOURCE = "https://raw.githubusercontent.com/facebookresearch/cruxeval/main/data/cruxeval.jsonl"
LICENSE = "MIT (c) 2023 Meta — https://github.com/facebookresearch/cruxeval"

PROMPT = (
    "You are given a Python function and an input. Determine the EXACT value "
    "the function returns.\n\n"
    "{code}\n\n"
    "Input: f({input})\n\n"
    "Reply with ONLY the returned value as a Python literal. No explanation, "
    "no variable name, no code block."
)


def dataset(bench):
    """Fetch once, cache locally. Cache is git-ignored."""
    p = os.path.join(bench, "fixtures", "cruxeval.jsonl")
    if not os.path.exists(p):
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with urllib.request.urlopen(SOURCE, timeout=120) as r:
            data = r.read()
        with open(p, "wb") as f:
            f.write(data)
    out = []
    with open(p) as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def subset(rows, n, seed=42):
    """Deterministic stride selection — same n and seed always give the same
    sample IDs, so two runs compare like for like."""
    if n >= len(rows):
        return rows
    step = len(rows) / n
    return [rows[int(i * step)] for i in range(n)]


def equivalent(pred, truth):
    """Exact match on VALUE, tolerant of formatting. Falls back to a normalised
    string compare when either side will not literal-eval."""
    if pred is None:
        return False
    p = pred.strip().strip("`").strip()
    # Models often answer "f([1,2]) == [3]" or "Output: [3]"; take the tail.
    for lead in ("output:", "returns:", "result:", "answer:"):
        if p.lower().startswith(lead):
            p = p[len(lead):].strip()
    if "==" in p:
        p = p.split("==")[-1].strip()
    try:
        return ast.literal_eval(p) == ast.literal_eval(truth)
    except Exception:
        return " ".join(p.split()) == " ".join(truth.split())


def run(args):
    m = C.manifest()
    ollama = m["endpoint"]["ollama"]
    limits = m["limits"]
    baseline_swap = C.swap_used_gb()
    versions = C.software_versions(ollama)
    done = C.completed_keys()

    rows = subset(dataset(C.BENCH), args.n, args.seed)
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
                why = ("rejected" if not info.get("accepted")
                       else "control ignored — identical to another mode")
                print(f"  SKIP {tag} [{mode}]: {why}")
                continue
            C.unload_all_except(ollama, keep=(tag,))
            hit = 0
            n = 0
            for row in rows:
                key = "|".join(str(x) for x in (
                    "cruxeval-o", row["id"], tag, 16384, mode, "warm", 0))
                if key in done and not args.force:
                    continue
                if C.safety_check(limits, baseline_swap):
                    print("  ABORT: machine limits"); return 2
                think = None if mode == "default" else m["reasoning_modes"][mode]["think"]
                err = []
                ans, t = "", {}
                try:
                    body = {"model": tag, "stream": False,
                            "messages": [{"role": "user", "content": PROMPT.format(
                                code=row["code"], input=row["input"])}],
                            "options": {"num_ctx": 16384, "num_predict": 2048,
                                        "temperature": 0, "top_p": 1.0, "seed": 42}}
                    if think is not None:
                        body["think"] = think
                    t0 = time.time()
                    r = C.post(f"{ollama}/api/chat", body, timeout=900)
                    msg = r.get("message") or {}
                    ans = msg.get("content") or ""
                    t = C.timings_from_ollama(r)
                    t["wall_s"] = time.time() - t0
                    t["reasoning_chars"] = len(msg.get("thinking") or "")
                except Exception as e:
                    err = [f"{type(e).__name__}: {str(e)[:150]}"]
                ok = equivalent(ans, row["output"]) if not err else False
                hit += 1 if ok else 0
                n += 1
                C.append_result({
                    "schema_version": C.SCHEMA_VERSION,
                    "run_id": f"crux-{tag}-{row['id']}-{mode}-{int(time.time())}",
                    "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    "git_commit": versions.get("git_commit"),
                    "software_versions": versions,
                    "task_suite": "cruxeval-o",
                    "task_id": row["id"],
                    "benchmark_source": {"url": SOURCE, "license": LICENSE,
                                         "adaptation": "zero-shot, 1 sample, temp 0, "
                                                       "pass@1 on a deterministic subset; "
                                                       "NOT comparable to published pass@k"},
                    "model_tag": tag,
                    "requested_context_tokens": 16384,
                    "actual_context_tokens": t.get("prompt_eval_count"),
                    "reasoning_mode_requested": mode,
                    "cold_or_warm": "warm", "repetition": 0,
                    "timings": t,
                    "score": {"points": 1 if ok else 0, "max": 1,
                              "expected": row["output"], "answer_chars": len(ans),
                              "reasoning_chars": t.get("reasoning_chars")},
                    "errors": err,
                })
            if n:
                print(f"  {tag:<26} {mode:<9} CRUXEval-O  {hit}/{n} "
                      f"({hit/n*100:.0f}%)")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model")
    ap.add_argument("--n", type=int, default=25, help="deterministic subset size")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--reasoning", action="append")
    ap.add_argument("--force", action="store_true")
    sys.exit(run(ap.parse_args()))


if __name__ == "__main__":
    main()
