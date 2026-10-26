#!/usr/bin/env python3
"""ailocal audit: pin discovery and outcome classification, OFFLINE.

A fake fetcher stands in for OSV, PyPI, npm, the Go proxy and GitHub, so this
runs in the gate without a network. The live audit is deliberately not in the
gate: it asks the internet a question whose answer changes daily.

The property under test is the one the audit exists for: a failed lookup is
UNVERIFIED and never CLEAN, and a registry 404 is an answer (YANKED), not a
failure.
"""
import json
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
from ailocal import audit  # noqa: E402

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond: PASS += 1; print(f"  PASS  {name}")
    else: FAIL += 1; print(f"  FAIL  {name}  {detail}")


def fetcher(osv=None, status=None, github=None, down=()):
    """osv: package name -> vulns; status: URL substring -> HTTP status;
    github: repo -> advisory list; down: URL substrings that raise."""
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
        if "/security-advisories" in url:
            return 200, []
        return 200, {"info": {"yanked": False}}
    return fetch


def run(pin, fetch):
    return audit.check(pin, fetch)


LITELLM = audit.Pin("ghcr.io/berriai/litellm", "image", "1.106.2", "compose",
                    github="BerriAI/litellm", pypi="litellm")
NET = audit.Pin("golang.org/x/net", "go", "0.60.0", "patch.toml")
MERMAID = audit.Pin("mermaid", "npm", "11.17.2", "VERSIONS.txt")

import os as _os  # noqa: E402
import tempfile as _tempfile  # noqa: E402

DECLARED_LITELLM = next(l.split("image:")[1].strip() for l in
                        (ROOT / "src/ailocal/resources/deploy/litellm/compose.yaml").read_text().splitlines()
                        if "image:" in l)


def live(ollama="0.40.8-ailocal.18631.3", litellm=("ailocal-litellm", DECLARED_LITELLM.split("@", 1)[1])):
    """A stub for what is running: never the machine, in the gate."""
    return lambda: {"ollama": ollama, "images": {"ghcr.io/berriai/litellm": litellm,
                                                  "searxng/searxng": ("", "")}}


print("=== A: every pin comes from its source-of-truth file ===")
found = {p.name: p for p in audit.pins(probe=live())}
compose = (ROOT / "src/ailocal/resources/deploy/litellm/compose.yaml").read_text()
check("the LiteLLM image is enumerated with its tag's version",
      found["ghcr.io/berriai/litellm"].version in compose)
check("every declared image has a publisher advisory source",
      all(p.github for p in found.values() if p.kind == "image"),
      [p.name for p in found.values() if p.kind == "image" and not p.github])
patch = tomllib.loads((ROOT / "src/ailocal/resources/deploy/ollama/patch.toml").read_text())
check("the Ollama patch base is audited at its base_version",
      found[audit.OLLAMA_MODULE].version == patch["base_version"])
check("every Ollama security dependency is enumerated",
      all(m in found for m in patch["security_dependencies"]))
vendored = (ROOT / "src/ailocal/resources/integrations/local-artifacts/vendor/VERSIONS.txt")
check("every vendored browser library is enumerated",
      all(line.split()[0] in found for line in vendored.read_text().splitlines() if line.strip()))

print("\n=== A2: what is running is audited beside what is declared ===")
check("a daemon on the declared base annotates the declared pin, no second pin",
      [p for p in audit.pins(probe=live()) if p.name == audit.OLLAMA_MODULE][0].notes == ["running: /api/version 0.40.8-ailocal.18631.3"]
      and sum(1 for p in audit.pins(probe=live()) if p.name == audit.OLLAMA_MODULE) == 1)
drift = [p for p in audit.pins(probe=live(ollama="0.41.0")) if p.name == audit.OLLAMA_MODULE]
check("a daemon on another base -> the running base is a second pin, audited at its own version",
      len(drift) == 2 and drift[1].version == "0.41.0" and drift[1].declared_in == "running: /api/version"
      and any("not this base" in n for n in drift[0].notes), drift)
down = [p for p in audit.pins(probe=live(ollama=None)) if p.name == audit.OLLAMA_MODULE]
check("a daemon that does not answer -> an unverified running pin, never a silent skip",
      len(down) == 2 and down[1].version == "" and audit.check(down[1], fetcher()).result == audit.UNVERIFIED, down)
imgs = {p.name: p for p in audit.pins(probe=live()) if p.kind == "image"}
check("a container serving the declared digest says so; one not running says so",
      imgs["ghcr.io/berriai/litellm"].notes == ["running: ailocal-litellm serves the declared digest"]
      and imgs["searxng/searxng"].notes == ["not running"], imgs)
imgs = {p.name: p for p in audit.pins(probe=live(litellm=("ailocal-litellm", "sha256:" + "0" * 64))) if p.kind == "image"}
check("a container serving another digest is reported as drift, not audited by version",
      any("NOT the declared" in n for n in imgs["ghcr.io/berriai/litellm"].notes), imgs)
r = audit.check(imgs["ghcr.io/berriai/litellm"], fetcher())
check("the running notes travel into the result", any("NOT the declared" in n for n in r.notes), r.notes)

print("\n=== B: outcomes ===")
r = run(LITELLM, fetcher())
check("every source answering with nothing is clean", r.result == audit.CLEAN, r)
check("a result names its sources and query time",
      len(r.sources) == 3 and r.queried_at.endswith("Z"), r.sources)

mal = {"id": "MAL-2026-2144", "summary": "credential stealer", "affected": []}
r = run(LITELLM, fetcher(osv={"litellm": [mal]}))
check("a malicious-package record is affected and flagged malicious",
      r.result == audit.AFFECTED and r.advisories[0]["malicious"])

r = run(LITELLM, fetcher(status={"pypi.org": 404}))
check("a PyPI 404 is YANKED: unpublishing is how malware is withdrawn",
      r.result == audit.YANKED, r)
r = run(MERMAID, fetcher(status={"registry.npmjs.org": 404}))
check("an npm 404 is YANKED", r.result == audit.YANKED)
r = run(NET, fetcher(status={"proxy.golang.org": 410}))
check("a Go proxy 410 is YANKED", r.result == audit.YANKED)

r = run(NET, fetcher(down=("osv.dev",)))
check("an unreachable source is UNVERIFIED, never clean",
      r.result == audit.UNVERIFIED and any("lookup failed" in n for n in r.notes), r)
r = run(LITELLM, fetcher(status={"pypi.org": 404}, down=("osv.dev",)))
check("a later outage does not erase a yank already observed", r.result == audit.YANKED)
r = run(audit.Pin("example/unmapped", "image", "1.0.0", "compose"), fetcher())
check("an image with no declared advisory source is UNVERIFIED", r.result == audit.UNVERIFIED)
r = run(audit.Pin("searxng/searxng", "image", "", "compose", github="searxng/searxng"), fetcher())
check("a pin with no orderable version is UNVERIFIED", r.result == audit.UNVERIFIED)

print("\n=== C: publisher advisory ranges ===")
ghsa = lambda rng: {"ghsa_id": "GHSA-test", "cve_id": "CVE-2026-1", "summary": "s",
                    "severity": "high",
                    "vulnerabilities": [{"vulnerable_version_range": rng,
                                         "patched_versions": "1.107.1"}]}
hit = run(LITELLM, fetcher(github={"BerriAI/litellm": [ghsa(">= 1.100.0, < 1.107.1")]}))
check("a version inside the range is affected, with the patched release",
      hit.result == audit.AFFECTED and hit.advisories[0]["fixed"] == ["1.107.1"])
for rng in ("< 1.83.10-stable", ">= 1.104.0rc1, < 1.104.0rc2", "<1.94.0"):
    r = run(LITELLM, fetcher(github={"BerriAI/litellm": [ghsa(rng)]}))
    check(f"1.106.2 is outside {rng!r}", r.result == audit.CLEAN, r.notes)
check("a release candidate orders before its release",
      audit.in_range("1.104.0rc1", "< 1.104.0") is True)
r = run(LITELLM, fetcher(github={"BerriAI/litellm": [ghsa("< 1.106.x-weird")]}))
check("an unparseable range is UNVERIFIED, not a miss", r.result == audit.UNVERIFIED)
r = run(LITELLM, fetcher(github={"BerriAI/litellm": [ghsa("<1.0")] * 100}))
check("a listing truncated at 100 is UNVERIFIED", r.result == audit.UNVERIFIED)

mod = {"id": "GO-2026-1", "summary": "s", "affected": [], "modified": "2026-10-01T00:00:00Z"}
r = run(NET, fetcher(osv={"golang.org/x/net": [mod]}))
check("an advisory carries its modified time, so a consumer can tell a revision apart",
      r.advisories[0]["modified"] == "2026-10-01T00:00:00Z", r.advisories)
r = run(LITELLM, fetcher(github={"BerriAI/litellm": [{**ghsa(">= 1.0.0"), "updated_at": "2026-09-09T00:00:00Z"}]}))
check("a publisher advisory's updated_at is its modified time", r.advisories[0]["modified"] == "2026-09-09T00:00:00Z")

dup = {"id": "GHSA-test", "aliases": ["CVE-2026-1"], "summary": "s", "affected": []}
r = run(LITELLM, fetcher(osv={"litellm": [dup]},
                         github={"BerriAI/litellm": [ghsa(">= 1.0.0")]}))
check("one issue reported by OSV and the publisher counts once", len(r.advisories) == 1)

print("\n=== D: the report and exit code ===")
doc = audit.report(fetch=fetcher(), pin_list=[LITELLM, NET, MERMAID])
check("an all-clean report exits 0", doc["status"] == audit.CLEAN and audit.exit_code(doc) == 0)
check("the report states what it does not cover", doc["coverage_gaps"])
doc = audit.report(fetch=fetcher(down=("osv.dev",)), pin_list=[NET])
check("only-unverified exits 2", audit.exit_code(doc) == 2)
doc = audit.report(fetch=fetcher(status={"pypi.org": 404}, down=("osv.dev",)),
                   pin_list=[LITELLM, NET])
check("a yank outranks unverified and exits 1",
      doc["status"] == audit.YANKED and audit.exit_code(doc) == 1)

print("\n=== E: every run is recorded where the contract says ===")
_os.environ["AILOCAL_STATE"] = _tempfile.mkdtemp(prefix="ailocal-audit-test-")
doc = audit.report(fetch=fetcher(), pin_list=[LITELLM, NET])
path = audit.write_record(doc)
check("the record is state_root()/audit/local.json", path == Path(_os.environ["AILOCAL_STATE"]) / "audit" / "local.json")
back = json.loads(path.read_text())
check("the record is the report: schema 2, kind, mode, time and results",
      back["schema_version"] == 2 and back["kind"] == "advisory-audit" and back["mode"] == "local"
      and back["queried_at"].endswith("Z") and len(back["results"]) == 2, sorted(back))
from ailocal import generation  # noqa: E402
check("the contract publishes the record path, home-relative, and the refresh command",
      generation._home_relative(Path.home() / ".local/state/ailocal/audit/local.json") == "~/.local/state/ailocal/audit/local.json"
      and generation._home_relative(Path("/opt/x/local.json")) == "/opt/x/local.json")

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
