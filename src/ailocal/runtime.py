"""runtime.py — the running stack: how it is composed, driven and inspected.

One owner for the Compose invocation, the lifecycle (start, stop, update,
teardown) and the status/metric/trace renderings, because they need the same
roots, the same rendered SearXNG settings and the same readiness signal.
"""
from __future__ import annotations

import glob
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from statistics import median
from pathlib import Path

from . import policy

OLLAMA = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")

BOLD, DIM, RESET = "\033[1m", "\033[2m", "\033[0m"
GREEN, RED, YELLOW, MAGENTA, CYAN = ("\033[32m", "\033[31m", "\033[33m",
                                     "\033[35m", "\033[1;36m")


def _c(text: object, colour: str) -> str:
    return f"{colour}{text}{RESET}"


def ok(m):   print(f"  {_c('✓', GREEN)} {m}")
def bad(m):  print(f"  {_c('✗', RED)} {m}")
def warn(m): print(f"  {_c('⚠', YELLOW)} {m}")
def dim(m):  print(f"  {_c('—', DIM)} {m}")
def hdr(m):  print(f"\n{BOLD}{m}{RESET}")
def step(m): print(f"\n▶ {m}")


def _fail(message: str) -> "SystemExit":
    return SystemExit(f"  {_c('✗', RED)} {message}")


def _confirm(prompt: str) -> bool:
    """Destructive operations ask, in one place, and default to no."""
    try:
        return input(f"  {prompt} [y/N]: ").strip().lower().startswith("y")
    except EOFError:
        return False


# ── composition ─────────────────────────────────────────────────────────────
#
# The stack is split across two files under deploy/ so LiteLLM and SearXNG are
# configured in their own locations, but they must come up as ONE Compose
# project sharing ONE network, so LiteLLM can reach SearXNG at
# http://searxng:8080.
#
# --project-directory pins relative-path resolution inside both compose files;
# --env-file names the environment explicitly, because Compose would otherwise
# auto-discover .env from the project directory and couple the secrets file to
# wherever the compose assets live. Do not drop either flag.

BRAVE_PLACEHOLDER = "__BRAVE_API_KEY__"


def env_file() -> Path:
    return policy.config_root() / ".env"


def searxng_settings() -> Path:
    return policy.state_root() / "searxng" / "settings.yml"


def proxy_url() -> str:
    port = os.environ.get("AILOCAL_LITELLM_PORT", "4000")
    return os.environ.get("AILOCAL_PROXY", f"http://127.0.0.1:{port}")


def env_value(name: str) -> str:
    """Read one key out of .env. Values may carry one layer of quotes."""
    path = env_file()
    if not path.is_file():
        return ""
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith(f"{name}="):
            v = line.split("=", 1)[1].strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                v = v[1:-1]
            return v
    return ""


def render_searxng_settings() -> None:
    """Render deploy/searxng/settings.yml with the Brave key, atomically.

    SearXNG has no environment interpolation for an engine's api_key, so the
    tracked settings.yml must stay secret-free and the rendered copy lives under
    the state root, OUTSIDE the checkout. Fails closed; never prints the key.
    """
    src = policy.data_root() / "deploy" / "searxng" / "settings.yml"
    out = searxng_settings()
    if not src.is_file():
        raise _fail(f"BRAVE_SETTINGS_GENERATION_FAILED: missing {src}")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.parent.chmod(0o700)
    text = src.read_text(encoding="utf-8")

    # No placeholder => Brave is intentionally not configured. Disabling Brave
    # must not break the deployment.
    if BRAVE_PLACEHOLDER in text:
        key = env_value("BRAVE_API")
        if not key:
            raise _fail(
                "BRAVE_KEY_MISSING: braveapi is configured in "
                "deploy/searxng/settings.yml but BRAVE_API is unset or empty in "
                ".env. Set it, or remove braveapi from keep_only to disable Brave.")
        if key in text:
            raise _fail(f"BRAVE_SETTINGS_SECRET_LEAK: key present in TRACKED {src}")
        text = text.replace(BRAVE_PLACEHOLDER, key)

    tmp = out.with_suffix(out.suffix + f".tmp.{os.getpid()}")
    try:
        with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600),
                  "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, out)
    except OSError as e:
        tmp.unlink(missing_ok=True)
        raise _fail(f"BRAVE_SETTINGS_GENERATION_FAILED: {e}") from None


def compose_argv(args) -> list[str]:
    data = policy.data_root()
    argv = ["docker", "compose", "--project-directory", str(data)]
    if env_file().is_file():
        argv += ["--env-file", str(env_file())]
    argv += ["-f", str(data / "deploy" / "litellm" / "compose.yaml"),
             "-f", str(data / "deploy" / "searxng" / "compose.yaml")]
    return argv + [str(a) for a in args]


def compose_env() -> dict:
    env = dict(os.environ)
    env.setdefault("DOCKER_CLI_HINTS", "false")
    env["AILOCAL_STATE"] = str(policy.state_root())
    env["AILOCAL_SEARXNG_SETTINGS"] = str(searxng_settings())
    env["AILOCAL_PROXY"] = proxy_url()
    return env


#: Subcommands that start or recreate containers: the SearXNG service mounts
#: the rendered settings by absolute path, so it must exist first.
_NEEDS_SETTINGS = frozenset({"up", "start", "restart", "create", "run"})


def compose(*args, check: bool = True, capture: bool = False):
    if args and args[0] in _NEEDS_SETTINGS:
        render_searxng_settings()
    policy.state_root().mkdir(parents=True, exist_ok=True)
    return subprocess.run(compose_argv(args), env=compose_env(),
                          check=check, capture_output=capture, text=capture)


def wait_ready(max_attempts: int = 30, progress: bool = False) -> bool:
    """Wait until the proxy accepts requests.

    /health/liveliness returns 200 as soon as LiteLLM is listening; the full
    /health endpoint blocks on every model and fails when Ollama is down, so it
    is the wrong signal here.
    """
    url = f"{proxy_url()}/health/liveliness"
    for attempt in range(max_attempts):
        if _get(url) is not None:
            if progress:
                print(" " * 20, end="\r")
            return True
        if progress:
            print(f"  Waiting... ({attempt * 3}s)", end="\r")
        time.sleep(3)
    return _get(url) is not None


def _get(url: str, timeout: int = 3) -> str | None:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.read().decode("utf-8", "replace")
    except (urllib.error.URLError, OSError, ValueError):
        return None


def _docker(*args: str, timeout: int = 10) -> str:
    try:
        r = subprocess.run(["docker", *args], capture_output=True, text=True,
                           timeout=timeout)
        return r.stdout.strip() if r.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def _container(name_env: str, default: str) -> str:
    return os.environ.get(name_env, default)


# ── model residency ─────────────────────────────────────────────────────────

def _loaded() -> dict:
    body = _get(f"{OLLAMA}/api/ps")
    try:
        return {m.get("name", ""): m for m in json.loads(body or "")["models"]}
    except (ValueError, KeyError, TypeError):
        return {}


def _find(loaded: dict, backend: str) -> dict | None:
    """A backend may be recorded with or without an explicit :latest tag."""
    if backend in loaded:
        return loaded[backend]
    if f"{backend}:latest" in loaded:
        return loaded[f"{backend}:latest"]
    stem = backend.split(":", 1)[0]
    return next((m for n, m in loaded.items() if n.split(":", 1)[0] == stem), None)


def _expires(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:                      # Ollama emits more than 6 fractional digits
        return datetime.fromisoformat(re.sub(r"(\.\d{6})\d+", r"\1", raw))
    except ValueError:
        return None


def _remaining(delta) -> str:
    secs = int(delta.total_seconds())
    if secs <= 0:
        return "expiring"
    h, m = secs // 3600, (secs % 3600) // 60
    return f"{h}h {m}m remaining" if h and m else (f"{h}h remaining" if h
                                                   else f"{m}m remaining")


def _state(cap: dict, loaded: dict, now: datetime) -> tuple[str, str]:
    """persistent | loaded | idle. A far-future expiry IS a pinned model."""
    m = _find(loaded, cap["backend"])
    if not m:
        return "idle", YELLOW
    exp = _expires(m.get("expires_at"))
    if cap.get("persistent") or (exp and (exp.year - now.year) > 5):
        return "persistent", MAGENTA
    return "loaded", GREEN


def _vscode_connector() -> bool:
    """The connector is an extension, so VS Code itself is the only registry."""
    try:
        r = subprocess.run(["code", "--list-extensions"], capture_output=True,
                           text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return False
    return any(line.strip().lower() == "gethnet.litellm-connector-copilot"
               for line in r.stdout.splitlines())


def _capabilities() -> list:
    path = policy.state_root() / "litellm" / "capabilities.json"
    if not path.is_file():
        raise SystemExit("capabilities.json missing — run ailocal sync")
    return json.loads(path.read_text())["capabilities"]


# ── gateway and traces ──────────────────────────────────────────────────────

METRIC_PREFIX = "tool_gateway_metric "


def metric_records(container: str, since: str | None = "2h",
                   path: Path | None = None) -> tuple[list, list, int]:
    """(request measurements, operational events, malformed count).

    Event records (gateway_init, bad_mode, …) are separated because averaging an
    operational signal into a request statistic corrupts every figure derived
    from it.
    """
    if path:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    else:
        args = ["logs"] + (["--since", since] if since else []) + [container]
        lines = _docker(*args, timeout=120).splitlines()
    records, events, malformed = [], [], 0
    for line in lines:
        idx = line.find(METRIC_PREFIX)
        if idx < 0:
            continue
        try:
            record = json.loads(line[idx + len(METRIC_PREFIX):])
        except ValueError:
            malformed += 1
            continue
        (events if record.get("event") else records).append(record)
    return records, events, malformed


def _gateway_summary(container: str) -> None:
    """The most recent request, for the dashboard."""
    rows, _, _ = metric_records(container)
    if not rows:
        dim("no gateway activity in this window (not evidence of a problem)")
        return
    d = max(rows, key=lambda r: r.get("bytes_in", 0))
    base = d.get("bytes_reachable") or 1
    got = d.get("bytes_kept_reachable")
    if got is None:
        got = d.get("bytes_kept") or 0
    ok(f"last request   {d.get('client')}  "
       f"{d.get('tools_in')} -> {d.get('tools_kept')} tools, "
       f"{base} -> {got} B ({100.0 * (base - got) / base:.0f}% cut)")
    dropped = d.get("dropped_groups") or []
    if dropped:
        print(f"                 removed: {', '.join(dropped)}")
    print(f"  {len(rows)} request(s) seen; ailocal metrics for detail")


def _summarize(records: list) -> dict:
    """Aggregate the gateway's metric stream.

    Every ratio uses bytes_kept_reachable over bytes_reachable — what the model
    received over what the route would have forwarded. bytes_kept and bytes_in
    give nonsense on /v1/responses, where LiteLLM drops namespace tools itself.

    `off`-mode records carry no negotiation decision and are counted separately
    rather than averaged in as zeros. Token figures are [APPROX]: the cl100k
    proxy, calibrated 1.009-1.021 against Ollama's prompt_eval_count. End-to-end
    latency is deliberately absent — these records hold only the hook's own
    overhead.
    """
    negotiated = [r for r in records if r.get("mode") in ("report", "filter")]
    acting = [r for r in negotiated
              if not r.get("passthrough") and r.get("bytes_reachable")]
    out = {
        "requests_total": len(records),
        "by_mode": dict(Counter(r.get("mode") or "unknown" for r in records)),
        "negotiated": len(negotiated),
        "filter_applied": len([r for r in negotiated if r.get("applied")]),
        "passthrough": len([r for r in negotiated if r.get("passthrough")]),
        "acting_requests": len(acting),
        "tokenizer": "cl100k-proxy (calibrated 1.009-1.021 vs prompt_eval_count)",
        "clients": dict(Counter(r.get("client") or "unknown" for r in records)),
        "model_classes": dict(Counter(r.get("model_class") or "unmatched"
                                      for r in records)),
        "routes": dict(Counter(r.get("route") or "unknown" for r in records)),
        "task_classes": dict(Counter(str(r.get("task_class")) for r in negotiated)),
        "registry_states": dict(Counter(r.get("registry") or "unknown"
                                        for r in records)),
    }
    if acting:
        ratios = [100.0 * (r["bytes_reachable"] - r.get("bytes_kept_reachable", 0))
                  / r["bytes_reachable"] for r in acting]
        out["reduction_pct"] = {"min": round(min(ratios), 1),
                                "median": round(median(ratios), 1),
                                "max": round(max(ratios), 1)}
        for key, field in (("bytes_reachable_total", "bytes_reachable"),
                           ("bytes_delivered_total", "bytes_kept_reachable"),
                           ("bytes_saved_by_drop", "bytes_dropped"),
                           ("bytes_saved_by_rewrite", "bytes_saved_by_rewrite"),
                           ("bytes_moot_litellm_already_dropped",
                            "bytes_prefiltered_by_litellm")):
            out[key] = sum(r.get(field, 0) for r in acting)
        toks_in = [r["tokens_est_in"] for r in acting
                   if isinstance(r.get("tokens_est_in"), int)]
        toks_kept = [r["tokens_est_kept"] for r in acting
                     if isinstance(r.get("tokens_est_kept"), int)]
        if toks_in and toks_kept:
            out["tokens_est_total_in"] = sum(toks_in)
            out["tokens_est_total_kept"] = sum(toks_kept)
    else:
        out["reduction_note"] = ("no request was both negotiated and "
                                 "non-passthrough — nothing to report over")
    overheads = sorted(r["overhead_ms"] for r in records
                       if isinstance(r.get("overhead_ms"), (int, float)))
    if overheads:
        out["hook_overhead_ms"] = {
            "median": round(median(overheads), 3),
            "p95": round(overheads[min(len(overheads) - 1,
                                       int(len(overheads) * 0.95))], 3),
            "max": round(max(overheads), 3)}
    dropped = Counter()
    groups = Counter()
    for r in acting:
        dropped.update(r.get("dropped_names") or [])
        groups.update(r.get("dropped_groups") or [])
    out["most_dropped_tools"] = dropped.most_common(15)
    out["dropped_group_frequency"] = groups.most_common()
    return out


def cmd_metrics(argv: list[str]) -> int:
    """Report what the gateway actually did, never an improvement it cannot
    substantiate."""
    container = _container("AILOCAL_LITELLM_CONTAINER", "ailocal-litellm")
    since = argv[argv.index("--since") + 1] if "--since" in argv else None
    path = Path(argv[argv.index("--file") + 1]) if "--file" in argv else None
    records, events, malformed = metric_records(container, since, path)
    if not records:
        print("No gateway metric records found.\nThe gateway is silent when "
              "AILOCAL_TOOL_GATEWAY=off, so this is NOT evidence\nthat no "
              "requests were served. Set it to report or filter first.")
        return 1

    s = _summarize(records)
    if "--json" in argv:
        print(json.dumps(s, indent=2))
        return 0

    W = 42
    print("=" * 70 + "\nGATEWAY METRICS\n" + "=" * 70)
    for label, key in (("requests with a metric record", "requests_total"),
                       ("  by mode", "by_mode"),
                       ("  negotiated (report/filter)", "negotiated"),
                       ("  filter actually applied", "filter_applied"),
                       ("  passthrough (forwarded intact)", "passthrough")):
        print(f"{label:{W}} {s[key]}")
    if malformed:
        print(f"{'  MALFORMED records skipped':{W}} {malformed}")
    if events:
        print(f"{'  operational events (not requests)':{W}} "
              f"{dict(Counter(e.get('event') for e in events))}")

    print()
    if not s["acting_requests"]:
        print(s["reduction_note"])
    else:
        print(f"PAYLOAD, over {s['acting_requests']} negotiated "
              "non-passthrough request(s)")
        for label, key in (("  bytes the route would forward", "bytes_reachable_total"),
                           ("  bytes the model received", "bytes_delivered_total"),
                           ("  saved by dropping tools", "bytes_saved_by_drop"),
                           ("  saved by rewriting schemas", "bytes_saved_by_rewrite"),
                           ("  (moot: LiteLLM dropped anyway)",
                            "bytes_moot_litellm_already_dropped")):
            print(f"{label:{W}} {s[key]}")
        r = s["reduction_pct"]
        print(f"{'  reduction min/median/max':{W}} "
              f"{r['min']}% / {r['median']}% / {r['max']}%")
        if "tokens_est_total_in" in s:
            print(f"{'  tokens_est in -> delivered':{W}} "
                  f"{s['tokens_est_total_in']} -> {s['tokens_est_total_kept']}")
        print(f"{'  tokenizer':{W}} {s['tokenizer']}")

    if "hook_overhead_ms" in s:
        o = s["hook_overhead_ms"]
        print(f"\nHOOK OVERHEAD  median {o['median']} ms | p95 {o['p95']} ms | "
              f"max {o['max']} ms\n  hook only; not end-to-end latency")

    print()
    for label, key in (("clients", "clients"), ("model classes", "model_classes"),
                       ("routes", "routes"), ("task classes", "task_classes"),
                       ("registry state", "registry_states")):
        print(f"{label:22} {s[key]}")
    if s["most_dropped_tools"]:
        print("\nMOST-DROPPED TOOLS")
        for name, n in s["most_dropped_tools"]:
            print(f"    {name:44} x{n}")
        print(f"  by group: {dict(s['dropped_group_frequency'])}")
    return 0


#: How to read a trace record, literally:
#:   ttfb_ms  time to the FIRST STREAMED CHUNK. A proxy for prompt-eval time,
#:            not a measurement of it — Ollama's prompt_eval_duration does not
#:            survive into the LiteLLM response.
#:   outcome  what the PROXY saw. A client that timed out at 60s while the proxy
#:            streamed happily still shows `streamed`; the client's disconnect
#:            is not observable from inside the proxy and is never recorded as
#:            though it were.
SILENT_MS = 60000


def trace_dir() -> Path:
    return Path(os.environ.get("AILOCAL_TRACE_HOST_DIR")
                or policy.state_root() / "captures" / "traces")


def trace_rows(directory: Path | None = None) -> list:
    rows = []
    for f in sorted(glob.glob(str((directory or trace_dir()) / "*.jsonl"))):
        with open(f, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    pass
    return rows


def _silent(rows) -> list:
    return [r for r in rows if isinstance(r.get("ttfb_ms"), (int, float))
            and r["ttfb_ms"] > SILENT_MS]


def _traces(directory: Path) -> None:
    """The three most recent, for the dashboard."""
    rows = trace_rows(directory)
    if not rows:
        return dim("no traces yet")
    fails = [r for r in rows if r.get("outcome") == "failure"]
    slow = _silent(rows)
    for r in rows[-3:]:
        when = time.strftime("%H:%M:%S", time.localtime(r.get("ts") or 0))
        t = r.get("ttfb_ms")
        tt = f"{t:.0f}ms" if isinstance(t, (int, float)) else "-"
        print(f"  {when}  {str(r.get('client') or '?'):11} "
              f"{str(r.get('capability') or '?'):13} ttfb={tt:>9}  {r.get('outcome')}")
    msg = f"  {len(rows)} trace(s)"
    if fails:
        msg += f", {_c(f'{len(fails)} failure(s)', RED)}"
    if slow:
        msg += f", {_c(f'{len(slow)} with >60s first byte', YELLOW)}"
    print(msg)
    if fails or slow:
        print("  -> ailocal trace --failures")


def cmd_trace(argv: list[str]) -> int:
    """The per-request timeline: which component, and when."""
    directory = trace_dir()
    rows = trace_rows(directory)
    if not rows:
        print(f"No traces in {directory}.\n\n"
              "Tracing is OFF unless AILOCAL_TRACE_DIR is set in .env. That is "
              "not evidence\nthat no requests were served — check with:\n"
              "    docker exec ailocal-litellm printenv AILOCAL_TRACE_DIR")
        return 1

    if "--failures" in argv:
        rows = [r for r in rows if r.get("outcome") in ("failure", "empty_stream")]
    elif "--slow" in argv:
        limit = float(argv[argv.index("--slow") + 1])
        rows = [r for r in rows if (r.get("total_ms") or 0) >= limit]
    elif "--id" in argv:
        wanted = argv[argv.index("--id") + 1]
        for r in (r for r in rows
                  if str(r.get("request_id", "")).startswith(wanted)):
            print("=" * 66)
            for k in sorted(r):
                if k == "traceback" and r[k]:
                    print(f"  {k}:")
                    for line in str(r[k]).splitlines()[-12:]:
                        print(f"      {line}")
                else:
                    print(f"  {k:18} {r[k]}")
        return 0

    if not rows:
        # NO REQUEST MATCHED — not that the system is healthy, and not that
        # nothing was served.
        print("No trace record matched that filter.")
        return 0

    print("─" * 96)
    print(f"{'when':9} {'id':17} {'model':24} {'ttfb':>8} {'total':>9} "
          f"{'tools':>5} {'msgs':>5}  outcome")
    print("─" * 96)
    for r in rows[-40:]:
        ts, ttfb, total = r.get("ts"), r.get("ttfb_ms"), r.get("total_ms")
        outcome = str(r.get("outcome"))
        if r.get("error_type"):
            outcome += f": {r['error_type']}"
        if isinstance(ttfb, (int, float)) and ttfb > SILENT_MS:
            outcome += ("  <-- >60s SILENCE before first byte; a client timeout "
                        "here looks like an 'API error' with no failing component")
        when = time.strftime("%H:%M:%S", time.localtime(ts)) if ts else "-"
        ttfb_s = f"{ttfb:.0f}ms" if isinstance(ttfb, (int, float)) else "-"
        total_s = f"{total:.0f}ms" if isinstance(total, (int, float)) else "-"
        print(f"{when:9} {str(r.get('request_id'))[:16]:17} "
              f"{str(r.get('requested_alias'))[:23]:24} {ttfb_s:>8} {total_s:>9} "
              f"{str(r.get('tools_declared') or '-'):>5} "
              f"{str(r.get('messages') or '-'):>5}  {outcome}")
    fails = [r for r in rows if r.get("outcome") == "failure"]
    print("─" * 96)
    print(f"{len(rows)} trace record(s), {len(fails)} failure(s), "
          f"{len(_silent(rows))} with >60s time-to-first-byte")
    if fails:
        print("\nInspect one in full:  ailocal trace --id "
              f"{str(fails[-1].get('request_id'))[:8]}")
    return 0


# ── lifecycle ───────────────────────────────────────────────────────────────

#: Files LiteLLM reads ONCE at boot and that are bind-mounted, so editing them
#: changes no Compose spec and `up -d` restarts nothing — the proxy keeps
#: serving the old routing and tool policy with nothing in the logs to say so.
#: Fingerprinting them makes the restart deliberate rather than remembered.
def _config_fingerprint() -> str:
    data, state = policy.data_root(), policy.state_root()
    paths = [state / "litellm" / "config.yaml",
             data / "deploy" / "litellm" / "registry.yaml"]
    paths += sorted((data / "deploy" / "litellm" / "hooks").glob("*.py"))
    paths += sorted((data / "deploy" / "litellm" / "instructions").glob("*.md"))
    h = hashlib.sha256()
    for p in paths:
        try:
            h.update(p.read_bytes())
        except OSError:
            pass
    return h.hexdigest()


def _running() -> list[str]:
    return _docker("ps", "--format", "{{.Names}}").splitlines()


def _preflight() -> None:
    step("Pre-flight checks")
    if not env_file().is_file():
        raise _fail(f"{env_file()} not found. Run ailocal install first.")
    ok(".env present")
    if subprocess.run(["docker", "ps"], capture_output=True).returncode != 0:
        raise _fail("Docker daemon is not running. Start Docker Desktop and retry.")
    ok("Docker daemon running")

    if not shutil.which("ollama"):
        warn("Ollama CLI not found. Install it from https://ollama.ai")
        return
    tags = _get(f"{OLLAMA}/api/tags")
    if tags is None:
        warn("Ollama is not running.")
        print("  Start the MANAGED service (not the GUI app, which competes "
              "for :11434):")
        print(f"    launchctl kickstart -k gui/{os.getuid()}/com.ailocal.ollama")
        print("  LiteLLM will start but model requests will fail until Ollama is up.")
        return
    ok("Ollama daemon responding")

    # The model set comes from the GENERATED artifact, never the profile.
    present = {m.get("name", "") for m in json.loads(tags).get("models", [])}
    stems = {n.split(":", 1)[0] for n in present}
    required = {r["model"] for r in policy.effective_summary()["roles"].values()}
    missing = sorted(m for m in required
                     if m not in present and m.split(":", 1)[0] not in stems)
    if missing:
        warn(f"Missing Ollama models: {' '.join(missing)}")
        print("  Run ailocal models-install to pull the full model set.")
    else:
        ok("Required Ollama models present")


def cmd_start(argv: list[str]) -> int:
    no_wait = "--no-wait" in argv
    _preflight()

    # No `docker compose pull` here by design: start (including the boot
    # LaunchAgent) must be reproducible and offline-safe, so it runs whatever
    # image is on disk. Images are refreshed deliberately, by update.
    step("Starting ailocal services")
    was_running = "ailocal-litellm" in _running()
    compose("up", "-d", "--remove-orphans")

    stamp = policy.state_root() / "litellm-config.sha"
    current = _config_fingerprint()
    previous = stamp.read_text() if stamp.is_file() else ""
    if was_running and previous and previous != current:
        step("LiteLLM config changed since last start — restarting to load it")
        _docker("restart", "ailocal-litellm", timeout=60)
        ok("ailocal-litellm restarted (routing/persona/hook changes are now live)")
    stamp.parent.mkdir(parents=True, exist_ok=True)
    stamp.write_text(current)

    if no_wait:
        ok("Services launched (skipping health wait)")
    else:
        step("Waiting for LiteLLM to become ready")
        if not wait_ready(30, progress=True):
            warn("LiteLLM did not become ready after 90s")
            print("  Check logs: docker logs ailocal-litellm")

    step("ailocal is running")
    print(f"\n  LiteLLM API  →  {proxy_url()}\n")
    print("  Clients:  ailocal clients     (Claude Code, Codex, VS Code)")
    print("            ailocal vscode      (Copilot Chat connector)")
    print("  Inspect:  ailocal status")
    return 0


def cmd_stop(argv: list[str]) -> int:
    remove_volumes = "--volumes" in argv
    if remove_volumes:
        warn("--volumes flag set: all Docker volumes will be removed.")
        if not _confirm("This destroys all database and cache data. Are you sure?"):
            print("Aborted.")
            return 0
    step("Stopping ailocal services")
    if remove_volumes:
        compose("down", "--volumes", "--remove-orphans")
        ok("Services stopped and volumes removed.")
    else:
        compose("down", "--remove-orphans")
        ok("Services stopped. Data volumes preserved.")
        print("  To also remove volumes: ailocal stop --volumes")
    return 0


def cmd_update(argv: list[str]) -> int:
    # The only non-regenerable state is .env (the master key): config is in git
    # and Ollama models re-pull, so a one-file snapshot is the whole backup.
    step("Snapshotting .env before update")
    if env_file().is_file():
        backups = policy.state_root() / "backups"
        backups.mkdir(parents=True, exist_ok=True)
        snap = backups / f".env.{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"
        shutil.copyfile(env_file(), snap)
        snap.chmod(0o600)
        ok(f"Saved {snap}")
    else:
        warn("No .env found — nothing to snapshot.")

    # NOT an image upgrade: every image is digest-pinned, so this re-fetches
    # the SAME digests and exists only to repair a locally deleted layer.
    step("Pulling pinned Docker images (digests unchanged by design)")
    compose("pull")

    if "--skip-models" not in argv:
        step("Updating Ollama models")
        from . import install
        if install.cmd_models([]):
            warn("Model update had warnings — services will still restart.")

    # Client configs are NOT redeployed here: that would rewrite the user's
    # client homes on every update. Redeploy explicitly with ailocal clients.
    step("Regenerating model config")
    from . import generation
    if generation.main([]):
        raise _fail("generation failed — services were not restarted")

    step("Restarting services")
    compose("up", "-d", "--remove-orphans")
    compose("restart", "litellm", "searxng")

    step("Validating health post-update")
    wait_ready(20)
    from .checks import run as checks_run
    if checks_run.main(["doctor"]):
        warn("Health check reported issues after update.")
        print("  Check logs: docker logs ailocal-litellm --tail=50")
        return 1
    step("Update complete — LiteLLM healthy.")
    return 0


def cmd_teardown(argv: list[str]) -> int:
    remove_images = "--images" in argv
    step("ailocal teardown")
    print("\n  This will permanently remove:")
    print("    • All ailocal containers and volumes")
    print("    • The ailocal Docker network")
    if remove_images:
        print("    • All pulled Docker images")
    print(f"\n  Your configuration in {policy.config_root()} is NOT touched.")
    print("  Re-run ailocal install + ailocal start to rebuild.\n")
    if not _confirm("Proceed?"):
        print("Aborted.")
        return 0

    step("Stopping containers and removing volumes")
    compose("down", "--volumes", "--remove-orphans", check=False)

    if "ailocal_net" in _docker("network", "ls", "--format", "{{.Name}}").splitlines():
        step("Removing Docker network")
        _docker("network", "rm", "ailocal_net")

    if remove_images:
        step("Removing Docker images")
        r = compose("config", "--images", check=False, capture=True)
        for img in filter(None, (l.strip() for l in r.stdout.splitlines())):
            if _docker("image", "inspect", img):
                _docker("rmi", img, timeout=60)
                ok(f"removed {img}")

    step("Teardown complete.")
    return 0


# ── renderings ──────────────────────────────────────────────────────────────

def _dashboard() -> None:
    state = policy.state_root()
    litellm = _container("AILOCAL_LITELLM_CONTAINER", "ailocal-litellm")
    searxng = _container("AILOCAL_SEARXNG_CONTAINER", "ailocal-searxng")
    port = os.environ.get("AILOCAL_LITELLM_PORT", "4000")
    proxy = os.environ.get("AILOCAL_PROXY", f"http://127.0.0.1:{port}")

    bar = "═" * 70
    print(bar)
    print(f" AILOCAL STATUS   {datetime.now():%Y-%m-%d %H:%M:%S}")
    print(bar)

    hdr("Services")
    (ok if _get(f"{OLLAMA}/api/tags") else bad)(
        f"Ollama        {OLLAMA}" if _get(f"{OLLAMA}/api/tags")
        else "Ollama        unreachable")
    names = _docker("ps", "--format", "{{.Names}}").splitlines()
    if litellm in names:
        health = _docker("inspect", litellm, "--format", "{{.State.Health.Status}}")
        (ok if health == "healthy" else warn)(f"LiteLLM       {health or 'unknown'}")
    else:
        bad("LiteLLM       not running")
    (ok if _get(f"{proxy}/health/liveliness") else bad)(
        f"Proxy         {proxy}" if _get(f"{proxy}/health/liveliness")
        else "Proxy         not responding")
    (ok if searxng in names else dim)(
        "SearXNG       running" if searxng in names else "SearXNG       not running")

    hdr("Gateway")
    gw = _docker("exec", litellm, "printenv", "AILOCAL_TOOL_GATEWAY") or "?"
    tn = _docker("exec", litellm, "printenv", "AILOCAL_TASK_NEGOTIATION") or "off"
    tr = _docker("exec", litellm, "printenv", "AILOCAL_TRACE_DIR")
    if gw == "filter":
        ok("mode          filter — tools removed before the model")
    elif gw == "report":
        warn("mode          report — measuring only, nothing removed")
    elif gw == "off":
        warn("mode          OFF — payloads not reduced")
    else:
        bad(f"mode          unknown ({gw})")
    (ok if tn == "on" else dim)(f"task negot.   {tn}")
    (ok if tr else dim)(f"tracing       {tr}" if tr else "tracing       off")
    _gateway_summary(litellm)

    hdr("Clients")
    client_root = policy.deployed_client_root()
    (ok if (client_root / "claude" / ".claude.json").is_file() else bad)(
        "Claude Code   configured, isolated from ~/.claude"
        if (client_root / "claude" / ".claude.json").is_file()
        else "Claude Code   not installed")
    (ok if (client_root / "codex" / "config.toml").is_file() else bad)(
        "Codex CLI     configured  (MCP registered, NOT reachable: "
        "docs/troubleshooting.md)"
        if (client_root / "codex" / "config.toml").is_file()
        else "Codex CLI     not installed")
    if _vscode_connector():
        ok("VS Code       connector installed  (chat turn unverified — needs GUI)")
    else:
        dim("VS Code       connector not installed")

    hdr("Recent requests")
    traces = trace_dir()
    if traces.is_dir():
        _traces(traces)
    else:
        dim("tracing off — set AILOCAL_TRACE_DIR to diagnose intermittent failures")

    hdr("Models")
    _verbose()


def _table() -> None:
    caps, loaded, now = _capabilities(), _loaded(), datetime.now(timezone.utc)
    w_cap = max(len("Capability"), *(len(c["name"]) for c in caps))
    w_bk = max(len("Backend"), *(len(c["backend"]) for c in caps))
    print(_c(f"{'Capability':<{w_cap}}  {'Backend':<{w_bk}}  Status", BOLD))
    for c in caps:
        label, colour = _state(c, loaded, now)
        print(f"{c['name']:<{w_cap}}  {c['backend']:<{w_bk}}  {_c(label, colour)}")


def _verbose() -> None:
    caps, loaded, now = _capabilities(), _loaded(), datetime.now(timezone.utc)
    print(_c("AILOCAL MODEL STATUS", BOLD))
    print("─" * 44)
    for c in caps:
        print(_c(c["role"], CYAN))
        print(f"  Model:      {c['backend']}")
        m = _find(loaded, c["backend"])
        if not m:
            print(f"  Loaded:     {_c('No', YELLOW)}")
        else:
            print(f"  Loaded:     {_c('Yes', GREEN)}")
            exp = _expires(m.get("expires_at"))
            if c.get("persistent") or (exp and (exp.year - now.year) > 5):
                print(f"  Keep Alive: {_c('Persistent', MAGENTA)}")
            elif exp:
                print(f"  Keep Alive: {_remaining(exp - now)}")
            print(f"  Context:    {c['context']}")
        print()


def cmd_status(argv: list[str]) -> int:
    mode = argv[0] if argv else ""
    if mode in ("", "--dashboard"):
        _dashboard()
    elif mode == "--table":
        _table()
    elif mode == "--models":
        _verbose()
    else:
        print("usage: ailocal status [--models|--table]", file=sys.stderr)
        return 1
    return 0


def cmd_compose(argv: list[str]) -> int:
    """Run one Compose subcommand against this composition: the single entry
    point for anything outside this module that drives the stack directly."""
    return compose(*argv, check=False).returncode


COMMANDS = {"status": cmd_status, "start": cmd_start, "stop": cmd_stop,
            "update": cmd_update, "teardown": cmd_teardown,
            "compose": cmd_compose, "trace": cmd_trace,
            "metrics": cmd_metrics}


def main(argv: list[str]) -> int:
    if not argv or argv[0] not in COMMANDS:
        print(f"usage: python -m ailocal.runtime <{'|'.join(COMMANDS)}> [options]",
              file=sys.stderr)
        return 2
    return COMMANDS[argv[0]](argv[1:])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
