"""Report update availability for the components ailocal declares.

Owns no discovery: checks/services.py does that, so nothing can disagree.
Modes are explicit: `local` adds this machine; `upstream` never asks it, as a
CI runner's absences are not ailocal's health. A newer release is AVAILABLE,
never SUPPORTED: promotion is deliberate.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from . import audit

SCHEMA_VERSION = 3   # 3: candidate comes from version_policy

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
    #: Local mode only.
    installed: str | None = None
    declared: str = ""
    available: str = ""
    state: str = CURRENT
    update_method: str = ""
    requires_local_validation: bool = True
    requires_restart: bool = False
    #: Newer stable upstream releases (0 = newest); None if unknown.
    position: int | None = None
    candidate: str = ""
    #: Recorded reason for holding below the candidate; "" if none.
    hold: str = ""
    notes: list[str] = field(default_factory=list)


def position(declared: str, releases: list[str]) -> int | None:
    """Return how many stable releases are newer than `declared` (0 = newest).

    Context only; version_policy picks the candidate. Pre-releases never count,
    and a declared version absent from `releases` returns None.
    """
    from . import version_policy as vp
    found = sorted({vp.stable(v) for v in releases if vp.stable(v)}, key=vp.key, reverse=True)
    declared = declared.lstrip("v")
    return found.index(declared) if declared in found else None


def advisory_fixes(pin: audit.Pin) -> tuple[list[str], str]:
    """Return (advisory fix versions for `pin`, a note if unreadable).

    These admit a release younger than seven days.
    """
    from . import audit
    try:
        r = audit.check(pin)
    except Exception as e:  # noqa: BLE001 -- the reason is reported, the young release stays out
        return [], f"advisories could not be read ({type(e).__name__}); no release under seven days is admitted"
    if r.result == audit.UNVERIFIED:
        return [], "advisories could not be read; no release under seven days is admitted"
    return sorted({f for a in r.advisories for f in a.get("fixed", []) if f}), ""


def position_notes(c: Component, why: str = "") -> list[str]:
    """Describe the policy's view of the declared version; never an action."""
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
    """Split `repo:tag@sha256:...` into (repo, tag, digest); tag may be ""."""
    head, _, digest = ref.partition("@")
    repo, sep, tag = head.rpartition(":")
    # A colon before a "/" is a registry port (localhost:5000/img), not a tag.
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

    # Candidate resolution asks the registry, identically in both modes.
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
    """Report the patched Ollama base (patch.toml vs /api/version).

    The hold is read from the manifest pinning the base, so it moves with the
    patches.
    """
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
    when = vp.age(mine) if mine else None
    if mine and when and when < vp.MIN_AGE:
        c.notes.append(f"the declared base was published {mine.published_at[:10]} and is eligible under the "
                       f"seven-day rule from {(datetime.now(timezone.utc) - when + vp.MIN_AGE):%Y-%m-%d}")
    if c.state == AVAILABLE:
        c.notes.append("a newer base needs the reviewed patches re-applied and the native regression "
                       "re-run before it can be selected (docs/maintenance.md)")
    return c


def _vendored(mode: str) -> list[Component]:
    """Report vendored browser libraries; discovery only, never replaced.

    Promotion runs the artifact and headless-browser suites, by hand.
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
        # DEGRADED, not CURRENT: "never looked" is not "up to date".
        c = Component(name=name, declared=ver, update_method="vendored",
                      state=DEGRADED, requires_local_validation=True,
                      notes=["no upstream discovery implemented; the declared "
                             "version is what ships, not a verified latest",
                             "promotion must pass the artifact and headless-browser "
                             "suites; the vendored bytes are the shipped artifact"])
        out.append(c)
    return out


def _self(mode: str) -> Component:
    """Report ailocal's own package; never self-updated in process.

    Replacing it swaps a bind-mounted dir; `ailocal start` then recreates.
    """
    c = Component(name="ailocal", update_method="self", requires_restart=True,
                  requires_local_validation=True,
                  notes=["self-update is external: replace the package, then run "
                         "`ailocal start` so the changed bind mount is re-resolved"])
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
