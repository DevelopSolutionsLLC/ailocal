#!/usr/bin/env python3
"""ailocal update reporting: the JSON contract and the local/upstream split.

The defining property under test is that upstream mode never consults the
machine. A CI runner has no Docker daemon and no models; reporting those
absences as ailocal's health would file a misleading report every week.
"""
import json
import sys
import unittest.mock as mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from ailocal import update  # noqa: E402

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond: PASS += 1; print(f"  PASS  {name}")
    else: FAIL += 1; print(f"  FAIL  {name}  {detail}")


print("=== A: image reference parsing ===")
sp = update._split
check("tag@digest splits into three parts",
      sp("ghcr.io/berriai/litellm:v1.98.0@sha256:abc") ==
      ("ghcr.io/berriai/litellm", "v1.98.0", "sha256:abc"))
check("digest-only keeps an empty tag, never invents one",
      sp("searxng/searxng@sha256:abc") == ("searxng/searxng", "", "sha256:abc"))
check("a registry port is not mistaken for a tag",
      sp("localhost:5000/img@sha256:abc")[0].endswith("/img"))

print("\n=== B: upstream mode never touches the machine ===")
with mock.patch("ailocal.checks.services._running_digest") as running, \
     mock.patch("ailocal.checks.services.check_updates", return_value=[]):
    doc = update.report(mode=update.UPSTREAM)
    check("upstream mode never asks for a running digest", not running.called)
check("upstream report declares its mode", doc["mode"] == update.UPSTREAM)
check("no component carries an `installed` field",
      all("installed" not in c for c in doc["components"]))
check("schema is versioned", doc["schema_version"] == 2)
check("project is identified", doc["project"] == "ailocal")

print("\n=== C: states and degradation ===")
check("unresolvable upstream degrades, never reports current",
      any(c["state"] == update.DEGRADED for c in doc["components"]),
      doc["status"])
check("status is never silently 'current' when a check failed",
      doc["status"] != "current")
check("every state is a known value",
      all(c["state"] in {update.CURRENT, update.AVAILABLE, update.BLOCKED,
                         update.DEGRADED} for c in doc["components"]))

print("\n=== D: declarations are read, not invented ===")
live = update.report(mode=update.UPSTREAM)
names = {c["name"] for c in live["components"]}
check("litellm is discovered from the compose declaration", "litellm" in names)
check("searxng is discovered from the compose declaration", "searxng" in names)
check("vendored browser libraries are reported",
      {"mermaid", "elkjs", "marked"} <= names, names)
lite = next(c for c in live["components"] if c["name"] == "litellm")
check("litellm declares a human-readable version tag",
      lite["declared"].startswith("v1."), lite["declared"])
sx = next(c for c in live["components"] if c["name"] == "searxng")
# The mapping was proven from the image's own OCI label
# (org.opencontainers.image.version) and confirmed by resolving the tag back to
# the pinned digest -- not inferred from publication dates.
check("searxng declares its proven version tag",
      sx["declared"].startswith("2026."), sx["declared"])
check("no image is left declared by digest alone",
      not any("digest only" in n for c in live["components"] for n in c["notes"]))

print("\n=== D2: the Ollama base, its N-position and its recorded hold ===")
pos = update.position
tags = ["v0.40.11", "v0.40.10", "v0.40.9-rc1", "v0.40.9", "v0.40.8", "v0.40.7"]
check("newer stable releases are counted; a pre-release is not a release", pos("0.40.8", tags) == (3, "0.40.9"))
check("the newest release is N-0", pos("0.40.11", tags)[0] == 0)
check("a version upstream never released has no position", pos("0.40.12", tags)[0] is None)
check("the candidate is the third-newest stable release", pos("0.40.7", tags)[1] == "0.40.9")
check("fewer than three releases -> no candidate", pos("1.0.0", ["v1.0.0", "v0.9.0"]) == (0, ""))
C = update.Component
check("N-0/N-1 are too new", "too new" in update.position_notes(C(name="x", position=1, candidate="a"))[0])
check("N-2 is at the hold", update.position_notes(C(name="x", position=2))[0].startswith("N-2: at"))
check("N-3 without a hold is flagged with the candidate",
      "no recorded hold" in update.position_notes(C(name="x", position=3, candidate="a"))[0])
check("N-3 with a recorded hold states the hold", "held: why" in update.position_notes(C(name="x", position=3, hold="why"))[0])
check("no position -> nothing said", update.position_notes(C(name="x")) == [])
RELEASES = [{"tag_name": t, "prerelease": "rc" in t, "draft": False} for t in tags]
with mock.patch("ailocal.checks.services.http_json", lambda url, **kw: RELEASES):
    o = update._ollama(update.UPSTREAM)
check("ollama is declared from patch.toml and placed against upstream's releases",
      o.declared == "0.40.8" and o.position == 3 and o.candidate == "0.40.9" and o.state == update.AVAILABLE
      and o.available == "0.40.11" and o.installed is None, o)
check("the hold is read from patch.toml and stated, never acted on",
      o.hold and any("held: the reviewed backport patches" in n for n in o.notes)
      and not any("upgrade" in n.lower() and "run" not in n.lower() for n in o.notes), o.notes)
with mock.patch("ailocal.checks.services.http_json",
                lambda url, **kw: {"version": "0.40.8-ailocal.18631.3"} if "api/version" in url else RELEASES):
    o = update._ollama(update.LOCAL)
check("local mode reads the running daemon's version", o.installed == "0.40.8-ailocal.18631.3", o)


def unreachable(url, **kw):
    raise sv.Unreachable("offline")


from ailocal.checks import services as sv  # noqa: E402
with mock.patch("ailocal.checks.services.http_json", unreachable):
    o = update._ollama(update.LOCAL)
check("offline -> degraded with the running daemon marked not running, never current",
      o.state == update.DEGRADED and o.installed == "not running" and o.position is None, o)

print("\n=== E: self-update is never performed here ===")
me = next(c for c in live["components"] if c["name"] == "ailocal")
check("ailocal self-update requires a restart", me["requires_restart"] is True)
check("self-update is external, not in-process", me["update_method"] == "self")
check("the recovery sequence is stated",
      any("ailocal start" in n for n in me["notes"]))
check("vendored assets require local validation",
      all(c["requires_local_validation"] for c in live["components"]
          if c["update_method"] == "vendored"))

print("\n=== E2: never claim CURRENT for something never checked ===")
# "We did not look" is not "up to date". An earlier cut reported the vendored
# browser libraries as current purely because nothing contradicted them, which
# is the same false confidence as treating a failed lookup as healthy.
for c in live["components"]:
    if c["update_method"] in ("vendored", "self"):
        check(f"{c['name']} is not reported current without discovery",
              c["state"] != update.CURRENT, c["state"])
        check(f"{c['name']} says why it is uncertain",
              any("not queried" in n or "no upstream discovery" in n
                  for n in c["notes"]), c["notes"])

print("\n=== F: module entry point ===")
import subprocess, os  # noqa: E402
r = subprocess.run([sys.executable, "-m", "ailocal.update", "--upstream-only", "--json"],
                   capture_output=True, text=True, timeout=300,
                   env={**os.environ,
                        "PYTHONPATH": str(Path(__file__).resolve().parent.parent / "src")})
check("`python -m ailocal.update` emits JSON (has __main__ guard)",
      r.stdout.strip().startswith("{"), r.stdout[:60])
check("output parses", isinstance(json.loads(r.stdout or "{}"), dict))

print(f"\n  PASS {PASS}   FAIL {FAIL}")
sys.exit(1 if FAIL else 0)
