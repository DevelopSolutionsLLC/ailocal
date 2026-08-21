#!/usr/bin/env python3
"""Compare candidate models for the agentic default. NOT a gate, NOT a test.

`measure_geometry.py` answers "do the profile's numbers still hold for the model
we already ship". This answers a different question: "should we still ship it".
Two MLX-served candidates run the same matrix and the script prints what it
measured. It asserts nothing, keeps no history and has no pass/fail opinion; a
human reads the table and decides.

    python3 tests/measure_agentic.py --quick    # 8K depth only, plumbing smoke
    python3 tests/measure_agentic.py            # full matrix
    python3 tests/measure_agentic.py --models gemma4:26b-mlx

FOUR THINGS ARE MEASURED, because decode tok/s alone picks the wrong model:

  decode + acceptance   tok/s from the API, paired with the `speculate_stats`
                        line the MLX runner emits for that same request. Ollama
                        0.32.13 runs MTP speculative decoding ON BY DEFAULT with
                        a runtime depth controller -- no flag, no DRAFT
                        directive. A candidate with no drafter weights simply
                        emits no stats line, and that absence IS the finding.
  prefill cold + warm   what Claude Code actually waits on. The warm repeat
                        exercises the runner's prefix trie; the gap between the
                        two is the real cost of a cache miss.
  tool loop             multi-step tool calling. "King for agentic default" is a
                        correctness question at least as much as a speed one.
  resident              GiB at depth, which is what the 64 GB profile rests on.

SAMPLING: each candidate runs at ITS OWN VENDOR-RECOMMENDED settings, not at the
profile's temp 0.1. Draft acceptance is temperature-sensitive, so measuring both
models at one arbitrary temperature would compare the setting, not the models.
What ailocal should then SHIP is a separate decision this script only informs.

TWO LABELS, kept apart deliberately:
  [REAL]    produced through the real runner on this machine.
  [APPROX]  the tool loop. It drives Ollama's /api/chat directly rather than the
            LiteLLM gateway, because the generated config has NO route for a
            challenger (config.yaml maps every ailocal-* alias onto the
            incumbent) and this script is forbidden to edit it. Both candidates
            are therefore measured on the same reduced path, which keeps the
            COMPARISON sound while making neither number a gateway measurement.
            --gateway additionally runs the incumbent through the real path, to
            price what the reduced path leaves out.

Nothing else may import this: it stops and reloads models, so anything running
beside it measures eviction.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))
# Reused wholesale rather than reimplemented: same host, same cold-load
# discipline, same salted filler, same version probe.
from measure_geometry import (  # noqa: E402
    GIB, api, engine_of, fill, load_cold, versions,
)

GATEWAY = "http://127.0.0.1:4000"


def server_log():
    """The log the RUNNING server writes to, resolved from the process itself.

    Not a constant. `~/.ollama/logs/server.log` is where a bare `ollama serve`
    lands, but ailocal starts Ollama from a launch agent that redirects stderr
    to ~/Library/Logs/ailocal/, so the ~/.ollama copy goes stale the moment
    ailocal is installed -- and reading it silently reports the speculation
    behaviour of whatever ran months ago. lsof asks the process instead.

    Returns None when the server's log target has been DELETED while the process
    holds it open (lsof marks it `(deleted)`). That is unreadable, not empty, so
    the speculation metric must report itself unavailable rather than absent.
    """
    pid = subprocess.run(["pgrep", "-f", "ollama serve"],
                         capture_output=True, text=True).stdout.split()
    if not pid:
        return None
    out = subprocess.run(["lsof", "-p", pid[0]],
                         capture_output=True, text=True).stdout
    for line in out.splitlines():
        if ".log" not in line or " 2u " not in line:  # fd 2 = stderr = the INFO stream
            continue
        if "(deleted)" in line:
            return None
        path = pathlib.Path(line.split()[-1])
        return path if path.is_file() else None
    return None

#: Vendor-recommended sampling, from each model's own `ollama show` Parameters
#: block cross-checked against the publisher's model card. NOT the profile's
#: values. `think` follows the model's documented mode for coding work.
CANDIDATES = {
    "gemma4:26b-mlx": {
        "label": "incumbent — architecture/implementation/review today",
        "options": {"temperature": 1.0, "top_k": 64, "top_p": 0.95},
        "think": True,
    },
    "qwen3.8:27b-mlx": {
        "label": "challenger — qwen3_5 arch, 27.8B, adds vision",
        # Thinking mode: temp 1.0 / top_p 0.95 / top_k 20. The instruct-mode
        # recommendation (0.7 / 0.80 / presence 1.5) applies with think off and
        # is what --no-think switches to, so neither mode is measured off-spec.
        "options": {"temperature": 1.0, "top_k": 20, "top_p": 0.95, "min_p": 0.0},
        "think": True,
        "options_nothink": {"temperature": 0.7, "top_k": 20, "top_p": 0.80,
                            "presence_penalty": 1.5},
    },
}

DEPTHS = (8192, 32768, 65536)
NUM_CTX = 180224  # the architecture role's live window, so this is its geometry

#: One deterministic two-step scenario. The model must READ before it can answer,
#: so a single-call reply is a failure of the loop, not a shortcut.
TOOLS = [
    {"type": "function", "function": {
        "name": "read_file",
        "description": "Read a UTF-8 text file and return its contents.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "Absolute path"}},
            "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "report",
        "description": "Report the final answer once the file has been read.",
        "parameters": {"type": "object", "properties": {
            "value": {"type": "string", "description": "The token found"}},
            "required": ["value"]}}},
]
FIXTURE_PATH = "/tmp/ailocal-bench/config.ini"
FIXTURE_BODY = "[service]\nbind = 127.0.0.1\nretry_token = ZX-4417\n"
EXPECT = "ZX-4417"
TASK = (f"Read {FIXTURE_PATH} and report the value of retry_token. "
        f"You must call read_file first; do not guess.")


# ── speculation ─────────────────────────────────────────────────────────────

_STATS = re.compile(
    r"acceptance=(?P<acc>[\d.]+).*?avg_draft=(?P<draft>[\d.]+).*?"
    r"max_draft=(?P<max>\d+).*?avg_accepted=(?P<acc_n>[\d.]+)")


def log_offset():
    """(path, byte offset) of the live server log, or None if unreadable.

    Taken BEFORE a request so the stats can be attributed to that request
    instead of scraped out of unrelated history -- the log holds months of runs.
    """
    log = server_log()
    if log is None:
        return None
    try:
        return log, log.stat().st_size
    except OSError:
        return None


def speculation_since(offset):
    """Parse `speculate_stats` lines written after `offset`.

    Returns None both when the runner emitted no stats AND when the log could
    not be resolved at all. The CALLER distinguishes them by checking the offset
    it passed in: a None offset means unreadable, and reporting that as "no
    drafter" would invent a finding out of a missing file.
    """
    if offset is None:
        return None
    log, offset = offset
    try:
        with log.open("rb") as fh:
            fh.seek(offset)
            tail = fh.read().decode("utf-8", "replace")
    except OSError:
        return None
    rows = [m.groupdict() for m in _STATS.finditer(tail)]
    if not rows:
        return None
    drafted = sum(float(r["draft"]) for r in rows)
    return {
        "batches": len(rows),
        "acceptance": sum(float(r["acc"]) for r in rows) / len(rows),
        "avg_draft": drafted / len(rows),
        "max_draft": max(int(r["max"]) for r in rows),
        "avg_accepted": sum(float(r["acc_n"]) for r in rows) / len(rows),
    }


# ── probes ──────────────────────────────────────────────────────────────────

def decode(model, opts, think, want=512):
    """Sustained decode rate on a short prompt, with the speculation it used.

    The prompt asks for bulk generation on purpose: acceptance on predictable
    code text is the case the profiles actually care about, and it is also the
    case the vendors quote their speedups from.
    """
    off = log_offset()
    r = api("/api/generate", {
        "model": model,
        "prompt": "Write a Python function that validates an IPv4 address, "
                  "with a docstring and three unit tests. Code only.",
        "stream": False, "think": think,
        "options": {**opts, "num_ctx": NUM_CTX, "num_predict": want},
        "keep_alive": "120s"})
    n = r.get("eval_count", 0)
    secs = r.get("eval_duration", 0) / 1e9
    # `off is None` is unreadable-log, which is NOT the same claim as "drafted
    # nothing". The caller must be able to tell them apart.
    return n, secs, r.get("done_reason", "?"), speculation_since(off), off is not None


def prefill_pair(model, depth, opts):
    """Cold prefill, then the identical prompt again while the trie is warm."""
    n, cold, res = fill(model, depth, NUM_CTX)
    warm = None
    if n:
        prompt = (
            "def process_record(record, index):\n    value = record.get('value')\n"
            * max(1, depth // 18)).replace("record", f"rec_{depth}")
        r = api("/api/generate", {
            "model": model, "prompt": prompt, "stream": False,
            "options": {**opts, "num_ctx": NUM_CTX, "num_predict": 8},
            "keep_alive": "120s"})
        warm = r.get("prompt_eval_duration", 0) / 1e9
    return n, cold, warm, res


def tool_loop(model, opts, think, rounds):
    """[APPROX] Multi-step tool calling against /api/chat. See module docstring.

    Counts a run complete only when the model called read_file, then reported
    the value that was actually in the file. A model that answers from thin air
    is not fast, it is wrong.
    """
    fx = pathlib.Path(FIXTURE_PATH)
    fx.parent.mkdir(parents=True, exist_ok=True)
    fx.write_text(FIXTURE_BODY)

    done = malformed = 0
    turns = []
    for _ in range(rounds):
        msgs = [{"role": "user", "content": TASK}]
        read_ok = False
        for turn in range(1, 7):
            try:
                r = api("/api/chat", {
                    "model": model, "messages": msgs, "tools": TOOLS,
                    "stream": False, "think": think,
                    "options": {**opts, "num_ctx": NUM_CTX, "num_predict": 1024},
                    "keep_alive": "120s"}, timeout=600)
            except (urllib.error.URLError, OSError):
                malformed += 1
                break
            m = r.get("message", {})
            msgs.append(m)
            calls = m.get("tool_calls") or []
            if not calls:
                # No call and no prior read: it answered from nothing.
                if read_ok and EXPECT in (m.get("content") or ""):
                    done += 1
                    turns.append(turn)
                break
            for c in calls:
                fn = c.get("function", {})
                name, args = fn.get("name"), fn.get("arguments") or {}
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except ValueError:
                        malformed += 1
                        args = {}
                if name == "read_file":
                    try:
                        out = pathlib.Path(args.get("path", "")).read_text()
                        read_ok = True
                    except OSError as exc:
                        out = f"error: {exc}"
                    msgs.append({"role": "tool", "name": name, "content": out})
                elif name == "report":
                    if read_ok and args.get("value", "").strip() == EXPECT:
                        done += 1
                        turns.append(turn)
                    calls = None
                    break
                else:
                    malformed += 1
                    msgs.append({"role": "tool", "name": name or "?",
                                 "content": "error: unknown tool"})
            if calls is None:
                break
    avg = sum(turns) / len(turns) if turns else None
    return done, rounds, malformed, avg


def gateway_probe():
    """Price the real path for the INCUMBENT only, which is all it routes.

    The key is generated, never checked in, and is read from wherever the
    installation actually put it. It is used and never printed.
    """
    key = None
    env = pathlib.Path.home() / ".config" / "ailocal" / ".env"
    if env.is_file():
        for line in env.read_text().splitlines():
            if line.startswith("LITELLM_MASTER_KEY="):
                key = line.split("=", 1)[1].strip()
    if not key:
        got = subprocess.run(
            ["docker", "exec", "ailocal-litellm", "printenv",
             "LITELLM_MASTER_KEY"], capture_output=True, text=True)
        key = got.stdout.strip() or None
    if not key:
        return "no gateway key found; skipped"

    body = json.dumps({
        "model": "ailocal-architecture",
        "messages": [{"role": "user", "content": TASK}],
        "tools": [{"type": "function", "function": t["function"]} for t in TOOLS],
        "max_tokens": 512}).encode()
    req = urllib.request.Request(
        GATEWAY + "/v1/chat/completions", data=body,
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {key}"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=600) as r:
            payload = json.load(r)
    except (urllib.error.URLError, OSError) as exc:
        return f"gateway unreachable: {exc}"
    took = time.time() - t0
    ch = (payload.get("choices") or [{}])[0].get("message", {})
    called = [c.get("function", {}).get("name")
              for c in (ch.get("tool_calls") or [])]
    return (f"{took:.1f}s first turn, tool_calls={called or 'none'} "
            f"(profile sampling, NOT vendor — this is the shipped path)")


# ── report ──────────────────────────────────────────────────────────────────

def run(model, spec, depths, rounds, quick):
    print(f"\n{'='*72}\n{model}   engine={engine_of(model)}\n  {spec['label']}")
    opts, think = spec["options"], spec["think"]
    print(f"  sampling     {opts}  think={think}   [vendor-recommended]")

    base = load_cold(model, NUM_CTX)
    if base:
        print(f"  weights      {base/GIB:.2f} GiB resident, no KV yet")

    n, secs, why, spec_stats, log_ok = decode(model, opts, think)
    if n and secs:
        print(f"  decode       {n} tokens in {secs:.1f}s = {n/secs:.1f} tok/s "
              f"(done_reason={why})")
    else:
        print(f"  decode       produced nothing (done_reason={why})")
    if spec_stats:
        print(f"  speculation  acceptance {spec_stats['acceptance']:.2f} over "
              f"{spec_stats['batches']} batches, avg_accepted "
              f"{spec_stats['avg_accepted']:.2f} of avg_draft "
              f"{spec_stats['avg_draft']:.1f} (max {spec_stats['max_draft']})")
    elif not log_ok:
        print("  speculation  UNAVAILABLE — the running server's log target is "
              "gone (see ~/Library/Logs/ailocal). Not a claim about drafting.")
    else:
        print("  speculation  NO speculate_stats emitted — this model decoded "
              "one token at a time (no MTP drafter for this blob)")

    for depth in depths:
        n, cold, warm, res = prefill_pair(model, depth, opts)
        if not (n and cold):
            print(f"  prefill {depth:>6}  no measurement")
            continue
        line = f"  prefill {depth:>6}  cold {n} tok in {cold:.1f}s ({n/cold:.0f} tok/s)"
        if warm:
            line += f", warm {warm:.1f}s ({n/warm:.0f} tok/s)"
        print(line)
        if res:
            kv = (res - base) / n / 1024 if base else None
            print(f"                 resident {res/GIB:.2f} GiB"
                  + (f", kv {kv:.1f} KB/token (lazy)" if kv else ""))

    if not quick:
        done, tot, bad, avg = tool_loop(model, opts, think, rounds)
        print(f"  tool loop    {done}/{tot} completed correctly, "
              f"{bad} malformed calls"
              + (f", {avg:.1f} turns avg" if avg else "")
              + "   [APPROX — direct /api/chat, not the gateway]")

    subprocess.run(["ollama", "stop", model], capture_output=True)


def main(argv=None):
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
    ap.add_argument("--models", nargs="+", default=list(CANDIDATES),
                    help="default: every candidate")
    ap.add_argument("--quick", action="store_true",
                    help="8K depth only and no tool loop; smokes the plumbing")
    ap.add_argument("--rounds", type=int, default=8,
                    help="tool-loop repetitions per model (default 8)")
    ap.add_argument("--gateway", action="store_true",
                    help="also price the real LiteLLM path (incumbent only)")
    args = ap.parse_args(argv)
    # Minutes per probe with a human watching: block buffering is
    # indistinguishable from hung.
    sys.stdout.reconfigure(line_buffering=True)

    ollama, mlx = versions()
    print(f"agentic candidate comparison   "
          f"{datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC}")
    print(f"ollama {ollama}   mlx {mlx}")
    print(f"num_ctx {NUM_CTX} for every probe (the architecture role's window)")
    print("OLLAMA_KV_CACHE_TYPE / OLLAMA_FLASH_ATTENTION do NOT reach the MLX "
          "runner; they are llama-server flags. KV below is unquantized.")

    depths = DEPTHS[:1] if args.quick else DEPTHS
    for model in args.models:
        spec = CANDIDATES.get(model)
        if not spec:
            print(f"\n{model}: not a known candidate, skipped")
            continue
        run(model, spec, depths, args.rounds, args.quick)

    if args.gateway:
        print(f"\n{'='*72}\ngateway (ailocal-architecture -> incumbent)")
        print(f"  {gateway_probe()}")

    print("\nNothing here is asserted. Read the four columns together: decode "
          "tok/s is meaningless if the tool loop does not complete, and prefill "
          "dominates what an agent session actually feels.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
    except (urllib.error.URLError, OSError) as exc:
        sys.exit(f"measurement could not run: {exc}")
