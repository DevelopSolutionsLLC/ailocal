"""quality.py — CORRECTNESS, not speed. Tasks are solved and the result executed.

Scoring is by EXECUTION, never by judgement or string similarity: the model's
code is run against hidden tests in a subprocess. A model that writes an
eloquent wrong function scores zero, which is the entire point.

Leakage controls:
  * hidden tests are never in the prompt — the model sees statement + signature;
  * no reference solution exists to leak, because correctness is decided by
    running the code;
  * generated code executes in a temp directory that is deleted afterwards.

Efficiency is recorded alongside correctness so the two can be traded off
explicitly. A model that passes after 4000 reasoning tokens and 90 seconds is
not equivalent to one that passes in 3 seconds, and the report must be able to
say so.

Sandboxing note: model-generated code runs in a subprocess with a timeout, in a
temp dir, with no network use by the harness. It is NOT a security sandbox. The
tasks are pure functions from a fixed local file; do not point this at untrusted
task definitions.
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
import common as C  # noqa: E402

PROMPT = (
    "{statement}\n\n"
    "Reply with ONLY a Python code block containing the complete function "
    "`{signature}`. No explanation, no example usage, no tests."
)

CODE_RE = re.compile(r"```(?:python)?\s*(.*?)```", re.S)


def extract_code(text):
    """Prefer a fenced block; fall back to the raw body when the model ignored
    the format. Returning None here would score a correct-but-unfenced answer as
    wrong, which would measure instruction-following, not coding."""
    if not text:
        return ""
    blocks = CODE_RE.findall(text)
    if blocks:
        return max(blocks, key=len).strip()
    return text.strip()


def execute(code, tests, timeout=30):
    """Run the model's function against hidden tests. Returns (passed, total, err)."""
    d = tempfile.mkdtemp(prefix="ailocal-bench-")
    try:
        passed = 0
        errs = []
        for i, t in enumerate(tests):
            body = t[0] if isinstance(t, list) else t
            src = code + "\n\n" + body + "\n"
            p = os.path.join(d, f"t{i}.py")
            with open(p, "w") as f:
                f.write(src)
            try:
                r = subprocess.run([sys.executable, p], capture_output=True,
                                   text=True, timeout=timeout, cwd=d)
                if r.returncode == 0:
                    passed += 1
                else:
                    errs.append((r.stderr or "").strip().splitlines()[-1][:120]
                                if r.stderr else "nonzero exit")
            except subprocess.TimeoutExpired:
                errs.append("timeout")
            except Exception as e:
                errs.append(f"{type(e).__name__}")
        return passed, len(tests), errs[:3]
    finally:
        shutil.rmtree(d, ignore_errors=True)


def ask(ollama, tag, prompt, think, timeout=1800):
    body = {"model": tag, "stream": False,
            "messages": [{"role": "user", "content": prompt}],
            "options": {"num_ctx": 16384, "num_predict": 8192,
                        "temperature": 0, "top_p": 1.0, "seed": 42}}
    if think is not None:
        body["think"] = think
    t0 = time.time()
    r = C.post(f"{ollama}/api/chat", body, timeout=timeout)
    msg = r.get("message") or {}
    t = C.timings_from_ollama(r)
    t["wall_s"] = time.time() - t0
    t["reasoning_chars"] = len(msg.get("thinking") or "")
    t["answer_chars"] = len(msg.get("content") or "")
    return msg.get("content") or "", t


def run(args):
    m = C.manifest()
    ollama = m["endpoint"]["ollama"]
    limits = m["limits"]
    baseline_swap = C.swap_used_gb()
    versions = C.software_versions(ollama)
    done = C.completed_keys()

    with open(os.path.join(C.BENCH, "tasks", "fastcode.json")) as f:
        suite = json.load(f)
    tasks = suite["tasks"]
    if args.task:
        tasks = [t for t in tasks if t["id"] == args.task]

    tags = [e["tag"] for e in m["models"]]
    if args.model:
        tags = [t for t in tags if t == args.model]

    # Only run reasoning modes the probe proved EFFECTIVE for each model.
    caps = {}
    cp = os.path.join(C.RESULTS, "capabilities.json")
    if os.path.exists(cp):
        caps = json.load(open(cp)).get("models", {})

    modes = args.reasoning or ["off", "standard", "deep"]
    for tag in tags:
        rm = (caps.get(tag) or {}).get("reasoning_modes") or {}
        for mode in modes:
            info = rm.get(mode)
            if info and not info.get("effective"):
                why = ("rejected by model" if not info.get("accepted")
                       else "control ignored (identical output to another mode)")
                print(f"  SKIP {tag} [{mode}]: {why}")
                continue
            C.unload_all_except(ollama, keep=(tag,))
            for task in tasks:
                key = "|".join(str(x) for x in (
                    "fastcode", task["id"], tag, 16384, mode, "warm", 0))
                if key in done and not args.force:
                    continue
                reason = C.safety_check(limits, baseline_swap)
                if reason:
                    print(f"  ABORT: {reason}")
                    return 2

                think = None if mode == "default" else m["reasoning_modes"][mode]["think"]
                err = []
                try:
                    out, t = ask(ollama, tag, PROMPT.format(
                        statement=task["statement"], signature=task["signature"]), think)
                except Exception as e:
                    out, t = "", {}
                    err = [f"{type(e).__name__}: {str(e)[:200]}"]

                if err:
                    passed, total, terrs = 0, len(task["tests"]), ["request failed"]
                else:
                    passed, total, terrs = execute(extract_code(out), task["tests"])

                rec = {
                    "schema_version": C.SCHEMA_VERSION,
                    "run_id": f"fc-{tag}-{task['id']}-{mode}-{int(time.time())}",
                    "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    "git_commit": versions.get("git_commit"),
                    "software_versions": versions,
                    "task_suite": "fastcode",
                    "task_id": task["id"],
                    "model_tag": tag,
                    "requested_context_tokens": 16384,
                    "actual_context_tokens": t.get("prompt_eval_count"),
                    "reasoning_mode_requested": mode,
                    "cold_or_warm": "warm",
                    "repetition": 0,
                    "timings": t,
                    "score": {"points": passed, "max": total,
                              "passed_all": passed == total,
                              "first_failures": terrs,
                              "reasoning_chars": t.get("reasoning_chars"),
                              "answer_chars": t.get("answer_chars")},
                    "errors": err,
                }
                C.append_result(rec)
                mark = "PASS" if passed == total else f"{passed}/{total}"
                print(f"  {tag:<26} {mode:<9} {task['id']:<22} {mark:<6} "
                      f"{t.get('wall_s', 0):5.1f}s  think={t.get('reasoning_chars', 0)}c")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model")
    ap.add_argument("--task")
    ap.add_argument("--reasoning", action="append")
    ap.add_argument("--force", action="store_true")
    sys.exit(run(ap.parse_args()))


if __name__ == "__main__":
    main()
