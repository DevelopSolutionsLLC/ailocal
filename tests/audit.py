#!/usr/bin/env python3
"""Advisory audit and update reporting, offline: a fake fetcher stands in for OSV,
the registries and GitHub, and a stub stands in for what is running. Writes nothing.
"""
import sys
import unittest.mock as mock
from datetime import datetime, timedelta, timezone
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
DECLARED = next(l.split("image:")[1].strip() for l in (DEPLOY / "litellm/compose.yaml").read_text().splitlines() if "image:" in l)


def live(ollama="0.40.14-ailocal.18631.1", litellm=("ailocal-litellm", DECLARED.split("@", 1)[1])):
    """What is running: a stub, never the machine."""
    return lambda: {"ollama": ollama, "images": {"ghcr.io/berriai/litellm": litellm, "searxng/searxng": ("", "")}}


def ghsa(rng):
    return {"ghsa_id": "GHSA-test", "cve_id": "CVE-2026-1", "summary": "s", "severity": "high",
            "vulnerabilities": [{"vulnerable_version_range": rng, "patched_versions": "1.107.1"}]}


def gh(*advisories, **kw):
    return audit.check(LITELLM, fetcher(github={"BerriAI/litellm": list(advisories)}, **kw))


S.section("advisory audit")
down = [p for p in audit.pins(probe=live(ollama=None)) if p.name == audit.OLLAMA_MODULE]
check(len(down) == 2 and audit.check(down[1], fetcher()).result == audit.UNVERIFIED,
      "a daemon that does not answer is an unverified pin, never a silent skip", down)
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
      and gh(ghsa(">= 1.104.0rc1, < 1.104.0rc2")).result == audit.CLEAN,
      "a publisher range is matched by version order: inside is AFFECTED with its fix, outside is CLEAN", hit)
codes = {"clean": audit.exit_code(audit.report(fetch=fetcher(), pin_list=[LITELLM, NET])),
         "only unverified": audit.exit_code(audit.report(fetch=fetcher(down=("osv.dev",)), pin_list=[NET])),
         "yank beside an outage": audit.exit_code(audit.report(
             fetch=fetcher(status={"pypi.org": 404}, down=("osv.dev",)), pin_list=[LITELLM, NET]))}
check(codes == {"clean": 0, "only unverified": 2, "yank beside an outage": 1},
      "exit status: clean 0, unverified 2, and an observed yank outranks a later outage", codes)

S.section("update report")
with mock.patch("ailocal.checks.services._running_digest"), \
        mock.patch("ailocal.checks.services.check_updates", return_value=[]):
    doc = update.report(mode=update.UPSTREAM)
check(any(c["state"] == update.DEGRADED for c in doc["components"]) and doc["status"] != "current",
      "a failed upstream check degrades, never reports current")
unchecked = [c for c in doc["components"] if c["update_method"] in ("vendored", "self")]
check(unchecked and all(c["state"] != update.CURRENT for c in unchecked),
      "nothing never checked upstream is reported CURRENT", [(c["name"], c["state"]) for c in unchecked])

from ailocal import version_policy as vp  # noqa: E402
NOW = datetime(2026, 11, 7, 12, tzinfo=timezone.utc)
iso = lambda days: (NOW - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")  # noqa: E731
REL = vp.from_github([{"tag_name": "v0.40.17", "published_at": iso(1), "prerelease": False},
                      {"tag_name": "v0.40.16", "published_at": iso(6.9), "prerelease": False},
                      {"tag_name": "v0.40.16-rc1", "published_at": iso(9), "prerelease": True},
                      {"tag_name": "v0.40.15", "published_at": iso(7), "prerelease": False},
                      {"tag_name": "v0.40.14", "published_at": iso(20), "prerelease": False}])
check(vp.candidate(REL, now=NOW)[0] == "0.40.15",
      "the candidate is the newest stable release at least seven full days old (#106)")
check(vp.candidate(REL, ["0.40.16"], now=NOW)[0] == "0.40.17" and vp.candidate(REL, ["0.40.18"], now=NOW)[0] == "0.40.15",
      "a release under seven days old is proposed only when it contains an advisory's fix (#106)")
GO = [{"version": "go1.27.3", "stable": True}, {"version": "go1.26.10", "stable": True}]
got = {v: vp.go_supported(v, GO)[0] for v in ("1.27.3", "go1.26.10", "1.27.1", "1.25.9")}
got["empty listing"] = vp.go_supported("1.27.3", [])[0]
check(got == {"1.27.3": True, "go1.26.10": True, "1.27.1": False, "1.25.9": False, "empty listing": False},
      "Go is supported only at the newest point release of a listed line; an empty listing refuses (#106)", got)

sys.exit(S.report())
