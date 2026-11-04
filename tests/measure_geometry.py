#!/usr/bin/env python3
"""Re-measure the context/output geometry the active profile cites. Out of the gate.

    python3 tests/measure_geometry.py [--tier 32gb] [--quick] [--time-budget S]

Prints measurements and asserts nothing; exit status says only whether it ran.
Stops and reloads models: run alone. Also the shared helpers for tests/measure_*.py.
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
# ~18 tokens per repetition; callers salt once per probe so probes share no prefix.
CHUNK = "def process_record(record, index):\n    value = record.get('value')\n"


class BudgetExpired(RuntimeError):
    """The whole-run deadline, distinct from an individual socket timeout."""


@contextmanager
def measurement_budget(seconds):
    """Bound an entire run, including blocked I/O; restores any outer timer."""
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


def positive(text):
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError("must be positive")
    return value


def parser(doc, budget=900):
    """The common CLI: the docstring's first line and a whole-run --time-budget."""
    ap = argparse.ArgumentParser(description=(doc or "").split("\n")[0])
    ap.add_argument("--time-budget", type=positive, default=budget,
                    help=f"whole-run wall-clock budget in seconds (default {budget})")
    return ap


def cli(main, errors=(urllib.error.URLError, OSError, ValueError, RuntimeError)):
    """Entry point: line-buffered (a silent probe looks hung), clean failure exits."""
    sys.stdout.reconfigure(line_buffering=True)
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
    except errors as exc:
        sys.exit(f"measurement could not run: {type(exc).__name__}: {exc}")


def api(path, payload=None, timeout=1800):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(HOST + path, data=data,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def release_model(model):
    """Bounded cleanup that never replaces the original measurement failure."""
    try:
        api("/api/generate", {"model": model, "keep_alive": 0}, timeout=10)
    except BudgetExpired:
        raise
    except (OSError, ValueError) as exc:
        print(f"model release failed for {model}: {type(exc).__name__}", file=sys.stderr)


def installed():
    """Exact installed tags to digests; measurements refuse anything else."""
    return {m["name"]: m["digest"] for m in api("/api/tags", timeout=10).get("models", [])}


def loaded_model(model):
    """The /api/ps entry for one exact tag; variants of a family are distinct."""
    def canonical(name):
        return name if ":" in name.rsplit("/", 1)[-1] else name + ":latest"
    return next((m for m in api("/api/ps").get("models", [])
                 if canonical(m["name"]) == canonical(model)), None)


def resident(model):
    entry = loaded_model(model)
    return entry["size"] if entry else None


def engine_of(model):
    """The runner Ollama reports after loading; tag names and formats do not decide."""
    runner = (loaded_model(model) or {}).get("runner")
    if runner in ("mlx", "llamacpp"):
        return "llama_cpp" if runner == "llamacpp" else runner
    raise RuntimeError(f"runner unavailable for {model}; load it before measuring")


def generate_cold(model, prompt, num_ctx, num_predict, options):
    subprocess.run(["ollama", "stop", model], capture_output=True)
    time.sleep(4)
    return api("/api/generate", {"model": model, "prompt": prompt, "stream": False,
                                 "options": {**(options or {}), "num_ctx": num_ctx,
                                             "num_predict": num_predict},
                                 "keep_alive": "120s"})


def load_cold(model, num_ctx, options=None):
    """Resident bytes after a cold load: all of eager llama_cpp KV, only MLX weights."""
    generate_cold(model, "hi", num_ctx, 1, options)
    time.sleep(2)
    return resident(model)


def fill(model, target_tokens, num_ctx, options=None):
    """One cold, uncached prefill: (prompt tokens, seconds, resident bytes)."""
    prompt = f"# geometry probe {target_tokens} {time.time_ns()}\n" + CHUNK * max(1, target_tokens // 18)
    r = generate_cold(model, prompt, num_ctx, 8, options)
    return r.get("prompt_eval_count", 0), r.get("prompt_eval_duration", 0) / 1e9, resident(model)


def ceiling(model, num_ctx, want, options=None):
    """done_reason 'length' at the requested count demonstrates the output budget."""
    r = api("/api/generate", {
        "model": model, "stream": False, "keep_alive": "60s",
        "prompt": "List 400 distinct short code-review rules, numbered, one per line. Do not stop early.",
        "options": {**(options or {}), "num_ctx": num_ctx, "num_predict": want}})
    return r.get("eval_count", 0), r.get("done_reason", "?")


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
        if lib.is_file():
            out = subprocess.run(["strings", str(lib)], capture_output=True, text=True).stdout
            found = sorted(set(re.findall(r"\b\d+\.\d+\.\d+(?:-\d+-g[0-9a-f]{7,})?\b", out)))
            if found:
                mlx = f"{found[0]} ({v}, best-effort probe)"
                break
    return ollama, mlx


def main(argv=None):
    ap = parser(__doc__)
    ap.add_argument("--tier", help="default: the active profile")
    ap.add_argument("--quick", action="store_true",
                    help="short resident/KV probe; skip deep prefill and output ceilings")
    args = ap.parse_args(argv)
    models = []
    try:
        with measurement_budget(args.time_budget):
            return measure(args, models)
    finally:
        for model in dict.fromkeys(models):
            release_model(model)


def measure(args, models):
    tier = args.tier or P.resolve_active_tier()
    ollama, mlx = versions()
    print(f"profile {tier}   {datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC}\nollama {ollama}   mlx {mlx}")
    if args.quick:
        print("quick scope: short-prefix residency; deep cache growth and output ceilings are unmeasured")

    seen, ceilings = {}, {}
    for role, cfg in P.profile_summary(tier)["roles"].items():
        model, num_ctx = cfg["active"], cfg["num_ctx"]
        options = {"draft_num_predict": cfg["draft_num_predict"]} if cfg.get("draft_num_predict") is not None else {}
        if not cfg.get("enabled") or cfg.get("max_output") is None:
            print(f"\n[{role}] {model} — no generation route, skipped")
            continue
        models.append(model)
        print(f"\n[{role}] {model}\n  geometry     input {cfg['context_input']} + output "
              f"{cfg['max_output']} = num_ctx {num_ctx}")

        if (model, num_ctx) in seen:  # roles share backends; measure KV once
            print(f"  kv           (same backend and window as {seen[model, num_ctx]})")
        else:
            seen[model, num_ctx] = role
            # A small load identifies the runner; Ollama may pick MLX for any tag.
            load_cold(model, min(num_ctx, 8192), options)
            eng = engine_of(model)
            print(f"  runner       {eng} (reported by Ollama)")
            if eng == "llama_cpp":
                low = max(1024, num_ctx // 4)  # a fixed 16384 exceeded the FIM window
                lo, hi = load_cold(model, low, options), load_cold(model, num_ctx, options)
                if lo and hi and num_ctx > low:
                    span = num_ctx - low
                    print(f"  resident     {hi/GIB:.2f} GiB at num_ctx {num_ctx} ({lo/GIB:.2f} GiB at {low})")
                    print(f"  kv/ctx-token {(hi - lo) / span / 1024:.1f} KB  (eager: whole window, "
                          f"includes OLLAMA_NUM_PARALLEL)")
                    if span < 16384:  # a short span is mostly fixed overhead, not KV
                        print(f"               ^ span is only {span} tokens: an upper bound, not a KV rate")
            else:
                base = load_cold(model, num_ctx, options)
                n, _, after = fill(model, min(cfg["context_input"], 4096 if args.quick else 40000),
                                   num_ctx, options)
                if base and after and n:
                    print(f"  resident     {base/GIB:.2f} GiB loaded, {after/GIB:.2f} GiB after {n} tokens")
                    print("  kv/token     not estimated from the short quick-mode prefix" if args.quick else
                          f"  kv/token     {(after-base)/n/1024:.1f} KB  (lazy: per token in use)")

        if args.quick:
            print("  output       not measured in quick mode")
            continue
        # The deepest admitted prompt: the worst cold resume before client compaction.
        n, secs, after = fill(model, cfg["context_input"], num_ctx, options)
        if n and secs:
            print(f"  cold prefill {n} tokens in {secs:.1f}s ({n/secs:.0f} tok/s) at full admission")
        if after:
            print(f"  resident     {after/GIB:.2f} GiB with {n} tokens in the window")
        # Generating max_output costs minutes; a shared (model, ceiling) is probed once.
        if (model, cfg["max_output"]) in ceilings:
            print(f"  output       (same ceiling on the same backend as {ceilings[model, cfg['max_output']]})")
        else:
            ceilings[model, cfg["max_output"]] = role
            got, why = ceiling(model, num_ctx, cfg["max_output"], options)
            print(f"  output       reached {got} of {cfg['max_output']} (done_reason={why})")

    print("\nCompare against the profile's comments; update them or the geometry if the engine moved.")
    return 0


if __name__ == "__main__":
    cli(main, (urllib.error.URLError, OSError, RuntimeError))
