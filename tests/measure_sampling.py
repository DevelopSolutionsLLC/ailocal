#!/usr/bin/env python3
"""A/B one model's sampling on executed, assertion-scored code. Out of the gate.

    python3 tests/measure_sampling.py                          # candidate sampling, one rep
    python3 tests/measure_sampling.py --settings all --reps 3  # historical Gemma A/B
    python3 tests/measure_sampling.py --model gemma4:26b-mlx [--json OUT] [--prompt-file P]

Throughput does not show quality, so each task's code runs in a timed subprocess
against hidden edge-case assertions; no judge model. Two perfect scores mean the
tasks are too easy, not that the settings are equal. Runs generated code: run alone.
"""
from __future__ import annotations

import hashlib
import json
import pathlib
import re
import statistics
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from measure_agentic import CANDIDATES, log_offset, speculation_since  # noqa: E402
from measure_geometry import (  # noqa: E402
    P, api, cli, installed, loaded_model, measurement_budget, parser, positive, release_model, versions,
)

NUM_CTX = 32768
# At 1200, thinking consumed the whole budget and empty answers scored as wrong code.
NUM_PREDICT = 8192
#: profile_old shipped before 2026-08-21; vendor is Google's Gemma 4 setting, shipped now.
SETTINGS = {
    "profile_old (t0.1/k20/p0.9)": {"temperature": 0.1, "top_k": 20, "top_p": 0.9, "repeat_penalty": 1.0},
    "vendor      (t1.0/k64/p0.95)": {"temperature": 1.0, "top_k": 64, "top_p": 0.95, "repeat_penalty": 1.0},
}

#: (name, prompt naming an exact signature, hidden assertions on edge cases models miss)
TASKS = [
    ('run_length_encode',
     "Write a Python function `run_length_encode(s: str) -> str` that compresses a string to counts, e.g. "
     "'aaabb' -> 'a3b2'. A single occurrence is written without a count: 'abc' -> 'abc'. The empty string "
     "returns ''. Return ONLY a fenced python code block, no explanation.",
     """
assert run_length_encode('aaabb') == 'a3b2'
assert run_length_encode('') == ''
assert run_length_encode('abc') == 'abc'
assert run_length_encode('a') == 'a'
assert run_length_encode('aab') == 'a2b'
assert run_length_encode('aaaaaaaaaaab') == 'a11b'
assert run_length_encode('éé🙂🙂🙂x') == 'é2🙂3x'
"""),
    ('median_of',
     "Write a Python function `median_of(xs: list) -> float` returning the median. For an even count "
     "return the mean of the two middle values. Raise ValueError on an empty list. Do not mutate the "
     "caller's list. Return ONLY a fenced python code block, no explanation.",
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
    ('chunk_evenly',
     "Write a Python function `chunk_evenly(xs: list, n: int) -> list` splitting xs into exactly n "
     "contiguous chunks whose sizes differ by at most 1, with the larger chunks first. "
     "chunk_evenly([1,2,3,4,5], 3) -> [[1,2],[3,4],[5]]. Raise ValueError if n < 1. If n > len(xs), "
     "trailing chunks are empty. Return ONLY a fenced python code block, no explanation.",
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
    ('normalise_path',
     "Write a Python function `normalise_path(p: str) -> str` that collapses a POSIX path: resolve '.' and "
     "'..', collapse repeated slashes, and drop any trailing slash. An absolute path stays absolute and "
     "never escapes root ('/a/../..' -> '/'). Do not use os.path or pathlib. Return ONLY a fenced python "
     "code block, no explanation.",
     """
assert normalise_path('/a/./b//c/') == '/a/b/c'
assert normalise_path('/a/../b') == '/b'
assert normalise_path('/a/../..') == '/'
assert normalise_path('/') == '/'
assert normalise_path('/../') == '/'
"""),
    ('parse_duration',
     "Write a Python function `parse_duration(s: str) -> int` converting a duration like '1h30m', '45s', "
     "'2h', '90m' into total SECONDS. Units are h, m, s and may appear at most once each, in that order. "
     "Raise ValueError on anything malformed, including the empty string and a bare number. Return ONLY a "
     "fenced python code block, no explanation.",
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
    """The longest fenced block, else the whole reply: formatting is not scored."""
    blocks = FENCE.findall(text or "")
    return max(blocks, key=len) if blocks else (text or "")


def score(code, checks):
    """(passed, reason) from running code plus assertions in a subprocess; a timeout fails."""
    with tempfile.TemporaryDirectory() as d:
        f = pathlib.Path(d) / "candidate.py"
        f.write_text(code + "\n" + checks)
        try:
            r = subprocess.run([sys.executable, str(f)], capture_output=True, text=True, timeout=15)
        except subprocess.TimeoutExpired:
            return False, "timeout"
    if r.returncode == 0:
        return True, "ok"
    tail = (r.stderr or "").strip().splitlines()
    return False, (tail[-1][:70] if tail else f"exit {r.returncode}")


def generate(model, prompt, opts, think=True, context=NUM_CTX, max_output=NUM_PREDICT, system=None, timeout=120):
    off = log_offset()
    messages = ([{"role": "system", "content": system}] if system is not None else []) + \
        [{"role": "user", "content": prompt}]
    r = api("/api/chat", {"model": model, "messages": messages, "stream": False, "think": think,
                          "options": {**opts, "num_ctx": context, "num_predict": max_output},
                          "keep_alive": "300s"}, timeout=timeout)
    message = r.get("message", {})
    n, secs = r.get("eval_count", 0), r.get("eval_duration", 0) / 1e9
    # Thinking is returned so an empty answer reads as an exhausted budget, not wrong code.
    return (message.get("content", ""), (n / secs if n and secs else None), speculation_since(off),
            message.get("thinking", "") or "", r.get("done_reason"), r.get("prompt_eval_count"), n)


def main(argv=None):
    ap = parser(__doc__)
    ap.add_argument("--model", help="default: the active implementation model")
    ap.add_argument("--reps", type=positive, default=1, help="attempts per task per setting")
    ap.add_argument("--json", help="also write the raw per-attempt record here")
    ap.add_argument("--settings", choices=("all", "candidate"), default="candidate",
                    help="candidate uses this model's own declared sampling")
    ap.add_argument("--prompt-file", help="UTF-8 system prompt for an explicit prompt ablation")
    ap.add_argument("--no-think", action="store_true", help="disable native thinking")
    ap.add_argument("--context", type=positive, default=NUM_CTX)
    ap.add_argument("--max-output", type=positive, default=NUM_PREDICT)
    args = ap.parse_args(argv)
    args.model = args.model or P.effective_role("primary")["model"]
    if args.context <= args.max_output:
        ap.error("context must be greater than output")
    if args.settings == "candidate":
        if args.model not in CANDIDATES:
            ap.error("--settings candidate requires a declared agentic candidate")
        settings, think = {"candidate": CANDIDATES[args.model]["options"]}, CANDIDATES[args.model]["think"]
    elif args.model != "gemma4:26b-mlx":
        ap.error("the historical sampling A/B is Gemma-specific; use --settings candidate")
    else:
        settings, think = SETTINGS, True
    think = False if args.no_think else think
    system = pathlib.Path(args.prompt_file).read_text(encoding="utf-8") if args.prompt_file else None
    try:
        with measurement_budget(args.time_budget):
            return measure(args, settings, think, system)
    finally:
        release_model(args.model)


def measure(args, settings, think, system):
    ollama, mlx = versions()
    identity = api("/api/show", {"model": args.model}, timeout=10)
    digest = installed().get(args.model)
    if digest is None:
        raise ValueError("model must already be installed with its exact tag")
    print(f"coding measurement — {args.model}   {datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC}\n"
          f"ollama {ollama}   mlx {mlx}\n[APPROX] direct Ollama, small function tasks; context={args.context}, "
          f"output={args.max_output}, think={think}; live implementation output ceiling="
          f"{P.effective_role('primary')['max_output']}\n{len(TASKS)} tasks x {args.reps} reps x "
          f"{len(settings)} settings = {len(TASKS) * args.reps * len(settings)} scored generations\n")

    record, totals = [], {}
    for label, opts in settings.items():
        print(f"── {label} " + "─" * 40)
        rates, accs, total = [], [], 0
        for name, prompt, checks in TASKS:
            passes, reasons = 0, []
            for _ in range(args.reps):
                started = time.monotonic()
                text, rate, spec, thought, finish, prompt_tokens, output_tokens = generate(
                    args.model, prompt, opts, think, args.context, args.max_output, system)
                ok, why = (score(extract(text), checks) if text.strip() else
                           (False, f"EMPTY answer ({len(thought)} chars of thinking), done_reason={finish}"))
                passes += ok
                reasons += [] if ok else [why]
                rates += [rate] if rate else []
                accs += [spec["acceptance"]] if spec else []
                record.append({"model": args.model, "setting": label, "task": name, "ok": ok, "digest": digest,
                               "details": identity.get("details"), "ollama": ollama,
                               "runner": (loaded_model(args.model) or {}).get("runner"),
                               "task_sha256": hashlib.sha256((prompt + "\0" + checks).encode()).hexdigest(),
                               "reason": why, "tok_s": rate, "seconds": time.monotonic() - started,
                               "done_reason": finish, "context": args.context, "truncated": finish == "length",
                               "empty": not text.strip(), "max_output": args.max_output, "think": think,
                               "options": opts, "system": system, "prompt_tokens": prompt_tokens,
                               "output_tokens": output_tokens, "response": text})
                if args.json:
                    pathlib.Path(args.json).write_text(json.dumps(record, indent=2))
            total += passes
            print(f"  {name:18} {'█' * passes}{'·' * (args.reps - passes)} {passes}/{args.reps}"
                  + (f"   {reasons[0]}" if reasons else ""))
        possible = len(TASKS) * args.reps
        totals[label] = (total, possible)
        print(f"  {'TOTAL':18} {total}/{possible} = {100 * total / possible:.0f}%"
              + (f"   median {statistics.median(rates):.1f} tok/s" if rates else "")
              + (f"   acceptance {statistics.median(accs):.2f}" if accs else "") + "\n")

    print("=" * 60)
    if len(totals) == 1:
        print("Single-setting measurement: compare independent runs; this is not an A/B conclusion.")
        return 0
    (la, (pa, na)), (lb, (pb, nb)) = totals.items()
    print(f"{la}  {100 * pa / na:.0f}%\n{lb}  {100 * pb / nb:.0f}%")
    if pa == na and pb == nb:
        print("\nBOTH PERFECT — no signal; make the tasks harder before concluding anything.")
    elif pa == pb:
        print("\nTIED below 100%: the settings did not separate on work that can fail.")
    else:
        gap = abs(pa - pb)
        print(f"\n{(lb if pb > pa else la).strip()} scored higher by {gap} of {na} attempts.")
        if gap < max(2, na // 10):
            print("That margin is within sampling noise at this rep count; raise --reps.")
    if args.json:
        print(f"\nraw record -> {args.json}")
    return 0


if __name__ == "__main__":
    cli(main)
