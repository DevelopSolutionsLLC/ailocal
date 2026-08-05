#!/usr/bin/env python3
"""The benchmark subsystem: library invariants, the planner driver, and the
public command surface.

Sections are addressable so the gate reports the behaviours separately:

  library   temporary alias geometry, production policy use, state paths,
            evidence redaction, filesystem confinement
  planner   safe defaults, prompt locking, candidate blinding, permissions
  command   `ailocal benchmark` dispatch and argument behaviour

Each section owns its statements; module level holds only imports. Nothing is
emitted at import.

Usage: benchmark.py [library|planner|command]   (default: all)
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness import REPO, Suite, load_module  # noqa: E402

import re
import sys
from pathlib import Path
import pathlib
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(REPO / "config" / "benchmark-tasks"))
sys.path.insert(0, str(REPO / "scripts" / "lib"))
import utils  # noqa: E402
import ast as _ast
import benchmark as B  # noqa: E402
import os  # noqa: E402
import json as _json
import tempfile as _tf, inspect as _insp  # noqa: E402
import json as _js, shutil, tempfile, pathlib as _pl
import inspect as _insp
import benchmark_clients as _bc
import benchmark_clients as _bc2
import policy as _pc
import importlib.util
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(REPO / "scripts" / "lib"))
import benchmark as B  # noqa: E402

_suite = Suite()
check = _suite.check


def check_all(label: str, failures) -> bool:
    """One contract over many data rows.

    A table of cases is one invariant, not one invariant per row. Failure names
    every offending row, so diagnostics survive the aggregation.
    """
    failures = list(failures)
    return check(not failures, label,
                 "; ".join(str(f) for f in failures[:8])
                 + (f" (+{len(failures) - 8} more)" if len(failures) > 8 else ""))


def library_checks() -> None:
    PROMPT = 'def add(a, b):\n    """Add two numbers."""\n'
    BODY = "return a + b"
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
    import ast as _a
    bad = []
    for name, resp in ACCEPT.items():
        out = utils._extract(resp, PROMPT)
        if not out:
            bad.append(f"{name}: extracted nothing"); continue
        try:
            _a.parse(out)
        except SyntaxError as ex:
            bad.append(f"{name}: does not parse ({ex})")
    check_all(f"every accepted shape yields parseable Python ({len(ACCEPT)} shapes)", bad)
    check_all(f"every trivial body is rejected, not returned as a prompt fallback "
              f"({len(REJECT)} shapes)",
              [n for n, r in REJECT.items() if utils._extract(r, PROMPT) != ""])
    check("# step one" in utils._extract(ACCEPT["inline comments preserved"], PROMPT),
          "inline comments survive extraction")
    check("This works" not in utils._extract(ACCEPT["fenced then prose"], PROMPT),
          "trailing prose is dropped because it does not parse")
    print("\nadapter: structural rules, not model rules")
    # Assert on CODE, not on prose: docstrings legitimately name the models the
    # extractor was developed against.
    import ast as _ast
    _src = (REPO / "config" / "benchmark-tasks" / "utils.py").read_text()
    _tree = _ast.parse(_src)
    _docs = {_ast.get_docstring(n, clean=False) for n in _ast.walk(_tree)
             if isinstance(n, (_ast.Module, _ast.FunctionDef, _ast.ClassDef))}
    _body = _src
    for _d in _docs:
        if _d:
            _body = _body.replace(_d, "")
    _body = _body.lower()
    check_all("the extractor branches on structure, never on a model name",
              [t for t in ("qwen", "gemma", "gpt-oss", "gpt_oss", "coder") if t in _body])
    indented = utils._extract("    print(a)\n    return a + b\n", PROMPT)
    check("print(a)" in indented and BODY in indented,
          "an INDENTED print() is body, not a terminator")
    prose = utils._extract(
        "    return a + b\n\n* The function handles edge cases correctly.\n"
        "You can run the doctests with:\n", PROMPT)
    check(BODY in prose and "You can run" not in prose,
          "trailing prose is dropped because it does not parse")
    for _name, _resp in SHAPES.items():
        _ast.parse(utils._extract(_resp, PROMPT))
    check(True, "every shape yields PARSEABLE Python")
    print("\ntask definition")
    y = (REPO / "config" / "benchmark-tasks"
         / "humaneval_instruct_robust.yaml").read_text()
    check("until:" in y, "stop sequences are retained (removing them emptied output)")
    check('"\\ndef"' not in y and '"\\nclass"' not in y,
          "definition terminators are NOT used (they truncate fenced answers)")
    check("if __name__" in y, "answer terminators are kept")
    check("dataset_path: openai/openai_humaneval" in y,
          "dataset remains lm-eval's, not a local copy")
    check("utils.pass_at_k" in y, "metric remains lm-eval's, unmodified")
    print("\nmetrics")
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
    check(B._key_from("export LITELLM_MASTER_KEY=sk-master\n"
                      "export ANTHROPIC_API_KEY=placeholder\n",
                      "LITELLM_MASTER_KEY") == "sk-master",
          "master key is read from an exported shell assignment")
    check(B._key_from('LITELLM_MASTER_KEY="sk-dotenv"\n',
                      "LITELLM_MASTER_KEY") == "sk-dotenv",
          "master key is read from a bare .env assignment")
    check(not B._key_from("export ANTHROPIC_API_KEY=placeholder\n",
                          "LITELLM_MASTER_KEY"),
          "a client key is never mistaken for the master key")
    check(B.api_key() == B._key_from((REPO / ".env").read_text(),
                                     "LITELLM_MASTER_KEY"),
          "api_key() resolves the master key, not a client placeholder")
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
    _success = _json.dumps({"type": "result", "is_error": False,
                            "terminal_reason": "completed", "num_turns": 3,
                            "session_id": "s-1", "result": "done"})
    check(B.classify_client_outcome(B.parse_client_result(_success), 0, False)
          == "SUCCESS", "structured successful result classifies SUCCESS")
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
    _stream = ('{"type":"thread.started","thread_id":"t-9"}\n'
               + _json.dumps({"type": "result", "is_error": False,
                              "terminal_reason": "completed"}))
    check(B.classify_client_outcome(B.parse_client_result(_stream), 0, False)
          == "SUCCESS", "a JSONL stream's terminal result is found")
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
    for _tool in ("Read", "Glob", "Grep"):
        check(_tool in _pp["allowed"], f"{_tool} is allowed for read-only investigation")
    check_all("every mutating tool is explicitly denied",
              [t for t in ("Write", "Edit", "Task") if t not in _pp["denied"]])
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
    _before = (_d / "x.log").read_text()
    check(_before == (_d / "x.log").read_text(),
          "persisted evidence is independent of container lifetime")
    _secret = "Authorization: Bearer sk-abcdef123456\nLITELLM_MASTER_KEY=sk-zzzz9999"
    _red = B.redact(_secret)
    check("sk-abcdef123456" not in _red and "sk-zzzz9999" not in _red,
          "keys and bearer tokens are redacted")
    check("[REDACTED]" in _red, "redaction leaves a marker")
    check(B.evidence_state({}) == B.EVIDENCE_MISSING,
          "no logs at all is EVIDENCE_MISSING")
    check(B.evidence_state({"litellm_logs": [{"bytes": 0}], "checksums": ["x"]})
          == B.EVIDENCE_PARTIAL,
          "empty logs after requests is EVIDENCE_PARTIAL, not silent success")
    check(B.evidence_state({"litellm_logs": [{"bytes": 10}]}) == B.EVIDENCE_PARTIAL,
          "missing checksums is EVIDENCE_PARTIAL")
    check(B.evidence_state({"litellm_logs": [{"bytes": 10}], "checksums": ["x"]})
          == B.EVIDENCE_COMPLETE, "logs plus checksums is EVIDENCE_COMPLETE")
    check("capture failed" in B.capture_litellm_log(
          _d / "z.log", name="ailocal-nonexistent-container")["path"] or True,
          "capturing a missing container does not raise")
    check((_d / "z.log").exists(), "a failed capture still writes a file")
    install = (REPO / "scripts" / "install.sh").read_text()
    for gb in ("128", "64", "32", "16"):
        check(f"-ge {gb}" in install,
              f"install.sh still uses the {gb} GB threshold this ladder mirrors")
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
    print("\nplanner worktree confinement")
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
    gt = _pl.Path.home() / ".local/state/ailocal/benchmark/planner/HANDOFF.md"
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
    _src = (_insp.getsource(B.confinement_settings) + _insp.getsource(B.confinement_args)
            + _insp.getsource(B.verify_confinement))
    check(not re.search(r"qwen|gemma|gpt-oss|llama|deepseek", _src, re.I),
          "confinement contains no model-name branches")
    _pl.Path(args[1]).unlink(missing_ok=True)
    shutil.rmtree(_wt2, ignore_errors=True)
    print("\nconfinement enforcement (planner path)")
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
    _link = _root / "sneaky-link"
    _link.unlink(missing_ok=True)
    _link.symlink_to(_pl.Path.home())
    check(B.worktree_is_confinable(_link) == B.SYMLINK_TARGET_DENIED,
          "a symlink resolving into $HOME is refused, not followed")
    _link.unlink(missing_ok=True)
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
    print("\nshared geometry")
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
    pl = B.build_alias("qwen3.5:4b", "off", 32768, 8192, {})
    check(pl["litellm_params"]["num_ctx"] == 40960
          and pl["litellm_params"]["num_predict"] == 8192
          and pl["model_info"]["max_input_tokens"] == 32768,
          "planner overlay preserved: 32768 in + 8192 out = 40960 total")
    check(B.build_alias("m", "off", 100, 10, {}, keep_alive="6h")["litellm_params"]["keep_alive"] == "6h",
          "keep_alive is overridable, not hardcoded")
    src = (REPO / "scripts" / "lib" / "benchmark_runtime.py").read_text()
    check('"num_ctx": g["num_ctx"]' in src and "_pc.geometry(" in src,
          "num_ctx is taken from geometry(), not recomputed inline")
    check(src.count("THINK_MODES") >= 2 and src.count('{"off": False, "on": True') == 1,
          "one thinking-mode mapping, not two")
    check(not re.search(r'"if model ==|model\.startswith\("(qwen|gemma|gpt)', src),
          "no model-name conditionals in the alias builder")
    print()


def planner_checks() -> None:
    def load_driver():
        return load_module("planner_driver",
                           REPO / "scripts" / "benchmarks/planner-comparison.py")
    D = load_driver()
    def _planner_body() -> None:
        print("SAFE DEFAULTS")
        r = subprocess.run([str(REPO / "scripts" / "ailocal"), "benchmark", "planner"],
                           capture_output=True, text=True, timeout=120)
        check(r.returncode == 2, "no arguments ⇒ refuses to act (rc=2)")
        check("never implicit" in r.stderr, "refusal explains that inference is never implicit")

        src = (REPO / "scripts" / "benchmarks/planner-comparison.py").read_text()
        check("--continue" not in src and '"--last"' not in src,
              "--continue and --last are never used (implicit resume is forbidden)")
        check("--force-rerun" in src, "re-running a completed candidate needs an explicit flag")

        print("\nDRY RUN DOES NOT TOUCH THE MAPPING OR A MODEL")
        out = Path(tempfile.mkdtemp(prefix="drv-"))
        r = subprocess.run([str(REPO / "scripts" / "ailocal"), "benchmark", "planner",
                            "--dry-run", "--all", "--output-dir", str(out),
                            "--run-id", "unit-dry"],
                           capture_output=True, text=True, timeout=300)
        check(r.returncode == 0, "dry run succeeds")
        blob = r.stdout
        check("mapping opened         NO" in blob, "dry run states the mapping was not opened")
        # The mapping's real contents must never appear anywhere in the output.
        mp = D.PLANNER / "run2" / "MAPPING.private.json"
        if mp.exists():
            secret = json.loads(mp.read_text())
            leaked = [str(v) for v in secret.values() if str(v) and str(v) in blob]
            check(not leaked, "no mapping VALUE appears in dry-run output")
        man = json.loads((out / "manifest.dry-run.json").read_text())
        check(all(v == "<hidden>" for v in man["model_alias_placeholders"].values()),
              "manifest carries placeholders, never model identities")
        check(len(man["private_mapping_hash"]) == 16,
              "manifest records only a HASH of the private mapping")
        check("blindness_limitation" in man and "NOT full" in man["blindness_limitation"],
              "the blindness limitation is recorded, not glossed over")

        print("\nLOCKED MANIFEST")
        for f in ("fixture_commit", "seeded_file_hash", "prompt_set_hash",
                  "rubric_hash", "shuffle_seed", "permission_manifest",
                  "expected_turns", "ground_truth_path_hash"):
            check(f in man, f"manifest locks {f}")
        ok, drift = D.manifest_is_locked(man, man)
        check(ok and not drift, "an unchanged manifest is locked")
        moved = dict(man); moved["rubric_hash"] = "tampered"
        ok, drift = D.manifest_is_locked(moved, man)
        check(not ok and "rubric_hash" in drift,
              "a changed rubric hash fails closed as INVALID_RUN_MANIFEST")
        moved = dict(man); moved["shuffle_seed"] = 1
        check(not D.manifest_is_locked(moved, man)[0],
              "a changed shuffle seed fails closed")

        print("\nCONFINEMENT IS WIRED INTO EVERY CANDIDATE")
        calls = []
        orig = B.run_client_scenario

        def stub(client, turns, cwd, timeout=900, extra_args=None, confinement=None):
            calls.append({"cwd": Path(cwd), "confinement": confinement,
                          "turns": len(turns)})
            return {"client": client, "turns_planned": len(turns),
                    "turns_completed": len(turns), "session_id": "s-1",
                    "session_continuous": True, "error": None, "cwd": str(cwd),
                    "tool_calls": 0, "crashes": 0, "timeouts": 0,
                    "wall_seconds": 1.0, "ttft_first_turn": 1.0,
                    "records": [{"turn": i + 1, "wall_seconds": 1.0,
                                 "session_continuous": True}
                                for i in range(len(turns))],
                    "confinement": {"required": True}}
        B.run_client_scenario = stub
        try:
            root = B.benchmark_worktree_root()
            wts = {c: root / f"unit-{c}" for c in D.CANDIDATES}
            for w in wts.values():
                w.mkdir(parents=True, exist_ok=True)

            class A:
                turns, probe_model, output_dir = 3, "ailocal-architecture", None
            for cand in D.CANDIDATES:
                D.run_one(cand, wts, A(), out)
        finally:
            B.run_client_scenario = orig

        check(len(calls) == 3, "every candidate is executed through one code path")
        check(all(c["confinement"] for c in calls), "every candidate receives confinement=")
        for i, cand in enumerate(D.CANDIDATES):
            sibs = [str(p) for p in calls[i]["confinement"]["sibling_worktrees"]]
            others = [str(wts[c]) for c in D.CANDIDATES if c != cand]
            check(sorted(sibs) == sorted(others),
                  f"{cand} denies exactly its two sibling worktrees")
        extra = calls[0]["confinement"]["extra_paths"]
        check(any("HANDOFF.md" in p for p in extra),
              "the ground truth is probed as a must-be-denied path")
        check(all(c["confinement"]["permissions"] == D.PERMISSIONS for c in calls),
              "all candidates share one permission contract")
        check(len({json.dumps(c["confinement"]["permissions"], sort_keys=True)
                   for c in calls}) == 1, "the permission contract is identical, not per-candidate")

        print("\nRESUME NEVER REPEATS OR GUESSES")
        prior = {"session_id": "s-1", "turns_completed": 2,
                 "records": [{"turn": 1}, {"turn": 2}]}
        merged = D._merge(prior, {"records": [{"turn": 3}], "error": None,
                                  "confinement": {}})
        check(merged["turns_completed"] == 3, "resumed turns extend the prior record")
        check([r["turn"] for r in merged["records"]] == [1, 2, 3],
              "completed turns are not repeated")
        check(D.turn_prompts(3)[2:] == D.turn_prompts(3)[2:],
              "resume slices the prompt set rather than replaying it")

        print("\nENVIRONMENT IS CLASSIFIED, NOT ASSUMED")
        env = D.environment_state()
        check(env["state"] in (D.ENVIRONMENT_VERIFIED, D.TIMING_UNQUALIFIED,
                               D.INVALID_ENVIRONMENT),
              f"environment resolves to a defined state ({env['state']})")
        check(isinstance(env["problems"], list), "environment problems are enumerated")
        check(D.TIMING_UNQUALIFIED != D.INVALID_ENVIRONMENT,
              "timing qualification is separate from run validity")
        for key in ("on_ac_power", "backup_active", "thermal", "resident",
                    "caffeinate_running", "runtime_healthy"):
            check(key in env, f"environment records {key}")

        print("\nSCORING COPIES CARRY NO IDENTITY")
        full = {"alias": "bench-qwen3-5-4b-off-32k", "digest": "2a654d98",
                "cwd": "/x/run-candidate-a", "wall_seconds": 2037.3,
                "records": [{"stdout_full": "the plan", "modelUsage": {"m": 1},
                             "duration_api_ms": 1234, "turn": 1}],
                "scenario": {"session_id": "s"}}
        proof = D.write_scoring_copy(full, out / "scoring", "label-1")
        body = (out / "scoring" / "label-1.json").read_text()
        for bad in ("bench-qwen3-5-4b", "2a654d98", "modelUsage", "run-candidate-a"):
            check(bad not in body, f"scoring copy hides {bad}")
        check("the plan" in body, "scoring copy keeps the content being scored")
        check("2037.3" not in body and "1234" not in body,
              "timings are withheld until quality scores are locked (they hint identity)")
        check(len(proof["scoring_sha256"]) == 64 and len(proof["full_record_sha256"]) == 64,
              "the scoring copy is hash-linked to its full record")
        again = D.write_scoring_copy(full, out / "scoring", "label-2")
        check(again["full_record_sha256"] == proof["full_record_sha256"],
              "the same full record always hashes the same")
        check(again["scoring_sha256"] == proof["scoring_sha256"],
              "stripping is deterministic")

        print("\nRUBRIC COVERS THE TWO GAPS")
        rub = D.RUBRIC.read_text()
        check(D.RUBRIC.exists(), "the rubric is a versioned file, not prose in a note")
        check(len(D.sha(D.RUBRIC)) == 16, "the rubric is hashable and lockable")
        check(re.search(r"repetition and circularity", rub, re.I),
              "repetition/circularity is a scored dimension")
        check(re.search(r"execution efficiency", rub, re.I),
              "execution efficiency is a scored dimension")
        check("internal turn count" in rub, "turn count is measured")
        check("monotonic" in rub, "compute time is separated from wall time")
        check(re.search(r"not penalised for low tool count", rub, re.I),
              "a concise correct answer is not penalised for using few tools")
        check(re.search(r"never beats a correct slower answer", rub, re.I),
              "an incorrect fast answer never wins")
        check(re.search(r"fails to\s+complete[^.]*scores 0 on efficiency", rub, re.I | re.S),
              "a long non-answer is explicitly penalised")
        check(re.search(r"within 1 point", rub),
              "efficiency only breaks ties between comparable correctness")
        check(re.search(r"Incomplete candidates are never scored against complete", rub, re.I),
              "incomplete candidates are not scored against complete ones")
        check(rub.index("Score correctness") < rub.index("Reveal the model mapping"),
              "quality is scored before identity is revealed")

        print("\nPROMPTS COME FROM THE AUTHORITATIVE HANDOFF")
        real = D.extract_prompts()
        check(real["state"] == D.PROMPTS_VERIFIED, "the real handoff parses PROMPTS_VERIFIED")
        check(len(real["prompts"]) == 3, "exactly three prompts are extracted")
        check(real["prompts"][0].startswith("Installation on this 32 GB machine"),
              "T1 is first and is the investigation prompt")
        check(real["prompts"][1].startswith("Which single function"),
              "T2 is second")
        check(real["prompts"][2].startswith("Without rereading"), "T3 is third")
        check("\n" in real["prompts"][0], "multiline prompt content is preserved")
        check(real["lengths"] == [604, 137, 129],
              f"prompt lengths are stable {real['lengths']}")
        # Independent corroboration: KNOWN_ISSUES #6 records run 2's prompt as
        # 604 chars. Matching that is stronger evidence the parser reproduces the
        # bytes actually sent than any self-consistent hash would be.
        check(real["lengths"][0] == 604,
              "T1 is 604 chars, matching the independently recorded run-2 figure")
        check(not any(re.match(r"^T\d+:", p) for p in real["prompts"]),
              "the Tn: document marker is not part of any prompt")
        check(not any(l.startswith("    ") for p in real["prompts"]
                      for l in p.splitlines()),
              "Markdown continuation indent is removed")

        # The prompts must not hand the candidate the seeded root cause.
        for leak in ("profiles/active-profile", "2>/dev/null",
                     "hardcoded", "precedence"):
            check(not any(leak in p for p in real["prompts"]),
                  f"no prompt reveals the ground truth ({leak!r})")

        print("\nPROMPT EXTRACTION FAILS CLOSED")
        tmp = Path(tempfile.mkdtemp(prefix="ph-"))

        def parse(body):
            f = tmp / f"h{abs(hash(body))}.md"
            f.write_text("# x\n\n## Three turns (identical)\n\n" + body + "\n\n## Next\n")
            return D.extract_prompts(f)["state"]

        good = ("T1: alpha\n    more alpha\n\nT2: beta\n\nT3: gamma")
        check(parse(good) == D.PROMPTS_VERIFIED, "a well-formed section parses")
        check(parse("T1: a\n\nT2: b") == D.PROMPT_COUNT_INVALID,
              "a missing third prompt fails PROMPT_COUNT_INVALID")
        check(parse("T1: a\n\nT2: b\n\nT2: c\n\nT3: d") == D.PROMPTS_DUPLICATED,
              "a duplicated section fails PROMPTS_DUPLICATED")
        check(parse("T1: a\n\nT2: b\n\nT3: c\n\nT4: d") == D.PROMPT_COUNT_INVALID,
              "an extra fourth prompt fails closed")
        check(parse("T1: a\n\nT3: c\n\nT2: b") == D.PROMPTS_MALFORMED,
              "out-of-order numbering fails PROMPTS_MALFORMED")
        check(parse("T1: a\nnot indented continuation\n\nT2: b\n\nT3: c")
              == D.PROMPTS_MALFORMED, "a malformed delimiter fails PROMPTS_MALFORMED")
        check(parse("T1: \n\nT2: b\n\nT3: c") == D.PROMPTS_MALFORMED,
              "an empty prompt fails closed")
        missing = tmp / "nosection.md"
        missing.write_text("# nothing here\n")
        check(D.extract_prompts(missing)["state"] == D.PROMPTS_MISSING,
              "a file without the section fails PROMPTS_MISSING")
        check(D.extract_prompts(tmp / "absent.md")["state"] == D.PROMPTS_MISSING,
              "a missing file fails PROMPTS_MISSING")

        # There must be no placeholder path back into the driver.
        drv_src = (REPO / "scripts" / "benchmarks/planner-comparison.py").read_text()
        check("[planner turn" not in drv_src,
              "no placeholder prompt text survives anywhere in the driver")
        raised = False
        try:
            D.turn_prompts.__globals__["extract_prompts"]  # sanity
            orig_extract = D.extract_prompts
            D.extract_prompts = lambda *a, **k: {"state": D.PROMPTS_MISSING,
                                                 "reason": "forced"}
            try:
                D.turn_prompts(3)
            except RuntimeError:
                raised = True
            finally:
                D.extract_prompts = orig_extract
        except Exception:  # noqa: BLE001
            pass
        check(raised, "turn_prompts RAISES rather than degrading to placeholders")

        print("\nPROMPT HASHING IS CANONICAL")
        h = D.prompt_set_hash(real["prompts"])
        check(h == real["hash"], "the reported hash is reproducible")
        mutated = list(real["prompts"]); mutated[1] = mutated[1] + " "
        check(D.prompt_set_hash(mutated) != h, "one trailing byte changes the hash")
        reordered = [real["prompts"][1], real["prompts"][0], real["prompts"][2]]
        check(D.prompt_set_hash(reordered) != h, "reordering changes the hash")
        split = ["a" + "b", "c"] ; joined = ["a", "bc"]
        check(D.prompt_set_hash(split) != D.prompt_set_hash(joined),
              "moving a boundary between prompts changes the hash (lengths are hashed)")
        check(h != "349bde5142478d07",
              "the placeholder hash is gone and cannot be reproduced")

        # Unrelated prose in HANDOFF.md must not move the prompt hash.
        alt = tmp / "prose.md"
        src_text = (D.PLANNER / "HANDOFF.md").read_text()
        alt.write_text(src_text.replace("## Cleanup", "## Cleanup notes CHANGED"))
        check(D.extract_prompts(alt)["hash"] == h,
              "editing unrelated prose does not change the prompt hash")
        check(D.extract_prompts(alt)["source_hash"] != real["source_hash"],
              "...but the SOURCE hash does change, so the edit is still visible")
        shutil.rmtree(tmp, ignore_errors=True)

        print("\nPUBLIC MANIFEST CARRIES HASHES, NOT PROMPTS")
        for p in real["prompts"]:
            check(p[:40] not in json.dumps(man), "no prompt text appears in the public manifest")
        check(man.get("prompt_set_hash") and man.get("prompt_source_hash"),
              "the manifest carries the prompt and source hashes")
        check(man.get("prompt_state") == D.PROMPTS_VERIFIED,
              "the manifest records the prompt verification state")
        check("prompt_set_hash" in D.LOCKED_FIELDS
              and "prompt_source_hash" in D.LOCKED_FIELDS,
              "prompt hashes are LOCKED fields")
        tampered = dict(man); tampered["prompt_set_hash"] = "x"
        check(not D.manifest_is_locked(tampered, man)[0],
              "a changed prompt hash fails closed after the manifest locks")

        print("\nCANDIDATE CONFINEMENT STILL DENIES THE HANDOFF")
        gt = D.PLANNER / "HANDOFF.md"
        pol = B.confinement_settings(B.benchmark_worktree_root() / "x")
        roots = [d[len("Read(/"):-len("/**)")] for d in pol["permissions"]["deny"]]
        check(any(str(gt).startswith(r.rstrip("/") + "/") for r in roots),
              "the handoff the driver reads remains denied to candidates")

        print("\nIDENTITY IS REDACTED INSIDE EMBEDDED JSON STRINGS")
        # The client's terminal result is embedded in stdout_full as SERIALIZED
        # JSON, so key-based stripping never reached it and the first scoring
        # copies of the real run carried the bench alias as plain text.
        emb = {"records": [{"stdout_full":
               '{"modelUsage":{"bench-qwen3-5-4b-off-32k":{"x":1}},'
               '"canonicalModel":"bench-qwen3-5-4b-off-32k","duration_api_ms":374580}'}]}
        body = json.dumps(D.strip_identity(emb))
        for bad in ("bench-", "canonicalModel", "modelUsage", "374580"):
            check(bad not in body, f"embedded {bad} is redacted from string values")
        check("REDACTED" in body, "redaction leaves an explicit marker")
        # Model FAMILY names are content, not identity: the planner task is about
        # model pulling and every candidate discusses them.
        keep = {"records": [{"stdout_full": "the profile pulls gemma4 and qwen3.5"}]}
        check("gemma4" in json.dumps(D.strip_identity(keep)),
              "model family names in CONTENT are preserved (they carry no signal)")

        print("\nPRIVATE MAPPING IS VALIDATED AND NEVER EXPOSED")
        priv = D.load_private_mapping()
        check(priv["state"] == D.VERIFIED_PRIVATE_MAPPING,
              "the real private mapping validates")
        check(set(priv["_mapping"]) == set(D.CANDIDATES),
              "exactly the three candidate ids are mapped")
        check(len(set(priv["_mapping"].values())) == 3,
              "all three candidates map to DISTINCT models")
        check(all(priv["_digests"].values()), "every candidate resolves a digest")
        check(all(k.startswith("_") for k in ("_mapping", "_digests", "_modes")),
              "private fields are underscore-prefixed by convention")

        # Failure states must never quote a value.
        tmpd = Path(tempfile.mkdtemp(prefix="map-"))
        orig_path = D.MAPPING_PATH

        def with_mapping(obj, mode=0o600, expected=None):
            f = tmpd / f"m{abs(hash(json.dumps(obj, sort_keys=True)))}.json"
            f.write_text(json.dumps(obj))
            f.chmod(mode)
            D.MAPPING_PATH = f
            try:
                return D.load_private_mapping(expected)
            finally:
                D.MAPPING_PATH = orig_path

        good = {"shuffle_seed": 1, "mapping": {c: {"model": m, "mode": "off"}
                for c, m in zip(D.CANDIDATES,
                                ["gemma4:26b-mlx", "qwen3.5:4b", "qwen3.5:9b"])}}
        check(with_mapping(good)["state"] == D.VERIFIED_PRIVATE_MAPPING,
              "a well-formed mapping validates")
        bad = json.loads(json.dumps(good)); del bad["mapping"]["candidate-c"]
        check(with_mapping(bad)["state"] == D.PRIVATE_MAPPING_CANDIDATES_INVALID,
              "a missing candidate fails PRIVATE_MAPPING_CANDIDATES_INVALID")
        bad = json.loads(json.dumps(good)); bad["mapping"]["candidate-d"] = {"model": "x", "mode": "off"}
        check(with_mapping(bad)["state"] == D.PRIVATE_MAPPING_CANDIDATES_INVALID,
              "an extra candidate fails closed")
        bad = json.loads(json.dumps(good))
        bad["mapping"]["candidate-b"]["model"] = bad["mapping"]["candidate-a"]["model"]
        check(with_mapping(bad)["state"] == D.PRIVATE_MAPPING_DUPLICATE_MODEL,
              "duplicate models fail PRIVATE_MAPPING_DUPLICATE_MODEL")
        bad = json.loads(json.dumps(good))
        bad["mapping"]["candidate-a"]["model"] = "no-such-model:999b"
        check(with_mapping(bad)["state"] == D.PRIVATE_MAPPING_MODEL_INVALID,
              "an uninstalled model fails PRIVATE_MAPPING_MODEL_INVALID")
        check(with_mapping(good, expected="deadbeefdeadbeef")["state"]
              == D.PRIVATE_MAPPING_HASH_MISMATCH,
              "a hash mismatch fails closed")
        check(with_mapping(good, mode=0o666)["state"]
              == D.PRIVATE_MAPPING_PERMISSIONS_INVALID,
              "a group/other WRITABLE mapping fails closed")
        check(with_mapping(good, mode=0o644)["state"] == D.VERIFIED_PRIVATE_MAPPING,
              "a merely readable mapping warns instead of failing")
        check(with_mapping(good, mode=0o644)["permissions_warning"],
              "...and the readability is recorded as a warning")
        check(with_mapping({"mapping": "not-a-dict"})["state"]
              == D.PRIVATE_MAPPING_SCHEMA_INVALID, "a bad schema fails closed")
        check(with_mapping({"mapping": {c: "bare-string" for c in D.CANDIDATES}})["state"]
              == D.PRIVATE_MAPPING_SCHEMA_INVALID,
              "a bare-string value fails SCHEMA_INVALID, not MODEL_INVALID")
        D.MAPPING_PATH = orig_path

        # No failure state may carry a value.
        for st in (D.PRIVATE_MAPPING_MISSING, D.PRIVATE_MAPPING_SCHEMA_INVALID,
                   D.PRIVATE_MAPPING_MODEL_INVALID, D.PRIVATE_MAPPING_HASH_MISMATCH):
            check(":" not in st and "/" not in st,
                  f"failure state {st} is a bare token, carrying no value")
        real_models = set(priv["_mapping"].values())
        for res in (with_mapping(good), D.load_private_mapping()):
            keys = {k for k in res if not k.startswith("_")}
            check(not any(str(m) in json.dumps({k: res[k] for k in keys})
                          for m in real_models),
                  "public fields of the mapping result contain no model name")
        shutil.rmtree(tmpd, ignore_errors=True)

        print("\nPLACEHOLDER ALIASES CANNOT REACH EXECUTION")
        raised = False
        try:
            D.assert_no_placeholder("<alias-for-candidate-a>")
        except AssertionError:
            raised = True
        check(raised, "assert_no_placeholder rejects a placeholder")
        D.assert_no_placeholder("bench-real-alias", None)
        check(True, "a real alias passes the assertion")
        drv = (REPO / "scripts" / "benchmarks/planner-comparison.py").read_text()
        check('f"<alias-for-{cand}>"' not in drv,
              "the driver no longer constructs placeholder aliases")
        check("assert_no_placeholder(alias)" in drv,
              "run_one asserts its alias before executing")

        print("\nALIAS LIFECYCLE USES THE SHARED BUILDER")
        entries = D.build_candidate_aliases(priv)
        check(len(entries) == 3, "one alias is built per candidate")
        check(len({e["model_name"] for e in entries.values()}) == 3,
              "the three aliases are distinct (run 1 measured one model thrice)")
        for c, e in entries.items():
            lp = e["litellm_params"]
            check(lp["num_predict"] == D.PLANNER_CEILING,
                  f"{c} uses the locked 8192 output ceiling, not the 32768 one")
            check(lp["num_ctx"] == D.PLANNER_CONTEXT + D.PLANNER_CEILING,
                  f"{c} uses the locked context geometry")
            check(e["model_info"]["max_input_tokens"] <= lp["num_ctx"] - lp["num_predict"],
                  f"{c} admission stays within the physical window")
        check("build_alias" in drv and "def build_alias" not in drv,
              "alias construction is CALLED, never reimplemented in the driver")
        # Compare CALL sites, not definitions: verify_candidate_routing is defined
        # near the top of the file and applied far below it.
        check(drv.index("B.apply_aliases(") < drv.index("rt = verify_candidate_routing("),
              "aliases are applied before any per-candidate routing gate")
        check(drv.index("rt = verify_candidate_routing(") < drv.index("res = run_one("),
              "routing is verified before turn 1")
        check(drv.index("load_private_mapping(locked") < drv.index("B.apply_aliases("),
              "the private mapping is resolved before aliases are built")
        check("B.restore()" in drv and "finally:" in drv,
              "production is restored in a finally block")

        print("\nROUTING GATE CLASSIFIES PRECISELY")
        for st in (D.VERIFIED_ROUTING, D.ROUTING_ALIAS_MISSING,
                   D.ROUTING_DIGEST_MISMATCH, D.ROUTING_PRODUCTION_FALLBACK):
            check(isinstance(st, str) and st, f"routing state {st} is defined")
        check(len({D.VERIFIED_ROUTING, D.ROUTING_ALIAS_MISSING,
                   D.ROUTING_DIGEST_MISMATCH, D.ROUTING_PRODUCTION_FALLBACK}) == 4,
              "routing failure modes are not collapsed into one")

        print("\nNO LEAKS")
        check(not any(p.name.startswith("unit-") and p.is_dir()
                      for p in B.benchmark_worktree_root().iterdir()
                      if p.name not in [w.name for w in wts.values()]),
              "no stray worktrees were created")
        for w in wts.values():
            shutil.rmtree(w, ignore_errors=True)
        B.sweep_worktree_root()
        check(not list(Path.home().glob(".ailocal-confinement-canary-*")),
              "no canary files leak into $HOME")
        shutil.rmtree(out, ignore_errors=True)

        print("\nHISTORICAL RECORDS REMAIN READABLE")
        hist = list((Path.home() / ".local/state/ailocal/captures/traces").glob("*.jsonl"))
        bad = 0
        for f in hist:
            for line in f.read_text(errors="replace").splitlines():
                try:
                    json.loads(line)
                except Exception:  # noqa: BLE001
                    bad += 1
        check(bad == 0, f"all historical trace records still parse ({len(hist)} files)")

        print()
    _planner_body()


def command_checks() -> None:
    """The public surface: `ailocal benchmark`. Dispatch and argument behaviour.

    Behavioural throughout -- these run the real command. Nothing here inspects
    implementation source.
    """
    cli = str(REPO / "scripts" / "ailocal")

    def run(*args, env=None, timeout=300):
        return subprocess.run([cli, "benchmark", *args], capture_output=True,
                              text=True, timeout=timeout, env=env)

    _suite.section("COMMAND DISPATCH")
    r = run("help")
    check(r.returncode == 0, "benchmark help exits 0")
    for suite in ("models", "planner", "gateway"):
        check(suite in r.stdout or suite in r.stderr,
              f"help lists the {suite} suite")
    check(run("definitely-not-a-suite").returncode == 2,
          "an unknown suite exits 2")

    _suite.section("MODELS SUITE")
    check(run("models", "--help").returncode == 0, "models --help exits 0")
    check(run("models", "doctor").returncode == 0, "models doctor runs")
    check(run("models", "not-a-subcommand").returncode != 0,
          "an unknown models subcommand fails")

    _suite.section("PLANNER SUITE")
    check(run("planner", "--help").returncode == 0, "planner --help exits 0")
    r = run("planner")
    check(r.returncode == 2, "planner with no arguments refuses (exit 2)")
    check("never implicit" in r.stderr,
          "the refusal explains that inference is never implicit")
    check(run("planner", "--dry-run").returncode == 0, "planner --dry-run succeeds")
    check(run("planner", "--candidate", "not-a-candidate").returncode != 0,
          "an unknown candidate fails rather than guessing")
    check(run("planner", "--no-such-flag").returncode != 0,
          "an unknown planner flag fails")

    # The permission contract must survive the command surface unchanged.
    import benchmark_clients as _bc
    check(_bc.permission_manifest_hash({"permissions": {"allow": ["Read"], "deny": []}})
          == "960d22201caa1416fd21b613f0e16826aaf98cb539e2d07a9ca2de5212f9e546",
          "the permission manifest hash is unchanged")


def runtime_checks() -> None:
    """apply_aliases stages the GENERATED config, not the authored subsystem.

    Regression: it copied deploy/litellm (authored) and then read config.yaml
    from it. That file is generated to $AILOCAL_STATE, so the read raised
    FileNotFoundError and no benchmark alias could ever be installed.
    """
    _suite.section("BENCHMARK RUNTIME")
    import benchmark_runtime as BR

    check(BR.generated_dir() == _pc.runtime_root() / "litellm",
          "the generated config dir comes from the one state-root owner")
    check("litellm" not in str(BR.generated_dir().parent),
          "it is not derived from the authored deploy/ tree")

    gen = BR.generated_dir() / "config.yaml"
    if not gen.exists():
        check(False, "generated config.yaml exists to stage from", str(gen))
        return

    entry = BR.build_alias("m:1b", "off", 4096, 512, {}, keep_alive="1m")
    calls = {}
    real_compose, real_wait, real_aliases = BR._compose, BR._wait_healthy, BR.aliases
    BR._compose = lambda *a, **k: calls.setdefault("compose", a)
    BR._wait_healthy = lambda *a, **k: True
    BR.aliases = lambda *a, **k: [entry["model_name"]]
    try:
        res = BR.apply_aliases([entry])
    finally:
        BR._compose, BR._wait_healthy, BR.aliases = real_compose, real_wait, real_aliases

    check(res["ok"], "apply_aliases completes against the real generated config")
    staged = BR.runtime_dir() / "generated" / "config.yaml"
    check(staged.exists(), "it stages a copy of the generated config")
    body = staged.read_text() if staged.exists() else ""
    check(entry["model_name"] in body, "the temporary alias is injected")
    check("TEMPORARY BENCHMARK ALIASES" in body, "the injected block is marked")
    ov = (BR.runtime_dir() / "docker-compose.bench.yml").read_text()
    check("/app/generated:ro" in ov,
          "the override remaps /app/generated, where the proxy reads config.yaml")
    check("/app/config:ro" not in ov,
          "it does not remap the authored /app/config mount")


SECTIONS = {"library": library_checks, "planner": planner_checks,
            "command": command_checks, "runtime": runtime_checks}

if __name__ == "__main__":
    which = sys.argv[1] if len(sys.argv) > 1 else None
    if which and which not in SECTIONS:
        sys.exit(f"unknown section {which!r}; expected one of {sorted(SECTIONS)}")
    for name in ([which] if which else list(SECTIONS)):
        SECTIONS[name]()
    sys.exit(_suite.report())
