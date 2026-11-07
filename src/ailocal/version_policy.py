"""Upstream release adoption policy; docs/architecture.md#version-policy.

Candidate: newest stable release published ≥ 7 days ago (GitHub
`published_at`); younger only if it fixes an advisory against the declared
version. The age defends against compromised releases, usually withdrawn in
days. Go "supported": newest point release of a line go.dev/dl lists.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

MIN_AGE = timedelta(days=7)
GO_RELEASES = "https://go.dev/dl/?mode=json"
_STABLE = re.compile(r"v?(\d+(?:\.\d+)+)")


@dataclass(frozen=True)
class Release:
    version: str        # without a leading "v"
    published_at: str   # ISO 8601, from the publisher's release record


def key(version: str) -> tuple[int, ...]:
    return tuple(int(x) for x in version.split("."))


def stable(version: str) -> str:
    """Return a stable tag's numeric version, or "" for anything else."""
    m = _STABLE.fullmatch(version.strip())
    return m.group(1) if m else ""


def from_github(releases: object) -> list[Release]:
    """Return stable, published GitHub releases, newest first."""
    out = {}
    for r in releases if isinstance(releases, list) else []:
        if not isinstance(r, dict) or r.get("prerelease") or r.get("draft"):
            continue
        v = stable(str(r.get("tag_name") or ""))
        if v and r.get("published_at"):
            out.setdefault(v, Release(v, str(r["published_at"])))
    return sorted(out.values(), key=lambda r: key(r.version), reverse=True)


def age(release: Release, now: datetime | None = None) -> timedelta | None:
    try:
        when = datetime.fromisoformat(release.published_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return (now or datetime.now(timezone.utc)) - when


def fixes(version: str, fixed: Sequence[str]) -> bool:
    """Return whether `version` contains any advisory fix version in `fixed`."""
    have = key(version)
    for f in fixed:
        v = stable(f.split()[-1].lstrip(">=")) if f else ""
        if v and have >= key(v):
            return True
    return False


def candidate(releases: list[Release], fixed: Sequence[str] = (), now: datetime | None = None) -> tuple[str, str]:
    """Return (version, why) of the adopted release, or ("", why) if none.

    `releases` newest first; `fixed` holds fix versions of advisories against
    the declared version, which admit a younger release.
    """
    for r in releases:
        a = age(r, now)
        if a is None:
            continue
        if a >= MIN_AGE:
            return r.version, f"newest release at least seven days old (published {r.published_at[:10]})"
        if fixed and fixes(r.version, list(fixed)):
            return r.version, f"security release (published {r.published_at[:10]}, under seven days)"
    return "", "no release is at least seven days old"


def go_supported(version: str, listing: list) -> tuple[bool, str]:
    """Return whether a Go toolchain is the newest point of a supported line."""
    current = [stable(str(r.get("version", "")).removeprefix("go"))
               for r in listing if isinstance(r, dict) and r.get("stable")]
    current = [v for v in current if v]
    if not current:
        return False, "go.dev listed no supported release"
    v = stable(version.removeprefix("go"))
    if v in current:
        return True, f"go{v} is current"
    return False, f"go{v or version} is not a current supported release (supported: " + \
        ", ".join(f"go{c}" for c in current) + ")"
