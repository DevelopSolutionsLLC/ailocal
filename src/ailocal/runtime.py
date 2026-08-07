"""runtime.py — the running stack: what is up, and what it is doing.

Absorbs status.sh, status_gateway.py and status_traces.py. The two Python
helpers existed only because nested heredoc quoting had broken this repository's
shell twice; with Python owning the workflow that failure mode is gone and they
are ordinary functions.

Three renderings of one query -- dashboard, table, verbose -- so they share the
resolution and differ only in output.
"""
from __future__ import annotations

import glob
import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
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

def _gateway_summary(container: str) -> None:
    logs = _docker("logs", "--since", "2h", container, timeout=30)
    rows = []
    for line in logs.splitlines():
        if "tool_gateway_metric " not in line:
            continue
        try:
            d = json.loads(line.split("tool_gateway_metric ", 1)[1])
        except ValueError:
            continue
        if not d.get("event"):
            rows.append(d)
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


def _traces(directory: Path) -> None:
    rows = []
    for f in glob.glob(str(directory / "*.jsonl")):
        with open(f, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    pass
    if not rows:
        dim("no traces yet")
        return
    fails = [r for r in rows if r.get("outcome") == "failure"]
    slow = [r for r in rows if isinstance(r.get("ttfb_ms"), (int, float))
            and r["ttfb_ms"] > 60000]
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
    traces = state / "captures" / "traces"
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


def main(argv: list[str]) -> int:
    mode = argv[0] if argv else ""
    if mode in ("", "--dashboard"):
        _dashboard()
    elif mode == "--table":
        _table()
    elif mode == "--models":
        _verbose()
    else:
        print("usage: ailocal status [--models|--table]", file=__import__("sys").stderr)
        return 1
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main(sys.argv[1:]))
