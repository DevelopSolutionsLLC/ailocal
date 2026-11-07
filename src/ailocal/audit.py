"""Audit every artifact ailocal pins against live advisory sources.

`ailocal check` proves the running digest is the declared one, not that the
declared one is still safe; that is an online question, kept out of the gate.

One result per pin:

    clean       every applicable source answered and named nothing -- today
    affected    inside an advisory's range; IDs and fixed versions listed
    yanked      unpublished, yanked or deprecated by its registry
    unverified  a lookup failed or nothing covers the pin -- NEVER read as clean

A registry 404 is an answer (how malware is withdrawn), not a failed lookup.
Images map explicitly to a GitHub repository; unmapped ones are unverified.
Running versions and digests are audited beside declared ones. Each run writes
`state_root()/audit/local.json` for Cadence; a week-old record is unverified.
"""
from __future__ import annotations

import json
import os
import re
import sys
import tomllib
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

SCHEMA_VERSION = 2   # 2: `modified` on advisories, `running` notes, `record`, `mode`

CLEAN = "clean"
AFFECTED = "affected"
YANKED = "yanked"
UNVERIFIED = "unverified"

_OSV = "https://api.osv.dev/v1/query"
_PYPI = "https://pypi.org/pypi/{name}/{version}/json"
_NPM = "https://registry.npmjs.org/{name}/{version}"
_GO_INFO = "https://proxy.golang.org/{mod}/@v/v{version}.info"
_GH_ADVISORIES = ("https://api.github.com/repos/{repo}/security-advisories"
                  "?per_page=100&state=published")

#: image → (GitHub repo, PyPI dist or None). Declared: a guess could audit the
#: wrong project clean.
IMAGE_SOURCES = {
    "ghcr.io/berriai/litellm": ("BerriAI/litellm", "litellm"),
    "searxng/searxng": ("searxng/searxng", None),
}
OLLAMA_MODULE = "github.com/ollama/ollama"
OLLAMA_REPO = "ollama/ollama"

#: Stated in every report so a clean result is never read as wider than it is.
COVERAGE_GAPS = [
    "container image layers are not scanned; use trivy on the running image ID (docs/maintenance.md)",
    "the running Ollama daemon is identified by /api/version only; payload integrity is `ailocal check`'s",
    "Go modules of the patched Ollama build beyond [security_dependencies] are not resolved",
    "model weights, AI clients and MCP packages: no advisory source is queried",
    "Go module retractions are not checked",
]


class Unavailable(Exception):
    """A source could not be asked. Becomes UNVERIFIED, never CLEAN."""


@dataclass
class Pin:
    name: str
    kind: str            # "image", "go", "npm"
    version: str         # "" when the declaration names no orderable version
    declared_in: str
    spec: str = ""
    github: str = ""     # publisher repository whose advisories apply
    pypi: str = ""       # PyPI distribution the image ships, if any
    notes: list[str] = field(default_factory=list)   # what is running, drift


@dataclass
class Result:
    name: str
    kind: str
    pin: str
    version: str
    declared_in: str
    result: str = CLEAN
    advisories: list[dict] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    queried_at: str = ""
    notes: list[str] = field(default_factory=list)



def http(url: str, body: dict | None = None, timeout: int = 20) -> tuple[int, Any]:
    """Return (status, parsed JSON or None); 404/410 are answers, not raised.

    A GitHub token from the env is sent to api.github.com only.

    Raises:
        Unavailable: Nothing answered.
    """
    from .primitives import Unreachable, fetch
    headers = {"Accept": "application/json", "User-Agent": "ailocal-audit"}
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token and url.startswith("https://api.github.com/"):
        headers["Authorization"] = f"Bearer {token}"
    try:
        status, raw = fetch(url, payload=body, headers=headers, timeout=timeout)
    except Unreachable as e:
        raise Unavailable(str(e)) from e
    if status in (404, 410):
        return status, None
    if status >= 400:
        raise Unavailable(f"HTTP {status}")
    try:
        return status, json.loads(raw.decode())
    except (ValueError, UnicodeDecodeError) as e:
        raise Unavailable(type(e).__name__) from e



_PRE = {"a": -3, "alpha": -3, "b": -2, "beta": -2, "rc": -1}


def _key(v: str) -> tuple | None:
    """Return (release tuple, pre-release rank), or None when unorderable.

    PEP 440 pre-releases sort before release; LiteLLM's `-stable` sorts as the
    bare version. Any other suffix is unorderable → UNVERIFIED, not guessed.
    """
    m = re.fullmatch(r"v?([0-9]+(?:\.[0-9]+)*)(?:-stable|-?(a|alpha|b|beta|rc)\.?([0-9]*))?",
                     v.strip())
    if not m:
        return None
    parts = [int(x) for x in m.group(1).split(".")]
    while len(parts) > 1 and parts[-1] == 0:
        parts.pop()
    pre = (_PRE[m.group(2)], int(m.group(3) or 0)) if m.group(2) else (0, 0)
    return tuple(parts), pre


_OPS = {"<": lambda a, b: a < b, "<=": lambda a, b: a <= b,
        ">": lambda a, b: a > b, ">=": lambda a, b: a >= b,
        "=": lambda a, b: a == b, "==": lambda a, b: a == b}


def in_range(version: str, rng: str) -> bool | None:
    """Match a GitHub `vulnerable_version_range`; None if unparseable."""
    v = _key(version)
    if v is None:
        return None
    clauses = [c.strip() for c in rng.split(",") if c.strip()]
    if not clauses:
        return None
    for clause in clauses:
        m = re.fullmatch(r"(<=|>=|==|<|>|=)?\s*(\S+)", clause)
        if m is None:
            return None
        bound = _key(m.group(2))
        if bound is None:
            return None
        if not _OPS[m.group(1) or "="](v, bound):
            return False
    return True


def _tag_version(tag: str) -> str:
    """Return a tag's release (`v1.2` → `1.2`, dated tags → date) or ""."""
    m = re.match(r"v?([0-9]+(?:\.[0-9]+)+)", tag)
    return m.group(1) if m else ""



def running() -> dict:
    """Return daemon version (or None) and running name, digest per image."""
    from .checks import services
    from .update import _split
    out: dict = {"ollama": None, "images": {}}
    try:
        out["ollama"] = services.http_json(f"{services.OLLAMA}/api/version").get("version")
    except services.Unreachable:
        pass
    for ref in services.declared_images():
        repo = _split(ref)[0]
        try:
            out["images"][repo] = services._running_digest(repo)
        except Exception:   # no docker: nothing is running
            out["images"][repo] = ("", "")
    return out


def pins(probe: Callable[[], dict] = running) -> list[Pin]:
    from . import policy
    from .checks import services
    from .update import _split
    live = probe()
    out: list[Pin] = []
    for ref in services.declared_images():
        repo, tag, digest = _split(ref)
        github, pypi = IMAGE_SOURCES.get(repo, ("", None))
        p = Pin(repo, "image", _tag_version(tag), "deploy/*/compose.yaml",
                spec=ref, github=github, pypi=pypi or "")
        name, got = live["images"].get(repo, ("", ""))
        if not name:
            p.notes.append("not running")
        elif got == digest:
            p.notes.append(f"running: {name} serves the declared digest")
        else:
            p.notes.append(f"running: {name} serves {got[:19] or 'an image with no repo digest'}, "
                           f"NOT the declared {digest[:19]}; `ailocal check` reports the drift and "
                           "a digest alone names no version to audit")
        out.append(p)

    manifest = policy.data_root() / "deploy" / "ollama" / "patch.toml"
    spec = tomllib.loads(manifest.read_text(encoding="utf-8"))
    where = "deploy/ollama/patch.toml"
    declared = Pin(OLLAMA_MODULE, "go", spec["base_version"], where,
                   spec=f"{OLLAMA_MODULE}@v{spec['base_version']} (patch base)",
                   github=OLLAMA_REPO)
    out.append(declared)
    ver = live["ollama"]
    base = _tag_version(ver or "")
    if ver is None:
        out.append(Pin(OLLAMA_MODULE, "go", "", "running: /api/version", spec="Ollama (running)",
                       github=OLLAMA_REPO, notes=["Ollama did not answer /api/version"]))
    elif base == spec["base_version"]:
        declared.notes.append(f"running: /api/version {ver}")
    else:
        declared.notes.append(f"running daemon is {ver}, not this base; audited separately below")
        out.append(Pin(OLLAMA_MODULE, "go", base, "running: /api/version",
                       spec=f"{OLLAMA_MODULE}@v{base} (running {ver})", github=OLLAMA_REPO))
    for mod, ver in spec.get("security_dependencies", {}).items():
        out.append(Pin(mod, "go", ver.lstrip("v"), f"{where} [security_dependencies]",
                       spec=f"{mod}@{ver}"))

    versions = (policy.data_root() / "integrations" / "local-artifacts"
                / "vendor" / "VERSIONS.txt")
    if versions.is_file():
        for line in versions.read_text().splitlines():
            name, _, ver = line.strip().partition(" ")
            if name and ver:
                out.append(Pin(name, "npm", ver, "local-artifacts/vendor/VERSIONS.txt",
                               spec=f"{name}@{ver}"))
    return out



def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _add(r: Result, adv: dict) -> None:
    """Add an advisory, merging on any shared ID across databases."""
    ids = {adv["id"], *adv.get("aliases", [])}
    for have in r.advisories:
        if ids & {have["id"], *have.get("aliases", [])}:
            have["aliases"] = sorted(set(have.get("aliases", [])) | (ids - {have["id"]}))
            return
    r.advisories.append(adv)


def _osv(r: Result, ecosystem: str, name: str, version: str, fetch: Callable[..., tuple[int, Any]]) -> None:
    body = {"package": {"name": name, "ecosystem": ecosystem}, "version": version}
    token = None
    while True:
        status, d = fetch(_OSV, {**body, **({"page_token": token} if token else {})})
        if status != 200 or d is None:
            raise Unavailable(f"OSV HTTP {status}")
        for v in d.get("vulns", []):
            if v.get("withdrawn"):
                continue
            fixed = sorted({e["fixed"] for a in v.get("affected", [])
                            if a.get("package", {}).get("name", "").lower() == name.lower()
                            for rg in a.get("ranges", []) if rg.get("type") != "GIT"
                            for e in rg.get("events", []) if "fixed" in e})
            _add(r, {"id": v["id"], "aliases": sorted(v.get("aliases", [])),
                     "summary": v.get("summary", ""), "fixed": fixed,
                     "severity": str((v.get("database_specific") or {})
                                     .get("severity", "")).lower(),
                     "modified": v.get("modified", ""),
                     # OSV files malicious packages under MAL- (OpenSSF feed).
                     "malicious": v["id"].startswith("MAL-")})
        token = d.get("next_page_token")
        if not token:
            break
    r.sources.append(f"osv.dev ({ecosystem})")


def _github(r: Result, repo: str, version: str, fetch: Callable[..., tuple[int, Any]]) -> None:
    status, d = fetch(_GH_ADVISORIES.format(repo=repo))
    if status != 200 or not isinstance(d, list):
        raise Unavailable(f"GitHub advisories HTTP {status}")
    if len(d) >= 100:
        raise Unavailable("advisory listing truncated at 100; paginate before trusting it")
    for a in d:
        for vuln in a.get("vulnerabilities") or []:
            rng = vuln.get("vulnerable_version_range") or ""
            hit = in_range(version, rng)
            if hit is None:
                raise Unavailable(f"cannot evaluate range {rng!r} of {a.get('ghsa_id')}")
            if hit:
                patched = vuln.get("patched_versions") or ""
                _add(r, {"id": a.get("ghsa_id", ""), "aliases": [a["cve_id"]] if a.get("cve_id") else [],
                         "summary": a.get("summary", ""), "fixed": [patched] if patched else [],
                         "severity": str(a.get("severity") or "").lower(),
                         "modified": a.get("updated_at", ""),
                         "malicious": a.get("type") == "malware"})
                break
    r.sources.append(f"github.com/{repo} security advisories")


def _registry(r: Result, url: str, label: str, fetch: Callable[..., tuple[int, Any]], deprecated_key: str = "") -> None:
    status, d = fetch(url)
    if status in (404, 410):
        r.result = YANKED
        r.notes.append(f"{r.version} is no longer published on {label}")
    elif status != 200:
        raise Unavailable(f"{label} HTTP {status}")
    elif label == "PyPI" and (d or {}).get("info", {}).get("yanked"):
        r.result = YANKED
        reason = d["info"].get("yanked_reason") or ""
        r.notes.append(f"yanked on PyPI{': ' + reason if reason else ''}")
    elif deprecated_key and (d or {}).get(deprecated_key):
        r.result = YANKED
        r.notes.append(f"deprecated on {label}: {d[deprecated_key]}")
    r.sources.append(label)


def check(p: Pin, fetch: Callable[..., tuple[int, Any]] | None = None) -> Result:
    fetch = fetch or http
    r = Result(p.name, p.kind, p.spec, p.version, p.declared_in, queried_at=_now(),
               notes=list(p.notes))
    if not p.version:
        r.result = UNVERIFIED
        r.notes.append("the declaration names no orderable version")
        return r
    if p.kind == "image" and not p.github:
        r.result = UNVERIFIED
        r.notes.append("no advisory source is declared for this image (IMAGE_SOURCES)")
        return r
    try:
        if p.kind == "image" and p.pypi:
            # Image ships this dist at the tag's version; PyPI files first.
            _registry(r, _PYPI.format(name=p.pypi, version=p.version), "PyPI", fetch)
            _osv(r, "PyPI", p.pypi, p.version, fetch)
        elif p.kind == "go":
            _osv(r, "Go", p.name, p.version, fetch)
            _registry(r, _GO_INFO.format(mod=p.name, version=p.version), "proxy.golang.org", fetch)
        elif p.kind == "npm":
            _osv(r, "npm", p.name, p.version, fetch)
            _registry(r, _NPM.format(name=p.name, version=p.version), "npm", fetch,
                      deprecated_key="deprecated")
        if p.github:
            _github(r, p.github, p.version, fetch)
    except Unavailable as e:
        # A later failure keeps earlier findings; only absence → UNVERIFIED.
        r.notes.append(f"lookup failed: {e}")
        if r.result != YANKED:
            r.result = UNVERIFIED
    if r.advisories and r.result != YANKED:
        r.result = AFFECTED
    return r


def report(fetch: Callable[..., tuple[int, Any]] | None = None, pin_list: list[Pin] | None = None) -> dict:
    results = [check(p, fetch) for p in (pins() if pin_list is None else pin_list)]
    worst = (YANKED if any(r.result == YANKED for r in results)
             else AFFECTED if any(r.result == AFFECTED for r in results)
             else UNVERIFIED if any(r.result == UNVERIFIED for r in results)
             else CLEAN)
    return {"schema_version": SCHEMA_VERSION, "project": "ailocal", "kind": "advisory-audit",
            "mode": "local", "status": worst,
            "queried_at": _now(), "coverage_gaps": COVERAGE_GAPS,
            "results": [asdict(r) for r in results]}


def record_path() -> Path:
    from . import policy
    return policy.state_root() / "audit" / "local.json"


def write_record(doc: dict) -> Path:
    """Overwrite the single latest-run record and return its path."""
    path = record_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=2) + "\n")
    return path


def exit_code(doc: dict) -> int:
    """1 when a pin is affected or yanked, 2 when only unverified, else 0."""
    return {YANKED: 1, AFFECTED: 1, UNVERIFIED: 2}.get(doc["status"], 0)



def _print(doc: dict) -> None:
    malicious = [(r["name"], a["id"]) for r in doc["results"] for a in r["advisories"]
                 if a.get("malicious")]
    if malicious:
        print("\n✗ INCIDENT — a pinned artifact is named as MALICIOUS:")
        for name, adv in malicious:
            print(f"    {name}: {adv}")
        print("  This is not an upgrade. Find where that exact version ran, rotate every\n"
              "  credential it could read, pin back to the last clean release, and check\n"
              "  the host for persistence.")
    print(f"\n▶ ailocal advisory audit ({doc['status']}, {doc['queried_at']})")
    mark = {CLEAN: "✓", AFFECTED: "✗", YANKED: "✗", UNVERIFIED: "⚠"}
    for r in doc["results"]:
        print(f"  {mark[r['result']]} {r['result']:10s} {r['name']} {r['version'] or '—'}"
              f"   [{r['declared_in']}]")
        for a in r["advisories"]:
            fixed = ", ".join(a["fixed"]) if a["fixed"] else "NO FIXED RELEASE"
            sev = f" {a['severity']}" if a["severity"] else ""
            print(f"        {a['id']}{sev}: {a['summary'][:90]}  (fixed: {fixed})")
        for n in r["notes"]:
            print(f"        {n}")
        print(f"        sources: {', '.join(r['sources']) or 'none answered'}")
    print("\n  Not covered:")
    for g in doc["coverage_gaps"]:
        print(f"    - {g}")
    if doc["status"] in (AFFECTED, YANKED):
        print("\n  Triage each advisory against this deployment before acting: upgrade to a\n"
              "  fixed release through the update lifecycle, remove the precondition, or\n"
              "  disable the component. An advisory with no fixed release cannot be\n"
              "  upgraded away. See docs/maintenance.md.")


def main(argv: list[str]) -> int:
    if {"-h", "--help"} & set(argv):
        print("usage: ailocal audit [--json]")
        return 0
    doc = report()
    try:
        doc["record"] = str(write_record(doc))
    except OSError as e:   # the audit's answer outranks its bookkeeping
        print(f"audit record not written ({e}); Cadence will report it missing", file=sys.stderr)
    if "--json" in argv:
        print(json.dumps(doc, indent=2))
    else:
        _print(doc)
        if doc.get("record"):
            print(f"\n  Recorded at {doc['record']}; `cadence audit --local` merges it for a week.")
    return exit_code(doc)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
