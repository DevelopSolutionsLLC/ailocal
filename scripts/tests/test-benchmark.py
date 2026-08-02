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

install = (REPO / "scripts" / "install.sh").read_text()
for gb in ("128", "64", "32", "16"):
    check(f"-ge {gb}" in install,
          f"install.sh still uses the {gb} GB threshold this ladder mirrors")

print()
if failures:
    print(f"FAILED ({len(failures)})")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("all benchmark checks passed")
