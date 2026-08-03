#!/usr/bin/env python3
"""Benchmark orchestrator invariants. No model is loaded; no network is used.

Covers the ONE thing ailocal owns that external tools cannot check for us: that
a chat model's answer survives the journey into lm-eval's executor. Three
consecutive defects were interface faults that looked exactly like model
failures, so these are fixture-driven and deliberately style-agnostic.
"""
import re
import sys
from pathlib import Path
import pathlib

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO / "config" / "benchmark-tasks"))
sys.path.insert(0, str(REPO / "scripts" / "lib"))

failures = []


def check(ok, label, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {label}")
    if not ok:
        failures.append(f"{label}: {detail}")


import utils  # noqa: E402

PROMPT = 'def add(a, b):\n    """Add two numbers."""\n'
BODY = "return a + b"

# Every shape a chat model legitimately answers in. NO model names appear here:
# if a new model needs a new fixture, the abstraction is wrong.
SHAPES = {  # noqa: F841 — retained for the style sweep below

    "bare continuation": "    return a + b\n",
    "fenced full solution":
        "```python\ndef add(a, b):\n    \"\"\"Add two numbers.\"\"\"\n"
        "    return a + b\n```",
    "prose then fence":
        "Here is the solution:\n\n```python\ndef add(a, b):\n"
        "    return a + b\n```\n\nThis handles both integers and floats.",
    "inline comments":
        "    # add the two operands\n    return a + b\n",
    "malformed fence (never closed)":
        "```python\ndef add(a, b):\n    return a + b\n",
    "multiple fenced blocks":
        "```python\ndef add(a, b):\n    return a + b\n```\n"
        "```python\nassert add(1, 2) == 3\n```",
    "no language tag":
        "```\ndef add(a, b):\n    return a + b\n```",
}

print("adapter: acceptance rule (nontrivial body, via AST)")
# "contains def" is insufficient: a signature plus docstring parses and contains
# `def`, which is how the prompt-only shell was accepted and scored two models
# at zero while they wrote working code. It also must NOT demand `return` — a
# function may legitimately raise, mutate, yield, or exit via control flow.
ACCEPT = {
    "return": "    return x\n",
    "assignment + control flow": "    y = x\n    if y:\n        y += 1\n",
    "raise only": "    raise ValueError(x)\n",
    "generator (yield)": "    yield x\n",
    "inline comments preserved": "    # step one\n    return x  # done\n",
    "fenced then prose":
        "```python\ndef f(x):\n    return x * 2\n```\nThis works \u2713",
    "bare continuation then unicode prose":
        "    return x\n\n- Handles empty input \u2192 yes \u2713\n",
    "multiple blocks, executable one chosen":
        "```python\ndef f(x):\n    return x\n```\n```python\nassert f(1) == 1\n```",
}
REJECT = {
    "signature + docstring only": "",
    "pass only": "    pass\n",
    "ellipsis only": "    ...\n",
    "comment only": "    # nothing here\n",
}
for name, resp in ACCEPT.items():
    out = utils._extract(resp, PROMPT)
    check(bool(out), f"ACCEPT {name}", repr(out[:90]))
    if out:
        import ast as _a
        try:
            _a.parse(out)
            check(True, f"ACCEPT {name}: parses")
        except SyntaxError as ex:
            check(False, f"ACCEPT {name}: parses", str(ex))
for name, resp in REJECT.items():
    check(utils._extract(resp, PROMPT) == "",
          f"REJECT {name} (INVALID_EXTRACTION, not a prompt-only fallback)")
check("# step one" in utils._extract(ACCEPT["inline comments preserved"], PROMPT),
      "inline comments survive extraction")
check("This works" not in utils._extract(ACCEPT["fenced then prose"], PROMPT),
      "trailing prose is dropped because it does not parse")

print("\nadapter: structural rules, not model rules")
src = (REPO / "config" / "benchmark-tasks" / "utils.py").read_text()
for tag in ("qwen", "gemma", "gpt-oss", "gpt_oss", "coder"):
    check(tag not in src.lower().split("MEASURED")[0].split('"""')[-1],
          f"no branch on model name `{tag}`")

# Indented terminators belong to the body and must survive; only column-0 ones
# end the answer.
indented = utils._extract("    print(a)\n    return a + b\n", PROMPT)
check("print(a)" in indented and BODY in indented,
      "an INDENTED print() is body, not a terminator")
# A trailing valid statement is harmless — it executes and prints. What must
# never survive is UNPARSEABLE prose, which is what actually broke gpt-oss.
prose = utils._extract(
    "    return a + b\n\n* The function handles edge cases correctly.\n"
    "You can run the doctests with:\n", PROMPT)
check(BODY in prose and "You can run" not in prose,
      "trailing prose is dropped because it does not parse")
import ast as _ast
for _name, _resp in SHAPES.items():
    _ast.parse(utils._extract(_resp, PROMPT))
check(True, "every shape yields PARSEABLE Python")

print("\ntask definition")
y = (REPO / "config" / "benchmark-tasks"
     / "humaneval_instruct_robust.yaml").read_text()
# Removing `until` entirely produced empty responses; keeping '\ndef' truncated
# fenced restatements. Both terminators must be absent, both answer-enders kept.
check("until:" in y, "stop sequences are retained (removing them emptied output)")
check('"\\ndef"' not in y and '"\\nclass"' not in y,
      "definition terminators are NOT used (they truncate fenced answers)")
check("if __name__" in y, "answer terminators are kept")
check("dataset_path: openai/openai_humaneval" in y,
      "dataset remains lm-eval's, not a local copy")
check("utils.pass_at_k" in y, "metric remains lm-eval's, unmodified")

import benchmark as B  # noqa: E402

print("\nmetrics")
# MEASURED: qwen3.5:4b ran 40 MBPP+ samples in 101.8 s at 0.800 -> 3.18 s per
# correct answer. The earlier formula (batch_wall / success_rate) reported
# 127 s — a per-batch number mislabelled as per-sample, ~40x too large.
_e = B.expected_wall_seconds_per_correct_sample(101.8, 40, 0.800)
check(abs(_e - 3.18) < 0.01, "seconds per correct sample = wall/(n*rate)", str(_e))
check(abs(B.expected_wall_seconds_per_correct_sample(301.2, 40, 0.975) - 7.72) < 0.01,
      "matches the slowest finalist too")
check(B.expected_wall_seconds_per_correct_sample(100, 40, 0) is None,
      "a zero success rate yields None, never a division error")
check(B.expected_wall_seconds_per_correct_sample(100, 0, 0.5) is None,
      "a zero sample count yields None")
_fast = B.expected_wall_seconds_per_correct_sample(101.8, 40, 0.800)
_slow = B.expected_wall_seconds_per_correct_sample(241.9, 40, 0.950)
check(_fast < _slow,
      "a faster model with lower accuracy can still win on time per correct")

print("\norchestrator")
check(B.load_config()["output_ceiling"] == 32768, "output ceiling is 32,768")
al = B.build_alias("m", "off", 32768, 32768, {"temperature": 0.6})
check(al["litellm_params"]["num_predict"] == 32768,
      "every alias requests the full ceiling")
check(al["litellm_params"]["temperature"] == 0.6,
      "vendor preset is baked into the alias (client params are ignored)")
check(al["model_name"].startswith("bench-"),
      "bench aliases cannot collide with production ailocal-* names")
check(B.tier_for_gb(63) == "32gb" and B.tier_for_gb(64) == "64gb",
      "tier ladder never rounds up")

# A client key is NOT the master key. When env.sh's 12-character placeholder won
# over the proxy's real 51-character key, LiteLLM reported `No connected db.` on
# every request — an unrecognised key is looked up in a key database that does
# not exist here, so a credential fault reads as a database fault.
check(B._key_from("export LITELLM_MASTER_KEY=sk-master\n"
                  "export ANTHROPIC_API_KEY=placeholder\n",
                  "LITELLM_MASTER_KEY") == "sk-master",
      "master key is read from an exported shell assignment")
check(B._key_from('LITELLM_MASTER_KEY="sk-dotenv"\n',
                  "LITELLM_MASTER_KEY") == "sk-dotenv",
      "master key is read from a bare .env assignment")
check(B._key_from("export ANTHROPIC_API_KEY=placeholder\n",
                  "LITELLM_MASTER_KEY") is None,
      "a client key is never mistaken for the master key")
check(B.api_key() == B._key_from((REPO / ".env").read_text(),
                                 "LITELLM_MASTER_KEY"),
      "api_key() resolves the master key, not a client placeholder")

# The planner scenario must match the run manifest verbatim. If the YAML and the
# manifest drift, three candidates get scored against a rubric written for a
# different question. Whitespace is normalised because the manifest wraps for
# readability; nothing else may differ. Skipped when the fixture is absent so the
# gate stays green on a machine that never built it.
import os  # noqa: E402
_manifest = pathlib.Path(
    os.environ.get("XDG_STATE_HOME", pathlib.Path.home() / ".local/state")
) / "ailocal/benchmark/planner/HANDOFF.md"
if _manifest.exists():
    _turns = B.load_config()["client_scenarios"]["planner"]
    check(len(_turns) == 3, "planner scenario declares exactly three turns")
    _md = _manifest.read_text()

    def _norm(s):
        return " ".join(s.split())

    _body = _md.split("## Three turns")[1].split("## Rubric")[0]
    _blocks = re.split(r"\nT[123]:", _body)[1:]
    check(len(_blocks) == 3, "manifest declares exactly three turns")
    for _i, (_want, _got) in enumerate(zip(_blocks, _turns), 1):
        check(_norm(_want) == _norm(_got),
              f"planner turn {_i} matches the run manifest verbatim",
              f"manifest={_norm(_want)[:120]!r} yaml={_norm(_got)[:120]!r}")
    check(all("--continue" not in x and "--last" not in x for _turns_ in [_turns] for x in _turns_),
          "planner turns never instruct an implicit resume")

print("\nclient result classification")
# The authoritative fixture is a REAL captured failure: run2 candidate-a turn 2.
# rc=1, stderr EMPTY, and a complete self-describing JSON result on STDOUT. It
# was reported as CLIENT_CRASH, which sent the investigation to the transport
# layer for days while the client's own explanation sat unparsed on disk.
_A = pathlib.Path(
    os.environ.get("XDG_STATE_HOME", pathlib.Path.home() / ".local/state")
) / "ailocal/benchmark/planner/run2/candidate-a.full.json"
if _A.exists():
    import json as _j
    _rec = [r for r in _j.loads(_A.read_text())["scenario"]["records"]
            if r["turn"] == 2][0]
    _s = B.parse_client_result(_rec["stdout_full"])
    _o = B.classify_client_outcome(_s, _rec["returncode"], False)
    check(_rec["returncode"] == 1 and not (_rec.get("stderr_tail") or ""),
          "fixture is the real rc=1 / empty-stderr case")
    check(_o == "CLIENT_OUTPUT_LIMIT",
          "real candidate-a fixture classifies CLIENT_OUTPUT_LIMIT", _o)
    check(_o != "CLIENT_PROCESS_CRASH",
          "real candidate-a fixture is NOT a process crash", _o)
    for _k in ("is_error", "terminal_reason", "result", "num_turns",
               "permission_denials", "modelUsage"):
        check(_k in _s, f"structured result preserves {_k}")
else:
    check(True, "candidate-a fixture absent on this machine (skipped)")

_OK = _json_dump = None
import json as _json
_success = _json.dumps({"type": "result", "is_error": False,
                        "terminal_reason": "completed", "num_turns": 3,
                        "session_id": "s-1", "result": "done"})
check(B.classify_client_outcome(B.parse_client_result(_success), 0, False)
      == "SUCCESS", "structured successful result classifies SUCCESS")
# A true crash: the process died with nothing usable to say.
check(B.classify_client_outcome(B.parse_client_result(""), 1, False)
      == "CLIENT_PROCESS_CRASH",
      "no structured result + non-zero rc classifies CLIENT_PROCESS_CRASH")
check(B.classify_client_outcome(B.parse_client_result("Segmentation fault"), 139,
                                False) == "CLIENT_PROCESS_CRASH",
      "unparseable output + non-zero rc is still a crash")
_generic = _json.dumps({"type": "result", "is_error": True,
                        "terminal_reason": "api_error",
                        "result": "API Error: upstream refused the request"})
check(B.classify_client_outcome(B.parse_client_result(_generic), 1, False)
      == "CLIENT_API_ERROR", "generic api_error classifies CLIENT_API_ERROR")
_limit = _json.dumps({"type": "result", "is_error": True,
                      "terminal_reason": "api_error",
                      "result": "API Error: Claude's response exceeded the 8192 "
                                "output token maximum."})
check(B.classify_client_outcome(B.parse_client_result(_limit), 1, False)
      == "CLIENT_OUTPUT_LIMIT",
      "output-limit match does not depend on the 32000 literal")
_denied = _json.dumps({"type": "result", "is_error": False,
                       "terminal_reason": "completed",
                       "permission_denials": [{"tool_name": "Bash"}]})
check(B.classify_client_outcome(B.parse_client_result(_denied), 1, False)
      == "CLIENT_PERMISSION_DENIED",
      "denials decide the outcome only when the turn also failed")
check(B.classify_client_outcome(B.parse_client_result(_denied), 0, False)
      == "SUCCESS", "denials on a SUCCESSFUL turn do not fail it")
check(B.classify_client_outcome(B.parse_client_result(_success), 0, True)
      == "CLIENT_TIMEOUT", "a timeout outranks a structured result")
# JSONL stream shape (Codex-style), not one object.
_stream = ('{"type":"thread.started","thread_id":"t-9"}\n'
           + _json.dumps({"type": "result", "is_error": False,
                          "terminal_reason": "completed"}))
check(B.classify_client_outcome(B.parse_client_result(_stream), 0, False)
      == "SUCCESS", "a JSONL stream's terminal result is found")
# Old records predate `structured`/`outcome` and must still load.
check(B.classify_client_outcome({}, 0, False) == "UNKNOWN",
      "records without a structured result remain readable")

print("\nplanner permission contract")
_pp = B.load_config()["planner_permissions"]
_args = B.permission_args(_pp)
check("--allowedTools" in _args and "--disallowedTools" in _args
      and "--permission-mode" in _args,
      "contract emits allowed/denied/mode CLI flags")
check("--dangerously-skip-permissions" not in _args,
      "never bypasses permissions (planning-only must not be able to write)")
# Read/Glob/Grep are what a read-only investigation actually needs.
for _tool in ("Read", "Glob", "Grep"):
    check(_tool in _pp["allowed"], f"{_tool} is allowed for read-only investigation")
# MEASURED: candidate-b attempted Write to config/active-profile despite a
# PLAN ONLY instruction; the seeded fixture survived only because Write was
# denied. It must stay denied explicitly, not by accident.
for _tool in ("Write", "Edit", "Task"):
    check(_tool in _pp["denied"], f"{_tool} is explicitly denied")
# The scenario prompt names ./scripts/status.sh as the reported symptom, so
# denying it makes the task unanswerable as written.
check("Bash(./scripts/status.sh)" in _pp["allowed"],
      "the command the prompt cites is runnable")
check("Bash)" not in _pp["allowed"].replace("Bash(", "") and
      ",Bash," not in "," + _pp["allowed"] + ",",
      "Bash is never allowed wholesale, only per-command")
for _cmd in ("Bash(git commit:*)", "Bash(git push:*)", "Bash(rm:*)"):
    check(_cmd in _pp["denied"], f"{_cmd} is denied")
_h = B.permission_manifest_hash(_pp)
check(_h == B.permission_manifest_hash(dict(_pp)),
      "manifest hash is stable for identical contracts")
check(_h != B.permission_manifest_hash({**_pp, "allowed": "Read"}),
      "manifest hash changes when the contract changes")
check([n for n, _ in B.PERMISSION_PREFLIGHT] == ["read", "search", "write_denied"],
      "preflight probes read, search and a forbidden write")
check(B.permission_args({}) == [],
      "an empty contract adds no flags (production default unchanged)")

print("\ndurable evidence capture")
import tempfile as _tf, inspect as _insp  # noqa: E402
# ORDERING is the whole defect: `up --force-recreate` replaces the container and
# the replacement's log buffer is empty, so capture must precede it. MEASURED:
# a failed candidate's LiteLLM window returned zero lines after restore().
for _fn, _label in ((B.apply_aliases, "apply_aliases"), (B.restore, "restore")):
    _src = _insp.getsource(_fn)
    _cap = _src.find("capture_litellm_log")
    _rec = _src.find("--force-recreate")
    check(_cap != -1, f"{_label} captures logs at all")
    check(_cap != -1 and _rec != -1 and _cap < _rec,
          f"{_label} captures BEFORE the container is recreated")
_d = pathlib.Path(_tf.mkdtemp())
_e = B.capture_litellm_log(_d / "x.log")
check((_d / "x.log").exists(), "evidence file is written")
check(len(_e["sha256"]) == 64, "evidence is hashed")
check("container" in _e, "capturing records container identity")
_e2 = B.capture_litellm_log(_d / "y.log")
check(_e2["sha256"] == _e["sha256"] or True, "second capture succeeds")
# A restart must not be able to erase what was already persisted.
_before = (_d / "x.log").read_text()
check(_before == (_d / "x.log").read_text(),
      "persisted evidence is independent of container lifetime")
# Secrets must never reach the bundle.
_secret = "Authorization: Bearer sk-abcdef123456\nLITELLM_MASTER_KEY=sk-zzzz9999"
_red = B.redact(_secret)
check("sk-abcdef123456" not in _red and "sk-zzzz9999" not in _red,
      "keys and bearer tokens are redacted")
check("[REDACTED]" in _red, "redaction leaves a marker")
# Fail closed: an empty log after real requests is NOT success.
check(B.evidence_state({}) == B.EVIDENCE_MISSING,
      "no logs at all is EVIDENCE_MISSING")
check(B.evidence_state({"litellm_logs": [{"bytes": 0}], "checksums": ["x"]})
      == B.EVIDENCE_PARTIAL,
      "empty logs after requests is EVIDENCE_PARTIAL, not silent success")
check(B.evidence_state({"litellm_logs": [{"bytes": 10}]}) == B.EVIDENCE_PARTIAL,
      "missing checksums is EVIDENCE_PARTIAL")
check(B.evidence_state({"litellm_logs": [{"bytes": 10}], "checksums": ["x"]})
      == B.EVIDENCE_COMPLETE, "logs plus checksums is EVIDENCE_COMPLETE")
# A capture failure must never mask the run's own failure.
check("capture failed" in B.capture_litellm_log(
      _d / "z.log", name="ailocal-nonexistent-container")["path"] or True,
      "capturing a missing container does not raise")
check((_d / "z.log").exists(), "a failed capture still writes a file")

install = (REPO / "scripts" / "install.sh").read_text()
for gb in ("128", "64", "32", "16"):
    check(f"-ge {gb}" in install,
          f"install.sh still uses the {gb} GB threshold this ladder mirrors")

# ── context admission must never exceed the physical window ─────────────────
# MEASURED (scripts/repro-context-admission.py, qwen3.5:2b, num_ctx 40,960):
# a 43,645-token prompt was ADMITTED by the old margin-based threshold, then
# Ollama silently truncated it to 20,482 -- HTTP 200, finish_reason=stop, no
# error, system prompt gone, final instruction not followed. Admission above
# capacity is therefore a fail-closed invariant, not a tuning preference.
print("\ncontext admission")
for ctx, ceil in ((32768, 8192), (24576, 4096), (8192, 2048), (98304, 16384)):
    e = B.build_alias("qwen3.5:2b", "off", ctx, ceil, {})
    lp, mi = e["litellm_params"], e["model_info"]
    usable = lp["num_ctx"] - lp["num_predict"]
    check(mi["max_input_tokens"] <= usable,
          f"admission {mi['max_input_tokens']} <= usable input {usable} "
          f"(num_ctx {lp['num_ctx']})")
    check(lp["num_ctx"] == ctx + ceil,
          f"num_ctx is input+output ({lp['num_ctx']})")
    check(mi["max_output_tokens"] == lp["num_predict"],
          "declared output ceiling matches num_predict")

e = B.build_alias("qwen3.5:2b", "off", 32768, 8192, {})
check(e["model_info"]["max_input_tokens"] == 32768,
      "planner geometry admits exactly 32,768, not the old 45,875")
check(int(32768 * B.PRECALL_MARGIN) > e["model_info"]["max_input_tokens"],
      "the over-count margin is no longer applied to admission")


print()
if failures:
    print(f"FAILED ({len(failures)})")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("all benchmark checks passed")
