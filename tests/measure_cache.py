#!/usr/bin/env python3
"""Show whether the prompt prefix cache is actually being reused. NOT a benchmark.

On Apple Silicon an agent turn's time-to-first-token is dominated by PREFILL, not
decode, and the one lever that controls prefill across turns is the KV prefix
cache: if the backend reuses the cached prefix, a follow-up turn re-evaluates
only the new tokens instead of the whole conversation. `registry.yaml` and
`docs/troubleshooting.md` both describe the cold-prefill stall that a cache MISS
produces; this script is how you confirm the cache is doing its job, and how you
catch a regression (an engine upgrade, a hybrid/recurrent model, an interleaving
request) that silently turns every turn cold.

Like `measure_geometry.py` it is deliberately NOT part of the gate, has no
history, no thresholds and no pass/fail opinion beyond a plain verdict line. It
prints what it measured; a human reads it. Exit status reflects whether the
MEASUREMENT ran, never whether the numbers were good.

Two modes:

    python3 tests/measure_cache.py            # active probe: cold then warm turn
    python3 tests/measure_cache.py --quick    # smaller prefix, faster
    python3 tests/measure_cache.py --tier 64gb
    python3 tests/measure_cache.py --tail 20  # PASSIVE: summarise the last N real
                                              # requests from the Ollama log, no
                                              # inference — run it after a session

The active probe sends raw, non-thinking requests straight to Ollama (:11434).
This measures backend prefix reuse without rewrapping the warm continuation in
a new chat template or dropping generated thinking. It does not prove that a
native client's actual prompts keep the same prefix; use the passive mode after
a real session for that evidence.

Reading the right signal is the whole trick. Ollama's HTTP `prompt_eval_count`
does NOT exclude the cached prefix -- it reports the FULL prompt length whether
the tokens were computed or served from cache, so a warm turn that reused
everything still reports the full count and looks like a total miss. [REAL]
confirmed on this host (Ollama 0.33.2): a warm turn reported prompt_eval_count
11115 while the runner's own log showed only 1030 tokens actually evaluated.
Ollama 0.33.3 added `prompt_eval_cached_count` to expose the served count
(docs.ollama.com/api/usage); below that version the uncached count appears only
in the server log's `prompt eval time = .. / N tokens` line, and the collapse of
`prompt_eval_duration` corroborates it. So this probe reads, in order of
preference: `prompt_eval_cached_count` when the Ollama build provides it, else the
server log's evaluated-token count, else -- with neither available -- the prefill
TIME collapse alone, reported as such.

It stops and reloads the probed model, so never run it beside another inference
suite. Importing it runs nothing.
"""
from __future__ import annotations

import argparse
import pathlib
import plistlib
import re
import subprocess
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))

from measure_geometry import (  # noqa: E402
    api, engine_of, versions, measurement_budget, release_model,
)
from ailocal import policy as P  # noqa: E402

# ~18 tokens per repetition, salted per run so a probe never reuses a cache left
# by an earlier probe: a cold turn must actually be cold.
CHUNK = "def process_record(record, index):\n    value = record.get('value')\n"

# Default macOS launchd stderr sink (Ollama logs to stderr). Overridable with
# --log; otherwise resolved from the login agent's plist, then this default.
DEFAULT_LOG = pathlib.Path.home() / "Library/Logs/ailocal/com.ailocal.ollama.err.log"
AGENT_PLIST = pathlib.Path.home() / "Library/LaunchAgents/com.ailocal.ollama.plist"


def resolve_log(explicit):
    """The Ollama log path: --log wins, then the agent plist, then the default."""
    if explicit:
        return pathlib.Path(explicit)
    try:
        with open(AGENT_PLIST, "rb") as fh:
            err = plistlib.load(fh).get("StandardErrorPath")
        if err:
            return pathlib.Path(err)
    except (OSError, ValueError):
        pass
    return DEFAULT_LOG


def generate(model, prompt, num_ctx, num_predict=8, options=None):
    """One /api/generate turn. Returns the whole response dict.

    The caller reads prompt_eval_count (the FULL prompt length -- see the module
    docstring; it is NOT cache-excluded), the optional prompt_eval_cached_count
    (the served count, Ollama >= 0.33.3 only), prompt_eval_duration (wall time on
    the UNCACHED tokens, i.e. the real prefill component of time-to-first-token),
    and response (the model's own output, which the warm turn appends so the
    probed sequence EXTENDS the cached one instead of diverging from it)."""
    return api("/api/generate", {
        "model": model, "prompt": prompt, "stream": False,
        "raw": True, "think": False,
        "options": {**(options or {}), "num_ctx": num_ctx, "num_predict": num_predict},
        "keep_alive": "120s"})


def evaluated_from_log(log, start_offset):
    """The uncached prompt-token count of the newest request logged after
    start_offset, read from the runner's own `prompt eval time = .. / N tokens`
    line (N is the tokens actually evaluated, cache excluded). Returns None when
    the log is unavailable or no completed request was appended."""
    if not log or not log.is_file():
        return None
    try:
        with open(log, "rb") as fh:
            fh.seek(start_offset)
            tail = fh.read().decode(errors="replace")
    except OSError:
        return None
    evaluated = None
    for line in tail.splitlines():
        m = re.search(r"prompt eval time =\s*[\d.]+ ms /\s*(\d+) tokens", line)
        if m:
            evaluated = int(m.group(1))
    return evaluated


def probe(model, num_ctx, depth, log=None, options=None):
    """Cold turn, then a warm turn that EXTENDS the same resident sequence.

    A real agent turn sends the prior prompt, the model's own reply, and a little
    new input -- a strict extension of what the backend just cached. The warm
    prompt here reproduces exactly that: cold prompt + the cold turn's own
    response + a one-line marker. That distinction is not cosmetic on a HYBRID
    RECURRENT model (qwen3.5 is Gated-DeltaNet): its state can be reused only
    where the new sequence extends the cached one, so appending a marker IN PLACE
    OF the generated output -- diverging right after the shared prompt -- reads a
    false 0% even though the real agent path reuses the whole prefix. The only
    tokens a working cache must re-evaluate here are that trailing marker."""
    # Salt once: replacing every identifier with a timestamp multiplied the
    # token count and made even the quick probe substantially larger than asked.
    prompt = f"# cache probe {time.time_ns()}\n" + CHUNK * max(1, depth // 18)

    subprocess.run(["ollama", "stop", model], capture_output=True)
    time.sleep(4)
    # 24 output tokens so the extension the warm turn reuses is a realistic reply,
    # not a single token; still trivially cheap next to the prompt.
    cold = generate(model, prompt, num_ctx, num_predict=24, options=options)
    cold_count = cold.get("prompt_eval_count", 0)
    cold_s = cold.get("prompt_eval_duration", 0) / 1e9
    print(f"  cold turn : {cold_count:>6} prompt tok, {cold_s:6.1f} s prefill "
          f"({cold_count / cold_s:5.0f} tok/s)" if cold_s else
          f"  cold turn : {cold_count:>6} prompt tok", flush=True)

    warm_prompt = prompt + cold.get("response", "") + "\n# follow-up marker line\n"
    off = log.stat().st_size if (log and log.is_file()) else None
    warm = generate(model, warm_prompt, num_ctx, options=options)
    warm_count = warm.get("prompt_eval_count", 0)
    warm_s = warm.get("prompt_eval_duration", 0) / 1e9

    # The uncached count: the API field (Ollama >= 0.33.3), else the server log,
    # else unknown -- prompt_eval_count itself is the full prompt and cannot stand
    # in for it. reuse is measured only when one of the first two is available.
    cached_field = warm.get("prompt_eval_cached_count")
    if cached_field is not None:
        evaluated, source = warm_count - cached_field, "prompt_eval_cached_count"
    else:
        evaluated = evaluated_from_log(log, off) if off is not None else None
        source = "server log" if evaluated is not None else None

    speedup = (cold_s / warm_s) if warm_s else float("inf")
    if evaluated is not None and warm_count:
        reuse = max(0.0, 1.0 - evaluated / warm_count)
        print(f"  warm turn : {warm_count:>6} prompt tok, {evaluated} evaluated "
              f"({source}), {warm_s:6.1f} s prefill -> {reuse * 100:4.1f}% reused",
              flush=True)
        if reuse >= 0.5:
            print(f"  verdict   : prefix cache ACTIVE -- warm turn skipped "
                  f"{warm_count - evaluated} tok, ~{speedup:.0f}x faster prefill")
        else:
            print("  verdict   : prefix cache INACTIVE -- the warm turn re-prefilled "
                  "the whole prompt. Every turn pays cold prefill.")
        return reuse

    # Neither the field nor the log is available: fall back to the prefill-TIME
    # collapse, which reflects only the uncached tokens. It proves reuse happened
    # but cannot quantify it, so report it as an inference, not a percentage.
    print(f"  warm turn : {warm_count:>6} prompt tok (full count; Ollama "
          f"{'<0.33.3 ' if cached_field is None else ''}reports no cached count "
          f"here), {warm_s:6.1f} s prefill", flush=True)
    if warm_s and cold_s and warm_s <= 0.5 * cold_s:
        print(f"  verdict   : prefix cache ACTIVE (inferred) -- warm prefill "
              f"collapsed ~{speedup:.0f}x; exact reuse needs the server log or "
              "Ollama >= 0.33.3.")
        return 1.0
    print("  verdict   : prefix cache reuse UNCONFIRMED -- no cached-token signal "
          "and no prefill-time collapse. Pass --log PATH or use --tail after a "
          "real session.")
    return 0.0


def tail(log, n):
    """PASSIVE: summarise the last N real requests from the Ollama log. No model
    call -- run it after a claude-local/codex-local session to see the hit rate
    the agent actually got. The client and LiteLLM do not surface cached tokens,
    so this log is the only window onto per-request reuse."""
    if not log.is_file():
        print(f"no Ollama log at {log} -- pass --log PATH, or this is not a launchd "
              "install. The active probe (no --tail) needs no log.")
        return
    text = log.read_text(errors="replace").splitlines()
    # Each request ends with a timing line. The FIRST "cached n_tokens" after the
    # previous request is the prefix this one INHERITED from the cache -- its real
    # reuse. The later "cached n_tokens" lines within a request are the recurrent
    # model snapshotting its own state as it prefills, not reuse, so taking the
    # last one would make even a cold turn look ~half-cached. First-wins per
    # request avoids that.
    rows, first_cached, seen = [], 0, False
    for line in text:
        m = re.search(r"cached n_tokens = (\d+)", line)
        if m:
            if not seen:
                first_cached, seen = int(m.group(1)), True
            continue
        m = re.search(r"prompt eval time =\s*[\d.]+ ms /\s*(\d+) tokens", line)
        if m:
            evaluated = int(m.group(1))
            rows.append((first_cached, evaluated))
            first_cached, seen = 0, False
    if not rows:
        print(f"no completed requests found in {log}")
        return
    rows = rows[-n:]
    print(f"last {len(rows)} request(s) from {log.name}:")
    print(f"  {'prompt_tok':>11} {'cached_tok':>11} {'prefilled_tok':>14} {'reuse%':>7}")
    for cached, evaluated in rows:
        total = cached + evaluated
        reuse = (cached / total * 100) if total else 0.0
        print(f"  {total:>11} {cached:>11} {evaluated:>14} {reuse:>6.1f}%")


def main(argv=None):
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
    ap.add_argument("--tier", help="default: the active profile")
    ap.add_argument("--quick", action="store_true",
                    help="smaller prefix (~4K tok), faster but a shorter proof")
    ap.add_argument("--tail", type=int, metavar="N",
                    help="passive: summarise the last N requests from the Ollama log")
    ap.add_argument("--log", help="Ollama log path (default: the login agent's)")
    ap.add_argument("--time-budget", type=int, default=600,
                    help="whole-run wall-clock budget in seconds (default 600)")
    args = ap.parse_args(argv)
    if args.time_budget < 1:
        ap.error("--time-budget must be positive")

    if args.tail is not None:
        tail(resolve_log(args.log), max(1, args.tail))
        return 0

    tier = args.tier or P.resolve_active_tier()
    role = P.resolve_role(tier, "architecture")
    model = role["active"]
    num_ctx = role["context_input"] + role["max_output"]
    depth = 4000 if args.quick else min(role["context_input"], 12000)
    options = {"draft_num_predict": role["draft_num_predict"]} if role.get("draft_num_predict") is not None else {}

    ollama, _ = versions()
    log = resolve_log(args.log)
    print(f"prompt-cache probe -- tier {tier}, model {model}, num_ctx {num_ctx}")
    print("  scope: raw backend continuation; not a native-client conversation")
    if not log.is_file():
        print(f"  note: no Ollama log at {log}; without it the exact reuse% is "
              "available only on Ollama >= 0.33.3 -- otherwise the verdict falls "
              "back to the prefill-time collapse. Pass --log PATH to point at it.")
    models = [model]
    try:
        with measurement_budget(args.time_budget):
            # Load once so the runner is known, then report it: cache behaviour is
            # a runner property (llama_cpp reuses a prefix; a recurrent/hybrid
            # model reuses only to its last snapshot offset).
            generate(model, "hi", num_ctx, num_predict=1, options=options)
            # The runner is informational -- cache behaviour is a runner property,
            # but older Ollama builds omit the field, and a missing name must not
            # abort the measurement that IS the point of this script.
            try:
                runner = engine_of(model)
            except RuntimeError:
                runner = "unreported"
            print(f"Ollama {ollama}, runner {runner}")
            probe(model, num_ctx, depth, log=log, options=options)
    finally:
        for m in dict.fromkeys(models):
            release_model(m)
    return 0


if __name__ == "__main__":
    sys.exit(main())
