#!/usr/bin/env python3
"""Test when a publish opens a window, via real publishes and a recording shim.

The opener is a detached Popen, so assertions wait for the shim's file;
reading it at once reports a false "never opened".
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
PORT = int(os.environ.get("TEST_AUTOOPEN_PORT", "7893"))

failures = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        failures.append(name)


# harness

def make_shim(tmp: str) -> Path:
    """Write a stand-in for `open`/`xdg-open` that records, never launches."""
    shim_dir = Path(tmp) / "shim"
    shim_dir.mkdir(parents=True, exist_ok=True)
    for name in ("open", "xdg-open", "cmd"):
        p = shim_dir / name
        p.write_text('#!/bin/sh\necho "$*" >> "$OPEN_LOG"\nexit 0\n')
        p.chmod(0o755)
    return shim_dir


def opened(log: Path, wait: float = 2.5) -> int:
    """Return how often the opener ran, waiting out the async Popen first."""
    deadline = time.time() + wait
    while time.time() < deadline:
        if Path(log).exists() and Path(log).read_text().strip():
            break
        time.sleep(0.05)
    time.sleep(0.35)          # let a second (unwanted) launch land too
    if not Path(log).exists():
        return 0
    return len([l for l in Path(log).read_text().splitlines() if l.strip()])


def fresh_server_module(tmp: str, shim: Path,
                        env_extra: dict[str, str] | None = None) -> dict[str, str]:
    """Run a scenario in its own process: server.py globals are per-process."""
    env = dict(os.environ)
    # Else the approved root is cwd and runs litter the checkout w/ .artifacts/.
    env.pop("CLAUDE_PROJECT_DIR", None)
    root = Path(tmp) / "root"
    root.mkdir(exist_ok=True)
    env.update({
        "PATH": f"{shim}:{env['PATH']}",
        "PYTHONPATH": str(HERE),
        "LOCAL_ARTIFACTS_ROOT": str(root),
        "XDG_STATE_HOME": str(Path(tmp) / "state"),
        "LOCAL_ARTIFACTS_PORT": str(PORT),
        "LOCAL_ARTIFACTS_IDLE_EXIT": "0",
        "LOCAL_ARTIFACTS_PRESENT_GRACE": "4.5",
    })
    for k in ("LOCAL_ARTIFACTS_AUTO_OPEN", "CLAUDE_CODE_ARTIFACT_AUTO_OPEN"):
        env.pop(k, None)
    env.update(env_extra or {})
    return env


def run_scenario(script: str, tmp: str, shim: Path,
                 env_extra: dict[str, str] | None = None,
                 timeout: float = 120) -> tuple[subprocess.CompletedProcess[str], Path]:
    log = Path(tmp) / f"open-{time.time_ns()}.log"
    log.write_text("")
    env = fresh_server_module(tmp, shim, env_extra)
    env["OPEN_LOG"] = str(log)
    p = subprocess.run([sys.executable, "-c", script], cwd=str(HERE), env=env,
                       capture_output=True, text=True, timeout=timeout)
    return p, log


#: Viewers are killed by owned port, never `pkill -f`: that hits real viewers.
DEFAULT_PORT = 7891


def _viewer_pid(port: int) -> int | None:
    """Return the PID listening on `port`, or None; the only kill handle."""
    out = subprocess.run(["lsof", "-t", f"-iTCP:{port}", "-sTCP:LISTEN"],
                         capture_output=True, text=True).stdout.split()
    return int(out[0]) if out else None


def kill_viewers() -> None:
    """Terminate only the viewer on this suite's port; never the default."""
    if PORT == DEFAULT_PORT:
        raise SystemExit(
            f"refusing to run: TEST_AUTOOPEN_PORT is the default {DEFAULT_PORT}, "
            f"which a real viewer uses. Set it to a free port.")
    pid = _viewer_pid(PORT)
    if pid is None:
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.time() + 5
    while time.time() < deadline:
        if _viewer_pid(PORT) is None:
            return
        time.sleep(0.1)
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


PRELUDE = """
import os, sys, time, json
import server
def pub(**kw):
    ok, msg = server.publish(**kw)
    return ok, msg
"""


# cases

def case_a_first_publish(tmp: str, shim: Path) -> None:
    p, log = run_scenario(PRELUDE + """
ok, msg = pub(title="a", content="# a", fmt="markdown")
print("OK" if ok else "FAILED")
time.sleep(1.5)
""", tmp, shim)
    check("A  first publish opens exactly one window", opened(log) == 1,
          f"stdout={p.stdout.strip()} stderr={p.stderr.strip()[-300:]}")
    kill_viewers()


def case_b_repeat_publish_while_watched(tmp: str, shim: Path) -> None:
    """Check a second publish w/ a watcher present opens nothing."""
    p, log = run_scenario(PRELUDE + """
import threading, urllib.request
ok, _ = pub(title="a", content="# a", fmt="markdown")
# Attach a real SSE watcher, exactly as an open tab would.
def watch():
    try:
        urllib.request.urlopen("http://127.0.0.1:%d/events" % server.PORT, timeout=30).read(1)
    except Exception:
        pass
threading.Thread(target=watch, daemon=True).start()
time.sleep(1.0)
ok2, _ = pub(title="b", content="# b", fmt="markdown")
print("OK" if ok and ok2 else "FAILED")
time.sleep(1.0)
""", tmp, shim)
    check("B  second publish with a tab attached opens nothing more",
          opened(log) == 1, f"stdout={p.stdout.strip()} stderr={p.stderr.strip()[-300:]}")
    kill_viewers()


def case_c_viewer_reaped(tmp: str, shim: Path) -> None:
    """Check a viewer replaced mid-session w/ no tab opens again."""
    p, log = run_scenario(PRELUDE + """
import subprocess, signal
ok, _ = pub(title="a", content="# a", fmt="markdown")
time.sleep(1.2)
# Reap ONLY the viewer bound to this run's own port -- never a pattern match,
# which would reach a real viewer in a concurrent session.
out = subprocess.run(["lsof", "-t", "-iTCP:%d" % server.PORT, "-sTCP:LISTEN"],
                     capture_output=True, text=True).stdout.split()
assert out, "no viewer owned port %d; refusing to guess a PID" % server.PORT
os.kill(int(out[0]), signal.SIGTERM)
for _ in range(50):
    still = subprocess.run(["lsof", "-t", "-iTCP:%d" % server.PORT, "-sTCP:LISTEN"],
                           capture_output=True, text=True).stdout.split()
    if not still:
        break
    time.sleep(0.1)
ok2, _ = pub(title="b", content="# b", fmt="markdown")
print("OK" if ok and ok2 else "FAILED")
time.sleep(1.5)
""", tmp, shim)
    check("C  publish after the viewer was reaped opens again",
          opened(log) == 2, f"stdout={p.stdout.strip()} stderr={p.stderr.strip()[-300:]}")
    kill_viewers()


def case_d_transient_disconnect(tmp: str, shim: Path) -> None:
    """Check a tab that drops and heals earns no second window."""
    p, log = run_scenario(PRELUDE + """
import threading, urllib.request
stop = threading.Event()
def watcher():
    # Reconnects like a real tab: drops, waits under the grace, comes back.
    while not stop.is_set():
        try:
            urllib.request.urlopen("http://127.0.0.1:%d/events" % server.PORT, timeout=2).read(1)
        except Exception:
            pass
        time.sleep(0.2)
threading.Thread(target=watcher, daemon=True).start()
ok, _ = pub(title="a", content="# a", fmt="markdown")
time.sleep(1.0)
ok2, _ = pub(title="b", content="# b", fmt="markdown")
stop.set()
print("OK" if ok and ok2 else "FAILED")
time.sleep(1.0)
""", tmp, shim)
    n = opened(log)
    check("D  flapping watcher causes no browser spam", n <= 1,
          f"opened {n}x stdout={p.stdout.strip()}")
    kill_viewers()


def case_e_invalid_mermaid(tmp: str, shim: Path) -> None:
    p, log = run_scenario(PRELUDE + """
ok, msg = pub(title="bad", content="classDiagram\\n    Stage <|--\\n ))) x (((", fmt="mermaid")
print("REFUSED" if not ok else "PUBLISHED")
time.sleep(1.0)
""", tmp, shim)
    check("E  invalid Mermaid is refused", "REFUSED" in p.stdout)
    check("E  invalid Mermaid opens nothing", opened(log, wait=1.0) == 0)
    kill_viewers()


def case_f_validator_unavailable(tmp: str, shim: Path) -> None:
    """Check no Chrome → Mermaid is refused and nothing opens."""
    p, log = run_scenario(PRELUDE + """
import mermaid_validate
mermaid_validate.find_chrome = lambda: None
ok, msg = pub(title="v", content="classDiagram\\n    A <|-- B\\n", fmt="mermaid")
print("REFUSED" if not ok else "PUBLISHED")
time.sleep(1.0)
""", tmp, shim)
    check("F  Mermaid refused when the validator is unavailable",
          "REFUSED" in p.stdout, f"stdout={p.stdout.strip()}")
    check("F  validator-unavailable opens nothing", opened(log, wait=1.0) == 0)
    kill_viewers()


def case_g_disabled(tmp: str, shim: Path) -> None:
    for var in ("LOCAL_ARTIFACTS_AUTO_OPEN", "CLAUDE_CODE_ARTIFACT_AUTO_OPEN"):
        p, log = run_scenario(PRELUDE + """
ok, _ = pub(title="a", content="# a", fmt="markdown")
print("OK" if ok else "FAILED")
time.sleep(1.0)
""", tmp, shim, env_extra={var: "0"})
        check(f"G  {var}=0 publishes but opens nothing",
              "OK" in p.stdout and opened(log, wait=1.0) == 0)
        kill_viewers()


def case_formats(tmp: str, shim: Path) -> None:
    for fmt, content in (("markdown", "# m"),
                         ("architecture",
                          json.dumps({"nodes": [{"id": "a", "label": "A"},
                                                {"id": "b", "label": "B"}],
                                      "edges": [{"from": "a", "to": "b"}]})),
                         ("mermaid", "classDiagram\n    A <|-- B\n")):
        p, log = run_scenario(PRELUDE + f"""
ok, msg = pub(title="f", content={content!r}, fmt="{fmt}")
print("OK" if ok else "FAILED:" + msg[:200])
time.sleep(1.5)
""", tmp, shim)
        check(f"    valid {fmt} publishes and opens once",
              "OK" in p.stdout and opened(log) == 1,
              f"stdout={p.stdout.strip()[:200]}")
        kill_viewers()


def case_precedence() -> None:
    """Check env precedence directly against the decision function."""
    import importlib
    sys.path.insert(0, str(HERE))
    os.environ.setdefault("LOCAL_ARTIFACTS_PORT", str(PORT))
    server = importlib.import_module("server")
    matrix = [
        ({},                                                   True,  "default"),
        ({"CLAUDE_CODE_ARTIFACT_AUTO_OPEN": "0"},              False, "CLAUDE_CODE_ARTIFACT_AUTO_OPEN"),
        ({"CLAUDE_CODE_ARTIFACT_AUTO_OPEN": "1"},              True,  "CLAUDE_CODE_ARTIFACT_AUTO_OPEN"),
        ({"LOCAL_ARTIFACTS_AUTO_OPEN": "0"},                   False, "LOCAL_ARTIFACTS_AUTO_OPEN"),
        ({"LOCAL_ARTIFACTS_AUTO_OPEN": "1"},                   True,  "LOCAL_ARTIFACTS_AUTO_OPEN"),
        ({"LOCAL_ARTIFACTS_AUTO_OPEN": "0",
          "CLAUDE_CODE_ARTIFACT_AUTO_OPEN": "0"},              False, "LOCAL_ARTIFACTS_AUTO_OPEN"),
        ({"LOCAL_ARTIFACTS_AUTO_OPEN": "1",
          "CLAUDE_CODE_ARTIFACT_AUTO_OPEN": "1"},              True,  "LOCAL_ARTIFACTS_AUTO_OPEN"),
        # conflicting: the ailocal-specific override must win, both directions
        ({"LOCAL_ARTIFACTS_AUTO_OPEN": "0",
          "CLAUDE_CODE_ARTIFACT_AUTO_OPEN": "1"},              False, "LOCAL_ARTIFACTS_AUTO_OPEN"),
        ({"LOCAL_ARTIFACTS_AUTO_OPEN": "1",
          "CLAUDE_CODE_ARTIFACT_AUTO_OPEN": "0"},              True,  "LOCAL_ARTIFACTS_AUTO_OPEN"),
    ]
    for env, want_enabled, want_source in matrix:
        for k in ("LOCAL_ARTIFACTS_AUTO_OPEN", "CLAUDE_CODE_ARTIFACT_AUTO_OPEN"):
            os.environ.pop(k, None)
        os.environ.update(env)
        got_enabled, got_source = server.auto_open_enabled()
        label = ", ".join(f"{k}={v}" for k, v in env.items()) or "neither set"
        check(f"    precedence: {label}",
              (got_enabled, got_source) == (want_enabled, want_source),
              f"got {(got_enabled, got_source)} want {(want_enabled, want_source)}")
    for k in ("LOCAL_ARTIFACTS_AUTO_OPEN", "CLAUDE_CODE_ARTIFACT_AUTO_OPEN"):
        os.environ.pop(k, None)

    # The grace window is only correct while it outlasts the tab's own reload.
    check("    grace window exceeds the tab self-heal delay",
          server.PRESENT_GRACE > server.VIEWER_RELOAD_MS / 1000)
    src = (HERE / "server.py").read_text()
    check("    viewer JS reload delay matches VIEWER_RELOAD_MS",
          f"}}, {server.VIEWER_RELOAD_MS}); }};" in src or
          f", {server.VIEWER_RELOAD_MS}); }}" in src)
    # Presentation must never be routed through the validator's browser.
    check("    presentation does not use the validator's Chrome",
          "find_chrome" not in src.split("def _open_browser")[1].split("def ")[0])


def main() -> int:
    print("test_autoopen.py — presentation lifecycle")
    kill_viewers()
    with tempfile.TemporaryDirectory() as tmp:
        shim = make_shim(tmp)
        print(" behavioural:")
        case_a_first_publish(tmp, shim)
        case_b_repeat_publish_while_watched(tmp, shim)
        case_c_viewer_reaped(tmp, shim)
        case_d_transient_disconnect(tmp, shim)
        case_e_invalid_mermaid(tmp, shim)
        case_f_validator_unavailable(tmp, shim)
        case_g_disabled(tmp, shim)
        case_formats(tmp, shim)
    print(" configuration:")
    case_precedence()
    kill_viewers()
    print()
    if failures:
        print(f"FAILED ({len(failures)}): " + ", ".join(failures))
        return 1
    print("all presentation-lifecycle checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
