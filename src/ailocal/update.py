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
from dataclasses import asdict, dataclass, field

SCHEMA_VERSION = 1

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
    notes: list[str] = field(default_factory=list)


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
    components = _images(mode) + _vendored(mode) + [_self(mode)]
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
