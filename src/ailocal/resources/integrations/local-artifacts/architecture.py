"""architecture.py — structured architecture diagrams.

    architecture JSON  ->  validate  ->  size  ->  ELK (node)  ->  SVG

The model supplies MEANING: nodes, groups, edges, labels, semantic kinds.
This module supplies GEOMETRY and PRESENTATION: sizes, coordinates, routing,
typography, colour. The model never writes a coordinate, and the emitted
artifact contains no script at all -- it is static SVG.
"""

import json
import subprocess
import shutil
from pathlib import Path

HERE = Path(__file__).resolve().parent
LAYOUT_JS = HERE / "layout.mjs"

MAX_NODES = 60
MAX_EDGES = 120
MAX_LABEL = 120

NODE_KINDS = ("client", "service", "router", "runtime", "model",
              "database", "external", "tool", "default")
EDGE_KINDS = ("request", "inference", "tool", "data", "dependency", "default")


class SpecError(Exception):
    """A malformed specification. The message is shown to the model."""


# ── text metrics ──────────────────────────────────────────────────────────────
# Sizing and rendering MUST agree, so both go through these. Advances are
# deliberate over-estimates: too wide only adds whitespace, too narrow overflows
# the node, and overflow is the defect this whole module exists to remove.
# Advances are deliberate over-estimates: too wide only adds whitespace, too
# narrow overflows the node, and overflow is the defect this module exists to
# remove. Sizes come from the theme so the renderer has one source.
def _typo(k, d):
    import json as _j
    with open(Path(__file__).resolve().parent / "themes" / "artifact-default.json",
              encoding="utf-8") as f:
        return _j.load(f)["typography"].get(k, d)


TITLE_PX, TITLE_ADV = _typo("title_px", 13.0), 0.60
SUB_PX, SUB_ADV = _typo("subtitle_px", 10.5), 0.62
BADGE_PX, BADGE_ADV = _typo("badge_px", 9.0), 0.62
ELABEL_PX, ELABEL_ADV = _typo("edge_label_px", 10.5), 0.62
TYPE_PX, TYPE_ADV = _typo("type_label_px", 8.5), 0.66


def _w(text, px, adv):
    return len(text or "") * px * adv


def node_size(n):
    """Card geometry. The type label is its own row (C4: an element states its
    type), and the accent rail eats a few px on the left."""
    title = _w(n["label"], TITLE_PX, TITLE_ADV)
    sub = _w(n.get("subtitle"), SUB_PX, SUB_ADV)
    badge = _w(n.get("badge"), BADGE_PX, BADGE_ADV) + 16
    tlabel = _w(TYPE_LABELS.get(n["kind"], ""), TYPE_PX, TYPE_ADV)
    width = max(title, sub, badge, tlabel) + 42
    width = max(158.0, min(340.0, width))
    height = 62.0
    if TYPE_LABELS.get(n["kind"]):
        height += 14.0
    if n.get("subtitle"):
        height += 18.0
    if n.get("badge"):
        height += 20.0
    return round(width), round(height)


# ── validation ────────────────────────────────────────────────────────────────

def _text(v, field, required=True):
    if v is None or v == "":
        if required:
            raise SpecError(f"'{field}' is required and must be a non-empty string.")
        return None
    if not isinstance(v, str):
        raise SpecError(f"'{field}' must be a string, got {type(v).__name__}.")
    if len(v) > MAX_LABEL:
        raise SpecError(f"'{field}' is {len(v)} characters; the limit is {MAX_LABEL}. "
                        f"Use a short label and put detail in 'subtitle'.")
    if "\n" in v or "\r" in v:
        raise SpecError(f"'{field}' must be a single line.")
    return v


def validate(spec):
    """Returns (nodes, groups, edges, meta). Raises SpecError with a fixable message."""
    if not isinstance(spec, dict):
        raise SpecError("the architecture spec must be a JSON object.")

    raw_nodes = spec.get("nodes")
    if not isinstance(raw_nodes, list) or not raw_nodes:
        raise SpecError("'nodes' must be a non-empty array.")
    if len(raw_nodes) > MAX_NODES:
        raise SpecError(f"{len(raw_nodes)} nodes exceeds the limit of {MAX_NODES}.")

    raw_groups = spec.get("groups") or []
    if not isinstance(raw_groups, list):
        raise SpecError("'groups' must be an array if present.")

    raw_edges = spec.get("edges") or []
    if not isinstance(raw_edges, list):
        raise SpecError("'edges' must be an array.")
    if len(raw_edges) > MAX_EDGES:
        raise SpecError(f"{len(raw_edges)} edges exceeds the limit of {MAX_EDGES}.")

    groups = {}
    for i, g in enumerate(raw_groups):
        if not isinstance(g, dict):
            raise SpecError(f"groups[{i}] must be an object.")
        gid = _text(g.get("id"), f"groups[{i}].id")
        if gid in groups:
            raise SpecError(f"duplicate group id '{gid}'.")
        members = g.get("nodes") or g.get("members") or []
        if not isinstance(members, list):
            raise SpecError(f"groups[{i}].nodes must be an array of node ids.")
        groups[gid] = {"id": gid,
                       "label": _text(g.get("label"), f"groups[{i}].label",
                                      required=False) or gid,
                       "members": [m for m in members if isinstance(m, str)]}

    nodes = {}
    order = []
    for i, n in enumerate(raw_nodes):
        if not isinstance(n, dict):
            raise SpecError(f"nodes[{i}] must be an object.")
        nid = _text(n.get("id"), f"nodes[{i}].id")
        if nid in nodes:
            raise SpecError(f"duplicate node id '{nid}'. Every node id must be unique.")
        if nid in groups:
            raise SpecError(f"'{nid}' is used as both a node id and a group id.")
        kind = n.get("kind") or "default"
        if kind not in NODE_KINDS:
            raise SpecError(f"nodes[{i}].kind '{kind}' is not valid. "
                            f"Use one of: {', '.join(NODE_KINDS)}.")
        grp = n.get("group")
        if grp is not None:
            grp = _text(grp, f"nodes[{i}].group")
            if grp not in groups:
                raise SpecError(f"node '{nid}' refers to group '{grp}', which is not "
                                f"declared in 'groups'.")
        # Membership may be declared from either end. [REAL] asked for an
        # architecture diagram, gemma4:26b-mlx used groups[].nodes rather than
        # nodes[].group; accepting only one form silently produced a diagram
        # with no boundaries at all.
        if grp is None:
            for gid_, g_ in groups.items():
                if nid in g_["members"]:
                    grp = gid_
                    break
        nodes[nid] = {
            "id": nid,
            "label": _text(n.get("label"), f"nodes[{i}].label"),
            "subtitle": _text(n.get("subtitle"), f"nodes[{i}].subtitle", required=False),
            "badge": _text(n.get("badge"), f"nodes[{i}].badge", required=False),
            "kind": kind,
            "group": grp,
        }
        order.append(nid)

    edges = []
    for i, e in enumerate(raw_edges):
        if not isinstance(e, dict):
            raise SpecError(f"edges[{i}] must be an object.")
        src = _text(e.get("from") or e.get("source"), f"edges[{i}].from")
        dst = _text(e.get("to") or e.get("target"), f"edges[{i}].to")
        for end, val in (("from", src), ("to", dst)):
            if val not in nodes:
                raise SpecError(f"edges[{i}].{end} is '{val}', which is not a declared "
                                f"node id. Declared nodes: {', '.join(order[:12])}"
                                + (" ..." if len(order) > 12 else ""))
        kind = e.get("kind") or "default"
        if kind not in EDGE_KINDS:
            raise SpecError(f"edges[{i}].kind '{kind}' is not valid. "
                            f"Use one of: {', '.join(EDGE_KINDS)}.")
        edges.append({"id": f"e{i}", "from": src, "to": dst, "kind": kind,
                      "label": _text(e.get("label"), f"edges[{i}].label", required=False)})

    for gid, g in groups.items():
        for m in g["members"]:
            if m not in nodes:
                raise SpecError(f"group '{gid}' lists node '{m}', which is not "
                                f"declared in 'nodes'.")
        if not any(n["group"] == gid for n in nodes.values()):
            raise SpecError(f"group '{gid}' has no members. Put the group id on "
                            f"each node as \"group\": \"{gid}\", or list the node "
                            f"ids in the group as \"nodes\": [...].")

    meta = {
        "title": _text(spec.get("title"), "title", required=False),
        "direction": (spec.get("direction") or "RIGHT").upper(),
    }
    if meta["direction"] not in ("RIGHT", "DOWN"):
        raise SpecError("'direction' must be 'RIGHT' or 'DOWN'.")
    return nodes, groups, edges, meta


# ── layout ────────────────────────────────────────────────────────────────────

def run_layout(nodes, groups, edges, meta):
    used_groups = [g for g in groups if any(n["group"] == g for n in nodes.values())]
    children = []
    for gid in used_groups:
        kids = [n for n in nodes.values() if n["group"] == gid]
        children.append({
            "id": f"__grp__{gid}",
            "layoutOptions": {"elk.padding": "[top=40,left=22,bottom=22,right=22]"},
            "children": [_elk_node(n) for n in kids],
        })
    children += [_elk_node(n) for n in nodes.values() if n["group"] is None]

    graph = {
        "id": "root",
        "layoutOptions": {
            "elk.algorithm": "layered",
            "elk.direction": meta["direction"],
            "elk.edgeRouting": "ORTHOGONAL",
            "elk.hierarchyHandling": "INCLUDE_CHILDREN",
            "elk.spacing.nodeNode": "44",
            "elk.layered.spacing.nodeNodeBetweenLayers": "80",
            "elk.spacing.edgeNode": "26",
            "elk.spacing.edgeEdge": "18",
            "elk.spacing.edgeLabel": "8",
            "elk.layered.spacing.edgeNodeBetweenLayers": "26",
            "elk.padding": "[top=24,left=24,bottom=24,right=24]",
            "elk.layered.nodePlacement.strategy": "NETWORK_SIMPLEX",
            "elk.layered.considerModelOrder.strategy": "NODES_AND_EDGES",
        },
        "children": children,
        "edges": [_elk_edge(e) for e in edges],
    }

    node_exe = shutil.which("node")
    if not node_exe:
        raise SpecError("architecture diagrams need Node.js on PATH to compute layout, "
                        "and it was not found. Publish an html artifact instead.")
    try:
        out = subprocess.run([node_exe, str(LAYOUT_JS)], input=json.dumps(graph),
                             capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        raise SpecError("layout timed out. Try a smaller diagram.")
    if out.returncode != 0:
        raise SpecError("layout engine failed: " + (out.stderr or "")[-300:])
    return json.loads(out.stdout)


def _elk_node(n):
    w, h = node_size(n)
    return {"id": n["id"], "width": w, "height": h}


def _elk_edge(e):
    d = {"id": e["id"], "sources": [e["from"]], "targets": [e["to"]]}
    if e["label"]:
        d["labels"] = [{"text": e["label"],
                        "width": round(_w(e["label"], ELABEL_PX, ELABEL_ADV)) + 10,
                        "height": 16}]
    return d


# ── design system ─────────────────────────────────────────────────────────────
# Every colour, typeface and measurement comes from themes/artifact-default.json.
# The model supplies semantics only.
#
# C4's notation guidance drives two rules here (c4model.com/diagrams/notation):
#   "every element must explicitly state its type", and be legible in black and
#   white / to colourblind readers. So KIND IS RENDERED AS A TEXT LABEL and
#   colour is only an accent -- which is also why runtime and model may sit in
#   the same hue family without becoming ambiguous. Edge kinds carry a dash
#   pattern for the same redundancy, and every diagram gets a key.

THEME_PATH = HERE / "themes" / "artifact-default.json"
with open(THEME_PATH, encoding="utf-8") as _f:
    THEME = json.load(_f)

TYPO = THEME["typography"]
GEO = THEME["geometry"]
EDGE_KINDS = THEME["edge_kinds"]
TYPE_LABELS = THEME["type_labels"]
ACCENTS = tuple(THEME["light"]["accent"])


def _css_vars(mode):
    t = THEME[mode]
    out = [f"--canvas:{t['canvas']}", f"--surface:{t['surface']}",
           f"--group:{t['group_surface']}", f"--ink:{t['ink']}",
           f"--muted:{t['muted']}", f"--faint:{t['faint']}",
           f"--border:{t['border']}"]
    for k, v in t["accent"].items():
        out.append(f"--a-{k}:{v}")
    return ";".join(out) + ";"


CSS = """
:root{%LIGHT%}
@media (prefers-color-scheme: dark){:root:not([data-theme="light"]){%DARK%}}
:root[data-theme="dark"]{%DARK%}
*{box-sizing:border-box}
body{margin:0;background:var(--canvas);color:var(--ink);font-family:%SANS%}
.wrap{max-width:1400px;margin:0 auto;padding:32px 24px 48px}
h1{font-size:19px;font-weight:640;letter-spacing:-.01em;margin:0 0 4px;text-wrap:balance}
.scope{font-size:12.5px;color:var(--muted);margin:0 0 20px;font-family:%MONO%}
.figure{background:var(--surface);border:1px solid var(--border);border-radius:10px;
  padding:22px;overflow-x:auto}
svg.diagram{display:block;width:100%;height:auto}
.legend{display:flex;flex-wrap:wrap;gap:8px 20px;margin-top:16px;padding-top:14px;
  border-top:1px solid var(--border);font-size:11.5px;color:var(--muted)}
.legend .item{display:flex;align-items:center;gap:7px}
.legend .sw{width:22px;height:0;border-top-width:2px;border-top-style:solid}
.legend .dot{width:10px;height:10px;border-radius:2px;border-left-width:3px;
  border-left-style:solid;background:var(--surface);border-top:1px solid var(--border);
  border-right:1px solid var(--border);border-bottom:1px solid var(--border)}
/* var() is not supported in SVG presentation attributes, only through CSS. */
.t-mono{font-family:%MONO%} .t-sans{font-family:%SANS%}
.f-ink{fill:var(--ink)} .f-muted{fill:var(--muted)} .f-faint{fill:var(--faint)}
.n-card{fill:var(--surface);stroke:var(--border)}
.g-box{fill:var(--group);stroke:var(--border);stroke-dasharray:5 4}
.g-label{fill:var(--faint);letter-spacing:.1em}
.e-label-bg{fill:var(--surface)}
.n-title{fill:var(--ink);font-weight:600}
"""
for _k in ACCENTS:
    CSS += (f".rail-{_k}{{fill:var(--a-{_k})}}\n"
            f".type-{_k}{{fill:var(--a-{_k});letter-spacing:.09em}}\n"
            f".ek-{_k}{{stroke:var(--a-{_k});fill:none}}\n"
            f".et-{_k}{{fill:var(--a-{_k})}}\n"
            f".ah-{_k}{{fill:var(--a-{_k})}}\n")
CSS = (CSS.replace("%LIGHT%", _css_vars("light"))
          .replace("%DARK%", _css_vars("dark"))
          .replace("%SANS%", TYPO["sans"]).replace("%MONO%", TYPO["mono"]))


def accent_for(kind):
    return kind if kind in THEME["light"]["accent"] else "default"


def esc(t):
    return (str(t).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


# ── SVG ───────────────────────────────────────────────────────────────────────

def _rounded_path(pts, r=9):
    """Orthogonal polyline with rounded corners. ELK gives axis-aligned bend
    points, so each corner is a quarter turn we can soften without changing the
    route it computed."""
    if len(pts) < 2:
        return ""
    d = [f"M {pts[0][0]:.1f} {pts[0][1]:.1f}"]
    for i in range(1, len(pts) - 1):
        (px, py), (cx, cy), (nx, ny) = pts[i - 1], pts[i], pts[i + 1]
        d1x, d1y = cx - px, cy - py
        d2x, d2y = nx - cx, ny - cy
        l1 = (d1x ** 2 + d1y ** 2) ** 0.5 or 1
        l2 = (d2x ** 2 + d2y ** 2) ** 0.5 or 1
        rr = min(r, l1 / 2, l2 / 2)
        d.append(f"L {cx - d1x / l1 * rr:.1f} {cy - d1y / l1 * rr:.1f}")
        d.append(f"Q {cx:.1f} {cy:.1f} {cx + d2x / l2 * rr:.1f} {cy + d2y / l2 * rr:.1f}")
    d.append(f"L {pts[-1][0]:.1f} {pts[-1][1]:.1f}")
    return " ".join(d)


def render_svg(nodes, groups, edges, meta, laid):
    """Semantics in, presentation out. Neutral card + accent rail + explicit type
    label, per C4's rule that an element states its type and that notation must
    survive black-and-white printing."""
    pos = {n["id"]: n for n in laid["nodes"]}
    W, H = laid["width"], laid["height"]
    used_kinds, used_edges = [], []
    parts = []

    defs = []
    for k in ACCENTS:
        defs.append(
            f'<marker id="ah-{k}" viewBox="0 0 10 10" refX="9" refY="5" '
            f'markerWidth="6.5" markerHeight="6.5" orient="auto-start-reverse">'
            f'<path class="ah-{k}" d="M 0 1 L 10 5 L 0 9 z"/></marker>')
    parts.append("<defs>" + "".join(defs) + "</defs>")

    for gid, g in groups.items():
        pg = pos.get(f"__grp__{gid}")
        if not pg:
            continue
        parts.append(f'<rect class="g-box" x="{pg["x"]:.0f}" y="{pg["y"]:.0f}" '
                     f'width="{pg["width"]:.0f}" height="{pg["height"]:.0f}" '
                     f'rx="{GEO["group_radius"]}" stroke-width="{GEO["group_stroke_px"]}"/>')
        parts.append(f'<text class="t-mono g-label" x="{pg["x"] + 15:.0f}" '
                     f'y="{pg["y"] + 23:.0f}" font-size="{TYPO["group_label_px"]}">'
                     f'{esc(g["label"].upper())}</text>')

    for e in edges:
        le = next((x for x in laid["edges"] if x["id"] == e["id"]), None)
        if not le or len(le["points"]) < 2:
            continue
        spec = EDGE_KINDS.get(e["kind"], EDGE_KINDS["default"])
        acc = accent_for(spec["accent"])
        if e["kind"] not in used_edges:
            used_edges.append(e["kind"])
        da = f' stroke-dasharray="{spec["dash"]}"' if spec["dash"] != "none" else ""
        parts.append(f'<path class="e-line ek-{acc}" d="{_rounded_path(le["points"])}"{da} '
                     f'stroke-width="{GEO["edge_stroke_px"]}" '
                     f'marker-end="url(#ah-{acc})"/>')

    for e in edges:
        le = next((x for x in laid["edges"] if x["id"] == e["id"]), None)
        if not le or not le.get("label") or not e["label"]:
            continue
        acc = accent_for(EDGE_KINDS.get(e["kind"], EDGE_KINDS["default"])["accent"])
        lb = le["label"]
        tw = _w(e["label"], ELABEL_PX, ELABEL_ADV)
        cx, cy = lb["x"] + lb["width"] / 2, lb["y"] + lb["height"] / 2
        parts.append(f'<rect class="e-label-bg" x="{cx - tw / 2 - 5:.1f}" '
                     f'y="{cy - 8:.1f}" width="{tw + 10:.1f}" height="16" rx="3"/>')
        parts.append(f'<text class="t-mono e-label et-{acc}" x="{cx:.1f}" y="{cy + 4:.1f}" '
                     f'text-anchor="middle" font-size="{ELABEL_PX}">{esc(e["label"])}</text>')

    for nid, n in nodes.items():
        pn = pos.get(nid)
        if not pn:
            continue
        kind = n["kind"]
        acc = accent_for(kind)
        if kind not in used_kinds:
            used_kinds.append(kind)
        x, y, w, h = pn["x"], pn["y"], pn["width"], pn["height"]
        cx = x + w / 2
        r = GEO["node_radius"]
        rail = GEO["accent_rail_px"]
        parts.append(f'<rect class="n-card" x="{x:.0f}" y="{y:.0f}" width="{w:.0f}" '
                     f'height="{h:.0f}" rx="{r}" stroke-width="{GEO["node_stroke_px"]}"/>')
        # accent rail: colour as a stripe, never a saturated fill behind text
        parts.append(f'<path class="rail-{acc}" d="M {x:.1f} {y + r:.1f} '
                     f'a {r} {r} 0 0 1 {r} {-r} l {rail - r} 0 l 0 {h:.1f} '
                     f'l {r - rail} 0 a {r} {r} 0 0 1 {-r} {-r} z"/>')
        ty = y + 22
        tl = TYPE_LABELS.get(kind, "")
        if tl:
            parts.append(f'<text class="t-mono type-{acc}" x="{x + 14:.0f}" y="{ty:.0f}" '
                         f'font-size="{TYPE_PX}">{esc(tl)}</text>')
            ty += 17
        parts.append(f'<text class="t-sans n-title" x="{cx:.0f}" y="{ty:.0f}" '
                     f'text-anchor="middle" font-size="{TITLE_PX}">{esc(n["label"])}</text>')
        if n.get("subtitle"):
            ty += 17
            parts.append(f'<text class="t-mono n-sub f-muted" x="{cx:.0f}" y="{ty:.0f}" '
                         f'text-anchor="middle" font-size="{SUB_PX}">{esc(n["subtitle"])}</text>')
        if n.get("badge"):
            by = y + h - 22
            parts.append(f'<text class="t-mono n-badge-text f-faint" x="{cx:.0f}" y="{by + 10:.0f}" '
                         f'text-anchor="middle" font-size="{BADGE_PX}">'
                         f'{esc(n["badge"].upper())}</text>')

    aria = meta.get("title") or "architecture diagram"
    svg = (f'<svg class="diagram" viewBox="0 0 {W:.0f} {H:.0f}" role="img" '
           f'aria-label="{esc(aria)}" xmlns="http://www.w3.org/2000/svg">'
           + "".join(parts) + "</svg>")

    # C4: every diagram carries a key.
    legend = []
    for k in used_kinds:
        acc = accent_for(k)
        legend.append(f'<span class="item"><span class="dot" '
                      f'style="border-left-color:var(--a-{acc})"></span>'
                      f'{esc(TYPE_LABELS.get(k, k) or k)}</span>')
    for k in used_edges:
        spec = EDGE_KINDS.get(k, EDGE_KINDS["default"])
        acc = accent_for(spec["accent"])
        style = "dashed" if spec["dash"] != "none" else "solid"
        legend.append(f'<span class="item"><span class="sw" '
                      f'style="border-top-color:var(--a-{acc});border-top-style:{style}">'
                      f'</span>{esc(spec["label"])}</span>')

    title = meta.get("title") or "Architecture"
    scope = f"{len(nodes)} elements &middot; {len(edges)} relationships"
    if groups:
        scope += f" &middot; {len(groups)} boundaries"
    return (f'<!DOCTYPE html>\n<html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<style>{CSS}</style></head><body><div class="wrap">'
            f'<h1>{esc(title)}</h1><p class="scope">{scope}</p>'
            f'<div class="figure">{svg}</div>'
            f'<div class="legend">{"".join(legend)}</div>'
            f'</div></body></html>')


def build(spec):
    """Full pipeline. Raises SpecError with a message the model can act on."""
    nodes, groups, edges, meta = validate(spec)
    laid = run_layout(nodes, groups, edges, meta)
    return render_svg(nodes, groups, edges, meta, laid)
