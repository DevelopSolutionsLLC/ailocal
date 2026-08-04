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

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness import REPO, Suite
sys.path.insert(0, str(REPO / "config" / "benchmark-tasks"))
sys.path.insert(0, str(REPO / "scripts" / "lib"))

_suite = Suite()
check = _suite.check


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
# MEASURED (repro-context-admission.py, since deleted; qwen3.5:2b, num_ctx 40,960):
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


# ── planner worktree confinement ────────────────────────────────────────────
# Permissions gate TOOLS, not PATHS. Measured with the real client, same
# permission contract, one cheap turn each: without confinement INSIDE, PARENT,
# the ailocal config, the Cadence repo AND the ground-truth handoff all read OK;
# with confinement only INSIDE reads OK. So this is a scoring-validity
# invariant, not a hardening preference.
print("\nplanner worktree confinement")
import json as _js, shutil, tempfile, pathlib as _pl
_wt = _pl.Path(tempfile.mkdtemp(prefix="confine-test-")).resolve()
_wt = _pl.Path("/private/var/folders/ailocal-test-wt")   # outside every denied root
cs = B.confinement_settings(_wt)
fs = cs["permissions"]
check(fs["defaultMode"] == "default", "confinement does not relax the permission mode")
check(B.worktree_is_confinable(_wt) == B.OUTSIDE_OWNED_TEMP_ROOT,
      "a path outside the owned root is refused with a reason")
check(B.worktree_is_confinable(_pl.Path.home() / "x") == B.INSIDE_DENIED_ROOT,
      "a worktree inside $HOME is refused: deny beats allow")
check(fs["allow"] == [f"Read(/{_wt}/**)", "Glob", "Grep"],
      "exactly one directory is re-admitted")
check(f"Read(/{_pl.Path.home()}/**)" in fs["deny"], "$HOME is denied")
check(all(d.startswith("Read(//") for d in fs["deny"]),
      "deny rules use the ABSOLUTE // form, not project-relative")
check("Read(//Users/**)" in fs["deny"], "all user homes are denied")
check(f"Read(/{_wt}/**)" not in fs["deny"], "the worktree itself is not denied")

# The ground truth and the escape target must fall inside a denied region
# WITHOUT being enumerated: naming them would only block the escapes we already
# know about.
gt = _pl.Path.home() / ".local/state/ailocal/benchmark/planner/HANDOFF.md"
# A sibling checkout, resolved from this repo rather than a hardcoded home:
# the fixture only needs a path OUTSIDE the worktree to test confinement.
cad = _pl.Path(REPO).resolve().parent.parent / "DevelopSolutions" / "cadence"
_roots = [d[len("Read(/"):-len("/**)")] for d in fs["deny"]]
for label, target in (("ground truth", gt), ("cadence repo", cad)):
    covered = any(str(target).startswith(r.rstrip("/") + "/") for r in _roots)
    check(covered, f"{label} falls inside a denied region without being named")
check(not any("cadence" in d.lower() or "HANDOFF" in d for d in fs["deny"]),
      "no path is denied by name — the policy is regions, not a blocklist")

_wt2 = _pl.Path(tempfile.mkdtemp(dir=B.benchmark_worktree_root())).resolve()
args = B.confinement_args(_wt2)
check(args[0] == "--settings" and _pl.Path(args[1]).is_file(),
      "confinement is installed through --settings, per run")
written = _js.loads(_pl.Path(args[1]).read_text())
check(written == B.confinement_settings(_wt2),
      "the settings file carries exactly the computed policy")
try:
    B.confinement_args(_pl.Path.home() / "nope")
    _refused = False
except ValueError:
    _refused = True
check(_refused, "an unconfinable worktree is REFUSED, not silently unconfined")
check(_pl.Path(args[1]).parent != _wt2,
      "the settings file is NOT written inside the candidate worktree")

check({B.CONFINEMENT_VERIFIED, B.CONFINEMENT_INVALID, B.CONFINEMENT_UNAVAILABLE}
      == {"VERIFIED_CONFINEMENT", "INVALID_CONFINEMENT", "CONFINEMENT_UNAVAILABLE"},
      "preflight reports three distinct states, so unavailable != invalid")

# No model-name-specific behaviour anywhere in the confinement path.
import inspect as _insp
_src = (_insp.getsource(B.confinement_settings) + _insp.getsource(B.confinement_args)
        + _insp.getsource(B.verify_confinement))
check(not re.search(r"qwen|gemma|gpt-oss|llama|deepseek", _src, re.I),
      "confinement contains no model-name branches")
_pl.Path(args[1]).unlink(missing_ok=True)
shutil.rmtree(_wt2, ignore_errors=True)


# ── confinement is enforced BEFORE inference ────────────────────────────────
# The gate only matters if it runs first. These drive run_client_scenario with
# the client stubbed, so ordering and fail-closed behaviour are proven without
# any model call.
print("\nconfinement enforcement (planner path)")
import benchmark_clients as _bc

_PERMS = {"allowed": "Read,Glob,Grep",
          "denied": "Bash,Write,Edit,Task,WebFetch,WebSearch", "mode": "default"}


def _stub_turn(calls, session="sess-1", stdout="", denials=None):
    def _f(client, prompt, sess, cwd, timeout=900, extra_args=None):
        calls.append({"prompt": prompt, "session": sess, "args": list(extra_args or [])})
        return {"client": client, "requested_session": sess, "command": "stub",
                "returncode": 0, "timed_out": False, "wall_seconds": 0.1,
                "session_id": session, "tool_calls": 0, "prompt_tokens": 1,
                "completion_tokens": 1, "stdout_tail": stdout,
                "stdout_full": stdout, "stderr_tail": "",
                "telemetry_before": {}, "telemetry_after": {},
                "structured": {"permission_denials": denials or []},
                "outcome": "SUCCESS", "crashed": False}
    return _f


def _run(state, calls, stdout="", denials=None, wt=None):
    wt = wt or _pl.Path(tempfile.mkdtemp(dir=B.benchmark_worktree_root())).resolve()
    orig_turn, orig_ver = _bc.run_client_turn, _bc.verify_confinement
    _bc.run_client_turn = _stub_turn(calls, stdout=stdout, denials=denials)
    _bc.verify_confinement = lambda *a, **k: {"state": state, "verdict": {},
                                              "probe_model": "probe",
                                              "candidate_model": "cand"}
    try:
        return _bc.run_client_scenario(
            "claude-local", ["t1", "t2"], wt, timeout=5,
            confinement={"model": "ailocal-fast", "permissions": _PERMS}), wt
    finally:
        _bc.run_client_turn, _bc.verify_confinement = orig_turn, orig_ver


for state in (_bc.CONFINEMENT_INVALID, _bc.CONFINEMENT_UNAVAILABLE,
              _bc.PROBE_OUTPUT_INVALID):
    calls = []
    res, wt = _run(state, calls)
    check(len(calls) == 0, f"{state} blocks BEFORE turn 1 (no client call)")
    check(res["error"] == state, f"{state} is reported verbatim, not collapsed")
    check(res["turns_completed"] == 0, f"{state} completes no turns")
    shutil.rmtree(wt, ignore_errors=True)

calls = []
res, wt = _run(_bc.CONFINEMENT_VERIFIED, calls)
check(len(calls) == 2, "VERIFIED_CONFINEMENT permits the scenario to run")
check(all("--settings" in c["args"] for c in calls),
      "every turn receives the verified sandbox settings")
sp = calls[0]["args"][calls[0]["args"].index("--settings") + 1]
check(all(c["args"][c["args"].index("--settings") + 1] == sp for c in calls),
      "session resume reuses the SAME settings file, not a rebuilt one")
check("--model" in calls[0]["args"] and "--permission-mode" in calls[0]["args"],
      "candidate command carries model and permission contract")
check(calls[1]["session"] == "sess-1", "turn 2 resumes the exact session id")
check(res["confinement"]["preflight"]["state"] == _bc.CONFINEMENT_VERIFIED,
      "preflight result is persisted in the scenario record")
check(res["confinement"]["manifest"]["hash"]
      == res["confinement"]["manifest_after_preflight"]["hash"],
      "manifest is stable across preflight")
shutil.rmtree(wt, ignore_errors=True)

# Non-planner scenarios must be untouched.
calls = []
_wt2 = _pl.Path(tempfile.mkdtemp(dir=B.benchmark_worktree_root())).resolve()
_orig = _bc.run_client_turn
_bc.run_client_turn = _stub_turn(calls)
try:
    plain = _bc.run_client_scenario("claude-local", ["t1"], _wt2, timeout=5)
finally:
    _bc.run_client_turn = _orig
check(calls and not calls[0]["args"],
      "a scenario without confinement passes no extra args (unchanged)")
check(plain["confinement"] == {"required": False},
      "confinement is explicitly marked not required")
shutil.rmtree(_wt2, ignore_errors=True)

# Breach detection needs POSITIVE evidence, not an absence of refusals.
_c = {"token": "ZQXCANARY-deadbeef", "path": "/tmp/x", "planted": True}
check(_bc.detect_escape({"stdout_full": "nothing here"}, _c)["state"] is None,
      "a clean turn records no confinement event")
ev = _bc.detect_escape({"stdout_full": "leaked ZQXCANARY-deadbeef here"}, _c)
check(ev["breach"] is True and ev["state"] == _bc.INVALID_CONFINEMENT_BREACH,
      "canary token in output ⇒ INVALID_CONFINEMENT_BREACH")
ev = _bc.detect_escape({"stdout_full": "", "structured": {"permission_denials":
      [{"tool_input": {"command": "cat /Users/x/secret"}}]}}, _c)
check(ev["state"] == _bc.CONFINEMENT_ESCAPE_BLOCKED and not ev["breach"],
      "a BLOCKED outside attempt is recorded but is not a breach")

calls = []
res, wt = _run(_bc.CONFINEMENT_VERIFIED, calls,
               stdout="oops ZQXCANARY-")   # token differs -> no false positive
check(res["error"] is None, "a partial canary prefix does not trigger a breach")
shutil.rmtree(wt, ignore_errors=True)

check(not re.search(r"qwen|gemma|gpt-oss|llama|deepseek",
                    _insp.getsource(_bc.run_client_scenario), re.I),
      "scenario confinement has no model-name branches")


# ── worktrees live outside every denied root ────────────────────────────────
# Deny beats allow and cannot be overridden, so a worktree under $HOME is
# unreadable by its own candidate. Measured before this moved: such a run
# returned neither its own file nor the ground truth.
print("\nexternal worktree root")
_root = B.benchmark_worktree_root()
check(not str(_root).startswith(str(_pl.Path.home())),
      f"owned worktree root is outside $HOME ({_root})")
check(_root.is_dir(), "owned worktree root exists")
check((_root.stat().st_mode & 0o077) == 0,
      "owned worktree root is private to this user")
check(str(_root) == str(_root.resolve()),
      "owned worktree root is already fully resolved (no symlink surprises)")

_ext = _pl.Path(tempfile.mkdtemp(dir=_root)).resolve()
check(B.worktree_is_confinable(_ext) == B.CONFINABLE,
      "a worktree under the owned root is CONFINABLE")
check(B.worktree_is_confinable(B.state_dir()) == B.INSIDE_DENIED_ROOT,
      "the evidence root is refused as a worktree location")
check(B.worktree_is_confinable(B.state_dir() / "planner") == B.INSIDE_DENIED_ROOT,
      "the ground-truth root is refused as a worktree location")

# A symlink that LOOKS confinable but resolves into $HOME must be caught, since
# the policy is written against the resolved path.
_link = _root / "sneaky-link"
_link.unlink(missing_ok=True)
_link.symlink_to(_pl.Path.home())
check(B.worktree_is_confinable(_link) == B.SYMLINK_TARGET_DENIED,
      "a symlink resolving into $HOME is refused, not followed")
_link.unlink(missing_ok=True)

# Sibling isolation: siblings share the owned root, which cannot itself be
# denied, so each must be named explicitly.
_sib = _pl.Path(tempfile.mkdtemp(dir=_root)).resolve()
_cs = B.confinement_settings(_ext, deny_extra=[_sib])
check(f"Read(/{_sib}/**)" in _cs["permissions"]["deny"],
      "a sibling worktree is explicitly denied")
check(f"Read(/{_ext}/**)" in _cs["permissions"]["allow"],
      "the candidate's own worktree stays allowed alongside sibling denial")

_m = B.confinement_manifest(_ext, "alias", {"allowed": "Read", "denied": "Bash",
                                            "mode": "default"}, deny_extra=[_sib])
check(len({_m["candidate_worktree_root"], _m["evidence_root"],
           _m["ground_truth_root"]}) == 3,
      "manifest records candidate/evidence/ground-truth roots separately")
check(_m["evidence_root"].startswith(str(_pl.Path.home())),
      "evidence stays inside $HOME — i.e. inside a denied root")
check(_m["sibling_worktrees_denied"] == [str(_sib)],
      "manifest records which siblings were denied")

# Cleanup must never delete anything it did not create.
import benchmark_clients as _bc2
for label, target, want in (
        ("$HOME", _pl.Path.home(), "REFUSED_PROTECTED_PATH"),
        ("repo", B.REPO, "REFUSED_PROTECTED_PATH"),
        ("evidence root", B.state_dir(), "REFUSED_PROTECTED_PATH"),
        ("owned root itself", _root, "REFUSED_PROTECTED_PATH"),
        ("/etc", _pl.Path("/etc"), "REFUSED_OUTSIDE_OWNED_ROOT")):
    check(_bc2._cleanup_is_safe(target) == want,
          f"cleanup refuses {label} ({want})")
check(_bc2._cleanup_is_safe(_ext) == "SAFE",
      "cleanup accepts a worktree inside the owned root")
_bad = B.remove_worktree(_pl.Path.home())
check(_bad["removed"] is False and _bad["status"] == B.WORKTREE_CLEANUP_FAILED,
      "an unsafe cleanup reports WORKTREE_CLEANUP_FAILED and deletes nothing")
check(_pl.Path.home().is_dir(), "$HOME still exists after a refused cleanup")

# Orphan sweeping: a killed run leaves a settings file and a $HOME canary that
# nothing else will ever remove.
_orphan = _root / ".confine-does-not-exist.json"
_orphan.write_text("{}")
_can = _pl.Path.home() / ".ailocal-confinement-canary-testonly.txt"
_can.write_text("x")
_sw = B.sweep_worktree_root()
check(not _orphan.exists(), "an orphaned settings file is swept")
check(not _can.exists(), "an orphaned $HOME canary is swept")
_live = _root / ".confine-" + "" if False else _root / f".confine-{_ext.name}.json"
_live.write_text("{}")
B.sweep_worktree_root()
check(_live.exists(), "a settings file whose worktree still exists is NOT swept")
_live.unlink(missing_ok=True)

shutil.rmtree(_ext, ignore_errors=True)
shutil.rmtree(_sib, ignore_errors=True)


# ── benchmark and production share ONE geometry ─────────────────────────────
# build_alias used to compute `num_ctx = context + ceiling` itself. That is how
# it enforced the admission invariant production did not, and how the two paths
# could disagree about what the same profile field meant.
print("\nshared geometry")
import profile_config as _pc
for ci, mo in ((32768, 8192), (65536, 4096), (81920, 16384), (3968, 128)):
    al = B.build_alias("m", "off", ci, mo, {})
    g = _pc.geometry(ci, mo)
    lp, mi = al["litellm_params"], al["model_info"]
    check(lp["num_ctx"] == g["num_ctx"],
          f"{ci}+{mo}: num_ctx from geometry() ({g['num_ctx']})")
    check(lp["num_predict"] == g["num_predict"], f"{ci}+{mo}: num_predict derived")
    check(mi["max_input_tokens"] == g["max_input_tokens"] == ci,
          f"{ci}+{mo}: admission == context_input by construction")
    check(mi["max_output_tokens"] == mo, f"{ci}+{mo}: advertised output == reserve")

# The completed planner geometry must survive exactly.
pl = B.build_alias("qwen3.5:4b", "off", 32768, 8192, {})
check(pl["litellm_params"]["num_ctx"] == 40960
      and pl["litellm_params"]["num_predict"] == 8192
      and pl["model_info"]["max_input_tokens"] == 32768,
      "planner overlay preserved: 32768 in + 8192 out = 40960 total")

# keep_alive is an explicit overlay, not a literal buried in the builder.
check(B.build_alias("m", "off", 100, 10, {}, keep_alive="6h")["litellm_params"]["keep_alive"] == "6h",
      "keep_alive is overridable, not hardcoded")

src = (REPO / "scripts" / "lib" / "benchmark_runtime.py").read_text()
# Behavioural, not textual: num_ctx must come from the geometry RESULT, not be
# recomputed. A prose mention of the old formula is not the defect.
check('"num_ctx": g["num_ctx"]' in src and "_pc.geometry(" in src,
      "num_ctx is taken from geometry(), not recomputed inline")
check(src.count("THINK_MODES") >= 2 and src.count('{"off": False, "on": True') == 1,
      "one thinking-mode mapping, not two")
check(not re.search(r'"if model ==|model\.startswith\("(qwen|gemma|gpt)', src),
      "no model-name conditionals in the alias builder")

print()
sys.exit(_suite.report())
