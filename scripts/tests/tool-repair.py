#!/usr/bin/env python3
"""test-tool-repair.py — regression tests for the tool-call repair layer.

The case that motivated this: a real Claude Code session where qwen3-coder's
ENTIRE reply was a fenced JSON tool call. The client executed nothing, the file
was never edited, and the session stalled while appearing to work.

The hard part is not detecting fenced JSON — it is NOT detecting it when the
fence is a tutorial example. These tests pin both directions, because a repair
layer that fires on documentation would execute commands the model never
intended, which is strictly worse than the bug it fixes.

Run: python3 scripts/tests/tool-repair.py   (stdlib only; exit 1 on failure)
"""
import importlib.util
import os
import sys
import types

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

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
spec = importlib.util.spec_from_file_location(
    "tool_repair", os.environ.get("AILOCAL_TOOL_REPAIR",
                                  os.path.join(ROOT, "config/litellm/tool_repair.py")))
tr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tr)

fails = 0
def check(cond, name):
    global fails
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    if not cond:
        fails += 1

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

print()
if fails:
    print(f"TOOL REPAIR TESTS: {fails} FAILED")
    sys.exit(1)
print("TOOL REPAIR TESTS: OK")
