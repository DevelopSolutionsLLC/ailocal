#!/usr/bin/env python3
"""Regenerate themes/carbon.tokens.json from a PINNED @carbon/themes release.

This is a DEVELOPER command. Nothing in the artifact runtime calls it, and
rendering never touches the network -- the generated file is committed.

    python3 tools/update_carbon_tokens.py            # regenerate at the pin
    python3 tools/update_carbon_tokens.py --check     # fail if the tree is stale
    python3 tools/update_carbon_tokens.py --pin 11.81.0

Why a generator and not a hand-copied palette: @carbon/themes ships its tokens
as DTCG JSON (src/dtcg/), so the values arrive from IBM's source of truth
instead of someone's eyedropper. We resolve ONLY the tokens the authored file
actually aliases, which keeps a 255 KB upstream down to the subset we use.

Determinism: output carries the version and the npm integrity hash but NO
timestamp, so regenerating at the same pin is byte-identical and provenance is
reproducible.
"""
import argparse
import base64
import hashlib
import io
import json
import re
import sys
import tarfile
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
AUTHORED = HERE / "themes/artifact.tokens.json"
GENERATED = HERE / "themes/carbon.tokens.json"

PIN = "11.80.0"
PKG = "@carbon/themes"
# Which upstream theme backs each of our two modes. g10 pairs a gray.10 ground
# with white layers; g100 is its dark counterpart (gray.100 ground, gray.90
# layers). That inversion is what keeps the two modes structurally identical.
THEME_FILES = {"light": "g10.json", "dark": "g100.json"}
PALETTE_FILE = "color-palette.json"

REF = re.compile(r"^\{([^}]+)\}$")


def fetch(pin):
    """Return (palette, {mode: theme}, integrity) from the pinned npm tarball."""
    meta_url = f"https://registry.npmjs.org/{PKG.replace('/', '%2f')}/{pin}"
    with urllib.request.urlopen(meta_url, timeout=60) as r:
        meta = json.load(r)
    dist = meta["dist"]
    integrity = dist.get("integrity") or ("sha1-" + dist["shasum"])
    with urllib.request.urlopen(dist["tarball"], timeout=120) as r:
        blob = r.read()

    # Verify before trusting a single byte of it.
    algo, _, b64 = integrity.partition("-")
    if algo.startswith("sha"):
        digest = hashlib.new(algo, blob).digest()
        if base64.b64decode(b64) != digest and algo != "sha1":
            sys.exit(f"integrity mismatch for {PKG}@{pin}")

    out = {}
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tf:
        for name, key in [(PALETTE_FILE, "palette")] + [
                (v, k) for k, v in THEME_FILES.items()]:
            m = tf.extractfile(f"package/src/dtcg/{name}")
            if m is None:
                sys.exit(f"{PKG}@{pin} does not contain src/dtcg/{name}")
            out[key] = json.load(m)
    return out["palette"], {k: out[k] for k in THEME_FILES}, integrity


def dig(tree, path):
    node = tree
    for part in path:
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def resolve(value, palette, depth=0):
    """Follow a Carbon alias chain down to a concrete DTCG colour object."""
    if depth > 16:
        sys.exit("circular reference in upstream tokens")
    if isinstance(value, str):
        m = REF.match(value.strip())
        if not m:
            sys.exit(f"unexpected non-reference upstream value: {value!r}")
        target = dig(palette, m.group(1).split("."))
        if target is None:
            sys.exit(f"upstream reference does not resolve: {value}")
        return resolve(target["$value"], palette, depth + 1)
    if isinstance(value, dict) and "colorSpace" in value:
        return value
    sys.exit(f"unrecognised upstream value shape: {value!r}")


def wanted(node, out=None):
    """Every {carbon....} alias the authored file actually uses."""
    out = set() if out is None else out
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "$value" and isinstance(v, str):
                m = REF.match(v.strip())
                if m and m.group(1).startswith("carbon."):
                    out.add(m.group(1)[len("carbon."):])
            else:
                wanted(v, out)
    elif isinstance(node, list):
        for v in node:
            wanted(v, out)
    return out


def insert(tree, path, token):
    node = tree
    for part in path[:-1]:
        node = node.setdefault(part, {})
    node[path[-1]] = token


def build(pin):
    palette, themes, integrity = fetch(pin)
    refs = wanted(json.loads(AUTHORED.read_text()))
    if not refs:
        sys.exit("the authored file aliases no Carbon tokens -- nothing to generate")

    tokens = {}
    for ref in sorted(refs):
        parts = ref.split(".")
        scope, rest = parts[0], parts[1:]
        if scope == "palette":
            src = dig(palette, rest)
            origin = f"{PALETTE_FILE}:{'.'.join(rest)}"
        elif scope in themes:
            src = dig(themes[scope], rest)
            origin = f"{THEME_FILES[scope]}:{'.'.join(rest)}"
        else:
            sys.exit(f"authored file references unknown Carbon scope: carbon.{ref}")
        if src is None or "$value" not in src:
            sys.exit(f"carbon.{ref} is not a token in {PKG}@{pin}")
        colour = resolve(src["$value"], palette)
        insert(tokens, parts, {
            "$type": "color",
            "$value": colour,
            "$description": src.get("$description", "").strip() or None,
            "$extensions": {"com.developsolutions.ailocal": {"upstream": origin}},
        })

    # Drop the null descriptions rather than emit them.
    def prune(n):
        if isinstance(n, dict):
            return {k: prune(v) for k, v in n.items() if v is not None}
        return n

    return prune({
        "$schema": "https://tr.designtokens.org/format/",
        "$description": (
            f"GENERATED from {PKG}@{pin} -- do not edit by hand. "
            f"Regenerate with tools/update_carbon_tokens.py. "
            f"Only the tokens themes/artifact.tokens.json aliases are included."
        ),
        "$extensions": {
            "com.developsolutions.ailocal": {
                "generator": "tools/update_carbon_tokens.py",
                "upstream": {
                    "package": PKG,
                    "version": pin,
                    "integrity": integrity,
                    "license": "Apache-2.0",
                    "source": "src/dtcg/",
                    "themes": THEME_FILES,
                },
                "format": "DTCG Format Module 2025.10 (Final Community Group Report)",
            }
        },
        "carbon": tokens["carbon"] if "carbon" in tokens else tokens,
    })


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--pin", default=PIN, help=f"upstream version (default {PIN})")
    ap.add_argument("--check", action="store_true",
                    help="exit non-zero if the committed file is stale")
    args = ap.parse_args()

    text = json.dumps(build(args.pin), indent=2, sort_keys=False) + "\n"

    if args.check:
        current = GENERATED.read_text() if GENERATED.exists() else ""
        if current != text:
            print(f"STALE: {GENERATED.name} does not match {PKG}@{args.pin}")
            print("Run: python3 tools/update_carbon_tokens.py")
            return 1
        print(f"current: {GENERATED.name} matches {PKG}@{args.pin}")
        return 0

    GENERATED.write_text(text)
    print(f"wrote {GENERATED.relative_to(HERE)} from {PKG}@{args.pin}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
