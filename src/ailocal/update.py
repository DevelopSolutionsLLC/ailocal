"""Update availability for the components ailocal declares.

THIS MODULE OWNS NO DISCOVERY LOGIC. Everything here is produced by
checks/services.py, which already pins images by digest, detects declared-vs-
running drift, resolves candidate digests through `docker buildx imagetools`,
and reports provenance. A second implementation of any of that would be a second
thing able to disagree about the same fact.

TWO MODES, deliberately explicit rather than sniffed from the environment:

    local     what this machine has, what the repo declares, what exists upstream
    upstream  what the repo declares and what exists upstream -- nothing else

The distinction is not cosmetic. A disposable CI runner has no Docker daemon,
no Ollama and no models, and GitHub rebuilds its image weekly. Reporting those
absences as ailocal's health would file a misleading report every week, so
upstream mode never asks the machine anything.

WHAT "VALIDATED" MEANS. The compose files declare `tag@digest`; the digest is
authoritative and the tag is what a human reads. A newer upstream release is
AVAILABLE -- a candidate nobody has run the gateway contract against -- and
never SUPPORTED. Promotion is deliberate.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

SCHEMA_VERSION = 3   # 3: candidate is the version policy's (seven-day age, security sooner), not ≈N-2
#                      2: position, candidate and hold on a component; the ollama component

_OLLAMA_RELEASES = "https://api.github.com/repos/ollama/ollama/releases?per_page=100"

LOCAL = "local"
UPSTREAM = "upstream"

CURRENT = "current"
AVAILABLE = "available"
BLOCKED = "blocked"
DEGRADED = "degraded"


@dataclass
class Component:
    name: str
    #: Local mode only; omitted upstream, where a runner's contents say nothing
    #: about this project.
    installed: str | None = None
    declared: str = ""
    available: str = ""
    state: str = CURRENT
    update_method: str = ""
    requires_local_validation: bool = True
    requires_restart: bool = False
    #: How many newer stable upstream releases exist (0 = newest). None when
    #: upstream was not asked or the declared version is not in its list.
    position: int | None = None
    #: The release the version policy adopts today (version_policy.candidate).
    candidate: str = ""
    #: The recorded reason this component is held older than its candidate,
    #: from its declaration file. Empty means no hold is recorded.
    hold: str = ""
    notes: list[str] = field(default_factory=list)


def position(declared: str, releases: list[str]) -> int | None:
    """How many stable releases are newer than `declared` (0 = newest). Context
    only: the candidate is chosen by version_policy, never by this count.

    `releases` are tags as upstream lists them; pre-releases and anything not
    purely numeric are dropped, so `v0.40.9-rc1` never counts as a release.
    A declared version absent from the list has no position: that is reported,
    not guessed."""
    from . import version_policy as vp
    found = sorted({vp.stable(v) for v in releases if vp.stable(v)}, key=vp.key, reverse=True)
    declared = declared.lstrip("v")
    return found.index(declared) if declared in found else None


def advisory_fixes(pin) -> tuple[list[str], str]:
    """(fix versions of advisories affecting `pin`, a note when they could not be read).

    These are what admit a release younger than seven days."""
    from . import audit
    try:
        r = audit.check(pin)
    except Exception as e:  # noqa: BLE001 -- the reason is reported, the young release stays out
        return [], f"advisories could not be read ({type(e).__name__}); no release under seven days is admitted"
    if r.result == audit.UNVERIFIED:
        return [], "advisories could not be read; no release under seven days is admitted"
    return sorted({f for a in r.advisories for f in a.get("fixed", []) if f}), ""


def position_notes(c: Component, why: str = "") -> list[str]:
    """What the version policy says about the declared version. Never an action."""
    from . import version_policy as vp
    if c.position is None:
        return []
    declared = c.declared.lstrip("v")
    if not c.candidate:
        return [f"N-{c.position}: {why or 'no release is at least seven days old'}"] if c.position else []
    if c.candidate == declared:
        return [f"N-{c.position}: at the policy's candidate ({why})" if why else f"N-{c.position}: at the policy's candidate"]
    if vp.key(c.candidate) < vp.key(declared):
        return [f"N-{c.position}: newer than the policy's candidate {c.candidate}; keep it only "
                "if it was adopted as a security release"]
    if c.hold:
        return [f"N-{c.position}: the policy's candidate is {c.candidate}; held: {c.hold}"]
    return [f"N-{c.position}: the policy's candidate is {c.candidate} ({why}) with no recorded hold"]


def _split(ref: str) -> tuple[str, str, str]:
    """`repo:tag@sha256:...` -> (repo, tag, digest). Tag may be empty."""
    head, _, digest = ref.partition("@")
    repo, sep, tag = head.rpartition(":")
    # A tag can never contain "/", so a colon followed by a path segment is a
    # registry port (localhost:5000/img), not a tag. Without this the repo is
    # truncated to the hostname and every downstream comparison silently misses.
    if not sep or "/" in tag:
        repo, tag = head, ""
    return repo, tag, digest


def _images(mode: str) -> list[Component]:
    from .checks import services as sv
    from .checks import CheckStatus
    out: list[Component] = []
    try:
        declared = sv.declared_images()
    except Exception:
        return [Component(name="images", state=DEGRADED, update_method="compose",
                          notes=["could not read the compose declarations"])]

    # One upstream query for every image, through the existing primitive.
    # Upstream candidate resolution is identical in both modes: it asks the
    # registry, not the machine.
    try:
        results = sv.check_updates(declared)
    except Exception:
        results = []

    by_repo = {}
    for r in results:
        for ref in declared:
            repo = _split(ref)[0]
            if repo in (r.summary or "") or repo in (r.detail or ""):
                by_repo.setdefault(repo, r)

    for ref in declared:
        repo, tag, digest = _split(ref)
        c = Component(name=repo.rpartition("/")[2] or repo, declared=tag or digest[:19],
                      update_method="compose", requires_local_validation=True)
        if mode == LOCAL:
            _name, running = sv._running_digest(repo)
            c.installed = (running[:19] if running
                           else "not running" if not _name else "no repo digest")
        r = by_repo.get(repo)
        if r is None:
            c.state = DEGRADED
            c.notes.append("upstream candidate could not be determined")
        elif r.status is CheckStatus.PASS:
            c.state = CURRENT
        elif r.status is CheckStatus.WARN:
            c.state = AVAILABLE
            c.notes.append(r.summary or "newer release available for review")
            c.notes.append("run the gateway contract (routing, tool calls, "
                           "deferred tools) before promoting")
        else:
            c.state = DEGRADED
            c.notes.append(r.summary or "upstream discovery unavailable")
        if not tag:
            c.notes.append("declared by digest only; no proven version tag")
        out.append(c)
    return out


def _ollama(mode: str) -> Component:
    """The patched Ollama base: declared in patch.toml, running at /api/version.

    Upstream is the GitHub release list, so the position counts releases, not
    tags. The hold is read from the same manifest that pins the base, because
    the reason the base is held is a fact about the patches, and it must move
    with them."""
    from . import policy
    from .checks import services
    try:
        spec = policy.ollama_patch_spec()
    except (OSError, KeyError, ValueError) as e:
        return Component(name="ollama", state=DEGRADED, update_method="patch",
                         notes=[f"patch.toml unreadable: {type(e).__name__}"])
    c = Component(name="ollama", declared=spec["base_version"], update_method="patch",
                  requires_restart=True, hold=str(spec.get("hold") or "").strip())
    if mode == LOCAL:
        try:
            c.installed = str(services.http_json(f"{services.OLLAMA}/api/version").get("version") or "")
        except services.Unreachable:
            c.installed = "not running"
        running_base = re.match(r"v?(\d+(?:\.\d+)+)", c.installed or "")
        if running_base and running_base.group(1) != spec["base_version"]:
            c.notes.append(f"the running daemon's base {running_base.group(1)} is not the declared "
                           f"{spec['base_version']}; `ailocal check` reports the selection")
    try:
        releases = services.http_json(_OLLAMA_RELEASES)
    except services.Unreachable as e:
        c.state = DEGRADED
        c.notes.append(f"could not list upstream releases: {e}")
        return c
    from . import audit, version_policy as vp
    tags = [r.get("tag_name", "") for r in releases if isinstance(r, dict)
            and not r.get("prerelease") and not r.get("draft")]
    c.position = position(spec["base_version"], tags)
    fixed, unread = advisory_fixes(audit.Pin(audit.OLLAMA_MODULE, "go", spec["base_version"],
                                             "deploy/ollama/patch.toml", github=audit.OLLAMA_REPO))
    if unread:
        c.notes.append(unread)
    c.candidate, why = vp.candidate(vp.from_github(releases), fixed)
    newest = next((t.lstrip("v") for t in tags if re.fullmatch(r"v?\d+(?:\.\d+)+", t)), "")
    if c.position is None:
        c.state = DEGRADED
        c.notes.append(f"the declared base is not in upstream's release list (newest {newest or 'unknown'})")
        return c
    c.state = AVAILABLE if c.position > 0 else CURRENT
    c.available = newest if c.position > 0 else ""
    c.notes += position_notes(c, why)
    mine = next((r for r in vp.from_github(releases) if r.version == spec["base_version"]), None)
    young = mine and (vp.age(mine) or vp.MIN_AGE) < vp.MIN_AGE
    if young:
        when = vp.age(mine)
        c.notes.append(f"the declared base was published {mine.published_at[:10]} and is eligible under the "
                       f"seven-day rule from {(datetime.now(timezone.utc) - when + vp.MIN_AGE):%Y-%m-%d}")
    if c.state == AVAILABLE:
        c.notes.append("a newer base needs the reviewed patches re-applied and the native regression "
                       "re-run before it can be selected (docs/maintenance.md)")
    return c


def _vendored(mode: str) -> list[Component]:
    """Vendored browser libraries. Discovery only -- these bytes ship in the wheel.

    Replacing them is not a package install: the artifact renderer depends on the
    exact files, so promotion runs the artifact and headless-browser suites. That
    lifecycle is deliberately not automated here.
    """
    from . import policy
    versions = (policy.data_root() / "integrations" / "local-artifacts"
                / "vendor" / "VERSIONS.txt")
    if not versions.is_file():
        return []
    out = []
    for line in versions.read_text().splitlines():
        name, _, ver = line.strip().partition(" ")
        if not name or not ver:
            continue
        # DEGRADED, not CURRENT. There is no upstream discovery for these yet,
        # and "we never looked" is not "up to date" -- the same reason a failed
        # network lookup is never reported as current. The declared version is
        # what ships; whether anything newer exists is simply unknown here.
        c = Component(name=name, declared=ver, update_method="vendored",
                      state=DEGRADED, requires_local_validation=True,
                      notes=["no upstream discovery implemented; the declared "
                             "version is what ships, not a verified latest",
                             "promotion must pass the artifact and headless-browser "
                             "suites; the vendored bytes are the shipped artifact"])
        out.append(c)
    return out


def _self(mode: str) -> Component:
    """ailocal's own package.

    Never mutated here. Replacing the executing pipx installation swaps the
    directory the running LiteLLM container bind-mounts, which the container then
    reads as EMPTY rather than missing. `ailocal start` already detects that by
    inode fingerprint and force-recreates; the safe sequence is package
    replacement followed by `ailocal start`, not an in-process self-update.
    """
    c = Component(name="ailocal", update_method="self", requires_restart=True,
                  requires_local_validation=True,
                  notes=["self-update is external: replace the package, then run "
                         "`ailocal start` so the changed bind mount is re-resolved"])
    # Same rule: nothing was queried, so nothing is known.
    c.state = DEGRADED
    c.notes.append("no authoritative release source declared; not queried")
    return c


def report(mode: str = LOCAL) -> dict:
    components = _images(mode) + [_ollama(mode)] + _vendored(mode) + [_self(mode)]
    status = ("updates_available" if any(c.state == AVAILABLE for c in components)
              else "blocked" if any(c.state == BLOCKED for c in components)
              else "degraded" if any(c.state == DEGRADED for c in components)
              else "current")
    return {"schema_version": SCHEMA_VERSION, "project": "ailocal", "mode": mode,
            "status": status,
            "components": [{k: v for k, v in asdict(c).items() if v is not None}
                           for c in components]}


def main(argv: list[str]) -> int:
    mode = UPSTREAM if "--upstream-only" in argv else LOCAL
    doc = report(mode=mode)
    if "--json" in argv:
        print(json.dumps(doc, indent=2))
    else:
        print(f"\n▶ ailocal components ({doc['status']}, {doc['mode']})")
        for c in doc["components"]:
            mark = {"current": "✓", "available": "→", "blocked": "✗",
                    "degraded": "⚠"}[c["state"]]
            got = c.get("installed") or "—"
            print(f"  {mark} {c['name']:16s} declared {c['declared'] or '—':22s} "
                  f"{('running ' + got) if mode == LOCAL else ''}"
                  f"{('  →  ' + c['available']) if c['available'] else ''}")
            for n in c["notes"]:
                print(f"        {n}")
    return 1 if doc["status"] in ("updates_available", "blocked") else \
        2 if doc["status"] == "degraded" else 0


if __name__ == "__main__":
    import sys
    raise SystemExit(main(sys.argv[1:]))
