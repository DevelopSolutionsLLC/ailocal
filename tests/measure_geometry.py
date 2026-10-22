#!/usr/bin/env python3
"""Reproduce the measurements the active profile cites. NOT a benchmark.

The profiles justify their context and output geometry with numbers -- KV bytes
per context token, cold-prefill rate, resident size at a window. Those numbers
came from a specific Ollama build, a specific MLX build and specific models, and
`resources/deploy/litellm/registry.yaml` says in as many words to revalidate
after any engine upgrade. This script is how that instruction is carried out.

It is deliberately NOT part of the gate, has no history, no thresholds and no
pass/fail opinion. It prints what it measured; a human compares that against the
profile and decides. Exit status reflects whether the MEASUREMENT ran, never
whether the numbers were good.

    python3 tests/measure_geometry.py            # active tier
    python3 tests/measure_geometry.py --tier 32gb
    python3 tests/measure_geometry.py --quick    # skip the slow prefill probe

Importing the helpers does not run measurements. Executing this script stops
and reloads models; never run it beside another inference suite.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import pathlib
import re
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))
from ailocal import policy as P  # noqa: E402

HOST = "http://127.0.0.1:11434"
GIB = 2 ** 30
# ~18 tokens per repetition. Salted per depth so two probes share no prefix:
# a warm prefix cache reports its own reuse, not the cost of a cold session.
CHUNK = "def process_record(record, index):\n    value = record.get('value')\n"


class BudgetExpired(RuntimeError):
    """The whole-run deadline, distinct from an individual socket timeout."""


@contextmanager
def measurement_budget(seconds):
    """Bound an entire opt-in run, including blocked I/O, on macOS/Linux."""
    previous_handler = signal.getsignal(signal.SIGALRM)
    previous_timer = signal.getitimer(signal.ITIMER_REAL)
    started = time.monotonic()

    def expired(signum, frame):
        raise BudgetExpired("measurement wall-clock budget exhausted; results are incomplete")

    signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, min(seconds, previous_timer[0])
                     if previous_timer[0] else seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
        if previous_timer[0]:
            remaining = max(0.000001, previous_timer[0] - (time.monotonic() - started))
            signal.setitimer(signal.ITIMER_REAL, remaining, previous_timer[1])


def release_model(model):
    """Bound cleanup without replacing the original measurement failure."""
    try:
        api('/api/generate', {'model': model, 'keep_alive': 0}, timeout=10)
    except BudgetExpired:
        raise
    except (OSError, ValueError) as exc:
        print(f"model release failed for {model}: {type(exc).__name__}", file=sys.stderr)


def api(path, payload=None, timeout=1800):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(HOST + path, data=data,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def loaded_model(model):
    """Resolve one exact model tag; variants of the same family are distinct."""
    def canonical(name):
        return name if ":" in name.rsplit("/", 1)[-1] else name + ":latest"

    for m in api("/api/ps").get("models", []):
        if canonical(m["name"]) == canonical(model):
            return m
    return None


def resident(model):
    """Resident bytes, or None. The reported context is LOAD-time context."""
    entry = loaded_model(model)
    return entry["size"] if entry else None


def load_cold(model, num_ctx):
    """Stop, then load at num_ctx with a one-token prompt. On llama_cpp this
    exposes the whole eagerly-allocated KV; on MLX it exposes only weights,
    because MLX allocates KV lazily as tokens arrive."""
    subprocess.run(["ollama", "stop", model], capture_output=True)
    time.sleep(4)
    api("/api/generate", {"model": model, "prompt": "hi", "stream": False,
                          "options": {"num_ctx": num_ctx, "num_predict": 1},
                          "keep_alive": "120s"})
    time.sleep(2)
    return resident(model)


def fill(model, target_tokens, num_ctx):
    """One cold, uncached prefill. Returns the API's own token accounting."""
    subprocess.run(["ollama", "stop", model], capture_output=True)
    time.sleep(4)
    prompt = (CHUNK * max(1, target_tokens // 18)).replace(
        "record", f"rec_{target_tokens}")
    r = api("/api/generate", {"model": model, "prompt": prompt, "stream": False,
                             "options": {"num_ctx": num_ctx, "num_predict": 8},
                             "keep_alive": "120s"})
    return (r.get("prompt_eval_count", 0),
            r.get("prompt_eval_duration", 0) / 1e9, resident(model))


def ceiling(model, num_ctx, want):
    """Does the role's output ceiling actually deliver? finish_reason is the
    signal: 'length' means num_predict bound it, 'stop' means the model chose to
    end below it. Either proves the ceiling is not silently smaller."""
    r = api("/api/generate", {
        "model": model,
        "prompt": "List 400 distinct short code-review rules, numbered, one per "
                  "line. Do not stop early.",
        "stream": False,
        "options": {"num_ctx": num_ctx, "num_predict": want},
        "keep_alive": "60s"})
    return r.get("eval_count", 0), r.get("done_reason", "?")


def engine_of(model):
    """Read the actual runner after loading; names and formats do not decide."""
    entry = loaded_model(model)
    runner = entry.get("runner") if entry else None
    if runner == "mlx":
        return "mlx"
    if runner == "llamacpp":
        return "llama_cpp"
    raise RuntimeError(f"runner unavailable for {model}; load it before measuring")


def versions():
    try:
        ollama = api("/api/version", timeout=10).get("version", "?")
    except BudgetExpired:
        raise
    except Exception:
        ollama = "unreachable"
    mlx = "unavailable"
    for v in ("mlx_metal_v4", "mlx_metal_v3"):
        lib = pathlib.Path(f"/Applications/Ollama.app/Contents/Resources/{v}/libmlx.dylib")
        if not lib.is_file():
            continue
        out = subprocess.run(["strings", str(lib)], capture_output=True, text=True).stdout
        found = sorted(set(re.findall(r"\b\d+\.\d+\.\d+(?:-\d+-g[0-9a-f]{7,})?\b", out)))
        if found:
            mlx = f"{found[0]} ({v}, best-effort probe)"
            break
    return ollama, mlx


def main(argv=None):
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
    ap.add_argument("--tier", help="default: the active profile")
    ap.add_argument("--quick", action="store_true",
                    help="skip the cold-prefill probe (the slow one)")
    ap.add_argument("--time-budget", type=int, default=900,
                    help="whole-run wall-clock budget in seconds (default 900)")
    args = ap.parse_args(argv)
    if args.time_budget < 1:
        ap.error('--time-budget must be positive')
    models = []
    try:
        with measurement_budget(args.time_budget):
            return measure(args, models)
    finally:
        for model in dict.fromkeys(models):
            release_model(model)


def measure(args, models):
    # A probe here costs minutes and a human is watching it. Block-buffered
    # stdout would print nothing until the end, which is indistinguishable from
    # hung -- the exact confusion docs/troubleshooting.md warns about.
    sys.stdout.reconfigure(line_buffering=True)

    tier = args.tier or P.resolve_active_tier()
    summary = P.profile_summary(tier)
    ollama, mlx = versions()
    print(f"profile {tier}   {datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC}")
    print(f"ollama {ollama}   mlx {mlx}")
    comp = summary.get("compaction") or {}
    if comp.get("window") and comp.get("pct"):
        trig = int(comp["window"]) * int(comp["pct"]) // 100
        print(f"compaction  window {comp['window']} x {comp['pct']}% = {trig} tokens")

    seen, ceilings = {}, {}
    for role, cfg in summary["roles"].items():
        model, num_ctx = cfg["active"], cfg["num_ctx"]
        if not cfg.get("enabled") or cfg.get("max_output") is None:
            print(f"\n[{role}] {model} — no generation route, skipped")
            continue
        models.append(model)
        print(f"\n[{role}] {model}")
        print(f"  geometry     input {cfg['context_input']} + output "
              f"{cfg['max_output']} = num_ctx {num_ctx}")

        # KV is measured once per (model, num_ctx); roles share backends.
        key = (model, num_ctx)
        if key in seen:
            print(f"  kv           (same backend and window as {seen[key]})")
        else:
            seen[key] = role
            # A small load identifies the runner before choosing an eager/lazy
            # KV measurement. Recent Ollama versions auto-select MLX even for
            # tags that do not end in -mlx.
            load_cold(model, min(num_ctx, 8192))
            eng = engine_of(model)
            print(f"  runner       {eng} (reported by Ollama)")
            if eng == "llama_cpp":
                # The low anchor scales with the window. A fixed 16384 exceeded
                # the FIM role's whole 4480-token window, so that role silently
                # reported no KV at all.
                low = max(1024, num_ctx // 4)
                lo = load_cold(model, low)
                hi = load_cold(model, num_ctx)
                if lo and hi and num_ctx > low:
                    span = num_ctx - low
                    per = (hi - lo) / span
                    print(f"  resident     {hi/GIB:.2f} GiB at num_ctx {num_ctx} "
                          f"({lo/GIB:.2f} GiB at {low})")
                    print(f"  kv/ctx-token {per/1024:.1f} KB  (eager: charged for "
                          f"the whole window, includes OLLAMA_NUM_PARALLEL)")
                    # A slope over a short span is mostly fixed allocation
                    # overhead, not KV: the FIM role's 3,360-token span reported
                    # 63.6 KB/token for a 3B model, six times a 2B measured over
                    # a wide one. Report it, but never let it be read as KV.
                    if span < 16384:
                        print(f"               ^ span is only {span} tokens; "
                              f"fixed overhead dominates, so treat this as an "
                              f"upper bound, not a KV rate")
            else:
                base = load_cold(model, num_ctx)
                depth = min(cfg["context_input"], 40000)
                n, _, after = fill(model, depth, num_ctx)
                if base and after and n:
                    print(f"  resident     {base/GIB:.2f} GiB loaded, "
                          f"{after/GIB:.2f} GiB after {n} tokens")
                    print(f"  kv/token     {(after-base)/n/1024:.1f} KB  (lazy: "
                          f"charged per token in use, not per window)")

        if not args.quick:
            depth = min(int(comp.get("window", 0)) * int(comp.get("pct", 0)) // 100
                        or cfg["context_input"], cfg["context_input"])
            n, secs, _ = fill(model, depth, num_ctx)
            if n and secs:
                print(f"  cold prefill {n} tokens in {secs:.1f}s "
                      f"({n/secs:.0f} tok/s) — the cost of resuming a session "
                      f"at the compaction point")

        # Deduped like KV, and for a sharper reason: this probe GENERATES up to
        # max_output tokens, which on a local 26B is minutes. architecture and
        # review share a model AND a ceiling, so probing both measures the same
        # thing twice at full cost.
        ckey = (model, cfg["max_output"])
        if ckey in ceilings:
            print(f"  output       (same ceiling on the same backend as "
                  f"{ceilings[ckey]})")
        else:
            ceilings[ckey] = role
            got, why = ceiling(model, num_ctx, cfg["max_output"])
            print(f"  output       reached {got} of {cfg['max_output']} "
                  f"(done_reason={why})")

    print("\nCompare against the profile's own comments and update them, or the "
          "geometry, if the engine has moved. This script asserts nothing.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
    except (urllib.error.URLError, OSError, RuntimeError) as exc:
        sys.exit(f"measurement could not run: {exc}")
