"""Deterministic checks: source, generated and deployed configuration.

Every check answers from files on disk, so `ailocal validate` runs with the
stack stopped. Docker is consulted only to compare the mounted config against
the repository copy; when it is absent that check reports BLOCKED and the rest
of validation continues.
"""

from __future__ import annotations

import json
import os
import re
import pathlib
import sys

from . import BLOCKED, FAIL, PASS, WARN, CheckResult

REPO = pathlib.Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(REPO / "scripts" / "lib"))

import policy as P  # noqa: E402


# ── source configuration ────────────────────────────────────────────────────

def check_active_tier() -> CheckResult:
    """Fail closed: an unresolvable tier must never fall through to a default."""
    try:
        tier = P.resolve_active_tier()
    except Exception as exc:
        return CheckResult("active-tier", FAIL, "cannot resolve the active profile",
                           f"{getattr(exc, 'code', type(exc).__name__)}: {exc}",
                           "ailocal install, or write the tier to $AILOCAL_STATE/active-profile")
    return CheckResult("active-tier", PASS, f"active tier resolves ({tier})")


def check_profiles_parse() -> list[CheckResult]:
    """Every tier must load under the one constrained parser."""
    out = []
    for tier in P.TIERS:
        try:
            prof = P.load_profile(tier)
        except Exception as exc:
            out.append(CheckResult(f"profile:{tier}", FAIL, f"{tier}.yaml is invalid",
                                   f"{getattr(exc, 'code', type(exc).__name__)}: {exc}"))
            continue
        roles = [r for r in P.ROLES if r in prof]
        out.append(CheckResult(f"profile:{tier}", PASS,
                               f"{tier} parses ({len(roles)} capabilities)"))
    return out


def check_capabilities_declare_backends(tier: str | None = None) -> list[CheckResult]:
    tier = tier or P.resolve_active_tier()
    profile = P.load_profile(tier)
    out = []
    for name in P.ROLES:
        if name not in profile:
            continue
        backend = P.resolve_role(tier, name).get("active")
        out.append(CheckResult(f"capability:{name}", PASS if backend else FAIL,
                               f"{name} → {backend or '(no backend!)'}"))
    return out


def check_client_mappings() -> list[CheckResult]:
    """Every clients.yaml mapping must target a capability this tier defines."""
    tier = P.resolve_active_tier()
    known = set(P.load_profile(tier))
    try:
        policy = P.load_client_policy()
    except P.ProfileError as exc:
        return [CheckResult("client-policy", FAIL, "clients.yaml is invalid",
                            f"{exc.code}: {exc}")]
    out = []
    for client, mapping in sorted(policy.items()):
        unknown = sorted({v for v in mapping.values()
                          if isinstance(v, str) and v not in known})
        out.append(CheckResult(
            f"client:{client}", FAIL if unknown else PASS,
            f"{client} mappings resolve" if not unknown
            else f"{client} targets unknown capabilities: {', '.join(unknown)}",
            remediation=None if not unknown else "edit profiles/clients.yaml"))
    return out


# ── generated state ─────────────────────────────────────────────────────────

def check_generated_present() -> list[CheckResult]:
    """Every generated artefact exists, under the one runtime root."""
    root = P.runtime_root()
    expected = [P.effective_profile_path(),
                root / "litellm" / "capabilities.json",
                root / "litellm" / "config.yaml",
                root / "integration-contract.json",
                root / "clients" / "model_catalog.json",
                root / "clients" / "configure.zsh",
                root / "clients" / "claude" / "settings.json",
                root / "clients" / "codex" / "config.toml"]
    missing = [str(p.relative_to(root)) for p in expected if not p.is_file()]
    return [CheckResult(
        "generated", PASS if not missing else FAIL,
        f"all {len(expected)} generated artefacts present under {root}"
        if not missing else f"missing: {', '.join(missing)}",
        remediation=None if not missing else "ailocal sync")]


def check_effective_profile() -> CheckResult:
    p = P.effective_profile_path()
    if not p.is_file():
        return CheckResult("effective-profile", FAIL, "effective-profile.json is missing",
                           remediation="ailocal sync")
    try:
        doc = json.loads(p.read_text())
    except ValueError as exc:
        return CheckResult("effective-profile", FAIL,
                           "effective-profile.json is not valid JSON", str(exc),
                           "ailocal sync")
    tier = doc.get("tier") or doc.get("profile")
    try:
        active = P.resolve_active_tier()
    except Exception:
        return CheckResult("effective-profile", BLOCKED,
                           "cannot compare: the active tier is unresolvable")
    ok = tier == active
    return CheckResult("effective-profile", PASS if ok else FAIL,
                       f"effective profile is {tier}" if ok
                       else f"effective profile is {tier}, active tier is {active}",
                       remediation=None if ok else "ailocal sync")


def _model_names(cfg: pathlib.Path) -> list[str]:
    """Declared model_name values. Commented examples are documentation: the
    config carries a disabled cloud-fallback block that names raw backend tags.
    """
    out = []
    for ln in cfg.read_text().splitlines():
        stripped = ln.strip()
        if stripped.startswith("#") or "model_name:" not in ln:
            continue
        out.append(ln.split("model_name:", 1)[1].split("#", 1)[0].strip())
    return out


def check_alias_uniqueness() -> CheckResult:
    """A duplicated alias silently shadows a capability."""
    cfg = P.runtime_root() / "litellm" / "config.yaml"
    if not cfg.is_file():
        return CheckResult("aliases", FAIL, "generated config.yaml is missing",
                           remediation="ailocal sync")
    names = _model_names(cfg)
    dupes = sorted({n for n in names if names.count(n) > 1})
    return CheckResult("aliases", FAIL if dupes else PASS,
                       f"{len(names)} model_name entries are unique" if not dupes
                       else f"DUPLICATED model_name: {', '.join(dupes)}",
                       remediation=None if not dupes else "ailocal sync")


def check_no_raw_backend_tags() -> CheckResult:
    """Clients must reach models through ailocal-<capability>, never a raw tag."""
    cfg = P.runtime_root() / "litellm" / "config.yaml"
    if not cfg.is_file():
        return CheckResult("raw-tags", FAIL, "generated config.yaml is missing")
    raw = [n for n in _model_names(cfg)
           if not n.startswith(("ailocal-", "bench-"))]
    return CheckResult("raw-tags", FAIL if raw else PASS,
                       "no raw backend tags exposed as model_name" if not raw
                       else f"raw backend tag(s) exposed: {', '.join(raw)}",
                       remediation=None if not raw else "ailocal sync")


# ── deployed client state ───────────────────────────────────────────────────

def check_codex_no_mcp() -> CheckResult:
    """Codex cannot dispatch namespaced tools, so an empty MCP section is correct."""
    out = []
    for label, path in (("generated", P.runtime_root() / "clients/codex/config.toml"),
                        ("deployed", pathlib.Path(os.environ.get(
                            "XDG_CONFIG_HOME", pathlib.Path.home() / ".config"))
                            / "ailocal/codex/config.toml")):
        if not path.is_file():
            continue
        n = sum(1 for ln in path.read_text().splitlines()
                if ln.startswith("[mcp_servers"))
        if n:
            out.append(f"{label}: {n} block(s)")
    return CheckResult("codex-mcp", FAIL if out else PASS,
                       "codex declares zero [mcp_servers.*] blocks" if not out
                       else f"codex declares MCP blocks — {'; '.join(out)}",
                       remediation=None if not out else "ailocal clients codex")


# ── deployed container state ────────────────────────────────────────────────

def check_mount_drift() -> CheckResult:
    """Container must run this repo's config.yaml; Docker absent is BLOCKED."""
    from . import services as S
    if not S.docker_available():
        return CheckResult("mount-drift", BLOCKED,
                           "cannot compare mounted config: Docker unavailable")
    state, _ = S.container_state()
    if state != "running":
        return CheckResult("mount-drift", BLOCKED,
                           f"cannot compare mounted config: container is {state}")
    inside = S.container_file("/app/generated/config.yaml")
    if inside is None:
        return CheckResult("mount-drift", FAIL,
                           "/app/generated/config.yaml is not readable in the container",
                           remediation="ailocal start")
    local = (P.runtime_root() / "litellm" / "config.yaml").read_text()
    ok = inside.strip() == local.strip()
    return CheckResult("mount-drift", PASS if ok else FAIL,
                       "container is running the repo's config.yaml" if ok
                       else "MOUNT DRIFT: the container config.yaml is not the generated one",
                       remediation=None if ok else "ailocal start")


# ── compose source layout ───────────────────────────────────────────────────

def check_compose_layout() -> list[CheckResult]:
    """deploy/ is canonical; a root compose file means two competing stacks."""
    out = []
    root_compose = [p for p in (REPO / "docker-compose.yml", REPO / "docker-compose.yaml")
                    if p.exists()]
    out.append(CheckResult(
        "compose-root", FAIL if root_compose else PASS,
        "no root docker-compose.yml (deploy/ is canonical)" if not root_compose
        else "root docker-compose.yml exists — the stack must be defined only under deploy/",
        remediation=None if not root_compose else "remove the root compose file"))

    published = []
    for f in sorted((REPO / "deploy").rglob("docker-compose*.yml")):
        for ln in f.read_text().splitlines():
            t = ln.strip().lstrip("- ").strip('"')
            if (t and t[0].isdigit() and ":" in t
                    and not t.startswith("127.0.0.1")):
                published.append(f"{f.relative_to(REPO)}: {t}")
    out.append(CheckResult(
        "compose-bindings", FAIL if published else PASS,
        "all published ports bound to 127.0.0.1" if not published
        else "service(s) published beyond localhost",
        detail="\n".join(published) or None,
        remediation=None if not published else "bind the port to 127.0.0.1"))
    return out


def _compose_env() -> dict:
    """Compose interpolates AILOCAL_SEARXNG_SETTINGS, which compose.sh exports
    after rendering. Point at the same rendered file so `config` can resolve.
    """
    return {**os.environ, "DOCKER_CLI_HINTS": "false",
            "AILOCAL_SEARXNG_SETTINGS": os.environ.get(
                "AILOCAL_SEARXNG_SETTINGS",
                str(P.runtime_root() / "searxng" / "settings.yml"))}


def _compose_json() -> dict | None:
    """The merged compose configuration, or None when Docker cannot render it."""
    import subprocess
    try:
        r = subprocess.run(
            ["docker", "compose", "--project-directory", str(REPO),
             "-f", str(REPO / "deploy" / "litellm" / "compose.yaml"),
             "-f", str(REPO / "deploy" / "searxng" / "compose.yaml"),
             "config", "--format", "json"],
            capture_output=True, text=True, timeout=60, cwd=REPO,
            env=_compose_env())
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    try:
        return json.loads(r.stdout)
    except ValueError:
        return None


def check_compose_config() -> list[CheckResult]:
    """Compose validity, one project, shared network, and api_base agreement.

    Service discovery breaks silently if the services land on different networks.
    """
    doc = _compose_json()
    if doc is None:
        return [CheckResult("compose-config", BLOCKED,
                            "cannot render the merged compose config "
                            "(Docker unavailable or the config is invalid)",
                            remediation="docker compose config")]
    out = [CheckResult("compose-config", PASS, "merged compose config is valid")]

    name = doc.get("name", "")
    out.append(CheckResult("compose-project", PASS if name == "ailocal" else FAIL,
                           f"single compose project: {name}" if name == "ailocal"
                           else f"compose project is {name or '?'}, expected 'ailocal'"))

    svcs = doc.get("services") or {}
    nets = {n: sorted((svcs.get(n) or {}).get("networks") or {})
            for n in ("litellm", "searxng")}
    shared = len({tuple(v) for v in nets.values()}) == 1 and all(nets.values())
    out.append(CheckResult(
        "compose-network", PASS if shared else FAIL,
        f"litellm and searxng share network: {','.join(nets['litellm'])}" if shared
        else f"litellm and searxng are NOT on a shared network: {nets}"))

    cfg = P.runtime_root() / "litellm" / "config.yaml"
    agrees = ("searxng" in svcs and cfg.is_file()
              and re.search(r"api_base:\s*http://searxng:8080", cfg.read_text()))
    out.append(CheckResult(
        "search-api-base", PASS if agrees else FAIL,
        "search api_base matches the searxng compose service name" if agrees
        else "search api_base and the searxng service name do not agree"))
    return out


def check_client_slots() -> list[CheckResult]:
    """Client slot assignments, from the rule policy owns."""
    try:
        problems = P.slot_problems()
    except P.ProfileError as exc:
        return [CheckResult("client-slots", FAIL, "cannot check client slots",
                            f"{exc.code}: {exc}")]
    if not problems:
        return [CheckResult("client-slots", PASS,
                            "claude slots respect profile geometry")]
    return [CheckResult("client-slots", FAIL if sev == "error" else WARN, msg,
                        remediation="edit profiles/clients.yaml")
            for sev, msg in problems]


def check_generated_in_sync() -> CheckResult:
    """Regeneration is a fixed point; drift means a hand edit. Active tier only."""
    import subprocess
    try:
        r = subprocess.run([sys.executable, str(REPO / "scripts" / "sync-models.py"), "--check"],
                           capture_output=True, text=True, timeout=180)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return CheckResult("generated-sync", BLOCKED,
                           "cannot check generated drift", str(exc))
    if r.returncode in (126, 127):
        # The generator itself could not run (a missing tool, not drift).
        # Reporting FAIL here would blame the configuration for a broken host.
        return CheckResult("generated-sync", BLOCKED,
                           "cannot check generated drift: the generator did not run",
                           (r.stdout + r.stderr).strip()[-200:] or f"exit {r.returncode}")
    if r.returncode == 0:
        return CheckResult("generated-sync", PASS,
                           (r.stdout.strip().splitlines() or ["generated files in sync"])[-1])
    return CheckResult("generated-sync", FAIL,
                       "generated files have drifted from their source",
                       (r.stdout + r.stderr).strip()[-400:],
                       "ailocal sync && commit the result")


def deterministic_checks(tier: str | None = None) -> list[CheckResult]:
    """Everything `ailocal validate` runs. No live service calls."""
    results: list[CheckResult] = [check_active_tier()]
    results += check_profiles_parse()
    results += check_capabilities_declare_backends(tier)
    results += check_client_mappings()
    results += check_compose_layout()
    results += check_compose_config()
    results += check_generated_present()
    results += check_client_slots()
    results += [check_effective_profile(), check_alias_uniqueness(),
                check_no_raw_backend_tags(), check_codex_no_mcp(),
                check_mount_drift()]
    # Generated-file drift only makes sense against the active tier.
    if tier is None:
        results.append(check_generated_in_sync())
    return results
