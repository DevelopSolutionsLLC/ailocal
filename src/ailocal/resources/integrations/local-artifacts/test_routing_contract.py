#!/usr/bin/env python3
"""What the routing contract SAYS -- deterministic, no inference.

The behavioural counterpart (tests/artifact-routing.sh) samples a real local
model and reports a RATE. These two must not be confused: this file proves the
description still contains the language that was measured to work, and nothing
more. It cannot prove a model will obey it, and a green run here is not evidence
that routing works.
"""
import importlib.util, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
spec = importlib.util.spec_from_file_location("srv", HERE / "server.py")
srv = importlib.util.module_from_spec(spec); spec.loader.exec_module(srv)

FAILS = 0
def check(label, cond, detail=""):
    global FAILS
    print(("  ok    " if cond else "  FAIL  ") + label + ("" if cond else f"  -- {detail}"))
    if not cond: FAILS += 1

D = srv.TOOL_DESCRIPTION
print("ROUTING CONTRACT (deterministic; says nothing about model behaviour)")

# Verbs measured to fix real misses. Each was absent when a real session missed.
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

# The exact sentence a user typed, decomposed. Routing is semantic, so assert
# the CONSTITUENTS the description must cover, not the sentence itself.
PHRASE = "can you draw me a code architecture diagram of the classes and objects in this pipeline"
for token in ("draw", "architecture", "class"):
    check(f"'{PHRASE[:28]}...' is covered via: {token}", token in D)

# alwaysLoad is what keeps the tool EAGER under ToolSearch. [REAL] the failing
# session made zero ToolSearch calls and still reached the tool, which is only
# true because of this metadata -- so it is load-bearing for routing.
mcp_src = (HERE / "server.py").read_text()
check("the tool opts out of ToolSearch deferral",
      '"anthropic/alwaysLoad": True' in mcp_src)
check("a searchHint exists for when it IS deferred",
      '"anthropic/searchHint"' in mcp_src)

print(("FAIL %d" % FAILS) if FAILS else "PASS")
sys.exit(1 if FAILS else 0)
