#!/usr/bin/env python3
"""check_diagram.py — objective geometry gate for architecture artifacts.

Adapted from the checker written for the hosted-Claude diagram benchmark. It
answers the question a screenshot cannot: are any two things on top of each
other. Parses the generated SVG and measures, rather than trusting the renderer.

    python3 check_diagram.py <artifact.html> [--expect-nodes a,b] [--expect-edges 4]
"""
import re
import sys

TITLE_PX, TITLE_ADV = 13.0, 0.60
SUB_PX, SUB_ADV = 10.5, 0.62
BADGE_PX, BADGE_ADV = 9.0, 0.62
TYPE_PX, TYPE_ADV = 8.5, 0.66
ELABEL_PX, ELABEL_ADV = 10.5, 0.62


def _attrs(tag, svg):
    out = []
    for m in re.finditer(r"<%s\b([^>]*)>" % tag, svg):
        a = dict(re.findall(r'([\w:-]+)="([^"]*)"', m.group(1)))
        if tag == "text":
            end = svg.index("</text>", m.end())
            a["_txt"] = re.sub(r"&[#\w]+;", "X", svg[m.end():end])
        out.append(a)
    return out


def _f(d, k, dflt=0.0):
    try:
        return float(d.get(k, dflt))
    except (TypeError, ValueError):
        return dflt


def check(path, expect_nodes=None, expect_edges=None):
    doc = open(path, encoding="utf-8").read()
    if "<svg" not in doc:
        return ["no <svg> found in the artifact"], {}
    svg = doc[doc.index("<svg"):doc.index("</svg>")]
    vb = re.search(r'viewBox="0 0 ([\d.]+) ([\d.]+)"', svg)
    if not vb:
        return ["svg has no viewBox"], {}
    VW, VH = float(vb.group(1)), float(vb.group(2))

    rects = _attrs("rect", svg)
    texts = _attrs("text", svg)
    paths = _attrs("path", svg)

    def box(r):
        x, y = _f(r, "x"), _f(r, "y")
        return (x, y, x + _f(r, "width"), y + _f(r, "height"), r.get("class", ""))

    nodes = [box(r) for r in rects if "n-card" in r.get("class", "")]
    groups = [box(r) for r in rects if "g-box" in r.get("class", "")]
    plates = [box(r) for r in rects if "e-label-bg" in r.get("class", "")]

    def tbox(t):
        cls = t.get("class", "")
        if "n-title" in cls:
            px, adv = TITLE_PX, TITLE_ADV
        elif "n-sub" in cls:
            px, adv = SUB_PX, SUB_ADV
        elif "n-badge-text" in cls or "g-label" in cls:
            px, adv = BADGE_PX, BADGE_ADV
        elif "type-" in cls:
            px, adv = TYPE_PX, TYPE_ADV
        else:
            px, adv = ELABEL_PX, ELABEL_ADV
        ls = _f(t, "letter-spacing")
        n = len(t["_txt"].strip())
        w = n * (px * adv + ls)
        x, y = _f(t, "x"), _f(t, "y")
        anchor = t.get("text-anchor", "start")
        x0 = x if anchor == "start" else (x - w if anchor == "end" else x - w / 2)
        return (x0, y - px * 0.8, x0 + w, y + px * 0.25, t["_txt"].strip(), cls)

    tb = [tbox(t) for t in texts]

    def overlap(a, b):
        return not (a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1])

    def inside(a, b):   # a inside b
        return a[0] >= b[0] and a[1] >= b[1] and a[2] <= b[2] and a[3] <= b[3]

    fails = []

    for i in range(len(nodes)):
        for j in range(i + 1, len(nodes)):
            if overlap(nodes[i], nodes[j]):
                fails.append(f"node boxes overlap: {nodes[i][:4]} vs {nodes[j][:4]}")

    for g in groups:
        for n in nodes:
            if overlap(g, n) and not inside(n, g):
                fails.append(f"node straddles a group boundary: {n[:4]} vs group {g[:4]}")

    for i in range(len(groups)):
        for j in range(i + 1, len(groups)):
            if overlap(groups[i], groups[j]):
                fails.append(f"group boundaries overlap: {groups[i][:4]} vs {groups[j][:4]}")

    for t in tb:
        if "e-label" in t[5]:
            continue
        host = [n for n in nodes + groups if overlap(t, n)]
        if host and not any(inside(t, h) for h in host):
            fails.append(f"text {t[4]!r} escapes its box")
        if t[0] < -1 or t[2] > VW + 1 or t[1] < -1 or t[3] > VH + 1:
            fails.append(f"text {t[4]!r} outside viewBox")

    for i in range(len(tb)):
        for j in range(i + 1, len(tb)):
            if overlap(tb[i], tb[j]):
                fails.append(f"text collision: {tb[i][4]!r} vs {tb[j][4]!r}")

    for p in plates:
        for n in nodes:
            if overlap(p, n):
                fails.append(f"edge label plate {p[:4]} overlaps node {n[:4]}")

    for n in nodes + groups:
        if n[0] < -1 or n[1] < -1 or n[2] > VW + 1 or n[3] > VH + 1:
            fails.append(f"box outside viewBox: {n[:4]} (viewBox {VW}x{VH})")

    edge_paths = [p for p in paths if "e-line" in p.get("class", "")]
    if expect_edges is not None and len(edge_paths) != expect_edges:
        fails.append(f"expected {expect_edges} edges, drew {len(edge_paths)}")
    if not all("marker-end" in p for p in edge_paths):
        fails.append("an edge has no arrowhead (direction not shown)")

    labels = {t[4] for t in tb}
    for want in (expect_nodes or []):
        if want not in labels:
            fails.append(f"expected node label {want!r} not present")

    stats = {
        "viewBox": f"{VW:.0f}x{VH:.0f}", "nodes": len(nodes), "groups": len(groups),
        "edges": len(edge_paths), "texts": len(tb),
        "edge_kinds": len({p.get("class", "") for p in edge_paths}),
        "dashed_edges": sum(1 for p in edge_paths if "stroke-dasharray" in p),
    }
    return fails, stats


if __name__ == "__main__":
    path = sys.argv[1]
    exp_nodes = exp_edges = None
    for i, a in enumerate(sys.argv):
        if a == "--expect-nodes":
            exp_nodes = [x.strip() for x in sys.argv[i + 1].split(",")]
        if a == "--expect-edges":
            exp_edges = int(sys.argv[i + 1])
    fails, stats = check(path, exp_nodes, exp_edges)
    for k, v in stats.items():
        print(f"  {k}: {v}")
    print()
    if fails:
        for f in fails:
            print("  FAIL " + f)
        print(f"\n  {len(fails)} geometry problems")
        sys.exit(1)
    print("  PASS  no overlaps, no collisions, nothing clipped")
