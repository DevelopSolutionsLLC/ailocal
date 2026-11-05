#!/usr/bin/env python3
"""Advisory audit and update reporting, offline: a fake fetcher stands in for OSV,
the registries and GitHub, and a stub stands in for what is running. Writes only
a temp state root.
"""
import json
import os
import subprocess
import sys
import tempfile
import tomllib
import unittest.mock as mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness import REPO, Suite  # noqa: E402
from ailocal import audit, update  # noqa: E402

S = Suite("AUDIT")
check = S.check
DEPLOY = REPO / "src/ailocal/resources/deploy"


def fetcher(osv=None, status=None, github=None, down=()):
    """osv: package -> vulns; status: URL fragment -> HTTP status; github: repo -> advisories; down: fragments that fail."""
    osv, status, github = osv or {}, status or {}, github or {}

    def fetch(url, body=None):
        if any(d in url for d in down):
            raise audit.Unavailable("simulated outage")
        if url == audit._OSV:
            return 200, {"vulns": osv.get(body["package"]["name"], [])}
        for repo, advisories in github.items():
            if f"/repos/{repo}/security-advisories" in url:
                return 200, advisories
        for frag, code in status.items():
            if frag in url:
                return code, None
        return (200, []) if "/security-advisories" in url else (200, {"info": {"yanked": False}})
    return fetch


LITELLM = audit.Pin("ghcr.io/berriai/litellm", "image", "1.106.2", "compose", github="BerriAI/litellm", pypi="litellm")
NET = audit.Pin("golang.org/x/net", "go", "0.60.0", "patch.toml")
MERMAID = audit.Pin("mermaid", "npm", "11.17.2", "VERSIONS.txt")
DECLARED = next(l.split("image:")[1].strip() for l in (DEPLOY / "litellm/compose.yaml").read_text().splitlines() if "image:" in l)


def live(ollama="0.40.14-ailocal.18631.1", litellm=("ailocal-litellm", DECLARED.split("@", 1)[1])):
    """What is running: a stub, never the machine."""
    return lambda: {"ollama": ollama, "images": {"ghcr.io/berriai/litellm": litellm, "searxng/searxng": ("", "")}}


def ollama_pins(**kw):
    return [p for p in audit.pins(probe=live(**kw)) if p.name == audit.OLLAMA_MODULE]


S.section("pins come from their source-of-truth files")
found = {p.name: p for p in audit.pins(probe=live())}
patch = tomllib.loads((DEPLOY / "ollama/patch.toml").read_text())
vendored = REPO / "src/ailocal/resources/integrations/local-artifacts/vendor/VERSIONS.txt"
check(found["ghcr.io/berriai/litellm"].version in (DEPLOY / "litellm/compose.yaml").read_text()
      and all(p.github for p in found.values() if p.kind == "image"), "images are enumerated with versions and advisory sources")
check(found[audit.OLLAMA_MODULE].version == patch["base_version"] and all(m in found for m in patch["security_dependencies"]),
      "the Ollama base and its security dependencies are enumerated")
check(all(l.split()[0] in found for l in vendored.read_text().splitlines() if l.strip()), "vendored browser libraries are enumerated")

S.section("running state beside declared")
same = ollama_pins()
check(len(same) == 1 and same[0].notes == ["running: /api/version 0.40.14-ailocal.18631.1"],
      "a daemon on the declared base annotates the pin")
drift = ollama_pins(ollama="0.41.0")
check(len(drift) == 2 and drift[1].version == "0.41.0" and any("not this base" in n for n in drift[0].notes),
      "a daemon on another base becomes a second pin at its own version", drift)
down = ollama_pins(ollama=None)
check(len(down) == 2 and audit.check(down[1], fetcher()).result == audit.UNVERIFIED,
      "a daemon that does not answer is an unverified pin, never a silent skip", down)
imgs = {p.name: p for p in audit.pins(probe=live()) if p.kind == "image"}
check(imgs["ghcr.io/berriai/litellm"].notes == ["running: ailocal-litellm serves the declared digest"]
      and imgs["searxng/searxng"].notes == ["not running"], "running and absent containers are reported", imgs)
imgs = {p.name: p for p in audit.pins(probe=live(litellm=("ailocal-litellm", "sha256:" + "0" * 64))) if p.kind == "image"}
r = audit.check(imgs["ghcr.io/berriai/litellm"], fetcher())
check(any("NOT the declared" in n for n in r.notes), "another running digest is reported as drift in the result", r.notes)

S.section("outcomes")
r = audit.check(LITELLM, fetcher())
check(r.result == audit.CLEAN and len(r.sources) == 3 and r.queried_at.endswith("Z"),
      "all sources answering with nothing is CLEAN, naming sources and time", r)
r = audit.check(LITELLM, fetcher(osv={"litellm": [{"id": "MAL-2026-2144", "summary": "stealer", "affected": []}]}))
check(r.result == audit.AFFECTED and r.advisories[0]["malicious"], "a malicious-package record is AFFECTED and flagged")
for pin, frag, code in ((LITELLM, "pypi.org", 404), (MERMAID, "registry.npmjs.org", 404), (NET, "proxy.golang.org", 410)):
    check(audit.check(pin, fetcher(status={frag: code})).result == audit.YANKED, f"a {frag} {code} is YANKED")
r = audit.check(NET, fetcher(down=("osv.dev",)))
check(r.result == audit.UNVERIFIED and any("lookup failed" in n for n in r.notes), "a failed lookup is UNVERIFIED, never CLEAN", r)
check(audit.check(LITELLM, fetcher(status={"pypi.org": 404}, down=("osv.dev",))).result == audit.YANKED,
      "a later outage does not erase an observed yank")
for pin, label in ((audit.Pin("example/unmapped", "image", "1.0.0", "compose"), "no advisory source"),
                   (audit.Pin("searxng/searxng", "image", "", "compose", github="searxng/searxng"), "no orderable version")):
    check(audit.check(pin, fetcher()).result == audit.UNVERIFIED, f"a pin with {label} is UNVERIFIED")


def ghsa(rng, **extra):
    return {"ghsa_id": "GHSA-test", "cve_id": "CVE-2026-1", "summary": "s", "severity": "high", **extra,
            "vulnerabilities": [{"vulnerable_version_range": rng, "patched_versions": "1.107.1"}]}


def gh(*advisories, **kw):
    return audit.check(LITELLM, fetcher(github={"BerriAI/litellm": list(advisories)}, **kw))


S.section("publisher advisory ranges")
hit = gh(ghsa(">= 1.100.0, < 1.107.1"))
check(hit.result == audit.AFFECTED and hit.advisories[0]["fixed"] == ["1.107.1"], "a version in range is AFFECTED, with the fix")
for rng in ("< 1.83.10-stable", ">= 1.104.0rc1, < 1.104.0rc2", "<1.94.0"):
    check(gh(ghsa(rng)).result == audit.CLEAN, f"1.106.2 is outside {rng!r}")
check(audit.in_range("1.104.0rc1", "< 1.104.0") is True, "a release candidate orders before its release")
check(gh(ghsa("< 1.106.x-weird")).result == audit.UNVERIFIED, "an unparseable range is UNVERIFIED")
check(gh(*[ghsa("<1.0")] * 100).result == audit.UNVERIFIED, "a listing truncated at 100 is UNVERIFIED")
r = audit.check(NET, fetcher(osv={"golang.org/x/net": [{"id": "GO-1", "summary": "s", "affected": [], "modified": "2026-10-01T00:00:00Z"}]}))
check(r.advisories[0]["modified"] == "2026-10-01T00:00:00Z"
      and gh(ghsa(">= 1.0.0", updated_at="2026-09-09T00:00:00Z")).advisories[0]["modified"] == "2026-09-09T00:00:00Z",
      "advisories carry their modified time from OSV and the publisher")
dup = {"id": "GHSA-test", "aliases": ["CVE-2026-1"], "summary": "s", "affected": []}
check(len(gh(ghsa(">= 1.0.0"), osv={"litellm": [dup]}).advisories) == 1, "one issue from OSV and the publisher counts once")

S.section("report, exit code and record")
doc = audit.report(fetch=fetcher(), pin_list=[LITELLM, NET, MERMAID])
check(doc["status"] == audit.CLEAN and audit.exit_code(doc) == 0 and doc["coverage_gaps"], "a clean report exits 0 and states gaps")
check(audit.exit_code(audit.report(fetch=fetcher(down=("osv.dev",)), pin_list=[NET])) == 2, "only-unverified exits 2")
doc = audit.report(fetch=fetcher(status={"pypi.org": 404}, down=("osv.dev",)), pin_list=[LITELLM, NET])
check(doc["status"] == audit.YANKED and audit.exit_code(doc) == 1, "a yank outranks unverified and exits 1")
with tempfile.TemporaryDirectory() as box, mock.patch.dict(os.environ, {"AILOCAL_STATE": box}):
    path = audit.write_record(audit.report(fetch=fetcher(), pin_list=[LITELLM, NET]))
    back = json.loads(path.read_text())
    check(path == Path(box) / "audit/local.json" and back["schema_version"] == 2 and back["kind"] == "advisory-audit"
          and back["mode"] == "local" and len(back["results"]) == 2, "the record is state_root()/audit/local.json", sorted(back))
from ailocal import generation  # noqa: E402
check(generation._home_relative(Path.home() / ".local/state/ailocal/audit/local.json") == "~/.local/state/ailocal/audit/local.json"
      and generation._home_relative(Path("/opt/x/local.json")) == "/opt/x/local.json",
      "the contract publishes the record path home-relative (string only; nothing is read)")

S.section("installation audits run")
from ailocal import install  # noqa: E402
try:  # findings are a normal state; only the audit itself breaking fails
    install.audit()
    install.client_audit()
    check(True, "install.audit() and install.client_audit() raise nothing")
except Exception as exc:  # noqa: BLE001
    S.error("install.audit() and install.client_audit() raise nothing", exc)

S.section("update report")
check(update._split("ghcr.io/berriai/litellm:v1.98.0@sha256:abc") == ("ghcr.io/berriai/litellm", "v1.98.0", "sha256:abc")
      and update._split("searxng/searxng@sha256:abc") == ("searxng/searxng", "", "sha256:abc")
      and update._split("localhost:5000/img@sha256:abc")[0].endswith("/img"),
      "image references split without inventing a tag or misreading a registry port")
with mock.patch("ailocal.checks.services._running_digest") as running, \
        mock.patch("ailocal.checks.services.check_updates", return_value=[]):
    doc = update.report(mode=update.UPSTREAM)
check(not running.called and doc["mode"] == update.UPSTREAM and all("installed" not in c for c in doc["components"])
      and doc["schema_version"] == 2 and doc["project"] == "ailocal", "upstream mode never consults the machine")
check(any(c["state"] == update.DEGRADED for c in doc["components"]) and doc["status"] != "current"
      and all(c["state"] in {update.CURRENT, update.AVAILABLE, update.BLOCKED, update.DEGRADED} for c in doc["components"]),
      "a failed upstream check degrades, never reports current")
comp = {c["name"]: c for c in update.report(mode=update.UPSTREAM)["components"]}
check({"litellm", "searxng", "mermaid", "elkjs", "marked"} <= comp.keys() and comp["litellm"]["declared"].startswith("v1.")
      and comp["searxng"]["declared"].startswith("2026.")
      and not any("digest only" in n for c in comp.values() for n in c["notes"]),
      "components and their version tags are read from the declarations", sorted(comp))
me = comp["ailocal"]
check(me["requires_restart"] is True and me["update_method"] == "self" and any("ailocal start" in n for n in me["notes"]),
      "self-update is external and states the restart")
unchecked = [c for c in comp.values() if c["update_method"] in ("vendored", "self")]
check(all(c["state"] != update.CURRENT and any("not queried" in n or "no upstream discovery" in n for n in c["notes"])
          for c in unchecked) and all(c["requires_local_validation"] for c in unchecked if c["update_method"] == "vendored"),
      "nothing never checked is reported CURRENT", [(c["name"], c["state"]) for c in unchecked])

pos, C = update.position, update.Component
tags = ["v0.40.17", "v0.40.16", "v0.40.15-rc1", "v0.40.15", "v0.40.14", "v0.40.13"]
check(pos("0.40.14", tags) == (3, "0.40.15") and pos("0.40.17", tags)[0] == 0 and pos("0.40.18", tags)[0] is None
      and pos("0.40.13", tags)[1] == "0.40.15" and pos("1.0.0", ["v1.0.0", "v0.9.0"]) == (0, ""),
      "release position skips pre-releases; the candidate is the third-newest stable")
notes = update.position_notes
check("too new" in notes(C(name="x", position=1, candidate="a"))[0] and notes(C(name="x", position=2))[0].startswith("N-2: at")
      and "no recorded hold" in notes(C(name="x", position=3, candidate="a"))[0]
      and "held: why" in notes(C(name="x", position=3, hold="why"))[0] and notes(C(name="x")) == [],
      "position notes: N-0/1 too new, N-2 at the hold, N-3 flagged unless held")
RELEASES = [{"tag_name": t, "prerelease": "rc" in t, "draft": False} for t in tags]
with mock.patch("ailocal.checks.services.http_json", lambda url, **kw: RELEASES):
    o = update._ollama(update.UPSTREAM)
check(o.declared == "0.40.14" and o.position == 3 and o.candidate == "0.40.15" and o.state == update.AVAILABLE
      and o.available == "0.40.17" and o.installed is None and o.hold
      and any("held: the reviewed backport patches" in n for n in o.notes), "the Ollama base is placed upstream; its hold is stated", o)
with mock.patch("ailocal.checks.services.http_json",
                lambda url, **kw: {"version": "0.40.14-ailocal.18631.1"} if "api/version" in url else RELEASES):
    check(update._ollama(update.LOCAL).installed == "0.40.14-ailocal.18631.1", "local mode reads the running daemon")
from ailocal.checks import services as sv  # noqa: E402
with mock.patch("ailocal.checks.services.http_json", mock.Mock(side_effect=sv.Unreachable("offline"))):
    o = update._ollama(update.LOCAL)
check(o.state == update.DEGRADED and o.installed == "not running" and o.position is None, "offline degrades, never current", o)
r = subprocess.run([sys.executable, "-m", "ailocal.update", "--upstream-only", "--json"], capture_output=True, text=True, timeout=300)
try:
    check(isinstance(json.loads(r.stdout), dict), "`python -m ailocal.update` emits JSON")
except ValueError:
    check(False, "`python -m ailocal.update` emits JSON", r.stdout[:80] + r.stderr[-200:])

sys.exit(S.report())
