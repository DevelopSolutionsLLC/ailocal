"""compete.py — four prompts, one per role, at 32K/64K/128K, across real thinking modes.

This is the suite that decides role placement. Not throughput, not retrieval,
not 799 CRUXEval samples: four tasks that map 1:1 onto the roles being filled.

  fastcode        -> ailocal-completion / fast     executed tests
  implementation  -> ailocal-implementation        executed tests
  architecture    -> ailocal-architecture          hidden signal manifest
  review          -> ailocal-review                precision + recall

CONTEXT IS REAL, NOT DECLARED. Each task is WRAPPED in repo-like filler
calibrated to the target size, so the model must locate the task inside a large
context and then solve it. Declaring num_ctx 128K with a 500-token prompt would
measure nothing; the question is whether a model can still do architecture when
the task is buried in 128K of code.

THINKING MODES: only those the probe proved EFFECTIVE for that model. qwen3.5's
`deep` is ignored (ollama/ollama#13353) and qwen3-coder rejects thinking
entirely; running them anyway would measure the same thing twice and present it
as a complete grid.

SETTINGS: each model runs at its vendor-documented sampling, output capped at
32,768 -- a ceiling, so a long correct answer is not truncated into a wrong one.
"""
import argparse
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C          # noqa: E402
import fixtures as F        # noqa: E402
import judged as J          # noqa: E402

CODE_RE = re.compile(r"```(?:python)?\s*(.*?)```", re.S)

WRAPPER = (
    "You are working in the repository below. Read it, then complete the TASK "
    "at the end.\n\n===== REPOSITORY =====\n{context}\n\n===== TASK =====\n{task}\n"
)


def extract_code(text):
    if not text:
        return ""
    blocks = CODE_RE.findall(text)
    return (max(blocks, key=len) if blocks else text).strip()


def execute(code, tests, timeout=30):
    d = tempfile.mkdtemp(prefix="ailocal-compete-")
    try:
        passed, errs = 0, []
        for i, body in enumerate(tests):
            p = os.path.join(d, f"t{i}.py")
            with open(p, "w") as f:
                f.write(code + "\n\n" + body + "\n")
            try:
                r = subprocess.run([sys.executable, p], capture_output=True,
                                   text=True, timeout=timeout, cwd=d)
                if r.returncode == 0:
                    passed += 1
                elif r.stderr:
                    errs.append(r.stderr.strip().splitlines()[-1][:100])
            except subprocess.TimeoutExpired:
                errs.append("timeout")
            except Exception as e:
                errs.append(type(e).__name__)
        return passed, len(tests), errs[:2]
    finally:
        shutil.rmtree(d, ignore_errors=True)


def effective_modes(caps, tag):
    """Only modes proven to change behaviour. Falls back to a single run when the
    probe has no opinion, rather than inventing three."""
    rm = (caps.get(tag) or {}).get("reasoning_modes") or {}
    modes = [k for k, v in rm.items() if v.get("effective")]
    return modes or ["standard"]


def run(args):
    m = C.manifest()
    ollama = m["endpoint"]["ollama"]
    versions = C.software_versions(ollama)
    done = C.completed_keys()
    baseline_swap = C.swap_used_gb()
    cache = F.load_cache(C.BENCH)

    with open(os.path.join(C.BENCH, "tasks", "compete.json")) as f:
        tasks = json.load(f)["tasks"]
    if args.task:
        tasks = [t for t in tasks if t["id"] == args.task]

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
        modes = args.reasoning or effective_modes(caps, tag)
        C.unload_all_except(ollama, keep=(tag,))
        for target in targets:
            # Build the repo-like wrapper ONCE per (model, size); reuse across
            # tasks and modes so the only thing varying is what we intend to vary.
            ckey = f"{tag}|{target}|late"
            if ckey in cache:
                ctx_text, _ = F.build(target, "late", scale=cache[ckey]["scale"])
            else:
                try:
                    ctx_text, measured, scale, _ = F.ratio_calibrate(
                        lambda t: _measure(ollama, tag, t), target)
                    cache[ckey] = {"scale": scale, "measured": measured,
                                   "fingerprint": F.fingerprint(ctx_text)}
                    F.save_cache(C.BENCH, cache)
                except Exception as e:
                    print(f"  SKIP {tag} @{target}: calibration {type(e).__name__}")
                    continue
            for mode in modes:
                for task in tasks:
                    key = "|".join(str(x) for x in (
                        "compete", task["id"], tag, target, mode, "warm", 0))
                    if key in done and not args.force:
                        continue
                    if C.safety_check(m["limits"], baseline_swap):
                        print("  ABORT: machine limits"); return 2

                    prompt = WRAPPER.format(context=ctx_text, task=task["statement"])
                    opts, prefix = C.model_params(
                        tag, mode, "coding" if task["kind"] == "execute" else "base",
                        thinking=(mode != "off"))
                    opts["num_ctx"] = target
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
                        sc = {"points": 0, "max": 1, "note": "request failed"}
                    elif task["kind"] == "execute":
                        p_, tot, e_ = execute(extract_code(out), task["tests"])
                        sc = {"points": p_, "max": tot, "passed_all": p_ == tot,
                              "failures": e_}
                    elif task["kind"] == "signals":
                        sc = J.score_architecture(out, task)
                    else:
                        sc = J.score_review(out, task)
                    sc["answer_chars"] = len(out)
                    sc["reasoning_chars"] = t.get("reasoning_chars")
                    sc["truncated"] = t.get("truncated")

                    C.append_result({
                        "schema_version": C.SCHEMA_VERSION,
                        "run_id": f"cmp-{tag}-{task['id']}-{target}-{mode}-{int(time.time())}",
                        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                        "git_commit": versions.get("git_commit"),
                        "software_versions": versions,
                        "task_suite": "compete", "task_id": task["id"],
                        "role": task.get("role"),
                        "model_tag": tag,
                        "requested_context_tokens": target,
                        "actual_context_tokens": t.get("prompt_eval_count"),
                        "reasoning_mode_requested": mode,
                        "sampling": opts, "system_prefix": prefix,
                        "cold_or_warm": "warm", "repetition": 0,
                        "timings": t, "score": sc, "errors": err,
                    })
                    mark = (f"{sc['points']}/{sc['max']}"
                            if not err else "ERR")
                    print(f"  {tag:<26} {target//1024:>4}K {mode:<9} "
                          f"{task['id']:<15} {mark:<7} {t.get('wall_s', 0):6.1f}s"
                          f"{'  TRUNC' if sc.get('truncated') else ''}")
        C.unload_all_except(ollama)
    return 0


def _measure(ollama, tag, text):
    r = C.post(f"{ollama}/api/chat", {
        "model": tag, "stream": False,
        "messages": [{"role": "user", "content": text}],
        "options": {"num_ctx": 262144, "num_predict": 4, "temperature": 0},
    }, timeout=1800)
    return r.get("prompt_eval_count")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model")
    ap.add_argument("--exclude", action="append")
    ap.add_argument("--task")
    ap.add_argument("--context", type=int, action="append")
    ap.add_argument("--reasoning", action="append")
    ap.add_argument("--force", action="store_true")
    sys.exit(run(ap.parse_args()))


if __name__ == "__main__":
    main()
