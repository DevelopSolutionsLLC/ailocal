#!/usr/bin/env python3
"""Check what the routing contract SAYS: deterministic, no inference.

Proves the description keeps the measured language, not that a model obeys
it; tests/artifact-routing.sh measures the behavioural rate.
"""
from __future__ import annotations

import importlib.util, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
spec = importlib.util.spec_from_file_location("srv", HERE / "server.py")
srv = importlib.util.module_from_spec(spec); spec.loader.exec_module(srv)

FAILS = 0
def check(label: str, cond: object, detail: str = "") -> None:
    global FAILS
    print(("  ok    " if cond else "  FAIL  ") + label + ("" if cond else f"  -- {detail}"))
    if not cond: FAILS += 1

D = srv.TOOL_DESCRIPTION
print("ROUTING CONTRACT (deterministic; says nothing about model behaviour)")

# Each verb was absent when a real session missed.
for verb in ("publish", "create", "draw", "sketch", "make", "render", "show",
             "visualize", "present"):
    check(f"verb is named: {verb}", verb in D)

for noun in ("flowchart", "diagram", "architecture", "class", "chart",
             "dashboard", "report"):
    check(f"noun is named: {noun}", noun in D)

check("the rule is stated, not just the vocabulary",
      "A visual verb" in D and "MEANS call this tool" in D)
check("fenced source is explicitly ruled out",
      "instead of returning" in D and "fenced code block" in D)
check("the source-on-request carve-out survives",
      "explicitly ask for the source" in D)
check("form selection is mapped to the question",
      "class or type relationships" in D and "runtime interaction" in D
      and "process/control flow" in D)

# Routing is semantic: assert a real user sentence's constituents.
PHRASE = "can you draw me a code architecture diagram of the classes and objects in this pipeline"
for token in ("draw", "architecture", "class"):
    check(f"'{PHRASE[:28]}...' is covered via: {token}", token in D)

# alwaysLoad keeps the tool eager under ToolSearch: load-bearing for routing.
mcp_src = (HERE / "server.py").read_text()
check("the tool opts out of ToolSearch deferral",
      '"anthropic/alwaysLoad": True' in mcp_src)
check("a searchHint exists for when it IS deferred",
      '"anthropic/searchHint"' in mcp_src)

print(("FAIL %d" % FAILS) if FAILS else "PASS")
sys.exit(1 if FAILS else 0)
