#!/usr/bin/env python3
"""Compare candidate models for the agentic default. Out of the gate; asserts nothing.

    python3 tests/measure_agentic.py --quick    # decode-only plumbing smoke
    python3 tests/measure_agentic.py            # active model, 32K decode/tools pilot
    python3 tests/measure_agentic.py --prefill  # opt into expensive prefill probes
    python3 tests/measure_agentic.py --models gemma4:26b-mlx

Measures decode tok/s with MTP acceptance, cold/warm prefill, resident memory and a
tool loop, each candidate at its vendor sampling. The tool loop is [APPROX]: direct
/api/chat, because the gateway routes only the incumbent. Stops and reloads models: run alone.
"""
from __future__ import annotations

import json
import pathlib
import re
import statistics
import subprocess
import sys
import time
import urllib.error
from datetime import datetime, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from measure_geometry import (  # noqa: E402
    CHUNK, GIB, P, api, cli, engine_of, generate_cold, load_cold, measurement_budget,
    parser, positive, release_model, resident, versions,
)

_NEUTRAL = {"min_p": 0.0, "presence_penalty": 0.0, "repeat_penalty": 1.0}
#: Vendor-recommended sampling from each model card, not the profile's values.
CANDIDATES = {
    "huihui_ai/gemma-4-abliterated:26b-qat": {
        "label": "64/128 GB Cyber — author's abliterated QAT GGUF", "think": True, "num_ctx": 32768,
        "options": {"temperature": 1.0, "top_k": 64, "top_p": 0.95, **_NEUTRAL}},
    "gemma4:26b-mlx": {
        "label": "incumbent — architecture/implementation/review today", "think": True,
        "options": {"temperature": 1.0, "top_k": 64, "top_p": 0.95}},
    "qwen3.5:9b": {
        "label": "fast-role challenger / 32 GB architecture model", "think": False, "num_ctx": 135168,
        "options": {"temperature": 0.7, "top_k": 20, "top_p": 0.8, **_NEUTRAL, "presence_penalty": 1.5}},
    "qwen3.5:4b": {
        "label": "16 GB architecture/implementation/review/fast model", "think": False, "num_ctx": 135168,
        "options": {"temperature": 0.7, "top_k": 20, "top_p": 0.8, **_NEUTRAL, "presence_penalty": 1.5}},
    "qwen3.5:2b": {  # the 2B card has a distinct non-thinking preset
        "label": "smaller fast-role challenger", "think": False, "num_ctx": 135168,
        "options": {"temperature": 1.0, "top_k": 20, "top_p": 1.0, **_NEUTRAL, "presence_penalty": 2.0}},
    "qwen3.8:27b-mlx": {  # thinking-mode preset; instruct mode differs
        "label": "challenger — qwen3_5 arch, 27.8B, adds vision", "think": "medium",
        "options": {"temperature": 1.0, "top_k": 20, "top_p": 0.95, **_NEUTRAL}},
}
# Small-tier Cyber candidates take the shipped profile's sampling; host-measured only.
for _tier in ("16gb", "32gb"):
    _cfg = P.resolve_role(_tier, "cyber", P.data_root())
    CANDIDATES[_cfg["model"]] = {
        "label": f"{_tier} unrestricted GGUF candidate; target hardware unmeasured",
        "options": {k: _cfg[k] for k in ("temperature", "top_p", "top_k", "min_p",
                                         "presence_penalty", "repeat_penalty", "stop")},
        "think": _cfg["reasoning"], "num_ctx": _cfg["total_context"]}

DEPTHS = (8192, 32768, 65536)
#: Two steps: the model must read before it can answer.
TOOLS = [
    {"type": "function", "function": {
        "name": "read_file", "description": "Read a UTF-8 text file and return its contents.",
        "parameters": {"type": "object", "required": ["path"], "properties": {
            "path": {"type": "string", "description": "Absolute path"}}}}},
    {"type": "function", "function": {
        "name": "report", "description": "Report the final answer once the file has been read.",
        "parameters": {"type": "object", "required": ["value"], "properties": {
            "value": {"type": "string", "description": "The token found"}}}}},
]
FIXTURE_PATH = "/tmp/ailocal-bench/config.ini"
FIXTURE_BODY = "[service]\nbind = 127.0.0.1\nretry_token = ZX-4417\n"
EXPECT = "ZX-4417"
TASK = f"Read {FIXTURE_PATH} and report the value of retry_token. You must call read_file first; do not guess."

_STATS = re.compile(r"acceptance=(?P<acc>[\d.]+).*?avg_draft=(?P<draft>[\d.]+).*?"
                    r"max_draft=(?P<max>\d+).*?avg_accepted=(?P<acc_n>[\d.]+)")


def server_log():
    """The running server's stderr log, from lsof: the launch agent redirects it to
    ~/Library/Logs/ailocal, so ~/.ollama/logs goes stale. None if deleted or absent."""
    pid = subprocess.run(["pgrep", "-f", "ollama serve"], capture_output=True, text=True).stdout.split()
    if not pid:
        return None
    for line in subprocess.run(["lsof", "-p", pid[0]], capture_output=True, text=True).stdout.splitlines():
        if ".log" in line and " 2u " in line:
            path = pathlib.Path(line.split()[-1])
            return None if "(deleted)" in line or not path.is_file() else path
    return None


def log_offset():
    """(log, size) taken before a request so its stats are not scraped from history."""
    log = server_log()
    try:
        return (log, log.stat().st_size) if log else None
    except OSError:
        return None


def speculation_since(offset):
    """speculate_stats written after offset; None for no stats AND for no log (the
    caller tells them apart by the offset it passed)."""
    if offset is None:
        return None
    log, start = offset
    try:
        with log.open("rb") as fh:
            fh.seek(start)
            rows = [m.groupdict() for m in _STATS.finditer(fh.read().decode("utf-8", "replace"))]
    except OSError:
        return None
    if not rows:
        return None
    mean = lambda key: sum(float(r[key]) for r in rows) / len(rows)  # noqa: E731
    return {"batches": len(rows), "acceptance": mean("acc"), "avg_draft": mean("draft"),
            "max_draft": max(int(r["max"]) for r in rows), "avg_accepted": mean("acc_n")}


def decode(model, opts, think, ctx, want=128, repeats=5):
    """Median decode rate over repeats (single runs spread 68.7-100.8 tok/s with a 2.9
    outlier), with speculation aggregated: the runner does not log every request."""
    rates, accs, reason, log_ok = [], [], "?", False
    for _ in range(repeats):
        off = log_offset()
        log_ok = log_ok or off is not None
        r = api("/api/chat", {
            "model": model, "stream": False, "think": think, "keep_alive": "120s",
            "messages": [{"role": "user", "content": "Write a Python function that validates an "
                          "IPv4 address, with a docstring and three unit tests. Code only."}],
            "options": {**opts, "num_ctx": ctx, "num_predict": want}}, timeout=120)
        n, secs, reason = r.get("eval_count", 0), r.get("eval_duration", 0) / 1e9, r.get("done_reason", "?")
        if n and secs:
            rates.append(n / secs)
        if st := speculation_since(off):
            accs.append(st)
    if not rates:
        return None, reason, None, log_ok
    spec = {key: statistics.median(a[key] for a in accs)
            for key in ("acceptance", "avg_accepted", "avg_draft")} if accs else None
    if spec:
        spec.update(max_draft=max(a["max_draft"] for a in accs), reported=len(accs), of=repeats)
    return ({"median": statistics.median(rates), "lo": min(rates), "hi": max(rates), "runs": len(rates)},
            reason, spec, log_ok)


def prefill_pair(model, depth, opts, ctx):
    """Cold prefill, then the identical prompt again while the prefix trie is warm."""
    prompt = f"# prefill probe {depth} {time.time_ns()}\n" + CHUNK * max(1, depth // 18)
    cold = generate_cold(model, prompt, ctx, 8, opts)
    n, res = cold.get("prompt_eval_count", 0), resident(model)
    if not n:
        return 0, 0, None, res
    warm = api("/api/generate", {"model": model, "prompt": prompt, "stream": False, "keep_alive": "120s",
                                 "options": {**opts, "num_ctx": ctx, "num_predict": 8}})
    return n, cold.get("prompt_eval_duration", 0) / 1e9, warm.get("prompt_eval_duration", 0) / 1e9, res


def tool_loop(model, opts, think, ctx, rounds):
    """[APPROX] A round completes only if read_file ran and the file's value was reported.
    Odd rounds inject one transient read failure the model must retry through."""
    fx = pathlib.Path(FIXTURE_PATH)
    fx.parent.mkdir(parents=True, exist_ok=True)
    fx.write_text(FIXTURE_BODY)
    r = dict.fromkeys(("done", "bad_args", "unknown_tool", "no_call", "transport", "thinking",
                       "calls", "fault_rounds", "fault_recovered"), 0)
    r.update(rounds=rounds, turns=[])
    for round_no in range(rounds):
        fault = fault_pending = round_no % 2 == 1
        r["fault_rounds"] += fault
        msgs, read_ok, finished = [{"role": "user", "content": TASK}], False, False
        for turn in range(1, 7):
            try:
                resp = api("/api/chat", {
                    "model": model, "messages": msgs, "tools": TOOLS, "stream": False, "think": think,
                    "options": {**opts, "num_ctx": ctx, "num_predict": 1024}, "keep_alive": "120s"},
                    timeout=120)
            except (urllib.error.URLError, OSError):
                r["transport"] += 1
                break
            m = resp.get("message", {})
            msgs.append(m)
            r["thinking"] += bool((m.get("thinking") or "").strip())
            calls = m.get("tool_calls") or []
            if not calls:  # a reply with no prior read answered from nothing
                finished = read_ok and EXPECT in (m.get("content") or "")
                r["no_call"] += not finished
                break
            for c in calls:
                fn = c.get("function", {})
                name, args = fn.get("name"), fn.get("arguments") or {}
                r["calls"] += 1
                if isinstance(args, str):  # runtime could not fold the JSON in: not native-valid
                    r["bad_args"] += 1
                    try:
                        args = json.loads(args)
                    except ValueError:
                        args = {}
                if name == "read_file":
                    path = args.get("path")
                    if not isinstance(path, str) or not path:
                        r["bad_args"] += 1
                        out = "error: missing required argument 'path'"
                    elif fault_pending:
                        fault_pending, out = False, "error: EAGAIN resource temporarily unavailable; retry the read"
                    else:
                        try:
                            out, read_ok = pathlib.Path(path).read_text(), True
                        except OSError as exc:
                            out = f"error: {exc}"
                    msgs.append({"role": "tool", "name": name, "content": out})
                elif name == "report":
                    value = args.get("value")
                    r["bad_args"] += not isinstance(value, str) or not value
                    finished = read_ok and isinstance(value, str) and value.strip() == EXPECT
                    calls = None
                    break
                else:
                    r["unknown_tool"] += 1
                    msgs.append({"role": "tool", "name": name or "?", "content": "error: unknown tool"})
            if calls is None:
                break
        if finished:
            r["done"] += 1
            r["turns"].append(turn)
            r["fault_recovered"] += fault
    r["avg_turns"] = sum(r["turns"]) / len(r["turns"]) if r["turns"] else None
    return r


def run(model, spec, depths, rounds, quick, repeats):
    try:
        return _run(model, spec, depths, rounds, quick, repeats)
    finally:
        release_model(model)


def _run(model, spec, depths, rounds, quick, repeats):
    ctx = spec.get("num_ctx") or P.effective_role("architecture")["num_ctx"]
    opts, think = spec["options"], spec["think"]
    print(f"\n{'=' * 72}\n{model}\n  {spec['label']}\n  sampling     {opts}  think={think}\n  num_ctx      {ctx}")
    base = load_cold(model, ctx)
    print(f"  runner       {engine_of(model)} (reported by Ollama)")
    if base:
        print(f"  weights      {base / GIB:.2f} GiB resident, no KV yet")

    rate, why, st, log_ok = decode(model, opts, think, ctx, repeats=repeats)
    print(f"  decode       median {rate['median']:.1f} tok/s (spread {rate['lo']:.1f}-{rate['hi']:.1f} "
          f"over {rate['runs']} runs, done_reason={why})" if rate else
          f"  decode       produced nothing (done_reason={why})")
    if st:
        print(f"  speculation  acceptance {st['acceptance']:.2f} median, avg_accepted {st['avg_accepted']:.2f} "
              f"of avg_draft {st['avg_draft']:.1f} (max {st['max_draft']}) — reported by {st['reported']}/{st['of']} runs")
    elif not log_ok:
        print("  speculation  UNAVAILABLE — the server's log target is gone (see ~/Library/Logs/ailocal)")
    else:
        print("  speculation  no speculate_stats emitted; drafting status UNVERIFIED")

    for depth in depths:
        n, cold, warm, res = prefill_pair(model, depth, opts, ctx)
        if not (n and cold):
            print(f"  prefill {depth:>6}  no measurement")
            continue
        print(f"  prefill {depth:>6}  cold {n} tok in {cold:.1f}s ({n / cold:.0f} tok/s)"
              + (f", warm {warm:.1f}s ({n / warm:.0f} tok/s)" if warm else ""))
        if res:
            kv = (res - base) / n / 1024 if base else None
            print(f"                 resident {res / GIB:.2f} GiB" + (f", kv {kv:.1f} KB/token (lazy)" if kv else ""))

    if not quick:
        t = tool_loop(model, opts, think, ctx, rounds)
        print(f"  tool loop    {t['done']}/{t['rounds']} completed correctly"
              + (f", {t['avg_turns']:.1f} turns avg" if t["avg_turns"] else "") + "   [APPROX]")
        print(f"  tool args    {t['bad_args']} invalid of {t['calls']} calls (what tool_repair.py would fix)")
        print(f"  recovery     {t['fault_recovered']}/{t['fault_rounds']} rounds recovered from an injected failure")
        print(f"  other        {t['unknown_tool']} unknown-tool, {t['no_call']} gave up, "
              f"{t['transport']} transport errors, {t['thinking']} turns with reasoning")


def main(argv=None):
    ap = parser(__doc__)
    ap.add_argument("--models", nargs="+", help="default: the active architecture model only")
    ap.add_argument("--quick", action="store_true", help="skip the tool loop; with --prefill, 8K depth only")
    ap.add_argument("--prefill", action="store_true", help="opt into cold/warm context measurements")
    ap.add_argument("--repeats", type=positive, default=3, help="decode samples per model")
    ap.add_argument("--rounds", type=positive, default=3, help="tool-loop rounds per model")
    ap.add_argument("--context", type=int, default=32768, help="candidate context for a bounded pilot")
    args = ap.parse_args(argv)
    args.models = args.models or [P.effective_role("architecture")["model"]]
    if unknown := sorted(set(args.models) - set(CANDIDATES)):
        ap.error(f"unknown candidates: {', '.join(unknown)}")
    if args.context < 16384:
        ap.error("--context must be at least 16384 (8K prefill plus decode headroom)")
    with measurement_budget(args.time_budget):
        return measure(args)


def measure(args):
    ollama, mlx = versions()
    print(f"agentic candidate comparison   {datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC}\n"
          f"ollama {ollama}   mlx {mlx}\nnum_ctx {args.context} pilot override; active architecture "
          f"window {P.effective_role('architecture')['num_ctx']}\n"
          "OLLAMA_KV_CACHE_TYPE / OLLAMA_FLASH_ATTENTION do not reach the MLX runner; MLX KV is unquantized.")
    depths = tuple(d for d in (DEPTHS[:1] if args.quick else DEPTHS) if d <= args.context // 2) if args.prefill else ()
    for model in args.models:
        run(model, {**CANDIDATES[model], "num_ctx": args.context}, depths, args.rounds, args.quick, args.repeats)
    print("\nRead the columns together: decode tok/s means nothing if the tool loop fails.")
    return 0


if __name__ == "__main__":
    cli(main, (urllib.error.URLError, OSError, RuntimeError))
