"""Deterministic checks: source, generated and deployed configuration.

Every check here answers from files on disk. None of them needs a running
LiteLLM, Ollama or model, which is the point: `ailocal validate` has to be
usable on a stopped stack, and it previously was not -- a static run failed with
"could not read `ollama list`" because a daemon was down.

Docker is consulted for one thing only: comparing the config the container
mounted against the repository copy. That is still deterministic consistency
rather than runtime inference, so when Docker is absent the check reports
BLOCKED and the rest of validation continues.
"""

from __future__ import annotations

import json
import os
import pathlib
import sys

from . import BLOCKED, FAIL, PASS, CheckResult

REPO = pathlib.Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(REPO / "scripts" / "lib"))

import profile_config as P  # noqa: E402


def _sync():
    """sync-models.py, loaded by path: it is a script, not an importable module."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "sync_models", REPO / "scripts" / "sync-models.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── source configuration ────────────────────────────────────────────────────

def check_active_tier() -> CheckResult:
    """Fail closed: an unresolvable tier must never fall through to a default."""
    try:
        tier = P.resolve_active_tier()
    except Exception as exc:
        return CheckResult("active-tier", FAIL, "cannot resolve the active profile",
                           f"{getattr(exc, 'code', type(exc).__name__)}: {exc}",
                           "echo <tier> > config/active-profile")
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
    sm = _sync()
    models = sm.load_models_yaml(sm.profile_path(explicit=tier))
    out = []
    for name, cfg in sorted(models.items()):
        backend = (cfg or {}).get("active")
        out.append(CheckResult(f"capability:{name}", PASS if backend else FAIL,
                               f"{name} → {backend or '(no backend!)'}"))
    return out


def check_client_mappings() -> list[CheckResult]:
    """Every clients.yaml mapping must target a capability this tier defines."""
    sm = _sync()
    models = sm.load_models_yaml(sm.profile_path())
    clients = sm.load_clients_yaml()
    out = []
    for client, mapping in sorted(clients.items()):
        if not isinstance(mapping, dict):
            continue
        unknown = sorted({v for v in mapping.values()
                          if isinstance(v, str) and v not in models})
        out.append(CheckResult(
            f"client:{client}", FAIL if unknown else PASS,
            f"{client} mappings resolve" if not unknown
            else f"{client} targets unknown capabilities: {', '.join(unknown)}",
            remediation=None if not unknown else "edit config/clients.yaml"))
    return out


# ── generated state ─────────────────────────────────────────────────────────

def check_generated_present() -> list[CheckResult]:
    """The generated artefacts every consumer reads must exist."""
    expected = [
        "config/effective-profile.json",
        "config/capabilities.generated.json",
        "config/clients/model_catalog.json",
        "config/litellm/config.yaml",
        "config/integration-contract.json",
    ]
    out = []
    for rel in expected:
        p = REPO / rel
        out.append(CheckResult(f"generated:{rel}", PASS if p.is_file() else FAIL,
                               rel if p.is_file() else f"{rel} is missing",
                               remediation=None if p.is_file() else "ailocal sync"))
    return out


def check_effective_profile() -> CheckResult:
    p = REPO / "config" / "effective-profile.json"
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
    cfg = REPO / "config" / "litellm" / "config.yaml"
    if not cfg.is_file():
        return CheckResult("aliases", FAIL, "config/litellm/config.yaml is missing",
                           remediation="ailocal sync")
    names = _model_names(cfg)
    dupes = sorted({n for n in names if names.count(n) > 1})
    return CheckResult("aliases", FAIL if dupes else PASS,
                       f"{len(names)} model_name entries are unique" if not dupes
                       else f"DUPLICATED model_name: {', '.join(dupes)}",
                       remediation=None if not dupes else "ailocal sync")


def check_no_raw_backend_tags() -> CheckResult:
    """Clients must reach models through ailocal-<capability>, never a raw tag."""
    cfg = REPO / "config" / "litellm" / "config.yaml"
    if not cfg.is_file():
        return CheckResult("raw-tags", FAIL, "config/litellm/config.yaml is missing")
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
    for label, path in (("generated", REPO / "config/clients/codex/config.toml"),
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
    """The container must be running this repository's config.yaml.

    Unique to validate-deployment.sh before consolidation. A stale mount leaves
    every source check passing while the proxy serves something else entirely.
    Docker is required, so its absence is BLOCKED rather than a failure.
    """
    from . import services as S
    if not S.docker_available():
        return CheckResult("mount-drift", BLOCKED,
                           "cannot compare mounted config: Docker unavailable")
    state, _ = S.container_state()
    if state != "running":
        return CheckResult("mount-drift", BLOCKED,
                           f"cannot compare mounted config: container is {state}")
    inside = S.container_file("/app/config/config.yaml")
    if inside is None:
        return CheckResult("mount-drift", FAIL,
                           "/app/config/config.yaml is not readable in the container",
                           remediation="ailocal start")
    local = (REPO / "config" / "litellm" / "config.yaml").read_text()
    ok = inside.strip() == local.strip()
    return CheckResult("mount-drift", PASS if ok else FAIL,
                       "container is running the repo's config.yaml" if ok
                       else "MOUNT DRIFT: container config.yaml != config/litellm/config.yaml",
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


def check_generated_in_sync() -> CheckResult:
    """Regenerating must be a fixed point: drift means someone hand-edited.

    Only meaningful for the ACTIVE tier -- generated files reflect that one.
    """
    import subprocess
    try:
        r = subprocess.run([str(REPO / "scripts" / "sync-models.sh"), "--check"],
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
    results += check_generated_present()
    results += [check_effective_profile(), check_alias_uniqueness(),
                check_no_raw_backend_tags(), check_codex_no_mcp(),
                check_mount_drift()]
    # Generated-file drift only makes sense against the active tier.
    if tier is None:
        results.append(check_generated_in_sync())
    return results
