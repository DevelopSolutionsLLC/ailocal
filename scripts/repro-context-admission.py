#!/usr/bin/env python3
"""repro-context-admission.py — does the benchmark admit prompts the model
cannot physically hold, and if so what is lost?

THE QUESTION. The planner alias geometry is:

    num_ctx                     40,960   physical window (input + output)
    num_predict                  8,192   reserved for output
    usable input                32,768   what a prompt may actually occupy
    admission threshold         45,875   what the pre-call guard lets through
    candidate-A observed input  39,157 -> 71,164

So the guard admits up to 45,875 tokens into a window that holds 32,768, and
candidate-A ran well past both. Ollama is reported to trim silently rather than
error. This measures whether that is true HERE, and which content disappears.

METHOD. Four bands around the two thresholds, with the EXACT planner geometry
so the numbers transfer directly. Prompts are sized by the PROVIDER'S OWN
tokenizer, not by character estimates: one filler unit is measured once via
prompt_eval_count, then repeated to hit each target and the achieved size is
re-measured rather than assumed.

Five sentinels are placed at fixed positions. Truncation direction is read from
WHICH sentinels come back, never inferred:

    front-trimming  -> SYSTEM and EARLY vanish, FINAL survives
    tail-trimming   -> FINAL vanishes, SYSTEM survives
    no truncation   -> all five survive and prompt_eval_count matches intent

The final instruction is deliberately placed LAST, because that is the position
that survives front-trimming -- if the model cannot follow it, the loss is not
merely of context but of the task itself.

Counts are kept strictly separate and never conflated:
    intended_tokens          what we built, measured by the provider tokenizer
    litellm_estimated_tokens LiteLLM's chars/4 guess (NOT a provider count)
    provider_eval_tokens     Ollama prompt_eval_count -- the only ground truth
"""
from __future__ import annotations

import json
import sys
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts" / "lib"))
import benchmark as B  # noqa: E402

MODEL = "qwen3.5:2b"          # cheap; the geometry is what is under test
MODE = "off"
CONTEXT = 32768               # -> num_ctx 40,960, num_predict 8,192
CEILING = 8192
OLLAMA = "http://127.0.0.1:11434"

SENTINELS = {
    "SYSTEM": "ZQX-SYSTEM-71431",
    "EARLY": "ZQX-EARLY-58207",
    "MIDDLE": "ZQX-MIDDLE-33914",
    "LATE": "ZQX-LATE-88602",
    "FINAL": "ZQX-FINAL-24775",
}

# Filler that cannot be confused with a sentinel and does not invite the model
# to summarise it. Neutral, uniform, and cheap to tokenize.
FILLER_UNIT = ("The quarterly maintenance log records routine equipment checks "
               "and nominal readings for each subsystem without incident. ")

# Re-centred on the threshold that measurement showed actually binds. Run 1
# established that 37,272 tokens -- above "usable input" 32,768 -- was evaluated
# IN FULL, so num_ctx - num_predict is not a hard input limit. The thresholds
# worth straddling are num_ctx (40,960) and the admission ceiling (45,875).
BANDS = [
    {"band": "A", "target": 30000, "label": "below usable input (32,768)"},
    {"band": "B", "target": 38000, "label": "above usable, below num_ctx"},
    {"band": "C", "target": 44000, "label": "above num_ctx, below admission"},
    {"band": "D", "target": 52000, "label": "above num_ctx AND above admission"},
]


def ollama_tokens(text: str, model: str) -> int:
    """Provider-measured prompt size. Uses prompt_eval_count with num_predict=0,
    so nothing is generated and the number is the tokenizer's, not ours."""
    body = json.dumps({"model": model, "prompt": text, "stream": False,
                       "options": {"num_predict": 0, "num_ctx": 65536}}).encode()
    req = urllib.request.Request(f"{OLLAMA}/api/generate", data=body,
                                 headers={"content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=600) as r:
        return json.loads(r.read()).get("prompt_eval_count") or 0


def build_prompt(target_tokens: int, per_unit: float) -> str:
    """Sentinels at fixed positions, filler in between, sized by measured cost."""
    need = max(0, target_tokens - 200)          # leave room for the sentinels
    units = max(1, int(need / max(per_unit, 0.01)))
    # THREE filler segments are emitted, so the per-segment count must divide by
    # three. Dividing by four made every band land at ~75% of target and the
    # decisive threshold (num_ctx) was never crossed on the first run.
    q = max(1, units // 3)
    seg = lambda n: FILLER_UNIT * n  # noqa: E731
    return (
        f"[{SENTINELS['SYSTEM']}] You are reviewing a maintenance archive.\n"
        f"[{SENTINELS['EARLY']}] Beginning of archive.\n"
        f"{seg(q)}"
        f"[{SENTINELS['MIDDLE']}] Midpoint of archive.\n"
        f"{seg(q)}"
        f"[{SENTINELS['LATE']}] Near end of archive.\n"
        f"{seg(q)}"
        f"[{SENTINELS['FINAL']}] End of archive.\n\n"
        "FINAL INSTRUCTION: List every marker code beginning with ZQX- that "
        "appears anywhere above, one per line, exactly as written. Then write "
        "the single word DONE. Output nothing else."
    )


def main() -> int:
    run_id = time.strftime("ctxadmit-%Y%m%dT%H%M%SZ", time.gmtime())
    bundle = Path.home() / ".local/state/ailocal/benchmark/evidence" / run_id
    bundle.mkdir(parents=True, exist_ok=True)
    print(f"evidence -> {bundle}")

    entry = B.build_alias(MODEL, MODE, CONTEXT, CEILING, {})
    alias = entry["model_name"]
    geom = {"num_ctx": entry["litellm_params"]["num_ctx"],
            "num_predict": entry["litellm_params"]["num_predict"],
            "usable_input": entry["litellm_params"]["num_ctx"] - CEILING,
            "admission_limit": entry["model_info"]["max_input_tokens"]}
    print(f"alias={alias}  geometry={geom}")

    print("calibrating filler cost with the provider tokenizer...")
    base = ollama_tokens(FILLER_UNIT * 100, MODEL) / 100.0
    print(f"  {base:.3f} tokens per filler unit")

    applied = B.apply_aliases([entry])
    if not applied["ok"]:
        B.restore()
        print(f"alias install FAILED: {applied}")
        return 1

    key = B.api_key()
    results = []
    try:
        for b in BANDS:
            prompt = build_prompt(b["target"], base)
            intended = ollama_tokens(prompt, MODEL)
            print(f"\n=== band {b['band']} ({b['label']}) target={b['target']} "
                  f"intended={intended} ===")
            since = time.time()
            t0 = time.time()
            try:
                out = B._json(f"{B.LITELLM}/v1/chat/completions",
                              {"model": alias, "max_tokens": 400,
                               "messages": [{"role": "user", "content": prompt}]},
                              key=key, timeout=1800)
                err = None
            except Exception as exc:  # noqa: BLE001
                out, err = {}, f"{type(exc).__name__}: {exc}"
            wall = round(time.time() - t0, 1)

            usage = (out or {}).get("usage") or {}
            provider_eval = usage.get("prompt_tokens")
            text = ""
            try:
                text = out["choices"][0]["message"]["content"] or ""
            except Exception:  # noqa: BLE001
                pass
            returned = sorted(k for k, v in SENTINELS.items() if v in text)
            missing = sorted(set(SENTINELS) - set(returned))
            lost = (intended - provider_eval) if isinstance(provider_eval, int) else None

            rec = {**b, "alias": alias, "geometry": geom,
                   "intended_tokens": intended,
                   "provider_eval_tokens": provider_eval,
                   "tokens_lost": lost,
                   "completion_tokens": usage.get("completion_tokens"),
                   "finish_reason": ((out.get("choices") or [{}])[0]
                                     .get("finish_reason")),
                   "sentinels_returned": returned, "sentinels_missing": missing,
                   "final_instruction_followed": "DONE" in text.upper(),
                   "error": err, "wall_seconds": wall,
                   "response_head": text[:400],
                   "telemetry": B.telemetry(), "since": since}
            results.append(rec)
            print(f"  intended={intended} provider_eval={provider_eval} "
                  f"lost={lost} finish={rec['finish_reason']} err={err}")
            print(f"  sentinels returned={returned}")
            print(f"  missing={missing} DONE={rec['final_instruction_followed']}")
    finally:
        rest = B.restore()
        print(f"\nrestored={rest['restored']} leaked={rest['leaked']} "
              f"production={len(rest['production'])}")
        (bundle / "results.json").write_text(json.dumps(
            {"run_id": run_id, "model": MODEL, "geometry": geom,
             "tokens_per_filler_unit": base, "sentinels": SENTINELS,
             "restore": rest, "bands": results}, indent=1, default=str))
        print(f"evidence written: {bundle / 'results.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
