"""Live runtime checks: containers, proxy, Ollama, search.

One implementation per primitive, and every call is bounded. Nothing here is
deterministic; what can be decided without a running stack belongs in config.py.
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import shutil
import subprocess
import urllib.error
import urllib.request

from .. import policy
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
    """The LiteLLM master key. Nothing outside this module resolves credentials.

    Order matters: clients/env.sh carries ANTHROPIC_API_KEY and OPENAI_API_KEY
    for the CLIENTS, which are not necessarily the master key. An unrecognised
    key sends LiteLLM to a key database that does not exist here, and the fault
    surfaces as "No connected db" rather than as a credential error.
    """
    # The generated environment holds the master key; clients/env.sh is an
    # installed asset. Neither is found by walking up from this file once the
    # package is installed.
    from .. import environment
    dotenv = environment.generated_file()
    env_sh = policy.data_root() / "clients" / "env.sh"
    files = {p: p.read_text() for p in (dotenv, env_sh) if p.is_file()}
    # (variable, files to search) in strict precedence order. The environment
    # outranks every file for the master key AND for both client keys, so a
    # shell-exported OPENAI_API_KEY still wins over an installed env.sh.
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
    """Bare reachability, for callers that want a boolean rather than a report."""
    try:
        http_json(f"{PROXY}/health/liveliness", timeout=timeout)
        return True
    except Unreachable:
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


def check_litellm_version(name: str = CONTAINER) -> CheckResult:
    """The RUNNING LiteLLM must be the one that was validated.

    `main-stable` floats, so the tag is not evidence; the installed distribution
    metadata is (litellm exposes no __version__). An unverifiable version is a
    failure, not a pass."""
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


def check_config_mount(name: str = CONTAINER) -> CheckResult:
    """The container can still SEE the hooks it was started with.

    `/app/config` is bind-mounted from the INSTALLED package's resources
    directory. Reinstalling the package — `pipx install --force`, an upgrade,
    anything that recreates the venv — replaces that directory, and the running
    container keeps holding the old inode, which is now empty.

    [REAL] Observed here: after a reinstall, `docker exec ... ls /app/config`
    returned nothing while `docker ps` said "Up 20 minutes (healthy)" and
    /health/liveliness answered 200. Python had already imported the hooks at
    boot, so the proxy kept serving from memory — and nothing anywhere said that
    the files behind tool filtering, system transport and tool repair were gone.
    The next restart would have loaded an empty callback list instead.

    Cheap and deterministic: one `ls`. Remediation is a restart, which
    re-resolves the bind path.
    """
    r = _run(["docker", "exec", name, "ls", "/app/config/hooks"])
    # A replaced mount shows up two ways depending on when it is caught: an
    # empty listing, or `ls` failing outright with "No such file or directory".
    # Both are the same fault, so both must FAIL. WARN is kept for a genuinely
    # inconclusive result — docker unreachable, exec refused — where
    # check_container is the one that should be speaking.
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
            remediation=f"docker restart {name}")
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
            remediation=None if ok else "ailocal start"))
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
                       remediation="ailocal install")


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
    override = os.environ.get("AILOCAL_SEARXNG_SETTINGS")
    # The state root has one owner; do not re-derive it here.
    settings = (pathlib.Path(override) if override
                else policy.state_root() / "searxng" / "settings.yml")
    if not settings.is_file():
        return CheckResult("brave-key", BLOCKED, "rendered SearXNG settings not readable")
    text = settings.read_text(errors="replace")
    m = re.search(r"- name:\s*braveapi(.*?)(?=\n  - name:|\Z)", text, re.S)
    if not m:
        return CheckResult("brave-key", PASS, "braveapi engine not configured")
    body = m.group(1)
    km = re.search(r"api_key:\s*(\S+)", body)
    configured = bool(km) and km.group(1) not in ('""', "''", "null", "~")
    inactive = bool(re.search(r"(inactive|disabled):\s*true", body))
    if not configured:
        return CheckResult("brave-key", PASS,
                           "braveapi present, no key configured "
                           "(optional; keyless search engines remain available)")
    return CheckResult("brave-key", PASS,
                       f"braveapi key configured (engine {'inactive' if inactive else 'ACTIVE'}); "
                       "default checks never query it")


def check_searxng_external() -> CheckResult:
    """OPT-IN ONLY. A federated search that reaches paid engines.

    Reached exclusively through `ailocal check --external-search`. Never call
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

    Without enforcement the request returns 200 with a garbage answer, so this
    is worth the one large bounded request it costs.
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
                       "ailocal start")


# ── container supply chain ──────────────────────────────────────────────────
# What a vulnerability scanner does not answer: is every declared image pinned
# to an immutable digest, does the RUNNING image match the DECLARED one, and is
# the service reachable off-host. Editing a compose file restarts nothing, so a
# repository can look patched while the old image keeps serving.

def declared_images() -> list[str]:
    """Image references from the compose files, in declaration order."""
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
    """(container name, running repo digest) for the first container of `repo`."""
    for cid in _run(["docker", "ps", "-q"]).stdout.split():
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
    """The running digest must be the declared one. They diverge silently."""
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


def check_loopback() -> list[CheckResult]:
    """A package finding is only an exposure if something can reach it."""
    out = []
    for cid in _run(["docker", "ps", "-q"]).stdout.split():
        name = _run(["docker", "inspect", cid,
                     "--format", "{{.Name}}"]).stdout.strip().lstrip("/")
        ports = _run(["docker", "port", cid]).stdout.strip()
        if not ports:
            out.append(CheckResult("reachability", PASS,
                                   f"{name} publishes no ports"))
            continue
        exposed = [line.split()[-1] for line in ports.splitlines()
                   if not line.split()[-1].startswith(("127.0.0.1:", "[::1]:"))]
        out.append(CheckResult("reachability", PASS, f"{name} bound to loopback only")
                   if not exposed else
                   CheckResult("reachability", FAIL,
                               f"{name} reachable off-host: {' '.join(exposed)}"))
    return out


def check_provenance(images: list[str]) -> list[CheckResult]:
    """A digest proves the bytes did not change, not who published them.

    Missing cosign degrades the report; it is never installed automatically.
    `cosign triangulate` must NOT be used to prove existence — it only computes
    the expected .sig tag from the digest and succeeds for unsigned images.
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
            # Presence is not validity: a real assertion needs the publisher's
            # identity, which upstream does not document for these images.
            out.append(CheckResult("provenance", PASS,
                                   f"{repo}: signature published for this digest "
                                   "(identity policy not configured)"))
        else:
            out.append(CheckResult("provenance", WARN,
                                   f"{repo}: could not determine signature status"))
    return out


#: Upstream discovery per repository. Qdrant belongs to another product and is
#: deliberately absent.
def check_updates(images: list[str]) -> list[CheckResult]:
    """Discovery only. Never pulls over a running service, never rewrites a pin.

    Digest inequality alone does not mean "behind": the LiteLLM pin tracks
    main-stable, which runs AHEAD of the newest tagged release, so a digest-only
    comparison proposes a downgrade as an update. Compare versions where a
    version is knowable."""
    out = []
    for image in images:
        ref, cur = (image.split("@", 1) + [""])[:2]
        repo = ref.split(":", 1)[0]
        if repo == "ghcr.io/berriai/litellm":
            try:
                body = http_json("https://api.github.com/repos/BerriAI/"
                                 "litellm/releases/latest", timeout=20)
            except Unreachable:
                body = {}
            newest = (body or {}).get("tag_name") or ""
            candidate = f"{repo}:{newest}"
        elif repo == "searxng/searxng":
            # Rolling `latest`, no semver releases — which is exactly why it
            # must stay digest-pinned.
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
                                       f"ahead of newest release {newest})"))
                continue
        if remote and remote == cur:
            out.append(CheckResult("update", PASS, f"{repo} is current ({newest})"))
        else:
            out.append(CheckResult("update", WARN,
                                   f"{repo}: {newest} available for review",
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
