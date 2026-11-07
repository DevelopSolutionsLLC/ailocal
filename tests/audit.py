#!/usr/bin/env python3
"""Test advisory audit and release selection offline; writes nothing.

A fake fetcher stands in for OSV, the registries and GitHub.
"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness import Suite  # noqa: E402
from ailocal import audit  # noqa: E402
from ailocal import version_policy as vp  # noqa: E402

S = Suite("AUDIT")
check = S.check


def fetcher(osv=None, status=None, github=None, down=()):
    """Fake fetcher.

    Args:
        osv: package → vulns.
        status: URL fragment → HTTP status.
        github: repo → advisories.
        down: URL fragments that fail.
    """
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


def ghsa(rng):
    return {"ghsa_id": "GHSA-test", "cve_id": "CVE-2026-1", "summary": "s", "severity": "high",
            "vulnerabilities": [{"vulnerable_version_range": rng, "patched_versions": "1.107.1"}]}


def gh(*advisories, **kw):
    return audit.check(LITELLM, fetcher(github={"BerriAI/litellm": list(advisories)}, **kw))


S.section("advisory audit")
unread = {"OSV outage": audit.check(NET, fetcher(down=("osv.dev",))).result,
          "unparseable range": gh(ghsa("< 1.106.x-weird")).result,
          "listing truncated at 100": gh(*[ghsa("<1.0")] * 100).result,
          "no advisory source": audit.check(audit.Pin("example/unmapped", "image", "1.0.0", "compose"), fetcher()).result}
check(set(unread.values()) == {audit.UNVERIFIED},
      "an advisory answer that could not be read whole is UNVERIFIED, never CLEAN", unread)
r = audit.check(LITELLM, fetcher(osv={"litellm": [{"id": "MAL-2026-2144", "summary": "stealer", "affected": []}]}))
check(r.result == audit.AFFECTED and r.advisories[0]["malicious"], "a malicious-package record is AFFECTED and flagged", r)
hit = gh(ghsa(">= 1.100.0, < 1.107.1"))
check(hit.result == audit.AFFECTED and hit.advisories[0]["fixed"] == ["1.107.1"]
      and {gh(ghsa(rng)).result for rng in (">= 1.104.0rc1, < 1.104.0rc2", "< 1.99.0")} == {audit.CLEAN},
      "a publisher range is matched by version order: inside is AFFECTED with its fix, outside is CLEAN", hit)
codes = {"clean": audit.exit_code(audit.report(fetch=fetcher(), pin_list=[LITELLM, NET])),
         "only unverified": audit.exit_code(audit.report(fetch=fetcher(down=("osv.dev",)), pin_list=[NET])),
         "yank beside an outage": audit.exit_code(audit.report(
             fetch=fetcher(status={"pypi.org": 404}, down=("osv.dev",)), pin_list=[LITELLM, NET]))}
check(codes == {"clean": 0, "only unverified": 2, "yank beside an outage": 1},
      "exit status: clean 0, unverified 2, and an observed yank outranks a later outage", codes)

S.section("release selection")
NOW = datetime(2026, 11, 7, 12, tzinfo=timezone.utc)
iso = lambda days: (NOW - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")  # noqa: E731
REL = vp.from_github([{"tag_name": "v0.40.17", "published_at": iso(1), "prerelease": False},
                      {"tag_name": "v0.40.16", "published_at": iso(6.9), "prerelease": False},
                      {"tag_name": "v0.40.16-rc1", "published_at": iso(9), "prerelease": True},
                      {"tag_name": "v0.40.15", "published_at": iso(7), "prerelease": False},
                      {"tag_name": "v0.40.14", "published_at": iso(20), "prerelease": False}])
check(vp.candidate(REL, now=NOW)[0] == "0.40.15",
      "a release under seven full days old is never proposed (#106)", vp.candidate(REL, now=NOW))

sys.exit(S.report())
