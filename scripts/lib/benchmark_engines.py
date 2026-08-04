"""benchmark_engines.py — adapters for the external evaluation engines.

MOVED VERBATIM from benchmark.py. lm-eval, EvalPlus and the throughput/cold-load
probes. External tools keep owning datasets, prompting and scoring; this module
only invokes them and reads their artefacts.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

from benchmark_evidence import REPO, state_dir
from benchmark_runtime import LITELLM, api_key, telemetry, unload


def venv_bin(name: str) -> Path:
    base = os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share")
    return Path(base) / "ailocal" / "benchmark" / "venv" / "bin" / name


# ── external engine ─────────────────────────────────────────────────────────


def run_lm_eval(alias: str, task: str, limit: int, out_dir: Path,
                timeout: int = 7200, sample_timeout: int = 300) -> dict:
    """Invoke lm-evaluation-harness against an authenticated LiteLLM alias.

    lm-eval owns the dataset, the prompting, the extraction and the scoring.
    ailocal supplies only the endpoint, the credential and the alias.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    cmd = [str(venv_bin("lm_eval")),
           "--model", "local-chat-completions",
           "--model_args", (f"base_url={LITELLM}/v1/chat/completions,"
                            f"model={alias},num_concurrent=1,max_retries=1,"
                            f"tokenized_requests=False,"
                            # PER-SAMPLE wall limit. A thinking model with a
                            # 32,768 budget will fill it: one gsm8k question ran
                            # 6 minutes generating to the ceiling. The ceiling is
                            # NOT reduced — this bounds real-world usability so a
                            # single runaway cannot stall the screen.
                            f"timeout={sample_timeout}"),
           "--tasks", task,
           "--apply_chat_template",
           # humaneval_plus/mbpp_plus execute generated code. lm-eval requires
           # explicit consent; execution is the whole point of EvalPlus.
           "--confirm_run_unsafe_code",
           # Per-sample records. Without these a score cannot be audited, and an
           # EXTRACTION failure is indistinguishable from a model failure — the
           # exact confusion that made six models look like they scored 0.000 on
           # humaneval_plus.
           "--log_samples",
           # Our corrected task definitions (extraction only; datasets, tests
           # and metrics remain lm-eval's).
           "--include_path", str(REPO / "config" / "benchmark-tasks"),
           "--output_path", str(out_dir)]
    if limit:
        cmd += ["--limit", str(limit)]
    started = time.time()
    before = telemetry()
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                       env={**os.environ, "OPENAI_API_KEY": api_key()})
    wall = time.time() - started

    scores = {}
    for f in sorted(out_dir.rglob("results_*.json")):
        try:
            d = json.loads(f.read_text())
            for tname, metrics in (d.get("results") or {}).items():
                scores[tname] = {k: v for k, v in metrics.items()
                                 if isinstance(v, (int, float))}
        except Exception:
            pass
    a = audit(out_dir)
    return {"alias": alias, "task": task, "returncode": r.returncode,
            "audit": a, "confidence": a["confidence"],
            "wall_seconds": round(wall, 1), "scores": scores,
            "telemetry_before": before, "telemetry_after": telemetry(),
            "stderr_tail": r.stderr[-600:] if r.returncode else ""}


def _stream_timed(alias: str, prompt: str, key: str, timeout: int) -> dict:
    """One streamed request. TTFT is the wall time to the FIRST content token.

    Streaming is the only way to separate prefill from decode here: LiteLLM does
    not expose Ollama's prompt_eval_duration, so without TTFT a decode rate would
    silently include the prefill.
    """
    payload = {"model": alias, "messages": [{"role": "user", "content": prompt}],
               "stream": True, "stream_options": {"include_usage": True}}
    req = urllib.request.Request(
        f"{LITELLM}/v1/chat/completions", data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {key}"}, method="POST")
    started = time.time()
    ttft, usage, chunks = None, {}, 0
    with urllib.request.urlopen(req, timeout=timeout) as r:
        for raw in r:
            line = raw.decode().strip()
            if not line.startswith("data:"):
                continue
            body = line[5:].strip()
            if body == "[DONE]":
                break
            try:
                d = json.loads(body)
            except json.JSONDecodeError:
                continue
            if d.get("usage"):
                usage = d["usage"]
            delta = ((d.get("choices") or [{}])[0].get("delta") or {})
            if ttft is None and (delta.get("content") or delta.get("reasoning_content")):
                ttft = time.time() - started
            chunks += 1
    wall = time.time() - started
    return {"ttft_s": round(ttft, 3) if ttft else None,
            "wall_seconds": round(wall, 3),
            "prompt_tokens": usage.get("prompt_tokens"),
            "completion_tokens": usage.get("completion_tokens"),
            "chunks": chunks}
#: A real prefill on this hardware runs 200-1,200 tok/s. Anything far above is a
#: cache hit, not a fast machine — measured, a cache-served prefill reported
#: 1,196,380 tok/s while looking entirely normal on token counts alone.
IMPLAUSIBLE_INPUT_RATE = 20_000


def _stats(values: list) -> dict:
    vals = sorted(v for v in values if v)
    if not vals:
        return {"n": 0}
    mid = len(vals) // 2
    median = vals[mid] if len(vals) % 2 else (vals[mid - 1] + vals[mid]) / 2
    mean = sum(vals) / len(vals)
    var = sum((v - mean) ** 2 for v in vals) / len(vals)
    return {"median": round(median, 1), "min": round(vals[0], 1),
            "max": round(vals[-1], 1), "n": len(vals),
            "cv": round((var ** 0.5) / mean, 3) if mean else None}


def cold_load_seconds(model: str, alias: str, key: str = None) -> float:
    """Time a genuine cold load: evict, then send a one-token request.

    Kept SEPARATE from the rate probes — folding a model load into a prefill rate
    is how a 30 GB model looks slow at inference when it was actually loading.
    """
    key = key or api_key()
    unload(model)
    time.sleep(3)
    r = _stream_timed(alias, "Reply with one word: ok", key, 900)
    return r["wall_seconds"]


def throughput_probe(alias: str, model: str, reps: int = 3,
                     timeout: int = 900) -> dict:
    """Operational rates, measured through the SAME authenticated path.

    These are END-TO-END: they include HTTP, queueing and the proxy. Ollama's
    internal counters are NOT exposed through LiteLLM, so:

        native_prompt_eval_tps  = unavailable
        native_generation_tps   = unavailable

    Never call these native throughput. They never touch a quality score.
    """
    key = api_key()
    out = {"alias": alias, "model": model,
           "native_prompt_eval_tps": "unavailable",
           "native_generation_tps": "unavailable"}

    out["cold_load_seconds"] = cold_load_seconds(model, alias, key)
    # Everything below runs RESIDENT, so load time cannot pollute a rate.

    prefill, decode, invalid = [], [], []
    for rep in range(reps):
        # A unique nonce AND unique body per repetition: measured, a changed
        # leading nonce alone does NOT bust llama.cpp's cache — only different
        # content forces a real prefill.
        seed = 7 + rep * 13
        body = "\n".join(f"row {i}: value {(i * seed) % 991}" for i in range(4000))
        p = (f"# sample {rep}-{seed}\nRead this table.\n{body}\n"
             f"Reply with exactly one word: ok")
        r = _stream_timed(alias, p, key, timeout)
        rate = (r["prompt_tokens"] / r["wall_seconds"]
                if r["prompt_tokens"] and r["wall_seconds"] else None)
        r["end_to_end_input_rate"] = round(rate, 1) if rate else None
        if rate and rate > IMPLAUSIBLE_INPUT_RATE:
            r["validity"] = "INVALID_CACHE_CONTAMINATION"
            invalid.append(r)
        else:
            r["validity"] = "VALID"
            prefill.append(r)

        # Same task contract for every model, and a NATURAL one — not repeated
        # filler, which some models refuse or truncate.
        d = _stream_timed(
            alias,
            f"Write a clear explanation of how a hash table works, including "
            f"collision handling and resizing. Aim for about 600 words. "
            f"(variant {rep})", key, timeout)
        gen = (d["wall_seconds"] - d["ttft_s"]) if d.get("ttft_s") else None
        if gen and gen > 0 and d.get("completion_tokens"):
            d["end_to_end_output_rate"] = round(d["completion_tokens"] / gen, 1)
        elif d.get("completion_tokens") and d["wall_seconds"]:
            d["conservative_end_to_end_output_rate"] = round(
                d["completion_tokens"] / d["wall_seconds"], 1)
        decode.append(d)

    out["prefill_samples"] = prefill + invalid
    out["decode_samples"] = decode
    out["end_to_end_input_rate"] = _stats(
        [s["end_to_end_input_rate"] for s in prefill])
    out["end_to_end_output_rate"] = _stats(
        [s.get("end_to_end_output_rate") for s in decode])
    out["conservative_end_to_end_output_rate"] = _stats(
        [s.get("conservative_end_to_end_output_rate") for s in decode])
    out["ttft_s"] = _stats([s.get("ttft_s") for s in decode])
    out["invalid_cache_contaminated"] = len(invalid)
    return out


def audit(out_dir: Path, n: int = 4) -> dict:
    """Prove extraction did not destroy the answer, before ranking anyone.

    Three defects in a row were interface faults, not model faults, and each
    looked exactly like a model failing. So no suite is trusted until a few
    samples are checked: raw response vs what was actually executed.

    Returns a CONFIDENCE grade. Only HIGH may influence a recommendation.
    """
    samples = sorted(out_dir.rglob("samples_*.jsonl"))
    if not samples:
        return {"confidence": "LOW", "reason": "no per-sample records to audit",
                "checked": 0}
    rows = [json.loads(l) for l in samples[0].read_text().splitlines() if l.strip()][:n]
    checked, empty, shrunk = [], 0, 0
    for r in rows:
        raw = "".join(x for x in _flatten(r.get("resps")))
        ext = "".join(x for x in _flatten(r.get("filtered_resps")))
        # Extraction legitimately drops prose, but code that vanishes or
        # collapses is the signature of an interface fault.
        has_code = "def " in ext or "return" in ext
        if not ext.strip() or not has_code:
            empty += 1
        elif raw and len(ext) < len(raw) * 0.30:
            shrunk += 1
        checked.append({"raw_chars": len(raw), "extracted_chars": len(ext),
                        "has_code": has_code,
                        "extract_tail": ext[-90:]})
    bad = empty + shrunk
    if empty:
        grade, why = "INVALID_TASK_INTERFACE", (
            f"{empty}/{len(rows)} samples extracted to no executable code — "
            f"the harness discarded the answer")
    elif shrunk:
        grade, why = "LOW", (f"{shrunk}/{len(rows)} samples lost >70% of the "
                             f"response during extraction")
    else:
        grade, why = "HIGH", (f"{len(rows)} samples audited: extraction "
                              f"preserved executable code")
    return {"confidence": grade, "reason": why, "checked": len(rows),
            "samples": checked, "suspect": bad}


def _flatten(v):
    if isinstance(v, str):
        yield v
    elif isinstance(v, (list, tuple)):
        for x in v:
            yield from _flatten(x)
