"""Live runtime checks: containers, proxy, Ollama, search; every call bounded.

What can be decided w/o a running stack belongs in config.py.
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import shutil
import subprocess
import urllib.request

from .. import policy, runtime
from . import BLOCKED, FAIL, PASS, WARN, CheckResult

# Callers may adjust a timeout but never drop it: unbounded → a hung check.
CONNECT_TIMEOUT = 5      # reachability and metadata
INSPECT_TIMEOUT = 15     # docker / ollama subprocesses
GENERATE_TIMEOUT = 120   # one bounded model response

PROXY = os.environ.get("AILOCAL_PROXY", "http://127.0.0.1:4000")
OLLAMA = policy.ollama_url()
CONTAINER = os.environ.get("AILOCAL_LITELLM_CONTAINER", "ailocal-litellm")


class Unreachable(Exception):
    """A bounded call did not produce an answer."""


def http_json(url: str, *, token: str | None = None, payload: dict | None = None,
              timeout: int = CONNECT_TIMEOUT) -> dict:
    """Make one bounded HTTP/JSON call; raise Unreachable rather than hang."""
    from ailocal import primitives
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        status, raw = primitives.fetch(url, payload=payload, headers=headers,
                                       method="POST" if payload is not None else "GET", timeout=timeout)
    except primitives.Unreachable as exc:
        raise Unreachable(str(exc)) from exc
    body = raw.decode(errors="replace")
    try:
        return json.loads(body or "{}")
    except ValueError:
        if status >= 400:
            raise Unreachable(f"HTTP {status}: {body[:200]}") from None
        raise Unreachable(f"not JSON: {body[:200]}") from None


def _key_from(text: str, var: str) -> str:
    """Read VAR=value from a .env or shell-export file. Never logs the value."""
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("export "):
            line = line[len("export "):].strip()
        if line.startswith(f"{var}="):
            return line.split("=", 1)[1].strip().strip("\"'")
    return ""


def master_key() -> str:
    """Return the LiteLLM master key; only this module resolves credentials.

    Order matters: env.sh's client keys need not be the master key, and a wrong
    key surfaces as "No connected db", not as a credential error.
    """
    from .. import environment
    dotenv = environment.generated_file()
    env_sh = policy.data_root() / "clients" / "env.sh"
    files = {p: p.read_text() for p in (dotenv, env_sh) if p.is_file()}
    # (variable, files) in precedence order; the env outranks every file.
    for var, paths in (("LITELLM_MASTER_KEY", (dotenv, env_sh)),
                       ("ANTHROPIC_API_KEY", ()), ("OPENAI_API_KEY", ()),
                       ("ANTHROPIC_API_KEY", (env_sh,)),
                       ("OPENAI_API_KEY", (env_sh,))):
        if os.environ.get(var):
            return os.environ[var]
        for path in paths:
            key = _key_from(files.get(path, ""), var)
            if key:
                return key
    raise RuntimeError("no LiteLLM API key found — is the stack installed?")


def proxy_healthy(timeout: int = CONNECT_TIMEOUT) -> bool:
    """Return bare proxy reachability, for callers that want a boolean."""
    try:
        http_json(f"{PROXY}/health/liveliness", timeout=timeout)
        return True
    except Unreachable:
        return False


def _run(cmd: list[str], timeout: int = INSPECT_TIMEOUT, *,
         env: dict | None = None) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
    except FileNotFoundError:
        return subprocess.CompletedProcess(cmd, 127, "", f"{cmd[0]} not found")
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(cmd, 124, "", f"timed out after {timeout}s")


# docker

def docker_available() -> bool:
    return _run(["docker", "info", "--format", "{{.ServerVersion}}"]).returncode == 0


def container_state(name: str = CONTAINER) -> tuple[str, str]:
    """Return (state, health); state is 'absent' if Docker cannot see it."""
    r = _run(["docker", "inspect", "-f",
              "{{.State.Status}}\t{{if .State.Health}}{{.State.Health.Status}}{{end}}",
              name])
    if r.returncode != 0:
        return ("absent", "")
    state, _, health = r.stdout.strip().partition("\t")
    return (state or "absent", health)


def container_file(path: str, name: str = CONTAINER) -> str | None:
    """Read a file from inside the container, or None if unreadable."""
    r = _run(["docker", "exec", name, "cat", path])
    return r.stdout if r.returncode == 0 else None


def check_litellm_version(name: str = CONTAINER) -> CheckResult:
    """Check the running LiteLLM is the validated one; unverifiable fails.

    `main-stable` floats, so read the dist metadata (no __version__), not tag.
    """
    expected = _run(["docker", "exec", name, "printenv",
                     "AILOCAL_LITELLM_VERSION"]).stdout.strip()
    if not expected:
        return CheckResult("litellm-version", FAIL,
                           "AILOCAL_LITELLM_VERSION is not set in the container",
                           remediation="the compose file must declare the "
                                       "validated version")
    actual = ""
    r = _run(["docker", "exec", name, "sh", "-c",
              "cat /app/.venv/lib/python*/site-packages/litellm-*.dist-info/METADATA"])
    for line in r.stdout.splitlines():
        if line.startswith("Version:"):
            actual = line.split(":", 1)[1].strip()
            break
    if not actual:
        return CheckResult("litellm-version", FAIL,
                           f"could not read the installed LiteLLM version from {name}")
    if actual != expected:
        return CheckResult("litellm-version", FAIL,
                           f"VERSION DRIFT: validated {expected}, running {actual}",
                           remediation="the image moved, or the pin changed "
                                       "without re-validating: re-run the gate, "
                                       "then update digest, "
                                       "AILOCAL_LITELLM_VERSION and the docs "
                                       "together")
    return CheckResult("litellm-version", PASS,
                       f"LiteLLM {actual} (matches the validated version)")


# ollama

def ollama_installed() -> set[str]:
    """Return installed model tags; empty when the daemon is unreachable."""
    try:
        doc = http_json(f"{OLLAMA}/api/tags")
    except Unreachable:
        return set()
    return {m.get("name", "") for m in doc.get("models") or []}


def ollama_loaded() -> set[str]:
    """Return models currently resident."""
    try:
        doc = http_json(f"{OLLAMA}/api/ps")
    except Unreachable:
        return set()
    return {m.get("name", "") for m in doc.get("models") or []}


# proxy

def served_aliases(token: str) -> set[str]:
    doc = http_json(f"{PROXY}/v1/models", token=token)
    return {m.get("id", "") for m in doc.get("data") or []}


def model_info(token: str) -> dict[str, dict]:
    """Return alias → its advertised parameters."""
    doc = http_json(f"{PROXY}/model/info", token=token)
    out = {}
    for entry in doc.get("data") or []:
        name = entry.get("model_name")
        if name:
            out[name] = entry.get("model_info") or {}
    return out


# checks

def check_docker() -> CheckResult:
    if docker_available():
        return CheckResult("docker", PASS, "Docker daemon responding")
    return CheckResult("docker", FAIL, "Docker daemon not responding",
                       remediation="Start Docker Desktop, then: ailocal start")


def check_container(name: str = CONTAINER) -> CheckResult:
    state, health = container_state(name)
    if state == "absent":
        return CheckResult("container", FAIL, f"{name} is not present",
                           remediation="ailocal start")
    if state != "running":
        return CheckResult("container", FAIL, f"{name} is {state}",
                           remediation="ailocal start")
    if health and health != "healthy":
        return CheckResult("container", WARN, f"{name} running, health={health}",
                           remediation=f"docker logs {name} --tail=50")
    return CheckResult("container", PASS, f"{name} running"
                       + (f" ({health})" if health else ""))


def check_config_mount(name: str = CONTAINER) -> CheckResult:
    """Check the container can still see the hooks it started with.

    A reinstall replaces the bind-mounted resources dir; the container keeps
    the old, empty inode and stays healthy from memory until restart. Fix is
    `ailocal start`, which also reconciles compose/generated-cfg changes.
    """
    # Stopped: check_container already reports it; skip the `docker exec`.
    state, _ = container_state(name)
    if state != "running":
        return CheckResult("config-mount", PASS,
                           f"skipped — {name} is {state}")

    r = _run(["docker", "exec", name, "ls", "/app/config/hooks"])
    # Empty listing or "No such file" are the same fault → FAIL; WARN only
    # when inconclusive (docker unreachable, exec refused).
    gone = ("no such file" in (r.stderr or "").lower()
            or "no such file" in (r.stdout or "").lower())
    if r.returncode != 0 and not gone:
        return CheckResult("config-mount", WARN,
                           "could not read /app/config/hooks in the container",
                           r.stderr.strip()[:200] or None)
    if gone or not r.stdout.strip():
        return CheckResult(
            "config-mount", FAIL,
            "/app/config is EMPTY in the running container — it is serving stale "
            "in-memory hooks; a reinstall replaced the mounted directory",
            remediation="ailocal start")
    return CheckResult("config-mount", PASS,
                       f"container sees {len(r.stdout.split())} hook file(s)")


def check_proxy_health() -> CheckResult:
    if not proxy_healthy():
        return CheckResult("proxy-health", FAIL, "LiteLLM /health/liveliness failed",
                           remediation="docker logs ailocal-litellm --tail=50")
    return CheckResult("proxy-health", PASS, "LiteLLM healthy")


def check_aliases(token: str, expected: list[str]) -> list[CheckResult]:
    try:
        served = served_aliases(token)
    except Unreachable as exc:
        return [CheckResult("aliases", FAIL, "cannot read /v1/models", str(exc),
                            "ailocal start")]
    out = []
    for alias in expected:
        ok = alias in served
        out.append(CheckResult(f"alias:{alias}", PASS if ok else FAIL,
                               f"serves {alias}" if ok else f"{alias} is NOT served",
                               remediation=None if ok else "ailocal start"))
    return out


def check_geometry(token: str, expected: dict[str, int]) -> list[CheckResult]:
    """Check advertised max_input_tokens matches the profile.

    Clients trust the advertisement over the model's real limit.
    """
    try:
        info = model_info(token)
    except Unreachable as exc:
        return [CheckResult("geometry", FAIL, "cannot read /model/info", str(exc))]
    out = []
    for alias, want in expected.items():
        got = (info.get(alias) or {}).get("max_input_tokens")
        ok = got == want
        out.append(CheckResult(
            f"geometry:{alias}", PASS if ok else FAIL,
            f"{alias} advertises max_input={got}" + ("" if ok else f" (profile says {want})"),
            remediation=None if ok else "ailocal start"))
    return out


def check_ollama() -> CheckResult:
    if ollama_installed():
        return CheckResult("ollama", PASS, "Ollama responding")
    return CheckResult("ollama", FAIL, "Ollama not responding",
                       remediation="Start Ollama, then: ailocal start")


def check_models_present(required: list[str]) -> CheckResult:
    installed = ollama_installed()
    missing = [m for m in required
               if m not in installed and f"{m}:latest" not in installed]
    if not missing:
        return CheckResult("models", PASS, f"all {len(required)} required models present")
    return CheckResult("models", FAIL, f"missing models: {', '.join(missing)}",
                       remediation="ailocal install")


MANAGED_OLLAMA = pathlib.Path.home() / "Library/LaunchAgents/com.ailocal.ollama.plist"


def ollama_listener_pid(timeout: float | None = INSPECT_TIMEOUT) -> str:
    """Return the pid listening on :11434, or "" if none."""
    from ailocal.primitives import command_output
    pids = command_output("lsof", "-nP", "-tiTCP:11434", "-sTCP:LISTEN",
                          timeout=timeout).split()
    return pids[0] if pids else ""


def _listener_executable() -> str:
    """Return the resolved executable of the process listening on :11434.

    lsof's txt entry is the mapped file: symlink target, or a deleted path.
    """
    pid = ollama_listener_pid()
    if not pid:
        return ""
    try:
        out = subprocess.run(["lsof", "-p", pid, "-a", "-d", "txt", "-Fn"], capture_output=True,
                             text=True, timeout=INSPECT_TIMEOUT).stdout
    except (OSError, subprocess.SubprocessError):
        return ""
    return next((line[1:] for line in out.splitlines() if line.startswith("n")), "")


def check_managed_ollama_runtime() -> CheckResult:
    """Check :11434 runs the selected binary via com.ailocal.ollama.

    A deleted binary still serves resident models; new loads then fail.
    """
    import plistlib
    fix = "ailocal install"
    try:
        with MANAGED_OLLAMA.open("rb") as stream:
            program = plistlib.load(stream).get("ProgramArguments") or []
        binary = policy.ollama_binary()
    except (OSError, ValueError, SystemExit) as error:
        return CheckResult("ollama-service", FAIL, f"managed Ollama service unreadable ({error})",
                           remediation=fix)
    if program != [binary, "serve"]:
        return CheckResult("ollama-service", FAIL,
                           f"com.ailocal.ollama runs {program[:1] or 'nothing'}, not {binary}",
                           remediation=fix)
    if not pathlib.Path(binary).is_file():
        return CheckResult("ollama-service", FAIL, f"{binary} does not exist", remediation=fix)
    running, want = _listener_executable(), os.path.realpath(binary)
    if running != want:
        return CheckResult("ollama-service", FAIL,
                           f"the daemon on 11434 runs {running or 'nothing'}, not {want}",
                           remediation=fix)
    return CheckResult("ollama-service", PASS, f"managed Ollama runs {binary} ({want})")


def check_selected_ollama_runtime() -> CheckResult:
    """Check the explicit patched selection, incl. the daemon identity."""
    try:
        policy.ollama_binary()  # includes owned payload integrity verification
        expected = policy.ollama_patch_spec()["version"]
        actual = http_json(f"{OLLAMA}/api/version").get("version")
        if actual == expected:
            return CheckResult("ollama-runtime", PASS, f"selected patched Ollama {actual} is running")
        return CheckResult("ollama-runtime", FAIL, "selected Ollama runtime differs from the running daemon",
                           remediation="ailocal install")
    except (SystemExit, OSError, ValueError, Unreachable):
        return CheckResult("ollama-runtime", FAIL, "selected patched Ollama is unavailable or modified",
                           remediation="ailocal install")


def check_generation(token: str, alias: str) -> CheckResult:
    """Check one bounded response on `alias`; the only call loading a model."""
    try:
        doc = http_json(f"{PROXY}/v1/chat/completions", token=token, payload={
            "model": alias, "temperature": 0,
            "messages": [{"role": "user", "content": "Reply with exactly: smoke-ok"}],
        }, timeout=GENERATE_TIMEOUT)
    except Unreachable as exc:
        return CheckResult("generation", FAIL, f"{alias} did not answer", str(exc))
    if doc.get("error"):
        return CheckResult("generation", FAIL, f"{alias} returned an error",
                           str(doc["error"])[:200])
    choices = doc.get("choices") or []
    content = (choices[0].get("message", {}).get("content", "") if choices else "")
    if not content.strip():
        return CheckResult("generation", FAIL, f"{alias} returned an empty response")
    return CheckResult("generation", PASS, f"{alias} answered ({content.strip()[:40]})")


#: Default checks' only query: `!wp` hits free Wikipedia; a bare query fans
#: out to every engine, incl. token-backed ones that spend quota.
FREE_ENGINE_QUERY = "!wp test"
FREE_ENGINE_NAME = "wikipedia"


def _searxng_state() -> CheckResult | None:
    """Guard search checks on container state; None means running."""
    state, _ = container_state("ailocal-searxng")
    if state == "absent":
        return CheckResult("searxng", WARN, "SearXNG container not present",
                           remediation="ailocal start")
    if state != "running":
        return CheckResult("searxng", WARN, f"SearXNG is {state}",
                           remediation="ailocal start")
    return None


def check_searxng() -> CheckResult:
    """Check reachability and JSON-API health w/o dispatching any engine.

    /config queries nothing. Search is optional: unavailable degrades.
    """
    guard = _searxng_state()
    if guard is not None:
        return guard
    r = _run(["docker", "exec", CONTAINER, "python", "-c",
              "import urllib.request,json;"
              "d=json.loads(urllib.request.urlopen("
              "'http://searxng:8080/config', timeout=5).read());"
              "print(len(d.get('engines') or []))"])
    if r.returncode != 0:
        return CheckResult("searxng", WARN, "LiteLLM cannot reach the SearXNG JSON API",
                           r.stderr.strip()[:200], "ailocal start")
    return CheckResult("searxng", PASS,
                       f"SearXNG JSON API reachable from LiteLLM "
                       f"({r.stdout.strip()} engines configured, no query issued)")


def check_searxng_query() -> CheckResult:
    """Run one search pinned to a free engine and prove only that engine ran.

    Else re-enabled federation would silently spend quota on every smoke run.
    """
    guard = _searxng_state()
    if guard is not None:
        return guard
    probe = (
        "import urllib.request,json,urllib.parse;"
        f"q=urllib.parse.quote({FREE_ENGINE_QUERY!r});"
        "d=json.loads(urllib.request.urlopen("
        "f'http://searxng:8080/search?q={q}&format=json', timeout=15).read());"
        "res=d.get('results') or [];"
        "eng=sorted({e for r in res for e in (r.get('engines') or [r.get('engine')]) if e});"
        "print(len(res));print(','.join(eng))"
    )
    r = _run(["docker", "exec", CONTAINER, "python", "-c", probe])
    if r.returncode != 0:
        return CheckResult("searxng-query", WARN, "free-engine search did not complete",
                           r.stderr.strip()[:200], "ailocal start")
    lines = r.stdout.strip().splitlines()
    count = lines[0] if lines else "0"
    engines = [e for e in (lines[1].split(",") if len(lines) > 1 and lines[1] else []) if e]
    unexpected = [e for e in engines if e != FREE_ENGINE_NAME]
    if unexpected:
        return CheckResult("searxng-query", FAIL,
                           "a default search dispatched engines beyond "
                           f"{FREE_ENGINE_NAME}: {', '.join(unexpected)}",
                           "default checks must not spend external API quota",
                           "restrict the query to a free engine")
    return CheckResult("searxng-query", PASS,
                       f"free-engine search returned {count} result(s) "
                       f"from {FREE_ENGINE_NAME} only (no external quota used)")


def check_brave_key_configured() -> CheckResult:
    """Report where Brave can be spent w/o spending a query or printing the key.

    Only the research MCP's ledger may spend Brave; a SearXNG render naming
    braveapi is stale, an uncounted path until `ailocal start` rewrites it.
    """
    override = os.environ.get("AILOCAL_SEARXNG_SETTINGS")
    # The state root has one owner; do not re-derive it here.
    settings = (pathlib.Path(override) if override
                else policy.state_root() / "searxng" / "settings.yml")
    if settings.is_file() and re.search(r"(?m)^  - name:\s*braveapi\s*$", settings.read_text(errors="replace")):
        return CheckResult("brave-key", WARN, "rendered SearXNG settings still define braveapi "
                           "(an uncounted Brave path)", remediation="ailocal start")
    if not runtime.env_value("BRAVE_API"):
        return CheckResult("brave-key", PASS, "no Brave key; research uses keyless providers only")
    return CheckResult("brave-key", PASS, "Brave key configured for research only, behind its budget "
                       "ledger; default checks never query it")


def check_searxng_external() -> CheckResult:
    """Run a federated search reaching paid engines; opt-in only.

    Only via `ailocal check --external-search`; never from doctor, smoke, the
    gate, install validation or a loop.
    """
    guard = _searxng_state()
    if guard is not None:
        return guard
    r = _run(["docker", "exec", CONTAINER, "python", "-c",
              "import urllib.request,json;"
              "d=json.loads(urllib.request.urlopen("
              "'http://searxng:8080/search?q=test&format=json', timeout=20).read());"
              "res=d.get('results') or [];"
              "eng=sorted({e for r in res for e in (r.get('engines') or [r.get('engine')]) if e});"
              "print(len(res));print(','.join(eng))"])
    if r.returncode != 0:
        return CheckResult("searxng-external", WARN, "federated search did not complete",
                           r.stderr.strip()[:200])
    lines = r.stdout.strip().splitlines()
    return CheckResult("searxng-external", PASS,
                       f"federated search returned {lines[0] if lines else 0} result(s) "
                       f"from {lines[1] if len(lines) > 1 else 'no engines'} "
                       "(keyless external engines queried; no Brave)")


def check_search_tool_registered() -> CheckResult:
    """Check LiteLLM registered searxng-search at boot.

    A misplaced search_tools block leaves the proxy healthy but search absent.
    """
    r = _run(["docker", "logs", CONTAINER], timeout=INSPECT_TIMEOUT)
    if r.returncode != 0:
        return CheckResult("search-tool", BLOCKED, "cannot read proxy logs")
    if "searxng-search" in (r.stdout + r.stderr):
        return CheckResult("search-tool", PASS,
                           "LiteLLM registered search tool searxng-search at boot")
    return CheckResult("search-tool", WARN,
                       "LiteLLM did NOT register searxng-search",
                       remediation="check search_tools placement in config.yaml, then: ailocal start")


def check_context_window(token: str, alias: str | None = None) -> CheckResult:
    """Check an oversized prompt is rejected, not silently truncated.

    Unenforced, it returns 200 w/ a garbage answer.
    """
    roles = {"ailocal-" + role: cfg
             for role, cfg in policy.effective_summary()["roles"].items()
             if cfg["enabled"] and cfg.get("max_input_tokens")}
    if alias is None and roles:
        alias = min(roles, key=lambda name: roles[name]["max_input_tokens"])
    if alias not in roles:
        return CheckResult("context-window", FAIL,
                           "context-window probe has no enabled selected capability")
    # Each ' x' is one admission token. Size from the selected limit so a
    # disabled completion role is not a missing-model request.
    filler = " x" * (roles[alias]["max_input_tokens"] + 128)
    try:
        doc = http_json(f"{PROXY}/v1/chat/completions", token=token, payload={
            "model": alias, "max_tokens": 16,
            "messages": [{"role": "user", "content": filler}],
        }, timeout=GENERATE_TIMEOUT)
    except Unreachable as exc:
        return CheckResult("context-window", FAIL,
                           "context-window probe did not complete", str(exc))
    if "ContextWindowExceededError" in json.dumps(doc):
        return CheckResult("context-window", PASS,
                           "context-window validation ENFORCED (oversized prompt rejected)")
    return CheckResult("context-window", FAIL,
                       "context-window validation NOT enforced — oversized prompt accepted",
                       "model_registrar likely failed; local models are being "
                       "silently truncated",
                       "ailocal start")


# container supply chain: digest pins, running = declared, loopback only.
# Editing compose restarts nothing, so a repo can look patched yet serve old.

def declared_images() -> list[str]:
    """Return image references from the compose files, in declaration order."""
    out = []
    for compose in sorted(policy.data_root().glob("deploy/*/compose.yaml")):
        for line in compose.read_text().splitlines():
            stripped = line.strip()
            if stripped.startswith("image:"):
                ref = stripped.split(":", 1)[1].strip()
                if ref and not ref.startswith("$"):
                    out.append(ref)
    return out


def check_pinned(images: list[str]) -> list[CheckResult]:
    if not images:
        return [CheckResult("images", BLOCKED, "no images declared under deploy/")]
    return [CheckResult("pin", PASS, f"pinned by digest: {i.split('@')[0]}")
            if "@sha256:" in i else
            CheckResult("pin", FAIL, f"NOT pinned (floating tag): {i}",
                        remediation="pin the digest in deploy/*/compose.yaml")
            for i in images]


def _running_digest(repo: str) -> tuple[str, str]:
    """Return (container name, running repo digest) for this install's repo."""
    for cid in _running_containers():
        image = _run(["docker", "inspect", cid,
                      "--format", "{{.Config.Image}}"]).stdout.strip()
        if not image.startswith(repo):
            continue
        name = _run(["docker", "inspect", cid,
                     "--format", "{{.Name}}"]).stdout.strip().lstrip("/")
        digests = _run(["docker", "inspect",
                        _run(["docker", "inspect", cid,
                              "--format", "{{.Image}}"]).stdout.strip(),
                        "--format", "{{range .RepoDigests}}{{.}} {{end}}"]).stdout
        for d in digests.split():
            if d.startswith(f"{repo}@"):
                return name, d.split("@", 1)[1]
        return name, ""
    return "", ""


def check_no_drift(images: list[str]) -> list[CheckResult]:
    """Check the running digest is the declared one; they diverge silently."""
    out = []
    for image in images:
        if "@sha256:" not in image:
            continue
        ref, want = image.split("@", 1)
        repo = ref.split(":", 1)[0]
        name, got = _running_digest(repo)
        if not name:
            out.append(CheckResult("drift", BLOCKED, f"{repo}: not running"))
        elif not got:
            out.append(CheckResult("drift", WARN,
                                   f"{name}: running image has no repo digest"))
        elif got == want:
            out.append(CheckResult("drift", PASS,
                                   f"{name} runs the declared digest ({want[7:19]})"))
        else:
            out.append(CheckResult("drift", FAIL,
                                   f"{name} DRIFT — declared {want[7:19]}, "
                                   f"running {got[7:19]}",
                                   remediation="ailocal start"))
    return out


def _running_containers() -> list[str]:
    """List this install's containers; Compose resolves overrides."""
    from ailocal import runtime
    return _run(runtime.compose_argv(["ps", "-q", "--status", "running", "--orphans=false"]),
                env=runtime.compose_env()).stdout.split()


def check_loopback() -> list[CheckResult]:
    """Check this install's published ports stay on loopback.

    The proxy may leave it via `ailocal bind` (warned each run); else FAIL.
    """
    from ailocal import environment
    from ailocal.runtime import _bind_matches
    bind = environment.bind()
    out = []
    for cid in _running_containers():
        name = _run(["docker", "inspect", cid,
                     "--format", "{{.Name}}"]).stdout.strip().lstrip("/")
        ports = _run(["docker", "port", cid]).stdout.strip()
        if not ports:
            out.append(CheckResult("reachability", PASS,
                                   f"{name} publishes no ports"))
            continue
        mappings = [line.split()[-1] for line in ports.splitlines()]
        exposed = [m for m in mappings if not m.startswith(("127.0.0.1:", "[::1]:"))]
        if name == CONTAINER and bind != environment.BIND_LOCAL:
            if exposed and all(_bind_matches(bind, m) for m in exposed):
                out.append(CheckResult("reachability", WARN,
                                       f"{name} reachable off-host on {bind} by `ailocal bind`; "
                                       "the master key is the only protection",
                                       remediation="ailocal bind local && ailocal start"))
            elif not exposed:
                out.append(CheckResult("reachability", WARN,
                                       f"{name} still binds loopback; `ailocal bind` set {bind}",
                                       remediation="ailocal start"))
            else:
                out.append(CheckResult("reachability", FAIL,
                                       f"{name} bound to {' '.join(exposed)}, not the configured {bind}"))
            continue
        out.append(CheckResult("reachability", PASS, f"{name} bound to loopback only")
                   if not exposed else
                   CheckResult("reachability", FAIL,
                               f"{name} reachable off-host: {' '.join(exposed)}"))
    return out


def check_provenance(images: list[str]) -> list[CheckResult]:
    """Check publisher signatures; a digest proves bytes, not who published.

    Missing cosign degrades; never auto-installed. `cosign triangulate` cannot
    prove existence: it succeeds for unsigned images.
    """
    if not shutil.which("cosign"):
        return [CheckResult("provenance", WARN,
                            "cosign not installed — signatures cannot be verified",
                            remediation="brew install cosign")]
    out = []
    for image in images:
        repo = image.split("@")[0].split(":")[0]
        tree = _run(["cosign", "tree", image], timeout=60).stdout
        if "No Supply Chain Security Related Artifacts" in tree:
            out.append(CheckResult("provenance", WARN,
                                   f"{repo}: publisher signs nothing for this digest"))
        elif "Signatures for an image" in tree:
            # Presence ≠ validity: upstream documents no signer identity.
            out.append(CheckResult("provenance", PASS,
                                   f"{repo}: signature published for this digest "
                                   "(identity policy not configured)"))
        else:
            out.append(CheckResult("provenance", WARN,
                                   f"{repo}: could not determine signature status"))
    return out


def check_updates(images: list[str]) -> list[CheckResult]:
    """Discover upstream versions; never pull, never rewrite a pin.

    Compare versions where knowable: main-stable runs ahead of the newest tag,
    so digest inequality alone would propose a downgrade. Qdrant: not ours.
    """
    out = []
    for image in images:
        ref, cur = (image.split("@", 1) + [""])[:2]
        repo = ref.split(":", 1)[0]
        why = ""
        if repo == "ghcr.io/berriai/litellm":
            # Newest release, any age; version_policy.py decides (#106).
            from .. import audit, update, version_policy as vp
            try:
                listing = http_json("https://api.github.com/repos/BerriAI/"
                                    "litellm/releases?per_page=100", timeout=20)
            except Unreachable:
                listing = []
            pinned = _litellm_pinned_version()
            fixed, unread = update.advisory_fixes(audit.Pin(repo, "image", pinned, "deploy/litellm/compose.yaml",
                                                             github="BerriAI/litellm", pypi="litellm")) \
                if pinned and listing else ([], "")
            chosen, why = vp.candidate(vp.from_github(listing), fixed)
            if listing and not chosen:
                out.append(CheckResult("update", PASS, f"{repo}: no release is at least seven days old"
                                       + (f"; {unread}" if unread else "")))
                continue
            newest = f"v{chosen}" if chosen else ""
            candidate = f"{repo}:{newest}"
        elif repo == "searxng/searxng":
            # Rolling `latest`, no semver: hence the digest pin.
            newest, candidate = "latest", "searxng/searxng:latest"
        else:
            out.append(CheckResult("update", BLOCKED,
                                   f"{repo}: no upstream discovery strategy"))
            continue
        if not newest:
            out.append(CheckResult("update", BLOCKED,
                                   f"{repo}: could not reach upstream (offline?)"))
            continue
        remote = _run(["docker", "buildx", "imagetools", "inspect", candidate,
                       "--format", "{{.Manifest.Digest}}"], timeout=60).stdout.strip()
        pinned_version = _litellm_pinned_version() if "litellm" in repo else ""
        if pinned_version and newest.lstrip("v")[:1].isdigit():
            newer = sorted([pinned_version, newest.lstrip("v")],
                           key=lambda v: [int(x) for x in v.split(".") if x.isdigit()])
            if newer[-1] == pinned_version:
                out.append(CheckResult("update", PASS,
                                       f"{repo} is current ({pinned_version}, at or "
                                       f"ahead of the policy's candidate {newest})"))
                continue
        if remote and remote == cur:
            out.append(CheckResult("update", PASS, f"{repo} is current ({newest})"))
        else:
            out.append(CheckResult("update", WARN,
                                   f"{repo}: {newest} available for review"
                                   + (f" ({why})" if "litellm" in repo else ""),
                                   remediation="edit the digest in "
                                               "deploy/*/compose.yaml, then "
                                               "ailocal start, then run the gate"))
    return out


def _litellm_pinned_version() -> str:
    compose = policy.data_root() / "deploy" / "litellm" / "compose.yaml"
    for line in compose.read_text().splitlines() if compose.is_file() else []:
        if "AILOCAL_LITELLM_VERSION=" in line:
            return line.split("AILOCAL_LITELLM_VERSION=", 1)[1].strip().strip('"')
    return ""


def supply_chain_checks(check_update: bool = False) -> list[CheckResult]:
    if not docker_available():
        return [CheckResult("docker", BLOCKED,
                            "docker not installed — cannot assess container posture")]
    images = declared_images()
    results = check_pinned(images) + check_no_drift(images) + check_loopback() \
        + check_provenance(images)
    if check_update:
        results += check_updates(images)
    return results
