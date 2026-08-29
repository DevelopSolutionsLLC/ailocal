#!/usr/bin/env python3
"""The AUTHORITATIVE half: real Mermaid 11.17.2, real Chrome.

FULL, not CORE -- it launches a browser per fixture. The control flow that
consumes these verdicts is asserted cheaply in test_mermaid_validate.py, which
also runs ONE real parse so a green CORE cannot mean this path is dead.

Every ACCEPT here is proved by the grammar, never by inspecting normaliser
output. That distinction is the whole point: the old suite asserted
`"class Router {" in cd_out` and passed while the payload that reached a user
did not parse at all.
"""
import importlib.util, os, sys, tempfile, time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
# Publishing writes .artifacts/ under the approved root, which defaults to cwd.
# Point it at a throwaway dir BEFORE server.py is imported (it resolves the root
# at import time), or the suite litters the checkout it is testing -- which the
# install-parity and root-cleanliness gates then correctly reject.
_ARTIFACT_ROOT = tempfile.mkdtemp(prefix="la-testroot-")
os.environ["LOCAL_ARTIFACTS_ROOT"] = _ARTIFACT_ROOT
spec = importlib.util.spec_from_file_location("srv", HERE / "server.py")
srv = importlib.util.module_from_spec(spec); spec.loader.exec_module(srv)
import mermaid_validate as V

FAILS = 0
def check(label, cond, detail=""):
    global FAILS
    print(("  ok    " if cond else "  FAIL  ") + label + ("" if cond else f"  -- {detail}"))
    if not cond: FAILS += 1

if not V.available():
    print("SKIP  no Chrome/Chromium; the grammar corpus cannot run here.")
    print("      (publishing mermaid REFUSES on this machine -- it does not fail open)")
    sys.exit(0)

def valid(src):
    return V.parse(src).state == V.VALID

# [REAL] recovered from the claude-local transcript that produced the bomb icon,
# not retyped from the chat: gemma4:26b-mlx corrupted one member mid-token.
CORRUPT_LINE = "+render_nodes(ctx: Episode%} -> list[dict]"
FIXED_LINE   = "+render_nodes(ctx: EpisodeContext) list[dict]"
RECOVERED = """classDiagram
    class EpisodeContext {
        +str episode_id
        +list[str] ollama_hosts
        +Path art_batch()
        +Path character_reference(name)
    }
    class Stage {
        <<abstract>>
        +str name
        +run(ctx: EpisodeContext) StageResult
        %s
    }
    class StageResult {
        +bool success
        +list[str] errors
    }
    class StageError {
        <<Exception>>
    }
    class NoRenderNodesError {
        <<Exception>>
    }
    StageError <|-- NoRenderNodesError
    Stage ..> StageResult : returns
    Stage ..> EpisodeContext : processes
"""

print("MERMAID GRAMMAR CORPUS (real Mermaid 11.17.2, real Chrome)")

t0 = time.time()
r = V.parse(RECOVERED % CORRUPT_LINE)
one = time.time() - t0
check("the recovered corrupt payload is REJECTED", r.state == V.INVALID, r)
check("the rejection names the offending line", "line" in r.detail.lower(), r.detail)
check("the rejection quotes the offending text", "Episode%}" in r.detail, r.detail)
check("the line-37-fixed payload is ACCEPTED", valid(RECOVERED % FIXED_LINE))
print(f"        (one validation: {one:.2f}s)")

CORPUS = {
 "plain classDiagram":        "classDiagram\n    class A\n    class B\n    A <|-- B\n",
 "class blocks with members": "classDiagram\n    class Router {\n        -routes: dict\n"
                              "        +dispatch(req)\n    }\n    class Handler {\n"
                              "        <<abstract>>\n        +handle(req)*\n    }\n"
                              "    Handler <|-- Router\n",
 "generic type annotations":  "classDiagram\n    class C {\n        +list[str] hosts\n"
                              "        +dict summary\n        +load(p) dict[str, int]\n    }\n",
 "method signatures":         "classDiagram\n    class S {\n"
                              "        +run(ctx: EpisodeContext) StageResult\n    }\n",
 "dependency edges":          "classDiagram\n    class A\n    class B\n    A ..> B : returns\n",
 "exception inheritance":     "classDiagram\n    class StageError {\n        <<Exception>>\n    }\n"
                              "    class NoRenderNodesError\n"
                              "    StageError <|-- NoRenderNodesError\n",
 "labels with parentheses":   "flowchart TD\n    Start[Reviewer(s) Assigned] --> B[done]\n",
 "sequenceDiagram":           "sequenceDiagram\n    run.py->>Stage: run(ctx)\n"
                              "    Stage-->>run.py: StageResult\n",
 "grouped architecture flow": "flowchart TB\n    subgraph orchestration\n        R[run.py]\n    end\n"
                              "    subgraph stages\n        G[generate]\n    end\n    R --> G\n",
}
for name, src in CORPUS.items():
    normalised, _ = srv.strip_mermaid_presentation(src)
    normalised, _ = srv.normalise_mermaid_labels(normalised)
    check(f"accepted and parses: {name}", valid(normalised))

# The existing label-quoting fix is load-bearing, measured by the grammar.
raw_paren = "flowchart TD\n    Start[Reviewer(s) Assigned] --> B[done]\n"
check("unquoted paren label is genuinely invalid", not valid(raw_paren))
check("normalisation is what makes it valid",
      valid(srv.normalise_mermaid_labels(raw_paren)[0]))

# ── negative control ──────────────────────────────────────────────────────────
# `->` is not a classDiagram relationship operator. Every string assertion the
# old suite made still holds -- both declarations survive the colour stripper,
# braces balance, the classDef is gone, an edge line is present -- and the
# diagram is still dead on arrival.
DECOY = """classDiagram
    class Router {
        -routes: dict
        +dispatch(req)
    }
    class Handler {
        <<abstract>>
    }
    classDef hot fill:#f9f
    Handler -> Router
"""
out, dropped = srv.strip_mermaid_presentation(DECOY)
check("negative control: old-style string assertions all PASS",
      "class Router {" in out and "class Handler {" in out
      and out.count("{") == out.count("}") and dropped == 1
      and "#f9f" not in out and "Handler -> Router" in out
      and "+dispatch(req)" in out and "<<abstract>>" in out)
check("negative control: but the grammar REJECTS it", not valid(out))

# ── end to end, through publish() ─────────────────────────────────────────────
ok, msg = srv.publish(title="Grammar Corrupt", content=RECOVERED % CORRUPT_LINE, fmt="mermaid")
check("publish() REFUSES the real corrupt payload", ok is False, msg)
ok, msg = srv.publish(title="Grammar Fixed", content=RECOVERED % FIXED_LINE, fmt="mermaid")
check("publish() ACCEPTS the corrected payload", ok is True, msg)
ok, msg = srv.publish(title="Grammar MD",
                      content="# R\n\nProse.\n\n```mermaid\n" + (RECOVERED % CORRUPT_LINE) + "```\n",
                      fmt="markdown")
check("publish() REFUSES a corrupt fence inside markdown", ok is False, msg)
ok, msg = srv.publish(title="Grammar MD OK",
                      content="# R\n\nProse.\n\n```mermaid\n" + (RECOVERED % FIXED_LINE) + "```\n",
                      fmt="markdown")
check("markdown WITH a valid fence still publishes (bb15bec intact)", ok is True, msg)

# ── browser isolation and cleanup ─────────────────────────────────────────────
tmp = Path(tempfile.gettempdir())
def profiles():
    return {p for p in tmp.glob("mermaid-validate-profile-*")}
def pages():
    return {p for p in tmp.glob("mermaid-validate-*") if "profile" not in p.name}

before_p, before_g = profiles(), pages()
V.parse(RECOVERED % FIXED_LINE)
check("the temporary Chrome profile is removed", profiles() == before_p,
      profiles() - before_p)
check("the temporary page directory is removed", pages() == before_g, pages() - before_g)

# The user's real profile must be untouched. Chrome is very likely running.
real_profile = Path.home() / "Library/Application Support/Google/Chrome"
if real_profile.is_dir():
    before = real_profile.stat().st_mtime
    r = V.parse(RECOVERED % CORRUPT_LINE)
    check("a verdict is still obtained while the user's Chrome may be running",
          r.state == V.INVALID, r)
    check("the user's default Chrome profile is not modified",
          real_profile.stat().st_mtime == before)
else:
    print("  NOTE  no default Chrome profile on this machine to compare against")

print(("FAIL %d" % FAILS) if FAILS else "PASS")
sys.exit(1 if FAILS else 0)
