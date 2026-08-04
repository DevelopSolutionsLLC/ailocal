"""Live runtime checks: containers, proxy, Ollama, search.

One implementation per primitive, and every call is bounded. Nothing here is
deterministic; what can be decided without a running stack belongs in config.py.
"""

from __future__ import annotations

import json
import os
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
    try:
        http_json(f"{PROXY}/health/liveliness")
    except Unreachable as exc:
        return CheckResult("proxy-health", FAIL, "LiteLLM /health/liveliness failed",
                           str(exc), "docker logs ailocal-litellm --tail=50")
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


def check_searxng() -> CheckResult:
    """Search is optional: unavailable search degrades, it does not fail."""
    state, _ = container_state("ailocal-searxng")
    if state == "absent":
        return CheckResult("searxng", WARN, "SearXNG container not present",
                           remediation="ailocal start")
    if state != "running":
        return CheckResult("searxng", WARN, f"SearXNG is {state}",
                           remediation="ailocal start")
    r = _run(["docker", "exec", CONTAINER, "python", "-c",
              "import urllib.request,json;"
              "print(len(json.loads(urllib.request.urlopen("
              "'http://searxng:8080/search?q=test&format=json', timeout=5)"
              ".read()).get('results',[])))"])
    if r.returncode != 0:
        return CheckResult("searxng", WARN, "LiteLLM cannot reach the SearXNG JSON API",
                           r.stderr.strip()[:200], "ailocal start")
    return CheckResult("searxng", PASS,
                       f"SearXNG JSON API reachable from LiteLLM ({r.stdout.strip()} results)")


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
