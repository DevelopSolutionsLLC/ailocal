"""humaneval.py — the STANDARD coding benchmark, run at 32K/64K/128K context.

Source:  https://github.com/openai/human-eval  (MIT)
Data:    data/HumanEval.jsonl.gz — 164 problems
         fields: task_id, prompt, entry_point, canonical_solution, test

WHY THIS REPLACES MY INVENTED TASKS. `chunk()` and `RetryBudget` were written by
me, which makes them unaudited and unshared: nobody can compare a number from
them to anything. HumanEval is the field standard for function-level code
generation, ships its own executable `check()` tests, and is what every model
card reports.

NOT VENDORED. Fetched at run time and cached under benchmarks/fixtures/
(git-ignored). Task IDs and the selection seed are recorded so a run is
reproducible without republishing someone else's dataset.

CONTEXT IS REAL. Each problem is embedded in repo-like filler calibrated to the
target size, so a 128K run means solving a standard problem while 128K of
surrounding code is in the window. This is a DEVIATION from upstream HumanEval,
which is a <1000-token task, and it is the whole point here: the question is not
"can it solve HumanEval" but "can it still solve HumanEval at 128K".

ADAPTATION, stated because it breaks comparability with published numbers:
upstream reports pass@k over multiple samples at temperature ~0.8 with the bare
prompt. This runs ONE sample per problem, at each model's vendor-documented
sampling, with the prompt embedded in long context. Internally comparable across
our models; NOT comparable to any published HumanEval score.

COLD every run: the model is unloaded first, so each measurement pays a real
load and prefill rather than replaying a cached prefix.
"""
import argparse
import datetime
import gzip
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C          # noqa: E402
import fixtures as F        # noqa: E402

SOURCE = "https://raw.githubusercontent.com/openai/human-eval/master/data/HumanEval.jsonl.gz"
LICENSE = "MIT — https://github.com/openai/human-eval"

WRAPPER = (
    "You are working in the repository below.\n\n===== REPOSITORY =====\n{ctx}\n\n"
    "===== TASK =====\nComplete this Python function. Reply with ONLY a code "
    "block containing the COMPLETE function including its signature. No "
    "explanation, no tests.\n\n{prompt}\n"
)


def dataset(bench):
    p = os.path.join(bench, "fixtures", "humaneval.jsonl")
    if not os.path.exists(p):
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with urllib.request.urlopen(SOURCE, timeout=120) as r:
            raw = r.read()
        with open(p, "w") as f:
            for line in gzip.decompress(raw).decode().splitlines():
                if line.strip():
                    f.write(line + "\n")
    return [json.loads(l) for l in open(p) if l.strip()]


def subset(rows, n):
    """Deterministic stride: same n always yields the same task_ids."""
    if n >= len(rows):
        return rows
    step = len(rows) / n
    return [rows[int(i * step)] for i in range(n)]


CODE_RE = __import__("re").compile(r"```(?:python)?\s*(.*?)```", __import__("re").S)


def extract(text):
    if not text:
        return ""
    b = CODE_RE.findall(text)
    return (max(b, key=len) if b else text).strip()


def check(code, problem, timeout=30):
    """Run the model's function against HumanEval's OWN test harness."""
    d = tempfile.mkdtemp(prefix="ailocal-he-")
    try:
        src = (code + "\n\n" + problem["test"] + "\n\n"
               f"check({problem['entry_point']})\n")
        p = os.path.join(d, "t.py")
        with open(p, "w") as f:
            f.write(src)
        try:
            r = subprocess.run([sys.executable, p], capture_output=True,
                               text=True, timeout=timeout, cwd=d)
            if r.returncode == 0:
                return 1, ""
            return 0, (r.stderr or "").strip().splitlines()[-1][:110] if r.stderr else "fail"
        except subprocess.TimeoutExpired:
            return 0, "timeout"
        except Exception as e:
            return 0, type(e).__name__
    finally:
        shutil.rmtree(d, ignore_errors=True)


def run(args):
    m = C.manifest()
    ollama = m["endpoint"]["ollama"]
    versions = C.software_versions(ollama)
    done = C.completed_keys()
    baseline_swap = C.swap_used_gb()
    cache = F.load_cache(C.BENCH)

    probs = subset(dataset(C.BENCH), args.n)
    tags = [e["tag"] for e in m["models"]]
    if args.model:
        tags = [t for t in tags if t == args.model]
    for x in (args.exclude or []):
        tags = [t for t in tags if t != x]
    targets = args.context or m["context_targets"]

    caps = {}
    cp = os.path.join(C.RESULTS, "capabilities.json")
    if os.path.exists(cp):
        caps = json.load(open(cp)).get("models", {})

    for tag in tags:
        rm = (caps.get(tag) or {}).get("reasoning_modes") or {}
        modes = args.reasoning or [k for k, v in rm.items() if v.get("effective")] or ["standard"]
        for target in targets:
            ckey = f"{tag}|{target}|late"
            if ckey not in cache:
                print(f"  SKIP {tag} @{target}: no calibration (run ruler/compete first)")
                continue
            for mode in modes:
                hit = n = 0
                for ix, prob in enumerate(probs):
                    key = "|".join(str(x) for x in (
                        "humaneval", prob["task_id"], tag, target, mode, "cold", 0))
                    if key in done and not args.force:
                        continue
                    if C.safety_check(m["limits"], baseline_swap):
                        print("  ABORT: machine limits"); return 2
                    C.unload_all_except(ollama)   # COLD every run

                    ctx, _ = F.build(target, "late", scale=cache[ckey]["scale"],
                                     nonce=ix + 1)
                    opts, prefix = C.model_params(tag, mode, "coding",
                                                  thinking=(mode != "off"))
                    opts["num_ctx"] = target
                    err, out, t = [], "", {}
                    try:
                        body = {"model": tag, "stream": False,
                                "messages": C.build_messages(
                                    WRAPPER.format(ctx=ctx, prompt=prob["prompt"]), prefix),
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
                        err = [f"{type(e).__name__}: {str(e)[:150]}"]

                    pts, why = (0, "request failed") if err else check(extract(out), prob)
                    if t.get("truncated"):
                        pts, why = None, "INVALID: hit the output ceiling"
                    hit += pts or 0
                    n += 1
                    C.append_result({
                        "schema_version": C.SCHEMA_VERSION,
                        "run_id": f"he-{tag}-{prob['task_id'].replace('/','_')}-{target}-{mode}-{int(time.time())}",
                        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                        "git_commit": versions.get("git_commit"),
                        "software_versions": versions,
                        "task_suite": "humaneval", "task_id": prob["task_id"],
                        "benchmark_source": {
                            "url": "https://github.com/openai/human-eval",
                            "license": LICENSE,
                            "adaptation": "1 sample per problem at vendor sampling, "
                                          "prompt EMBEDDED in long context. NOT "
                                          "comparable to published pass@k."},
                        "model_tag": tag,
                        "requested_context_tokens": target,
                        "actual_context_tokens": t.get("prompt_eval_count"),
                        "reasoning_mode_requested": mode,
                        "sampling": opts, "system_prefix": prefix,
                        "cold_or_warm": "cold", "repetition": 0,
                        "timings": t,
                        "score": {"points": pts, "max": 1, "note": why,
                                  "entry_point": prob["entry_point"],
                                  "answer_chars": len(out),
                                  "reasoning_chars": t.get("reasoning_chars"),
                                  "truncated": t.get("truncated")},
                        "errors": err,
                    })
                    print(f"  {tag:<22} {target//1024:>4}K {mode:<9} "
                          f"{prob['task_id']:<15} {'PASS' if pts else ('INV' if pts is None else 'fail'):<5} "
                          f"{t.get('wall_s', 0):6.1f}s")
                if n:
                    print(f"  -> {tag} @{target//1024}K [{mode}] pass@1 = {hit}/{n}")
    C.unload_all_except(ollama)
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model")
    ap.add_argument("--exclude", action="append")
    ap.add_argument("--context", type=int, action="append")
    ap.add_argument("--reasoning", action="append")
    ap.add_argument("--n", type=int, default=8, help="deterministic subset size")
    ap.add_argument("--force", action="store_true")
    sys.exit(run(ap.parse_args()))


if __name__ == "__main__":
    main()
