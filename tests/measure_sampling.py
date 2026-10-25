#!/usr/bin/env python3
"""A/B one model's SAMPLING on objectively-scored work. NOT a gate, NOT a test.

`measure_agentic.py` answers "which model is faster". It cannot answer "is the
output better", because tok/s does not know whether the code runs. This does the
other half: same model, same prompts, two sampling settings, and a score that
comes from EXECUTING what the model wrote rather than from reading it.

    python3 tests/measure_sampling.py                    # current candidate, one rep
    python3 tests/measure_sampling.py --settings all --reps 3  # explicit larger A/B
    python3 tests/measure_sampling.py --model gemma4:26b-mlx

WHY THIS EXISTS. The 64/128 GB profiles moved gemma4 from temp 0.1 / top_k 20 to
Google's published temp 1.0 / top_k 64 / top_p 0.95. That change was justified by
two things -- vendor guidance, and a measurement showing sampling costs no
throughput (draft acceptance 0.79 at 0.1 vs 0.77 at 1.0). Neither is evidence
about QUALITY. This script produces that evidence, or shows there is none.

HOW IT SCORES. Every coding task names an exact function signature and ships
hidden assertions. The model's code is extracted, executed in a subprocess with
a timeout, and the assertions decide. There is no judge model and no rubric: a
task passes when the code runs and the asserts hold. That makes the score
reproducible and immune to the grader's opinion, at the cost of only measuring
what is mechanically checkable.

HEADROOM IS THE POINT. `measure_agentic.py`'s tool loop scored 8/8 for every
candidate, which resolves nothing. These tasks include edge cases models
routinely miss (empty input, overflow, unicode, off-by-one) so that a real
difference has somewhere to show up. If BOTH settings score 100%, the tasks are
too easy and the result is "no signal", not "no difference" -- the script says so
rather than letting a ceiling read as a conclusion.

Importing this module does not run measurements. Executing it holds a model
resident and runs generated code; run alone with reviewed task inputs.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import re
import statistics
import subprocess
import sys
import tempfile
import time
import urllib.error
from datetime import datetime, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))
from measure_agentic import CANDIDATES, log_offset, speculation_since  # noqa: E402
from measure_geometry import api, loaded_model, versions, measurement_budget, release_model  # noqa: E402
from ailocal import policy as P  # noqa: E402

NUM_CTX = 32768
#: Generous on purpose. At 1200 this benchmark scored 4/5 tasks as WRONG when the
#: model had in fact answered nothing: with `think` on, gemma4 spent the whole
#: budget reasoning and returned an EMPTY response, which the scorer saw as a
#: NameError and recorded as a failed task. An output ceiling below what the
#: roles actually grant (8192-16384) does not measure the model, it measures the
#: ceiling -- and it fails SILENTLY, as a plausible-looking low score.
NUM_PREDICT = 8192  # a reduced measurement budget, not the current role ceiling

#: The two settings under comparison. `profile_old` is what the roles shipped
#: before 2026-08-21; `vendor` is Google's published Gemma 4 setting and what
#: they ship now. Named, not inlined, because the report has to say which won.
SETTINGS = {
    "profile_old (t0.1/k20/p0.9)":
        {"temperature": 0.1, "top_k": 20, "top_p": 0.9, "repeat_penalty": 1.0},
    "vendor      (t1.0/k64/p0.95)":
        {"temperature": 1.0, "top_k": 64, "top_p": 0.95, "repeat_penalty": 1.0},
}

#: Each task: a prompt naming an EXACT signature, and assertions the caller never
#: sees. The edge cases are chosen to be the ones small models actually miss --
#: empty input, single element, negatives, unicode, boundary indices -- so the
#: score has room to move. A task every model passes measures nothing.
TASKS = [
    ("run_length_encode",
     "Write a Python function `run_length_encode(s: str) -> str` that compresses "
     "a string to counts, e.g. 'aaabb' -> 'a3b2'. A single occurrence is written "
     "without a count: 'abc' -> 'abc'. The empty string returns ''. "
     "Return ONLY a fenced python code block, no explanation.",
     """
assert run_length_encode('aaabb') == 'a3b2'
assert run_length_encode('') == ''
assert run_length_encode('abc') == 'abc'
assert run_length_encode('a') == 'a'
assert run_length_encode('aab') == 'a2b'
assert run_length_encode('aaaaaaaaaaab') == 'a11b'
assert run_length_encode('éé🙂🙂🙂x') == 'é2🙂3x'
"""),
    ("median_of",
     "Write a Python function `median_of(xs: list) -> float` returning the median. "
     "For an even count return the mean of the two middle values. Raise "
     "ValueError on an empty list. Do not mutate the caller's list. "
     "Return ONLY a fenced python code block, no explanation.",
     """
assert median_of([3,1,2]) == 2
assert median_of([4,1,3,2]) == 2.5
assert median_of([-5,-1]) == -3.0
assert median_of([0]) == 0
assert median_of([5,5,5,5]) == 5
original = [3,1,2]
median_of(original)
assert original == [3,1,2], 'mutated the caller list'
try:
    median_of([]); raise SystemExit('no ValueError on empty')
except ValueError:
    pass
"""),
    ("chunk_evenly",
     "Write a Python function `chunk_evenly(xs: list, n: int) -> list` splitting "
     "xs into exactly n contiguous chunks whose sizes differ by at most 1, with "
     "the larger chunks first. chunk_evenly([1,2,3,4,5], 3) -> [[1,2],[3,4],[5]]. "
     "Raise ValueError if n < 1. If n > len(xs), trailing chunks are empty. "
     "Return ONLY a fenced python code block, no explanation.",
     """
assert chunk_evenly([1,2,3,4,5], 3) == [[1,2],[3,4],[5]]
assert chunk_evenly([1,2,3,4], 2) == [[1,2],[3,4]]
assert chunk_evenly([], 2) == [[],[]]
assert chunk_evenly([1], 3) == [[1],[],[]]
assert len(chunk_evenly(list(range(10)), 4)) == 4
assert chunk_evenly(list(range(10)), 4) == [[0,1,2],[3,4,5],[6,7],[8,9]]
try:
    chunk_evenly([1], 0); raise SystemExit('no ValueError on n=0')
except ValueError:
    pass
"""),
    ("normalise_path",
     "Write a Python function `normalise_path(p: str) -> str` that collapses a "
     "POSIX path: resolve '.' and '..', collapse repeated slashes, and drop any "
     "trailing slash. An absolute path stays absolute and never escapes root "
     "('/a/../..' -> '/'). Do not use os.path or pathlib. "
     "Return ONLY a fenced python code block, no explanation.",
     """
assert normalise_path('/a/./b//c/') == '/a/b/c'
assert normalise_path('/a/../b') == '/b'
assert normalise_path('/a/../..') == '/'
assert normalise_path('/') == '/'
assert normalise_path('/../') == '/'
"""),
    ("parse_duration",
     "Write a Python function `parse_duration(s: str) -> int` converting a "
     "duration like '1h30m', '45s', '2h', '90m' into total SECONDS. Units are "
     "h, m, s and may appear at most once each, in that order. Raise ValueError "
     "on anything malformed, including the empty string and a bare number. "
     "Return ONLY a fenced python code block, no explanation.",
     """
assert parse_duration('1h30m') == 5400
assert parse_duration('45s') == 45
assert parse_duration('2h') == 7200
assert parse_duration('1h1m1s') == 3661
for bad in ('', '10', 'abc', '1m1h', '1h1h', '1m1m', '1.5h', '-1h', '1h30mgarbage'):
    try:
        parse_duration(bad); raise SystemExit('accepted malformed %r' % bad)
    except ValueError:
        pass
"""),
]

FENCE = re.compile(r"```(?:python|py)?\s*\n(.*?)```", re.S)


def extract(text):
    """The code the model meant to ship.

    Prefer a fenced block; fall back to the whole reply only when there is no
    fence at all, since a model that wrote prose around unfenced code still
    deserves to be executed rather than scored zero on formatting.
    """
    blocks = FENCE.findall(text or "")
    return max(blocks, key=len) if blocks else (text or "")


def score(code, checks):
    """Execute the code plus its assertions. Returns (passed, short reason).

    Subprocess, not exec(): generated code may loop forever, call sys.exit, or
    shadow names in this process. A timeout is a FAILURE, not an error -- code
    that does not terminate has not solved the task.
    """
    with tempfile.TemporaryDirectory() as d:
        f = pathlib.Path(d) / "candidate.py"
        f.write_text(code + "\n" + checks)
        try:
            r = subprocess.run([sys.executable, str(f)], capture_output=True,
                               text=True, timeout=15)
        except subprocess.TimeoutExpired:
            return False, "timeout"
    if r.returncode == 0:
        return True, "ok"
    tail = (r.stderr or "").strip().splitlines()
    return False, (tail[-1][:70] if tail else f"exit {r.returncode}")


def generate(model, prompt, opts, think=True, context=NUM_CTX,
             max_output=NUM_PREDICT, system=None, timeout=120):
    off = log_offset()
    messages = [{"role": "user", "content": prompt}]
    payload = {"model": model, "messages": messages, "stream": False, "think": think,
               "options": {**opts, "num_ctx": context, "num_predict": max_output},
               "keep_alive": "300s"}
    if system is not None:
        messages.insert(0, {"role": "system", "content": system})
    r = api("/api/chat", payload, timeout=timeout)
    message = r.get("message", {})
    n, secs = r.get("eval_count", 0), r.get("eval_duration", 0) / 1e9
    # `thinking` is returned separately from `response`. Carried back so an empty
    # answer can be reported as what it is -- budget exhausted by reasoning --
    # rather than scored as a wrong answer.
    return (message.get("content", ""), (n / secs if n and secs else None),
            speculation_since(off), message.get("thinking", "") or "", r.get("done_reason"),
            r.get("prompt_eval_count"), n)


def main(argv=None):
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
    ap.add_argument("--model", help="default: the active implementation model")
    ap.add_argument("--reps", type=int, default=1,
                    help="attempts per task per setting (default 1, exploratory)")
    ap.add_argument("--json", help="also write the raw per-attempt record here")
    ap.add_argument("--settings", choices=("all", "candidate"), default="candidate",
                    help="candidate uses this model's own declared sampling")
    ap.add_argument("--prompt-file", help="UTF-8 system prompt for an explicit prompt ablation")
    ap.add_argument("--no-think", action="store_true",
                    help="disable native thinking for a controlled mode comparison")
    ap.add_argument("--context", type=int, default=NUM_CTX)
    ap.add_argument("--max-output", type=int, default=NUM_PREDICT)
    ap.add_argument("--time-budget", type=int, default=900, help="whole-run budget in seconds")
    args = ap.parse_args(argv)
    if args.model is None:
        args.model = P.effective_role("implementation")["model"]
    if args.reps < 1 or args.max_output < 1 or args.context <= args.max_output or args.time_budget < 1:
        ap.error("positive reps/output and context greater than output are required")
    if args.settings == "candidate":
        if args.model not in CANDIDATES:
            ap.error("--settings candidate requires a declared agentic candidate")
        candidate = CANDIDATES[args.model]
        settings = {"candidate": candidate["options"]}
        think = candidate["think"]
    else:
        if args.model != "gemma4:26b-mlx":
            ap.error("the historical sampling A/B is Gemma-specific; use --settings candidate")
        settings, think = SETTINGS, True
    if args.no_think:
        think = False
    system = pathlib.Path(args.prompt_file).read_text(encoding="utf-8") if args.prompt_file else None
    try:
        with measurement_budget(args.time_budget):
            return measure(args, settings, think, system)
    finally:
        release_model(args.model)


def measure(args, settings, think, system):
    sys.stdout.reconfigure(line_buffering=True)

    ollama, mlx = versions()
    identity = api("/api/show", {"model": args.model}, timeout=10)
    digest = next((item["digest"] for item in api("/api/tags", timeout=10).get("models", [])
                   if item["name"] == args.model), None)
    if digest is None:
        raise ValueError("model must already be installed with its exact tag")
    print(f"coding measurement — {args.model}   "
          f"{datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC}")
    print(f"ollama {ollama}   mlx {mlx}")
    print(f"[APPROX] direct Ollama, small function tasks; context={args.context}, "
          f"output={args.max_output}, think={think}; "
          f"live implementation output ceiling={P.effective_role('implementation')['max_output']}")
    print(f"{len(TASKS)} tasks x {args.reps} reps x {len(settings)} settings = "
          f"{len(TASKS) * args.reps * len(settings)} scored generations\n")

    record, totals = [], {}
    for label, opts in settings.items():
        print(f"── {label} " + "─" * 40)
        rates, accs, per_task = [], [], {}
        for name, prompt, checks in TASKS:
            passes, reasons = 0, []
            for _ in range(args.reps):
                started = time.monotonic()
                text, rate, spec, thought, finish, prompt_tokens, output_tokens = generate(
                    args.model, prompt, opts, think, args.context, args.max_output, system,
                    120)
                if not text.strip():
                    ok, why = False, (f"EMPTY answer ({len(thought)} chars of "
                                      f"thinking), done_reason={finish}")
                else:
                    ok, why = score(extract(text), checks)
                elapsed = time.monotonic() - started
                passes += ok
                if not ok:
                    reasons.append(why)
                if rate:
                    rates.append(rate)
                if spec:
                    accs.append(spec["acceptance"])
                record.append({"model": args.model, "setting": label, "task": name, "ok": ok,
                               "digest": digest, "details": identity.get("details"), "ollama": ollama,
                               "runner": (loaded_model(args.model) or {}).get("runner"),
                               "task_sha256": hashlib.sha256((prompt + '\0' + checks).encode()).hexdigest(),
                               "reason": why, "tok_s": rate, "seconds": elapsed,
                               "done_reason": finish, "context": args.context,
                               "truncated": finish == "length", "empty": not text.strip(),
                               "max_output": args.max_output, "think": think, "options": opts,
                               "system": system, "prompt_tokens": prompt_tokens,
                               "output_tokens": output_tokens, "response": text})
                if args.json:
                    pathlib.Path(args.json).write_text(json.dumps(record, indent=2))
            per_task[name] = passes
            bar = "█" * passes + "·" * (args.reps - passes)
            note = f"   {reasons[0]}" if reasons else ""
            print(f"  {name:18} {bar} {passes}/{args.reps}{note}")
        total = sum(per_task.values())
        possible = len(TASKS) * args.reps
        totals[label] = (total, possible)
        print(f"  {'TOTAL':18} {total}/{possible} = {100*total/possible:.0f}%"
              + (f"   median {statistics.median(rates):.1f} tok/s" if rates else "")
              + (f"   acceptance {statistics.median(accs):.2f}" if accs else "")
              + "\n")

    print("=" * 60)
    if len(totals) == 1:
        print("Single-setting measurement: compare independent runs; this is not an A/B conclusion.")
        return 0
    (la, (pa, na)), (lb, (pb, nb)) = totals.items()
    print(f"{la}  {100*pa/na:.0f}%")
    print(f"{lb}  {100*pb/nb:.0f}%")
    if pa == na and pb == nb:
        print("\nBOTH PERFECT — no signal. The tasks are too easy to separate "
              "these settings; this is NOT evidence they are equivalent. Make "
              "the tasks harder before concluding anything.")
    elif pa == pb:
        print("\nTIED on this task set. A tie at less than 100% is a real "
              "result: the settings did not separate on work that CAN fail.")
    else:
        win = lb if pb > pa else la
        gap = abs(pa - pb)
        print(f"\n{win.strip()} scored higher by {gap} of {na} attempts.")
        if gap < max(2, na // 10):
            print("That margin is small enough to be sampling noise at this "
                  "rep count. Raise --reps before treating it as a finding.")
    if args.json:
        pathlib.Path(args.json).write_text(json.dumps(record, indent=2))
        print(f"\nraw record -> {args.json}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
    except (urllib.error.URLError, OSError, ValueError, RuntimeError) as exc:
        sys.exit(f"measurement could not run: {exc}")
