"""Which upstream release ailocal may adopt. docs/architecture.md#version-policy.

A candidate is the newest stable release whose publication is at least seven
full days old. A younger release qualifies only when it contains the fix for an
advisory that affects the declared version: a security fix may not wait a week.
Publication evidence is the publisher's release record (GitHub `published_at`),
never a commit or build time.

The rule replaces a count ("two releases behind"), which a fast release train
turns into an arbitrary age and a slow one into a vulnerable pin: [REAL]
2026-11-02 it proposed go1.27.1 over go1.27.3 with 14 stdlib advisories open
against it (DevelopSolutionsLLC/cadence#134). The age is the defence against a
compromised release, which is usually withdrawn within days: [REAL] 2026-10-22
LiteLLM 1.107.0 shipped a credential stealer (BerriAI/litellm#47826).

For Go, "supported" is exact: the newest point release of one of the two
supported minor lines, which is what go.dev/dl/?mode=json lists.
"""

from __future__ import annotations

import re
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
    """The numeric version of a stable tag, or "" for a pre-release or anything else."""
    m = _STABLE.fullmatch(version.strip())
    return m.group(1) if m else ""


def from_github(releases: list) -> list[Release]:
    """Stable, published GitHub releases, newest first."""
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


def fixes(version: str, fixed: list[str]) -> bool:
    """Whether `version` contains any of the advisory fix versions in `fixed`."""
    have = key(version)
    for f in fixed:
        v = stable(f.split()[-1].lstrip(">=")) if f else ""
        if v and have >= key(v):
            return True
    return False


def candidate(releases: list[Release], fixed: list[str] = (), now: datetime | None = None) -> tuple[str, str]:
    """(version, why) of the release the policy adopts, or ("", why) when none qualifies.

    `releases` newest first; `fixed` lists the fix versions of advisories that
    affect the declared version, which admit a younger release."""
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
    """Whether a Go toolchain is the newest point release of a supported line."""
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
