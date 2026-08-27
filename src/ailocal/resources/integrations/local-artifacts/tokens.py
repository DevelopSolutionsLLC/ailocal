"""Resolve the DTCG token files into the flat dict the renderers read.

Two files feed this, and the split is the whole point:

  themes/artifact.tokens.json  HAND-AUTHORED. Which semantic role each value
                               serves, plus typography, geometry, edge dash
                               patterns and type labels, which Carbon has no
                               opinion about.
  themes/carbon.tokens.json    GENERATED from a pinned @carbon/themes release
                               by tools/update_carbon_tokens.py. Values only.

Renderers get semantic names -- surface, ink, accent.client -- and never learn
that a value came from blue.60. Swapping the upstream is therefore a
regeneration, not a renderer change.

No network, no dependency: this reads two committed JSON files.
"""
import json
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
THEMES = HERE / "themes"
AUTHORED = THEMES / "artifact.tokens.json"
GENERATED = THEMES / "carbon.tokens.json"

_REF = re.compile(r"^\{([^}]+)\}$")


def _dig(tree, path):
    node = tree
    for part in path:
        if not isinstance(node, dict) or part not in node:
            raise KeyError(f"token path does not resolve: {'.'.join(path)}")
        node = node[part]
    return node


def _hex(colour):
    """DTCG colour object -> the 6-digit hex an SVG attribute wants.

    `hex` is optional in DTCG, so components are the authority and the fallback
    is only trusted when present.
    """
    if "hex" in colour:
        return colour["hex"].lower()
    r, g, b = (round(max(0.0, min(1.0, c)) * 255) for c in colour["components"][:3])
    return f"#{r:02x}{g:02x}{b:02x}"


def _resolve(value, roots, depth=0):
    if depth > 16:
        raise ValueError("circular token reference")
    if isinstance(value, str):
        m = _REF.match(value.strip())
        if not m:
            return value
        return _resolve(_dig(roots, m.group(1).split("."))["$value"], roots, depth + 1)
    if isinstance(value, dict):
        if "colorSpace" in value:
            return _hex(value)
        if "value" in value and "unit" in value:      # DTCG dimension
            return value["value"]
    return value


def _walk(node, roots):
    """DTCG tree -> plain values, dropping $-metadata and unwrapping tokens."""
    if not isinstance(node, dict):
        return node
    if "$value" in node:
        return _resolve(node["$value"], roots)
    return {k: _walk(v, roots) for k, v in node.items() if not k.startswith("$")}


def load():
    authored = json.loads(AUTHORED.read_text(encoding="utf-8"))
    roots = dict(authored)
    roots.update(json.loads(GENERATED.read_text(encoding="utf-8")))
    theme = _walk(authored, roots)

    # Typography and geometry are authored as DTCG dimensions; the renderers
    # have always spelled the pixel scalars with a _px suffix, so keep that
    # contract rather than churn every call site for a naming preference.
    typo = theme["typography"]
    theme["typography"] = {
        k if k in ("sans", "mono") else f"{k}_px":
        ", ".join(f"'{p}'" if " " in p else p for p in v) if isinstance(v, list) else v
        for k, v in typo.items()
    }
    return theme


def provenance():
    """Where the colour values came from. For --version output and the docs."""
    gen = json.loads(GENERATED.read_text(encoding="utf-8"))
    return gen["$extensions"]["com.developsolutions.ailocal"]["upstream"]
