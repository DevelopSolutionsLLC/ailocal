"""Live runtime checks: containers, proxy, Ollama, search.

One implementation per primitive, and every call is bounded. Nothing here is
deterministic; what can be decided without a running stack belongs in config.py.
"""

from __future__ import annotations

import json
import os
import pathlib
import socket
import subprocess
import urllib.error
import urllib.request

from . import BLOCKED, FAIL, PASS, WARN, CheckResult

# One timeout policy. Callers may tighten or extend it for a specific call, but
# no call may opt out: an unbounded request in a validator is how a diagnostic
# turns into a hang.
CONNECT_TIMEOUT = 5      # reachability and metadata
INSPECT_TIMEOUT = 15     # docker / ollama subprocesses
GENERATE_TIMEOUT = 120   # one bounded model response

PROXY = os.environ.get("AILOCAL_PROXY", "http://127.0.0.1:4000")
OLLAMA = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")
CONTAINER = os.environ.get("AILOCAL_LITELLM_CONTAINER", "ailocal-litellm")


class Unreachable(Exception):
    """A bounded call did not produce an answer."""


def http_json(url: str, *, token: str | None = None, payload: dict | None = None,
              timeout: int = CONNECT_TIMEOUT) -> dict:
    """One bounded HTTP/JSON call. Raises Unreachable rather than hanging."""
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")
        try:
            return json.loads(body)
        except ValueError:
            raise Unreachable(f"HTTP {exc.code}: {body[:200]}") from exc
    except (urllib.error.URLError, OSError, ValueError, TimeoutError) as exc:
        raise Unreachable(str(exc)) from exc


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
    """The LiteLLM master key.

    Resolution order matters. clients/env.sh carries ANTHROPIC_API_KEY
    and OPENAI_API_KEY for the CLIENTS, and those are not necessarily the master
    key -- measured, they were 12-character placeholders while the running proxy
    held a 51-character key. An unrecognised key sends LiteLLM to a key database
    that does not exist here, so a credential fault surfaces as "No connected
    db." The master key is resolved from its own sources first.

    Callers must not construct this themselves; nothing outside this module
    should know where credentials live.
    """
    if os.environ.get("LITELLM_MASTER_KEY"):
        return os.environ["LITELLM_MASTER_KEY"]
    repo = pathlib.Path(__file__).resolve().parent.parent.parent
    for path in (repo / ".env", repo / "clients" / "env.sh"):
        if path.is_file():
            for line in path.read_text().splitlines():
                line = line.strip().lstrip("export ").strip()
                if line.startswith("LITELLM_MASTER_KEY="):
                    return line.split("=", 1)[1].strip().strip("\"'")
    for var in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        if os.environ.get(var):
            return os.environ[var]
    env = repo / "clients" / "env.sh"
    if env.is_file():
        for var in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
            for line in env.read_text().splitlines():
                line = line.strip().lstrip("export ").strip()
                if line.startswith(f"{var}="):
                    return line.split("=", 1)[1].strip().strip("\"'")
    raise RuntimeError("no LiteLLM API key found — is the stack installed?")


def proxy_healthy(timeout: int = CONNECT_TIMEOUT) -> bool:
    """Bare reachability, for callers that want a boolean rather than a report."""
    try:
        http_json(f"{PROXY}/health/liveliness", timeout=timeout)
        return True
    except Unreachable:
        return False


def port_open(host: str, port: int, timeout: int = CONNECT_TIMEOUT) -> bool:
    """Bounded TCP reachability, without spawning curl."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _run(cmd: list[str], timeout: int = INSPECT_TIMEOUT) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        return subprocess.CompletedProcess(cmd, 127, "", f"{cmd[0]} not found")
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(cmd, 124, "", f"timed out after {timeout}s")


# ── docker ──────────────────────────────────────────────────────────────────

def docker_available() -> bool:
    return _run(["docker", "info", "--format", "{{.ServerVersion}}"]).returncode == 0


def container_state(name: str = CONTAINER) -> tuple[str, str]:
    """(state, health). state is 'absent' when Docker cannot see the container."""
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


# ── ollama ──────────────────────────────────────────────────────────────────

def ollama_installed() -> set[str]:
    """Installed model tags. Empty set when the daemon is unreachable."""
    try:
        doc = http_json(f"{OLLAMA}/api/tags")
    except Unreachable:
        return set()
    return {m.get("name", "") for m in doc.get("models") or []}


def ollama_loaded() -> set[str]:
    """Models currently resident."""
    try:
        doc = http_json(f"{OLLAMA}/api/ps")
    except Unreachable:
        return set()
    return {m.get("name", "") for m in doc.get("models") or []}


# ── proxy ───────────────────────────────────────────────────────────────────

def served_aliases(token: str) -> set[str]:
    doc = http_json(f"{PROXY}/v1/models", token=token)
    return {m.get("id", "") for m in doc.get("data") or []}


def model_info(token: str) -> dict[str, dict]:
    """alias -> its advertised parameters."""
    doc = http_json(f"{PROXY}/model/info", token=token)
    out = {}
    for entry in doc.get("data") or []:
        name = entry.get("model_name")
        if name:
            out[name] = entry.get("model_info") or {}
    return out


# ── checks ──────────────────────────────────────────────────────────────────

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


def check_proxy_port() -> CheckResult:
    host, _, port = PROXY.rsplit("//", 1)[-1].partition(":")
    if port_open(host, int(port or 80)):
        return CheckResult("proxy-port", PASS, f"proxy reachable at {host}:{port}")
    return CheckResult("proxy-port", FAIL, f"proxy not reachable at {host}:{port}",
                       remediation="ailocal start")


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
                               remediation=None if ok else "ailocal sync && ailocal start"))
    return out


def check_geometry(token: str, expected: dict[str, int]) -> list[CheckResult]:
    """Advertised max_input_tokens must match the profile.

    Clients trust the advertisement over the model's real limit, so a proxy on
    stale config silently gives them the wrong window.
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
            remediation=None if ok else "ailocal sync && ailocal start"))
    return out


def check_ollama() -> CheckResult:
    if ollama_installed():
        return CheckResult("ollama", PASS, "Ollama responding")
    return CheckResult("ollama", FAIL, "Ollama not responding",
                       remediation="Start Ollama, then: ailocal start")


def check_models_present(required: list[str]) -> CheckResult:
    installed = ollama_installed()
    bases = {t.split(":")[0] for t in installed}
    missing = [m for m in required
               if m not in installed and m.split(":")[0] not in bases]
    if not missing:
        return CheckResult("models", PASS, f"all {len(required)} required models present")
    return CheckResult("models", FAIL, f"missing models: {', '.join(missing)}",
                       remediation="ailocal models install")


def check_generation(token: str, alias: str = "ailocal-fast") -> CheckResult:
    """One bounded model response. The only call here that loads a model."""
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


#: The one SearXNG query any DEFAULT check may issue. `!wp` restricts dispatch
#: to Wikipedia, which costs nothing. A bare query fans out to every enabled
#: engine — including braveapi, which spends a metered Brave allowance on each
#: call. A local health check must never consume an external quota.
FREE_ENGINE_QUERY = "!wp test"
FREE_ENGINE_NAME = "wikipedia"


def _searxng_state() -> CheckResult | None:
    """Container-state guard shared by the search checks. None means running."""
    state, _ = container_state("ailocal-searxng")
    if state == "absent":
        return CheckResult("searxng", WARN, "SearXNG container not present",
                           remediation="ailocal start")
    if state != "running":
        return CheckResult("searxng", WARN, f"SearXNG is {state}",
                           remediation="ailocal start")
    return None


def check_searxng() -> CheckResult:
    """Reachability and JSON-API health, WITHOUT dispatching any engine.

    /config is served by SearXNG itself and queries nothing, so this costs no
    external request. Search is optional: unavailable search degrades, it does
    not fail.
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
    """One real search, pinned to a free engine, and PROVE only that engine ran.

    Asserting on the returned engine names is the point: a configuration change
    that re-enables federation would otherwise silently start spending Brave
    quota on every smoke run.
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
    """Report whether a Brave key is present WITHOUT spending a query."""
    import os
    import pathlib as _pl
    import sys as _sys
    override = os.environ.get("AILOCAL_SEARXNG_SETTINGS")
    if override:
        settings = _pl.Path(override)
    else:
        # The state root has one owner; do not re-derive it here.
        _sys.path.insert(0, str(_pl.Path(__file__).resolve().parent.parent))
        import policy as _pc
        settings = _pc.state_root() / "searxng" / "settings.yml"
    if not settings.is_file():
        return CheckResult("brave-key", BLOCKED, "rendered SearXNG settings not readable")
    text = settings.read_text(errors="replace")
    import re as _re
    m = _re.search(r"- name:\s*braveapi(.*?)(?=\n  - name:|\Z)", text, _re.S)
    if not m:
        return CheckResult("brave-key", PASS, "braveapi engine not configured")
    body = m.group(1)
    km = _re.search(r"api_key:\s*(\S+)", body)
    configured = bool(km) and km.group(1) not in ('""', "''", "null", "~")
    inactive = bool(_re.search(r"(inactive|disabled):\s*true", body))
    if not configured:
        return CheckResult("brave-key", PASS, "braveapi present, no key configured")
    return CheckResult("brave-key", PASS,
                       f"braveapi key configured (engine {'inactive' if inactive else 'ACTIVE'}); "
                       "default checks never query it")


def check_searxng_external() -> CheckResult:
    """OPT-IN ONLY. A federated search that reaches paid engines.

    Reached exclusively through `ailocal smoke --external-search`. Never call
    this from doctor, smoke's default path, the gate, install validation or any
    stress loop: each call spends a metered Brave query.
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
                       "(external API quota consumed)")


def check_search_tool_registered() -> CheckResult:
    """LiteLLM must have registered searxng-search at boot.

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


def check_context_window(token: str, alias: str = "ailocal-completion") -> CheckResult:
    """An oversized prompt must be rejected, not silently truncated.

    Opt-in (`ailocal smoke --deep`): costly, and without enforcement the request
    returns 200 with a garbage answer.
    """
    filler = "the quick brown fox jumps over the lazy dog. " * 6000
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
                       "ailocal sync && ailocal start")
