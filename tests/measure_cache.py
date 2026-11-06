#!/usr/bin/env python3
"""Show whether the KV prefix cache is reused across turns. Out of the gate.

    python3 tests/measure_cache.py [--quick] [--tier 64gb] [--log PATH]  # active: cold, then warm turn
    python3 tests/measure_cache.py --tail 20                             # passive: last N logged requests

Ollama's prompt_eval_count includes cached tokens ([REAL] 11115 reported, 1030
evaluated), so reuse comes from prompt_eval_cached_count (Ollama >= 0.33.3), else
the server log, else the prefill-time collapse alone. The active probe stops and
reloads the model: run alone. Reads the live launch agent's log read-only.
"""
from __future__ import annotations

import pathlib
import plistlib
import re
import subprocess
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from measure_geometry import CHUNK, P, api, cli, engine_of, measurement_budget, parser, release_model, versions  # noqa: E402,E501

DEFAULT_LOG = pathlib.Path.home() / "Library/Logs/ailocal/com.ailocal.ollama.err.log"
AGENT_PLIST = pathlib.Path.home() / "Library/LaunchAgents/com.ailocal.ollama.plist"
EVAL_LINE = re.compile(r"prompt eval time =\s*[\d.]+ ms /\s*(\d+) tokens")


def resolve_log(explicit):
    """--log, else the launch agent's StandardErrorPath, else the default."""
    if explicit:
        return pathlib.Path(explicit)
    try:
        with open(AGENT_PLIST, "rb") as fh:
            if err := plistlib.load(fh).get("StandardErrorPath"):
                return pathlib.Path(err)
    except (OSError, ValueError):
        pass
    return DEFAULT_LOG


def generate(model, prompt, num_ctx, num_predict=8, options=None):
    """Raw, non-thinking: the warm continuation is neither re-templated nor hidden as thinking."""
    return api("/api/generate", {
        "model": model, "prompt": prompt, "stream": False, "raw": True, "think": False,
        "options": {**(options or {}), "num_ctx": num_ctx, "num_predict": num_predict},
        "keep_alive": "120s"})


def evaluated_from_log(log, start_offset):
    """Uncached prompt tokens of the newest request logged after start_offset, or None."""
    if not log or not log.is_file():
        return None
    try:
        with open(log, "rb") as fh:
            fh.seek(start_offset)
            found = EVAL_LINE.findall(fh.read().decode(errors="replace"))
    except OSError:
        return None
    return int(found[-1]) if found else None


def probe(model, num_ctx, depth, log=None, options=None):
    """Cold turn, then a warm turn that EXTENDS it with the model's own reply: a hybrid
    recurrent model reuses state only along an extension, so a diverging marker reads 0%."""
    prompt = f"# cache probe {time.time_ns()}\n" + CHUNK * max(1, depth // 18)  # salt once
    subprocess.run(["ollama", "stop", model], capture_output=True)
    time.sleep(4)
    cold = generate(model, prompt, num_ctx, num_predict=24, options=options)
    cold_count, cold_s = cold.get("prompt_eval_count", 0), cold.get("prompt_eval_duration", 0) / 1e9
    print(f"  cold turn : {cold_count:>6} prompt tok" +
          (f", {cold_s:6.1f} s prefill ({cold_count / cold_s:5.0f} tok/s)" if cold_s else ""))

    off = log.stat().st_size if (log and log.is_file()) else None
    warm = generate(model, prompt + cold.get("response", "") + "\n# follow-up marker line\n",
                    num_ctx, options=options)
    warm_count, warm_s = warm.get("prompt_eval_count", 0), warm.get("prompt_eval_duration", 0) / 1e9
    cached = warm.get("prompt_eval_cached_count")
    if cached is not None:
        evaluated, source = warm_count - cached, "prompt_eval_cached_count"
    else:
        evaluated = evaluated_from_log(log, off) if off is not None else None
        source = "server log"
    speedup = cold_s / warm_s if warm_s else float("inf")

    if evaluated is not None and warm_count:
        reuse = max(0.0, 1.0 - evaluated / warm_count)
        print(f"  warm turn : {warm_count:>6} prompt tok, {evaluated} evaluated ({source}), "
              f"{warm_s:6.1f} s prefill -> {reuse * 100:4.1f}% reused")
        print(f"  verdict   : prefix cache ACTIVE -- skipped {warm_count - evaluated} tok, ~{speedup:.0f}x faster"
              if reuse >= 0.5 else "  verdict   : prefix cache INACTIVE -- every turn pays cold prefill")
        return reuse
    print(f"  warm turn : {warm_count:>6} prompt tok (full count; no cached count), {warm_s:6.1f} s prefill")
    if warm_s and cold_s and warm_s <= 0.5 * cold_s:
        print(f"  verdict   : prefix cache ACTIVE (inferred) -- prefill collapsed ~{speedup:.0f}x; "
              "exact reuse needs the server log or Ollama >= 0.33.3")
        return 1.0
    print("  verdict   : reuse UNCONFIRMED -- no cached-token signal and no prefill collapse; "
          "pass --log PATH or use --tail after a real session")
    return 0.0


def tail(log, n):
    """Passive: per-request reuse from the log. Only the FIRST `cached n_tokens` after a
    request is inherited prefix; later ones are a recurrent model's own snapshots."""
    if not log.is_file():
        print(f"no Ollama log at {log} -- pass --log PATH; the active probe needs no log")
        return
    rows, first = [], None
    for line in log.read_text(errors="replace").splitlines():
        if m := re.search(r"cached n_tokens = (\d+)", line):
            first = int(m.group(1)) if first is None else first
        elif m := EVAL_LINE.search(line):
            rows.append((first or 0, int(m.group(1))))
            first = None
    if not rows:
        print(f"no completed requests found in {log}")
        return
    rows = rows[-n:]
    print(f"last {len(rows)} request(s) from {log.name}:\n"
          f"  {'prompt_tok':>11} {'cached_tok':>11} {'prefilled_tok':>14} {'reuse%':>7}")
    for cached, evaluated in rows:
        total = cached + evaluated
        print(f"  {total:>11} {cached:>11} {evaluated:>14} {(cached / total * 100) if total else 0.0:>6.1f}%")


def main(argv=None):
    ap = parser(__doc__, budget=600)
    ap.add_argument("--tier", help="default: the active profile")
    ap.add_argument("--quick", action="store_true", help="smaller prefix (~4K tok)")
    ap.add_argument("--tail", type=int, metavar="N", help="passive: summarise the last N logged requests")
    ap.add_argument("--log", help="Ollama log path (default: the login agent's)")
    args = ap.parse_args(argv)
    log = resolve_log(args.log)
    if args.tail is not None:
        tail(log, max(1, args.tail))
        return 0

    tier = args.tier or P.resolve_active_tier()
    role = P.resolve_role(tier, "primary")
    model, num_ctx = role["active"], role["context_input"] + role["max_output"]
    options = {"draft_num_predict": role["draft_num_predict"]} if role.get("draft_num_predict") is not None else {}
    ollama, _ = versions()
    print(f"prompt-cache probe -- tier {tier}, model {model}, num_ctx {num_ctx}\n"
          "  scope: raw backend continuation; not a native-client conversation")
    if not log.is_file():
        print(f"  note: no Ollama log at {log}; exact reuse% then needs Ollama >= 0.33.3")
    try:
        with measurement_budget(args.time_budget):
            generate(model, "hi", num_ctx, num_predict=1, options=options)
            try:  # informational; older builds omit the runner
                runner = engine_of(model)
            except RuntimeError:
                runner = "unreported"
            print(f"Ollama {ollama}, runner {runner}")
            probe(model, num_ctx, 4000 if args.quick else min(role["context_input"], 12000), log, options)
    finally:
        release_model(model)
    return 0


if __name__ == "__main__":
    cli(main)
