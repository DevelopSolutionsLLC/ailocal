#!/usr/bin/env python3
"""ailocal audit: pin discovery and outcome classification, OFFLINE.

A fake fetcher stands in for OSV, PyPI, npm, the Go proxy and GitHub, so this
runs in the gate without a network. The live audit is deliberately not in the
gate: it asks the internet a question whose answer changes daily.

The property under test is the one the audit exists for: a failed lookup is
UNVERIFIED and never CLEAN, and a registry 404 is an answer (YANKED), not a
failure.
"""
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

print("=== A: every pin comes from its source-of-truth file ===")
found = {p.name: p for p in audit.pins()}
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

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
