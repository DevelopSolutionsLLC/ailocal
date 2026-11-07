#!/usr/bin/env python3
"""Test design invariants: contrast, colour-independence, presentation.

Computed from the canonical tokens (pinned @carbon/themes). WCAG 2.2:
  normal text                                    >= 4.5:1
  large text                                     >= 3.0:1
  graphical objects NEEDED to understand content >= 3.0:1
Decorative borders need only be visible, not 3:1.
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import tokens as _tokens

THEME = _tokens.load()

PASS = FAIL = 0


def check(name: str, cond: object, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


def _lin(c: float) -> float:
    c /= 255
    return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4


def lum(hexstr: str) -> float:
    h = hexstr.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    return 0.2126 * _lin(r) + 0.7152 * _lin(g) + 0.0722 * _lin(b)


def contrast(a: str, b: str) -> float:
    la, lb = lum(a), lum(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


print("TEXT CONTRAST (>= 4.5:1)")
for mode in ("light", "dark"):
    m = THEME[mode]
    for role in ("ink", "muted", "faint"):
        r = contrast(m[role], m["surface"])
        check(f"{mode}/{role} on surface >= 4.5:1", r >= 4.5, f"{r:.2f}:1")

print("\nGRAPHICAL OBJECTS REQUIRED FOR UNDERSTANDING (>= 3.0:1)")
for mode in ("light", "dark"):
    m = THEME[mode]
    # Connectors carry the relationships, so they are load-bearing.
    worst, which = min((contrast(v, m["canvas"]), k) for k, v in m["accent"].items())
    check(f"{mode}/weakest connector vs canvas >= 3.0:1", worst >= 3.0,
          f"{which} {worst:.2f}:1")
    # The accent rail identifies a node's type alongside the printed label.
    worst, which = min((contrast(v, m["surface"]), k) for k, v in m["accent"].items())
    check(f"{mode}/weakest type rail vs surface >= 3.0:1", worst >= 3.0,
          f"{which} {worst:.2f}:1")

print("\nCOLOUR IS NOT THE ONLY CARRIER")
labels = THEME["type_labels"]
accents = THEME["light"]["accent"]
missing = [k for k in accents if k not in labels]
check("every accent kind has a printed text label", not missing, str(missing))
edges = THEME["edge_kinds"]
check("every edge kind carries a text label",
      all("label" in v for v in edges.values()))
# Accents are close in luminance, so identification cannot rely on hue.
gs = []
ks = [k for k in accents if k != "default"]
for i, a in enumerate(ks):
    for b in ks[i + 1:]:
        if contrast(accents[a], accents[b]) < 1.2:
            gs.append(f"{a}/{b}")
check("accents that are indistinguishable in greyscale are covered by labels",
      all(k in labels for pair in gs for k in pair.split("/")),
      f"close pairs: {gs}")

print("\nPRESENTATION BOUNDARY (the model owns meaning, not looks)")
os.environ["LOCAL_ARTIFACTS_AUTO_OPEN"] = "0"
spec = importlib.util.spec_from_file_location("srv", str(HERE / "server.py"))
srv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(srv)

# Real captured model output.
CAPTURED = """graph TD
    Start([Developer Submits PR]) --> CI[Run Tests]
    CI --> Review[Reviewer(s) Assigned]
    style Start fill:#f9f
    style CI fill:#e1f5fe
    classDef hot fill:#fff9c4,stroke:#333
    class CI hot
    linkStyle 0 stroke:#0f0
"""
out, dropped = srv.strip_mermaid_presentation(CAPTURED)
check("model-authored colour is stripped", dropped == 5, f"dropped {dropped}")
for hexval in ("#f9f", "#0f0", "#e1f5fe", "#fff9c4"):
    check(f"{hexval} does not survive into the page", hexval not in out)
check("semantic structure survives",
      "Start([Developer Submits PR])" in out and "-->" in out and "Reviewer(s)" in out)

# `class Foo {` declares a class here; stripping it like flowchart styling
# leaves an orphan brace and a syntax error.
CLASS_DIAGRAM = """classDiagram
    class Router {
        -routes: dict
        +dispatch(req)
    }
    class Handler {
        <<abstract>>
        +handle(req)*
    }
    classDef hot fill:#f9f
    Handler <|-- Router
"""
cd_out, cd_dropped = srv.strip_mermaid_presentation(CLASS_DIAGRAM)
check("class DECLARATIONS survive in a classDiagram",
      "class Router {" in cd_out and "class Handler {" in cd_out, cd_out)
check("no orphan brace is left behind",
      cd_out.count("{") == cd_out.count("}"), cd_out)
check("members survive", "+dispatch(req)" in cd_out and "<<abstract>>" in cd_out)
check("the relationship survives", "Handler <|-- Router" in cd_out)
check("classDef colour is still stripped there",
      cd_dropped == 1 and "#f9f" not in cd_out, f"dropped {cd_dropped}")

# In flowcharts `class` is presentation and must still be stripped.
FLOWCHART = """flowchart TD
    A[a] --> B[b]
    class A hot
    classDef hot fill:#f9f
"""
fc_out, fc_dropped = srv.strip_mermaid_presentation(FLOWCHART)
check("flowchart `class` is still stripped as presentation",
      fc_dropped == 2 and "class A hot" not in fc_out, f"dropped {fc_dropped}")
check("flowchart semantics survive", "A[a] --> B[b]" in fc_out)

print("\nSYNTAX NORMALISATION (narrow, idempotent, semantics preserved)")
q1, n1 = srv.normalise_mermaid_labels("Review[Reviewer(s) Assigned]")
check("unquoted parens get quoted", n1 == 1 and q1 == 'Review["Reviewer(s) Assigned"]', q1)
q2, n2 = srv.normalise_mermaid_labels(q1)
check("idempotent: a second pass changes nothing", n2 == 0 and q2 == q1, q2)
q3, n3 = srv.normalise_mermaid_labels("Start((Start)) --> B[Plain]")
check("node-shape syntax is untouched", n3 == 0, q3)
q4, n4 = srv.normalise_mermaid_labels('A["already (quoted)"]')
check("already-quoted labels are untouched", n4 == 0, q4)
check("label text is unchanged apart from the quotes",
      "Reviewer(s) Assigned" in q1)

print("\nAUTOMATED RUNS MUST NOT OPEN A BROWSER")
check("LOCAL_ARTIFACTS_AUTO_OPEN=0 suppresses the browser",
      'LOCAL_ARTIFACTS_AUTO_OPEN' in (HERE / "server.py").read_text())

print("\nA BORDER MUST AT LEAST BE VISIBLE ON WHAT IT SITS ON")
# Not 3:1, only distinct from its fill (border.subtle.01 matched it once).
for mode in ("light", "dark"):
    m = THEME[mode]
    for ground in ("surface", "group_surface", "canvas"):
        check(f"{mode}/border differs from {ground}", m["border"] != m[ground],
              f'both {m["border"]}')

print("\nACCENTS STAY LEGIBLE ON EVERY GROUND THEY ARE DRAWN ON")
# Connectors and rails also sit inside group fills; gate all three.
for mode in ("light", "dark"):
    m = THEME[mode]
    for ground in ("canvas", "surface", "group_surface"):
        worst, which = min((contrast(v, m[ground]), k) for k, v in m["accent"].items())
        check(f"{mode}/weakest accent vs {ground} >= 3.0:1", worst >= 3.0,
              f"{which} {worst:.2f}:1")

print("\nGENERATED TOKENS ARE DTCG-CONFORMANT AND REPRODUCIBLE")
gen = json.loads((HERE / "themes/carbon.tokens.json").read_text())
authored = json.loads((HERE / "themes/artifact.tokens.json").read_text())
up = gen["$extensions"]["com.developsolutions.ailocal"]["upstream"]
check("generated file records its upstream package and version",
      up.get("package") == "@carbon/themes" and up.get("version"))
check("generated file records an integrity hash for the pinned tarball",
      str(up.get("integrity", "")).startswith("sha"))
check("upstream licence is recorded for attribution", up.get("license") == "Apache-2.0")


def colours(node: object, out: list[dict] | None = None) -> list[dict]:
    out = [] if out is None else out
    if isinstance(node, dict):
        if "$value" in node and isinstance(node["$value"], dict) \
                and "colorSpace" in node["$value"]:
            out.append(node["$value"])
        else:
            for k, v in node.items():
                if not k.startswith("$") or k == "$value":
                    colours(v, out)
    return out


vals = colours(gen)
check("generated file actually contains colour tokens", len(vals) > 0, str(len(vals)))
# DTCG 2025.10 colour: colorSpace and components REQUIRED, hex optional.
check("every colour declares a colorSpace",
      all(c.get("colorSpace") == "srgb" for c in vals))
check("every colour carries numeric components",
      all(isinstance(c.get("components"), list)
          and len(c["components"]) == 3
          and all(isinstance(n, (int, float)) for n in c["components"]) for c in vals))
check("hex fallback, where present, is 6-digit notation",
      all(re.fullmatch(r"#[0-9a-fA-F]{6}", c["hex"]) for c in vals if "hex" in c))
check("hex fallback agrees with the components it falls back for",
      all(c["hex"].lower() ==
          "#%02x%02x%02x" % tuple(round(n * 255) for n in c["components"])
          for c in vals if "hex" in c))

# No literal colours in the mapping, or values drift from the palette.
literal = re.findall(r'"#[0-9a-fA-F]{3,8}"', (HERE / "themes/artifact.tokens.json").read_text())
check("the authored mapping hard-codes no colour of its own", not literal, str(literal))
# Same claim for the renderer.
src = (HERE / "architecture.py").read_text()
hexes = [h for h in re.findall(r'"#[0-9a-fA-F]{3,8}"', src)]
check("the renderer hard-codes no colour", not hexes, str(hexes))

print(f"\n  {PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
