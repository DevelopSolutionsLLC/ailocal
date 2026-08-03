#!/usr/bin/env python3
"""repro-output-limit.py — the A/B/C output-limit contract reproduction.

ONE QUESTION. Four links of the chain are already proven with live evidence:

    client asks max_tokens=32000
      -> static litellm_params.num_predict overrides the mapped client value
      -> Ollama stops exactly at num_predict
      -> LiteLLM reports finish_reason=length
      -> Anthropic wire reports stop_reason=max_tokens

The fifth is not: does Claude Code turn that NORMAL provider length termination
into a FATAL outer-turn CLIENT_OUTPUT_LIMIT? Everything here exists to answer
that and nothing else.

WHY THREE CASES. One case cannot separate "the backend stopped early" from
"the ceilings disagree". Varying them independently can:

    A  client 2000 / backend 2000   ceilings MATCHED
    B  client 4000 / backend 1000   backend LOWER   <- the decisive case
    C  client 1000 / backend 4000   client LOWER

If B fails and A succeeds, the fault is the MISMATCH. If A fails too, Claude
Code treats any length stop as fatal and the ceiling is not the fix.

DESIGN NOTES
  - All three aliases are installed in ONE apply_aliases call, so the run costs
    one restart rather than three, and every case shares an identical runtime.
  - CLAUDE_CODE_MAX_OUTPUT_TOKENS is set per case in os.environ. run_client_turn
    shells out without an explicit env, so the child inherits it. Verified that
    neither configure.zsh nor the client settings.json sets it, so nothing
    competes. It is never written to a file: process-scoped only.
  - restore() runs in `finally`. A reproduction that leaves temporary aliases in
    a user's runtime is worse than no reproduction.
  - Evidence is correlated to LiteLLM trace records by ALIAS, which is unique
    per case, not by timestamp.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts" / "lib"))
import benchmark as B  # noqa: E402

# qwen3.5:2b, not a coder model: Claude Code 2.1.220 sends a `thinking` param on
# every request, and a model that does not support thinking returns a hard 500
# ("does not support thinking") before a single token is generated -- measured.
# Production aliases drop it via additional_drop_params; a bench alias built by
# build_alias does not, so the model itself must accept it. mode "off" sends
# think=false, which qwen3.5 supports.
MODEL = "qwen3.5:2b"
MODE = "off"
CONTEXT = 8192

# One bounded task that reliably runs past 1,000 tokens without asking for
# filler. A coder model refuses "repeat X 500 times" often enough to poison a
# run; it never refuses to write a documented module with tests.
PROMPT = (
    "Write a complete Python module implementing a doubly linked list. "
    "Include: insert_front, insert_back, delete_value, search, reverse, "
    "to_list, and __len__. Give every method a full docstring with Args, "
    "Returns and Raises sections. Then write a comprehensive unittest.TestCase "
    "covering every method including edge cases (empty list, single element, "
    "duplicate values, deleting the head and the tail). Output only code."
)

# Deterministic, read-only, no subagents. The output-limit question must not be
# contaminated by tool behaviour or permission denials -- that is exactly what
# made candidate-a's 31 internal turns uninterpretable.
PERMS = {"allowed": "Read", "denied": "Bash,Write,Edit,Task,WebFetch,WebSearch",
         "mode": "default"}

CASES = [
    # D/E force the condition A-C never reached: this model answers the prompt
    # in ~1,500-1,700 tokens, so ceilings of 2000/1000/4000 were never hit and
    # every case ended finish=stop. A ceiling of 256 guarantees a LENGTH stop,
    # which is the only state that can exercise Claude Code's fatal handling.
    {"case": "D", "client_max": 4000, "num_predict": 256,
     "label": "backend far lower - forced length stop"},
    {"case": "E", "client_max": 256, "num_predict": 256,
     "label": "matched at a forced length stop"},
]

TRACES = Path.home() / ".local/state/ailocal/captures/traces"


def trace_records_for(alias: str, since: float) -> list:
    """Every trace record belonging to this case, joined by REQUEST ID.

    Correlating on `model` alone does not work: pre-call and stream_end records
    carry the ALIAS, but by the time async_log_success_event fires LiteLLM has
    resolved `model` to the BACKEND tag, so a completion record never matches
    the alias. Measured, and the reason the first run reported zero completion
    records for three cases that had produced them.

    So: find the request_ids that belong to this alias, then take every record
    carrying one of them. Each case has a unique alias, so a slow case cannot
    steal a fast one's records.
    """
    rows = []
    for day in sorted(TRACES.glob("*.jsonl")):
        for line in day.read_text(errors="replace").splitlines():
            try:
                r = json.loads(line)
            except Exception:  # noqa: BLE001
                continue
            if r.get("ts", 0) >= since:
                rows.append(r)
    ids = {r.get("request_id") for r in rows if r.get("model") == alias}
    ids.discard(None)
    return sorted((r for r in rows if r.get("request_id") in ids),
                  key=lambda r: r.get("ts", 0))


def main() -> int:
    run_id = time.strftime("outlimit-%Y%m%dT%H%M%SZ", time.gmtime())
    bundle = Path.home() / ".local/state/ailocal/benchmark/evidence" / run_id
    bundle.mkdir(parents=True, exist_ok=True)
    print(f"evidence -> {bundle}")

    # Disposable tiny repository. Claude Code needs a cwd; it must not be a real
    # one, so nothing it reads or writes can matter.
    work = bundle / "repo"
    work.mkdir()
    (work / "README.md").write_text("# disposable repro fixture\n")
    subprocess.run(["git", "init", "-q"], cwd=work, check=False)

    entries = [B.build_alias(MODEL, MODE, CONTEXT, c["num_predict"], {})
               for c in CASES]
    for c, e in zip(CASES, entries):
        c["alias"] = e["model_name"]
    # alias_name() encodes model+mode+context, NOT the ceiling, so all three
    # cases would collide on one name. Make them distinct.
    for c, e in zip(CASES, entries):
        e["model_name"] = f"{e['model_name']}-np{c['num_predict']}"
        c["alias"] = e["model_name"]

    applied = B.apply_aliases(entries)
    print(f"aliases installed: {applied['installed']}  ok={applied['ok']}")
    if not applied["ok"]:
        B.restore()
        print(f"FAILED to install aliases: missing={applied['missing']}")
        return 1

    results = []
    try:
        for c in CASES:
            print(f"\n=== case {c['case']} ({c['label']}): "
                  f"client_max={c['client_max']} num_predict={c['num_predict']} ===")
            os.environ["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] = str(c["client_max"])
            since = time.time()
            rec = B.run_client_turn(
                "claude-local", PROMPT, None, work, timeout=600,
                extra_args=B.permission_args(PERMS) + ["--model", c["alias"]])
            traces = trace_records_for(c["alias"], since)
            entry = {**c, "client": rec, "traces": traces,
                     "client_max_env": os.environ.get("CLAUDE_CODE_MAX_OUTPUT_TOKENS")}
            results.append(entry)

            s = rec.get("structured") or {}
            comp = [t for t in traces if t.get("phase") == "completion"]
            print(f"  rc={rec['returncode']} outcome={rec['outcome']} "
                  f"terminal_reason={s.get('terminal_reason')} "
                  f"is_error={s.get('is_error')}")
            for t in comp:
                print(f"  trace: req_out={t.get('requested_output_tokens')} "
                      f"eff_np={t.get('effective_num_predict')} "
                      f"comp_tok={t.get('completion_tokens')} "
                      f"finish={t.get('finish_reason')} "
                      f"stop={t.get('stop_reason')} "
                      f"evidence={t.get('completion_evidence')}")
    finally:
        os.environ.pop("CLAUDE_CODE_MAX_OUTPUT_TOKENS", None)
        rest = B.restore()
        print(f"\nrestored={rest['restored']} leaked={rest['leaked']} "
              f"production={len(rest['production'])}")
        (bundle / "results.json").write_text(
            json.dumps({"run_id": run_id, "model": MODEL, "prompt": PROMPT,
                        "permissions": PERMS, "applied": applied,
                        "restore": rest, "cases": results},
                       indent=1, default=str))
        print(f"evidence written: {bundle / 'results.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
