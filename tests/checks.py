#!/usr/bin/env python3
"""`ailocal check`, the read-only commands and documentation against their sources of truth.

The `exits` section runs `ailocal check` against the live installation and briefly rewrites the
active-tier marker in the config root (restored on exit). Everything else is in-process.
"""
from __future__ import annotations

import copy
import os
import re
import socket
import sys
import tempfile
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness import REPO, RESOURCES, Suite, run  # noqa: E402

from ailocal import policy as P  # noqa: E402
from ailocal.checks import BLOCKED, FAIL, PASS, SKIP, WARN, CheckResult, exit_code  # noqa: E402
from ailocal.checks import config as C  # noqa: E402
from ailocal.checks import services as S  # noqa: E402

suite = Suite("CHECKS, COMMANDS AND DOCS")
check = suite.check


def deterministic() -> None:
    suite.section("configuration checks need no stack")
    shipped = P.load_client_policy(RESOURCES)
    with patch.object(P, "load_client_policy", return_value=shipped):
        check(all(r.status is PASS for r in C.check_client_mappings()),
              "shipped client routing passes; delegate ceilings are not model targets")
    faults = [(("claude", "launch_default"), "missing-capability", "summary"),
              (("claude", "slots", "opus"), "missing-capability", "summary"),
              (("compat", "fixture-metadata"), "disk_gb", "summary"),
              (("codex", "delegate_max_mode"), "invalid-mode", "detail")]
    for path, value, field in faults:
        policy = copy.deepcopy(shipped)
        node = policy
        for key in path[:-1]:
            node = node[key]
        node[path[-1]] = value
        needle = "delegate_max_mode" if field == "detail" else value
        with patch.object(P, "load_client_policy", return_value=policy):
            check(any(r.status is FAIL and needle in (getattr(r, field) or "")
                      for r in C.check_client_mappings()), f"invalid {'.'.join(path)} fails the check")

    opened: list[str] = []

    def trap(target, *_a, **_k):
        opened.append(str(getattr(target, "full_url", target)))
        raise AssertionError("network call from a deterministic check")

    with patch.object(socket, "create_connection", trap), patch.object(urllib.request, "urlopen", trap):
        results = C.deterministic_checks()
    check(not opened, "deterministic checks open no socket", "; ".join(opened[:3]))
    check(results and all(isinstance(r, CheckResult) for r in results),
          f"deterministic checks return CheckResults ({len(results)})")
    mount = [r for r in results if r.name == "mount-drift"]
    check(mount and all(r.status is not FAIL or "readable" in r.summary for r in mount),
          "mount drift is deterministic; an unavailable Docker blocks it rather than failing")


def classification() -> None:
    suite.section("exit status and classification")
    for statuses, code in (((PASS,), 0), ((FAIL,), 1), ((WARN,), 0), ((BLOCKED,), 0), ((WARN, FAIL), 1)):
        results = [CheckResult(f"c{i}", s, "x") for i, s in enumerate(statuses)]
        check(exit_code(results) == code, f"{'+'.join(s.name for s in statuses)} exits {code}")
    roles = {"roles": {"completion": {"enabled": False, "max_input_tokens": 100},
                       "implementation": {"enabled": True, "max_input_tokens": 2000},
                       "cyber": {"enabled": True, "max_input_tokens": 500}}}
    with patch.object(S.policy, "effective_summary", return_value=roles), \
            patch.object(S, "http_json", return_value={"error": "ContextWindowExceededError"}) as request:
        verdict = S.check_context_window("fixture-key")
        payload = request.call_args.kwargs["payload"]
        check(verdict.status == PASS and payload["model"] == "ailocal-cyber"
              and len(payload["messages"][0]["content"].split()) > 500,
              "admission probe targets an enabled capability and exceeds its own input limit")
        request.reset_mock()
        check(S.check_context_window("fixture-key", "ailocal-completion").status == FAIL and not request.called,
              "a disabled probe target fails without inference")
    from ailocal.checks import host as Hst
    with patch.object(Hst.shutil, "which", lambda tool: None if tool == "rg" else "/bin/" + tool):
        rg = {r.name: r for r in Hst.check_cli_tools()}["cli:rg"]
    check(rg.status is WARN and rg.remediation == "brew install ripgrep",
          "a missing ripgrep warns with its install command (#64)")
    r = S.check_container("ailocal-definitely-not-a-container")
    check(r.status is FAIL and r.remediation, "an absent required container fails with remediation")
    # A reinstall leaves the container serving an empty bind mount while healthy; repair is `ailocal start`.
    for out in (SimpleNamespace(returncode=0, stdout="", stderr=""),
                SimpleNamespace(returncode=1, stdout="", stderr="ls: No such file or directory")):
        with patch.object(S, "container_state", return_value=("running", "")), \
                patch.object(S, "_run", return_value=out):
            r = S.check_config_mount()
        check(r.status is FAIL and r.remediation == "ailocal start",
              f"a detached config mount fails and points at `ailocal start` (rc {out.returncode})")


def bounded() -> None:
    suite.section("bounded and scoped probes")
    with patch.object(S, "ollama_installed", return_value={"fixture:gguf", "alias:latest"}):
        check(S.check_models_present(["fixture:mlx"]).status == FAIL
              and S.check_models_present(["fixture:gguf", "alias"]).status == PASS,
              "a required model matches its exact tag (omitted :latest resolves), never another variant")
    check(S.runtime._find({"fixture:gguf": {"size": 1}}, "fixture:mlx") is None,
          "residency cannot borrow another variant's memory or expiry")
    src = (REPO / "src/ailocal/checks/run.py").read_text()
    unbounded = [ln.strip() for ln in src.splitlines()
                 if re.search(r'(^|[;&|]|\$\()\s*curl\s', ln) and not ln.strip().startswith("#")
                 and not re.search(r'-m\s+\d|--max-time', ln)]
    check(not unbounded, "checks/run.py has no un-timeboxed curl call", "; ".join(unbounded[:2]))
    timeouts = {a: getattr(S, a, None) for a in ("CONNECT_TIMEOUT", "INSPECT_TIMEOUT", "GENERATE_TIMEOUT")}
    check(all(isinstance(v, int) and 0 < v <= 600 for v in timeouts.values()), f"service timeouts are bounded {timeouts}")

    # Default diagnostics never spend metered search quota: one free engine, and the federated
    # search reachable only through `--external-search`.
    services_src = (REPO / "src/ailocal/checks/services.py").read_text()
    probe = services_src.split("def check_searxng(")[1].split("def check_searxng_query(")[0]
    check(S.FREE_ENGINE_QUERY.strip().startswith("!wp") and S.FREE_ENGINE_NAME == "wikipedia"
          and "/config" in probe and "/search?" not in probe,
          "default search checks pin the free engine and the reachability probe queries none")
    callers = [q.relative_to(REPO) for q in (REPO / "src").rglob("*.py")
               if "/resources/" not in str(q) and q.name not in ("services.py", "run.py")
               and re.search(r"check_searxng_external|--external-search", q.read_text())]
    check("--external-search" in src and not callers,
          "the federated search is reachable only via `ailocal check --external-search`", str(callers))

    # Only this installation's containers are audited, never other projects or isolated test stacks.
    names = {"foreign": "/unrelated-service", "isolated": "/test-service", "local": "/custom-container-name"}
    calls = []

    def docker(argv, **_kw):
        calls.append(argv)
        value = ("local" if "compose" in argv and "ps" in argv else
                 "foreign isolated local" if argv[1:3] == ["ps", "-q"] else
                 "4000/tcp -> 127.0.0.1:4000" if argv[1] == "port" else
                 names[argv[2]] if argv[-1] == "{{.Name}}" else
                 "example/proxy:test@sha256:expected" if argv[-1] == "{{.Config.Image}}" else
                 "sha256:image" if argv[-1] == "{{.Image}}" else "example/proxy@sha256:expected")
        return SimpleNamespace(stdout=value, returncode=0)

    with patch.object(S, "_run", side_effect=docker):
        results = S.check_loopback()
        digest = S._running_digest("example/proxy")
    check(len(results) == 1 and "custom-container-name" in results[0].summary
          and [a for a in calls if a[1] == "port"] == [["docker", "port", "local"]]
          and digest == ("custom-container-name", "sha256:expected")
          and not any(a[1:3] == ["ps", "-q"] for a in calls),
          "reachability and digest checks select this installation and never enumerate the host")
    with tempfile.TemporaryDirectory() as box, patch.object(P, "deployed_client_root", return_value=Path(box)):
        fresh = C.check_local_artifacts().status
        (Path(box) / "claude").mkdir()
        (Path(box) / "claude/.claude.json").write_text('{"mcpServers": {"artifact": {"command": "missing"}}}')
        check(fresh is SKIP and C.check_local_artifacts().status is FAIL,
              "the artifact runtime is optional until registered, then must exist")


def exits() -> None:
    suite.section("`ailocal check` against the live installation")

    def rc(env: dict | None = None) -> int:
        return run([sys.executable, "-m", "ailocal.checks.run", "check"], timeout=900,
                   env={**os.environ, **(env or {})}).returncode

    check(rc({"AILOCAL_PROXY": "http://127.0.0.1:1"}) == 1, "check exits 1 when the proxy is unreachable")
    marker = P.active_profile_path()
    original, healthy = marker.read_text(), rc()
    try:
        marker.write_text("999gb\n")
        broken = rc()
    finally:
        marker.write_text(original)
    check(broken == 1, f"check exits 1 when the tier is unresolvable (got {broken})")
    config_layer = exit_code(C.deterministic_checks())
    after = rc()
    check(healthy == after == config_layer == 0,
          "a healthy install passes: check before and after a fault, and the config layer alone",
          f"check={healthy} restored={after} config-layer={config_layer}")


def commands() -> None:
    suite.section("read-only commands and the version")
    # `ailocal trace` and `status` once raised NameError on a deleted helper while every import check passed.
    for cmd in ("help", "--version", "status", "trace", "profile active-tier"):
        r = run([sys.executable, "-m", "ailocal.cli", *cmd.split()], timeout=120, cwd=REPO)
        tail = (r.stderr.strip().splitlines() or [""])[-1]
        check("Traceback" not in r.stderr and r.returncode != 124, f"`ailocal {cmd}` runs without a traceback", tail)

    def find(path: str, pattern: str) -> str:
        m = re.search(pattern, (REPO / path).read_text(encoding="utf-8"), re.M)
        return m.group(1) if m else ""
    project = find("pyproject.toml", r'^version = "([^"]+)"')
    package = find("src/ailocal/__init__.py", r'^__version__ = "([^"]+)"')
    check(project and project == package, "pyproject.toml and ailocal.__version__ agree", f"{project!r} vs {package!r}")
    check(f"## v{project}" in (REPO / "CHANGELOG.md").read_text(encoding="utf-8"),
          f"CHANGELOG.md has release notes for v{project}")


def docs() -> None:
    suite.section("documentation matches the implementation")
    # Shipped integration READMEs reach users inside the wheel, so they are documentation too.
    paths = [REPO / "README.md", REPO / "AGENTS.md", REPO / "RELEASING.md", *sorted((REPO / "docs").rglob("*.md")),
             *sorted((REPO / "src/ailocal/resources/integrations").rglob("*.md"))]
    text = {p: p.read_text(encoding="utf-8") for p in paths}

    def lines(pattern: str, exempt: str | None = None) -> list[str]:
        return [f"{p.relative_to(REPO)}:{i}" for p, t in text.items() for i, ln in enumerate(t.splitlines(), 1)
                if re.search(pattern, ln) and not (exempt and re.search(exempt, ln, re.I))]

    compose = (RESOURCES / "deploy/litellm/compose.yaml").read_text()
    m = re.search(r"AILOCAL_LITELLM_VERSION[=:]-?([0-9][^}\s]*)", compose)
    version = m.group(1) if m else ""
    check(version, "compose.yaml declares the validated LiteLLM version")
    ref = re.search(r"^\s*image:\s*(\S*litellm\S*)", compose, re.M)
    last = ref.group(1).split("@", 1)[0].rsplit("/", 1)[-1] if ref else ""
    tag = last.split(":", 1)[1] if ":" in last else ""
    # A floating tag is legitimate; a version tag must be the validated version.
    check(ref and (not re.fullmatch(r"v?\d+\.\d+(\.\d+)?", tag) or tag.lstrip("v") == version),
          "the litellm image tag agrees with AILOCAL_LITELLM_VERSION", f"tag={tag} version={version}")
    current = ".".join(version.split(".")[:2])
    stale = lines(r"LiteLLM\s+(?!" + re.escape(current) + r"\b)\d+\.\d+",
                              r"HISTORICAL|WITHDRAWN|SUPERSEDED|used to|no longer|changed at|previous|>=|≥")
    check(not stale, f"docs name LiteLLM {current} or mark another version historical", "; ".join(stale[:4]))

    check(not (RESOURCES / "deploy/litellm/hooks/system_transport.py").exists()
          and not lines("system_transport", r"HISTORICAL|removed|deleted|do not restore"),
          "the removed system_transport hook stays removed and undocumented as current")
    check((RESOURCES / "integrations/local-artifacts/server.py").is_file()
          and not lines(r"(git clone|pipx install|pip install).*local-artifacts"),
          "local-artifacts is bundled and no doc installs it separately")

    implementation = (RESOURCES / "clients/configure.template.zsh").read_text() + "".join(
        f.read_text() for f in sorted((REPO / "src/ailocal").rglob("*.py")))
    documented = {v for t in text.values() for v in re.findall(r"`(AILOCAL_[A-Z_]+)`", t)} - {"AILOCAL_"}
    missing = sorted(v for v in documented if v not in implementation and "ALIAS_OVERRIDE" not in v)
    check(not missing, "every documented AILOCAL_* variable exists in the implementation", ", ".join(missing))
    broken = [f"{p.relative_to(REPO)} -> {m.group(1)}" for p, t in text.items()
              for m in re.finditer(r"\]\(([^)#:]+\.md)(?:#[^)]*)?\)", t) if not (p.parent / m.group(1)).is_file()]
    check(not broken, "every relative markdown link resolves", "; ".join(broken[:5]))


if __name__ == "__main__":
    for section in (deterministic, classification, bounded, commands, docs, exits):
        try:
            section()
        except Exception as exc:  # noqa: BLE001 - one broken section must not hide the others
            suite.error(f"{section.__name__} section raised", exc)
    sys.exit(suite.report())
