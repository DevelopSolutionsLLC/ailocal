#!/usr/bin/env python3
"""Gateway-side request handling: tool-call repair.

  repair    recovery of fenced JSON tool calls, and the harder direction --
            refusing to fire on tutorial examples.

Persona injection was the other section; the mechanism it covered is gone (see
the note below). The docstring also advertised a `trace` section that was never
in SECTIONS.

Usage: gateway.py [repair]   (default: all)
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness import RESOURCES, REPO, Suite, load_module  # noqa: E402

_suite = Suite()
check = _suite.check

# ── (persona injection removed) ────────────────────────────────────────
# The injector merged a per-capability system prompt into every request. Its
# content was measured twice: the 34-line version made a toolless model invent
# <call:ls_tree>/<call:read_file> and fail to finish; the 12-line replacement
# produced no measurable difference through claude-local WITH tools (identical
# answers and tool counts on two tasks, 416 vs 454 words on a third). A
# subsystem delivering an unmeasurable effect is not a subsystem worth having,
# so the hook, the instruction files and the profile flag are gone.

# ── repair ──────────────────────────────────────────────────────────────
import os
import sys
import types
from pathlib import Path
try:
    from litellm.integrations.custom_logger import CustomLogger  # noqa: F401
except ImportError:
    _c = types.ModuleType("litellm.integrations.custom_logger")
    class _CL:
        def __init__(self, *a, **k): pass
    _c.CustomLogger = _CL
    sys.modules["litellm"] = types.ModuleType("litellm")
    sys.modules["litellm.integrations"] = types.ModuleType("litellm.integrations")
    sys.modules["litellm.integrations.custom_logger"] = _c
tr = load_module("tool_repair", os.environ.get(
    "AILOCAL_TOOL_REPAIR", RESOURCES / "deploy/litellm/hooks/tool_repair.py"))
TOOLS = [
    {"name": "Read", "description": "Read a file",
     "input_schema": {"type": "object",
                      "properties": {"file_path": {"type": "string"}},
                      "required": ["file_path"]}},
    {"name": "Bash", "description": "Run a command",
     "input_schema": {"type": "object",
                      "properties": {"command": {"type": "string"}},
                      "required": ["command"]}},
]

# ── trace ───────────────────────────────────────────────────────────────
import json
import re
import sys
from pathlib import Path

def repair_checks() -> None:
    print("\nMUST REPAIR — the reply IS the call")
    calls, left = tr.recover(
        '```json\n{"name": "Read", "arguments": {"file_path": "/tmp/a.py"}}\n```',
        TOOLS)
    check(calls is not None and len(calls) == 1, "sole fenced JSON becomes a tool call")
    if calls:
        check(calls[0]["function"]["name"] == "Read", "correct tool name")
        import json as _j
        check(_j.loads(calls[0]["function"]["arguments"])["file_path"] == "/tmp/a.py",
              "arguments preserved with correct types")
    check(not left, "no leftover text when the fence was the whole reply")
    calls, _ = tr.recover(
        '```\n{"name": "Bash", "arguments": {"command": "ls"}}\n```', TOOLS)
    check(calls is not None, "unlabelled fence also repaired")
    calls, _ = tr.recover('{"name": "Read", "arguments": {"file_path": "/tmp/b"}}', TOOLS)
    check(calls is not None, "bare JSON (no fence) still repaired — existing path intact")
    calls, _ = tr.recover(
        '<function=Read>\n<parameter=file_path>/tmp/c</parameter>\n</function>', TOOLS)
    check(calls is not None, "qwen <function=> format still repaired — existing path intact")
    print("\nMUST NOT REPAIR — the fence is an example, not a call")
    prose = ('To read a file, the agent emits a call like this:\n\n'
             '```json\n{"name": "Read", "arguments": {"file_path": "/tmp/x"}}\n```\n\n'
             'That is how the protocol works.')
    calls, left = tr.recover(prose, TOOLS)
    check(calls is None, "fenced example SURROUNDED BY PROSE is not executed")
    check(left == prose, "the explanation is returned unchanged")
    two = ('```json\n{"name": "Read", "arguments": {"file_path": "/a"}}\n```\n'
           '```json\n{"name": "Bash", "arguments": {"command": "rm -rf /"}}\n```')
    calls, _ = tr.recover(two, TOOLS)
    check(calls is None, "TWO fences are never auto-executed, even if both validate")
    calls, _ = tr.recover(
        '```json\n{"name": "DropDatabase", "arguments": {"db": "prod"}}\n```', TOOLS)
    check(calls is None, "a fenced call to an UNDECLARED tool is refused")
    calls, _ = tr.recover(
        '```json\n{"name": "Read", "arguments": {"wrong_field": "x"}}\n```', TOOLS)
    check(calls is None, "a fenced call missing a REQUIRED argument is refused")
    calls, _ = tr.recover('```python\nprint("hello")\n```', TOOLS)
    check(calls is None, "an ordinary code fence is not a tool call")
    calls, _ = tr.recover('Here is some JSON: {"name": "config", "value": 1}', TOOLS)
    check(calls is None, "JSON that is not a tool-call shape is left alone")
    print("\nGUARDS")
    calls, left = tr.recover("Just a normal answer.", TOOLS)
    check(calls is None and left == "Just a normal answer.", "plain prose untouched")
    calls, _ = tr.recover('```json\n{"name":"Read","arguments":{"file_path":"/a"}}\n```', [])
    check(calls is None, "no declared tools -> never repairs")
    calls, _ = tr.recover("", TOOLS)
    check(calls is None, "empty content -> no crash")


SECTIONS = {"repair": repair_checks}

if __name__ == "__main__":
    which = sys.argv[1] if len(sys.argv) > 1 else None
    if which and which not in SECTIONS:
        sys.exit(f"unknown section {which!r}; expected one of {sorted(SECTIONS)}")
    for name in ([which] if which else list(SECTIONS)):
        SECTIONS[name]()
    sys.exit(_suite.report())
