#!/usr/bin/env python3
"""Test the validator state machine and the publish gate that consumes it.

The `runner` seam replaces Chrome; grammar lives in test_mermaid_grammar.py.
Invariant: invalid or unchecked Mermaid never publishes. One bounded real
parse keeps a green CORE from hiding a broken real path.
"""
from __future__ import annotations

import importlib.util, os, sys, tempfile, time
from collections.abc import Callable
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
# Set the approved root before importing server.py, else .artifacts/ lands
# in the checkout.
_ARTIFACT_ROOT = tempfile.mkdtemp(prefix="la-testroot-")
os.environ["LOCAL_ARTIFACTS_ROOT"] = _ARTIFACT_ROOT
spec = importlib.util.spec_from_file_location("srv", HERE / "server.py")
assert spec is not None and spec.loader is not None
srv = importlib.util.module_from_spec(spec); spec.loader.exec_module(srv)
import mermaid_validate as V

FAILS = 0
def check(label: str, cond: object, detail: object = "") -> None:
    global FAILS
    print(("  ok    " if cond else "  FAIL  ") + label + ("" if cond else f"  -- {detail}"))
    if not cond: FAILS += 1

CORRUPT_LINE = "+render_nodes(ctx: Episode%} -> list[dict]"
FIXED_LINE   = "+render_nodes(ctx: EpisodeContext) list[dict]"
RECOVERED = """classDiagram
    class Stage {
        <<abstract>>
        +run(ctx: EpisodeContext) StageResult
        %s
    }
    class StageError {
        <<Exception>>
    }
    class NoRenderNodesError
    StageError <|-- NoRenderNodesError
"""

def dom(body: str) -> str:
    return f'<html><body><pre id="o">{body}</pre></body></html>'

print("MERMAID VALIDATOR STATE MACHINE (no browser)")

# the four states
r = V.parse("classDiagram\n class A\n", runner=lambda u: (dom(V._OK), None))
check("VALID when the grammar accepts", r.state == V.VALID, r)

r = V.parse("x", runner=lambda u: (dom(V._ERR + "Parse error on line 37: bad"), None))
check("INVALID when the grammar rejects", r.state == V.INVALID, r)
check("INVALID carries the parser message", "line 37" in r.detail, r.detail)

r = V.parse("x", runner=lambda u: (None, "could not start the browser"))
check("UNAVAILABLE when the browser will not start", r.state == V.UNAVAILABLE, r)

r = V.parse("x", runner=lambda u: ("", None))
check("UNAVAILABLE on an empty/timed-out run", r.state == V.UNAVAILABLE, r)

r = V.parse("x", runner=lambda u: (dom("nothing useful"), None))
check("UNAVAILABLE when no verdict marker appears", r.state == V.UNAVAILABLE, r)

_real_find = V.find_chrome
V.find_chrome = lambda: None
r = V.parse("classDiagram\n class A\n")
check("UNAVAILABLE when no Chrome exists", r.state == V.UNAVAILABLE, r)
check("and it says so usefully", "Chrome" in r.detail, r.detail)
V.find_chrome = _real_find

check("UNAVAILABLE is NEVER reported as VALID",
      V.UNAVAILABLE != V.VALID and V.parse("x", runner=lambda u: ("", None)).state != V.VALID)

# bounded termination: the reader must not outlive the deadline
class _Hang:
    """Fake a process that never emits and never exits."""
    def __init__(self) -> None:
        import os
        self._r, self._w = os.pipe()
        self.stdout = open(self._r, "rb", buffering=0)
    def poll(self) -> None: return None
t = time.time()
buf = V._read_until_verdict(_Hang(), time.monotonic() + 1.0)
el = time.time() - t
check("a hung browser is bounded by the deadline", 0.9 < el < 3.0, f"{el:.2f}s")
check("and yields no verdict (=> UNAVAILABLE)", buf == "")

# the publish gate consumes all three states
_real_parse = V.parse
def fixed(state: str, detail: str = "") -> Callable[..., V.Result]:
    return lambda *a, **k: V.Result(state, detail)

srv.mermaid_validate.parse = fixed(V.VALID)
ok, msg = srv.publish(title="Gate Valid", content="classDiagram\n class A\n", fmt="mermaid")
check("VALID publishes", ok is True, msg)

srv.mermaid_validate.parse = fixed(V.INVALID, "Parse error on line 37: got 'MINUS'")
ok, msg = srv.publish(title="Gate Invalid", content="whatever", fmt="mermaid")
check("INVALID refuses", ok is False, msg)
check("INVALID quotes the parser", "line 37" in msg and "does not parse" in msg, msg)
check("INVALID says it was NOT published", "NOT published" in msg, msg)

srv.mermaid_validate.parse = fixed(V.UNAVAILABLE, "no Chrome or Chromium was found")
ok, msg = srv.publish(title="Gate Unavailable", content="whatever", fmt="mermaid")
check("UNAVAILABLE refuses -- never a silent unvalidated publish", ok is False, msg)
check("UNAVAILABLE is described as unchecked, not as invalid",
      "could NOT be checked" in msg and "does not parse" not in msg, msg)
check("UNAVAILABLE tells the caller how to fix it",
      "Chrome" in msg and "html" in msg, msg)

ok, msg = srv.publish(title="Gate MD Unavailable",
                      content="# T\n\n```mermaid\nclassDiagram\n class A\n```\n",
                      fmt="markdown")
check("UNAVAILABLE refuses inside markdown too", ok is False, msg)

# Non-Mermaid formats must stay completely unaffected by a dead validator.
ok, msg = srv.publish(title="Gate Html", content="<!doctype html><p>hi</p>", fmt="html")
check("html still publishes when validation is unavailable", ok is True, msg)
ok, msg = srv.publish(title="Gate Prose", content="# Title\n\nProse only.\n", fmt="markdown")
check("prose markdown still publishes when validation is unavailable", ok is True, msg)

# the validator must not run when there is nothing to validate
calls = {"n": 0}
def counting(*a: object, **k: object) -> V.Result:
    calls["n"] += 1
    return V.Result(V.VALID)
srv.mermaid_validate.parse = counting
srv.check_mermaid("# Title\n\nProse only, no fences.\n", "markdown")
check("prose-only markdown starts NO validation", calls["n"] == 0, calls)
calls["n"] = 0
srv.check_mermaid("a\n```mermaid\nclassDiagram\n class A\n```\n"
                  "b\n```mermaid\nclassDiagram\n class B\n```\n", "markdown")
check("markdown validates once per fence", calls["n"] == 2, calls)
calls["n"] = 0
srv.check_mermaid("<!doctype html>", "html")
check("html starts NO validation", calls["n"] == 0, calls)
srv.mermaid_validate.parse = _real_parse

# one bounded REAL parse, so green CORE cannot hide a broken real path
if V.available():
    t = time.time()
    real = V.parse(RECOVERED % CORRUPT_LINE)
    el = time.time() - t
    check("REAL grammar rejects the recovered corrupt payload",
          real.state == V.INVALID, real)
    check("REAL rejection names the line", "line" in real.detail.lower(), real.detail)
    check("one real validation is bounded", el < 15, f"{el:.2f}s")
    print(f"        (one real validation: {el:.2f}s -- full corpus is in FULL)")
else:
    print("  NOTE  no Chrome: the real-grammar smoke is skipped; "
          "publishing mermaid would refuse, not fail open")

print(("FAIL %d" % FAILS) if FAILS else "PASS")
sys.exit(1 if FAILS else 0)
