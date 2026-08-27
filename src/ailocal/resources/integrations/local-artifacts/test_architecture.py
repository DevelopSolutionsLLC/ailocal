#!/usr/bin/env python3
"""Tests for the architecture + mermaid formats and their validation."""
import json, sys, tempfile, os
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
import architecture as A
import check_diagram

PASS = FAIL = 0
def check(name, cond, detail=""):
    global PASS, FAIL
    if cond: PASS += 1; print(f"  PASS  {name}")
    else: FAIL += 1; print(f"  FAIL  {name}  {detail}")

def spec_err(spec):
    try:
        A.validate(spec); return None
    except A.SpecError as e:
        return str(e)

BASE = {"nodes": [{"id": "a", "label": "A"}, {"id": "b", "label": "B"}],
        "edges": [{"from": "a", "to": "b"}]}

print("=== validation rejects malformed specs ===")
check("duplicate node id", "duplicate node id" in (spec_err(
    {"nodes":[{"id":"a","label":"A"},{"id":"a","label":"B"}]}) or ""))
check("edge to missing node", "not a declared node id" in (spec_err(
    {"nodes":[{"id":"a","label":"A"}],"edges":[{"from":"a","to":"ghost"}]}) or ""))
check("undeclared group", "not\n" not in "" and "is not declared" in (spec_err(
    {"nodes":[{"id":"a","label":"A","group":"nope"}]}) or ""))
check("invalid node kind", "is not valid" in (spec_err(
    {"nodes":[{"id":"a","label":"A","kind":"wizard"}]}) or ""))
check("invalid edge kind", "is not valid" in (spec_err(
    {"nodes":[{"id":"a","label":"A"},{"id":"b","label":"B"}],
     "edges":[{"from":"a","to":"b","kind":"telepathy"}]}) or ""))
check("empty nodes", "non-empty array" in (spec_err({"nodes":[]}) or ""))
check("not an object", "must be a JSON object" in (spec_err([1,2]) or ""))
check("missing label", "is required" in (spec_err({"nodes":[{"id":"a"}]}) or ""))
check("pathological label length", "the limit is" in (spec_err(
    {"nodes":[{"id":"a","label":"x"*200}]}) or ""))
check("multiline label", "single line" in (spec_err(
    {"nodes":[{"id":"a","label":"a\nb"}]}) or ""))
check("too many nodes", "exceeds the limit" in (spec_err(
    {"nodes":[{"id":f"n{i}","label":f"N{i}"} for i in range(A.MAX_NODES+1)]}) or ""))
check("id reused as group id", "both a node id and a group id" in (spec_err(
    {"groups":[{"id":"x","label":"X"}],"nodes":[{"id":"x","label":"X"}]}) or ""))
check("bad direction", "'direction' must be" in (spec_err(
    dict(BASE, direction="SIDEWAYS")) or ""))
check("valid spec accepted", spec_err(BASE) is None, spec_err(BASE) or "")

print("\n=== layout + render ===")
FULL = {
  "title": "ailocal request routing", "direction": "RIGHT",
  "groups": [{"id":"ailocal","label":"ailocal"},{"id":"models","label":"local models"}],
  "nodes": [
    {"id":"cc","label":"Claude Code","kind":"client","subtitle":"claude-local"},
    {"id":"ll","label":"LiteLLM","kind":"service","group":"ailocal","subtitle":"127.0.0.1:4000"},
    {"id":"rr","label":"Role routing","kind":"router","group":"ailocal"},
    {"id":"ol","label":"Ollama","kind":"runtime","group":"models","badge":"MLX"},
    {"id":"gm","label":"gemma4:26b-mlx","kind":"model","group":"models"},
    {"id":"mcp","label":"MCP tools","kind":"tool","subtitle":"stdio"}],
  "edges": [
    {"from":"cc","to":"ll","kind":"request","label":"/v1/messages"},
    {"from":"ll","to":"rr","kind":"inference"},
    {"from":"rr","to":"ol","kind":"inference","label":"num_ctx"},
    {"from":"ol","to":"gm","kind":"inference"},
    {"from":"cc","to":"mcp","kind":"tool","label":"stdio"}],
}
html = A.build(FULL)
tmp = Path(tempfile.mkdtemp()) / "arch.html"
tmp.write_text(html)
check("renders a document", html.startswith("<!DOCTYPE html>") and "<svg" in html)
check("contains NO script at all (static SVG)", "<script" not in html, "script found")
check("no external subresource", "//" not in html.replace("http://www.w3.org/2000/svg",""),
      "external ref present")
check("has a legend", 'class="legend"' in html)
check("supports dark theme", "prefers-color-scheme" in html)
check("MCP path is dashed, inference is not",
      'stroke-dasharray' in html and html.count("ek-tool") >= 1)
check("kind is a visible TYPE LABEL, not colour alone (C4 / colourblind-safe)",
      "CLIENT" in html and "SERVICE" in html and 'class="t-mono type-' in html)
check("nodes are neutral cards with an accent rail, not saturated fills",
      'class="n-card"' in html and 'class="rail-' in html)
check("every colour comes from the theme file, none inline in the renderer",
      not __import__("re").findall(r"#[0-9a-fA-F]{6}",
          open(Path(__file__).parent / "architecture.py").read()))

fails, stats = check_diagram.check(str(tmp), expect_edges=5,
    expect_nodes=["Claude Code","LiteLLM","Role routing","Ollama","gemma4:26b-mlx","MCP tools"])
print(f"        geometry: {stats}")
check("geometry gate: no overlaps or collisions", not fails, "; ".join(fails[:3]))

print("\n=== direction DOWN also lays out cleanly ===")
d = A.build(dict(FULL, direction="DOWN"))
tmp2 = Path(tempfile.mkdtemp()) / "down.html"; tmp2.write_text(d)
f2, s2 = check_diagram.check(str(tmp2), expect_edges=5)
check("DOWN geometry clean", not f2, "; ".join(f2[:3]))

print("\n=== scale ===")
big = {"nodes":[{"id":f"n{i}","label":f"Service {i}","kind":"service"} for i in range(24)],
       "edges":[{"from":f"n{i}","to":f"n{i+1}","kind":"data"} for i in range(23)]}
b = A.build(big)
tmp3 = Path(tempfile.mkdtemp()) / "big.html"; tmp3.write_text(b)
f3, s3 = check_diagram.check(str(tmp3), expect_edges=23)
check("24-node graph stays clean", not f3, "; ".join(f3[:3]))
print(f"        {s3}")

print("\n=== cycles do not break layout ===")
cyc = {"nodes":[{"id":"a","label":"A"},{"id":"b","label":"B"},{"id":"c","label":"C"}],
       "edges":[{"from":"a","to":"b"},{"from":"b","to":"c"},{"from":"c","to":"a"}]}
try:
    h = A.build(cyc)
    tmp4 = Path(tempfile.mkdtemp()) / "cyc.html"; tmp4.write_text(h)
    f4, _ = check_diagram.check(str(tmp4), expect_edges=3)
    check("cyclic graph renders cleanly", not f4, "; ".join(f4[:3]))
except A.SpecError as e:
    check("cyclic graph renders cleanly", False, str(e))

print(f"\n{'='*46}\n  PASS {PASS}   FAIL {FAIL}\n{'='*46}")
sys.exit(1 if FAIL else 0)
