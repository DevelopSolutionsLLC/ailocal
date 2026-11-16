#!/usr/bin/env python3
"""Test `ailocal check` and the read-only commands.

Only the live section runs against this installation; the rest is in-process.
"""
from __future__ import annotations

import os
import plistlib
import socket
import ssl
import sys
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness import REPO, Suite, run  # noqa: E402

from ailocal import environment  # noqa: E402
from ailocal import policy as P  # noqa: E402
from ailocal import tls  # noqa: E402
from ailocal.checks import FAIL, PASS  # noqa: E402
from ailocal.checks import host as H  # noqa: E402
from ailocal.checks import services as S  # noqa: E402

suite = Suite("CHECKS AND READ-ONLY COMMANDS")
check = suite.check


def services() -> None:
    suite.section("service probes")
    # A reinstall leaves a healthy container on an empty mount; fix: start.
    verdicts = []
    for out in (SimpleNamespace(returncode=0, stdout="", stderr=""),
                SimpleNamespace(returncode=1, stdout="", stderr="ls: No such file or directory")):
        with patch.object(S, "container_state", return_value=("running", "")), \
                patch.object(S, "_run", return_value=out):
            r = S.check_config_mount()
        verdicts.append((r.status, r.remediation))
    check(verdicts == [(FAIL, "ailocal start")] * 2,
          "a detached config mount fails and points at `ailocal start`", repr(verdicts))

    # #126: only the TLS sidecar leaves loopback, and only serving verified TLS.
    def listener(context: ssl.SSLContext | None) -> int:
        srv = socket.create_server(("127.0.0.1", 0))
        servers.append(srv)

        def answer() -> None:
            while True:
                try:
                    conn, _ = srv.accept()
                except OSError:
                    return
                try:
                    conn.settimeout(5)
                    if context:
                        conn = context.wrap_socket(conn, server_side=True)
                        conn.recv(1)
                    else:
                        conn.recv(1024)
                        conn.sendall(b"HTTP/1.0 200 OK\r\n\r\n")
                except OSError:
                    pass
                finally:
                    conn.close()
        threading.Thread(target=answer, daemon=True).start()
        return srv.getsockname()[1]

    def docker(name, mapping):
        return lambda argv, **_kw: SimpleNamespace(returncode=0, stdout=(
            mapping if argv[1] == "port" else "/" + name if argv[-1] == "{{.Name}}" else "x"))
    servers: list[socket.socket] = []
    outcomes = {}
    with tempfile.TemporaryDirectory() as state, patch.dict(os.environ, {"AILOCAL_STATE": state}):
        tls.ensure("10.11.12.13")
        served = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        served.load_cert_chain(tls.server_cert(), tls.server_key())
        plain, verified = listener(None), listener(served)
        cases = {"loopback proxy": ("127.0.0.1", S.CONTAINER, "4000/tcp -> 127.0.0.1:4000"),
                 "proxy on every interface": ("127.0.0.1", S.CONTAINER, f"4000/tcp -> 0.0.0.0:{plain}"),
                 "proxy off loopback while bound": ("10.11.12.13", S.CONTAINER, f"4000/tcp -> 0.0.0.0:{plain}"),
                 "sidecar serving plain HTTP": ("10.11.12.13", S.TLS_CONTAINER, f"4443/tcp -> 10.11.12.13:{plain}"),
                 "sidecar for another address": ("10.11.12.99", S.TLS_CONTAINER, f"4443/tcp -> 10.11.12.99:{verified}"),
                 "sidecar serving verified TLS": ("10.11.12.13", S.TLS_CONTAINER, f"4443/tcp -> 10.11.12.13:{verified}")}
        for label, (bind, name, mapping) in cases.items():
            with patch.object(S, "_run", side_effect=docker(name, mapping)), \
                    patch.object(S, "_running_containers", lambda **_kw: ["x"]), \
                    patch.object(S, "_dial", lambda m: ("127.0.0.1", int(m.rsplit(":", 1)[1]))), \
                    patch.object(environment, "bind", lambda b=bind: b):
                first = S.check_loopback()[0]
                outcomes[label] = (first.status, "plain HTTP" in first.summary)
    for srv in servers:
        srv.close()
    plain_cases = ("proxy on every interface", "proxy off loopback while bound", "sidecar serving plain HTTP")
    want = {label: (FAIL, label in plain_cases) for label in cases} \
        | {"loopback proxy": (PASS, False), "sidecar serving verified TLS": (PASS, False)}
    check(outcomes == want, "only the TLS sidecar leaves loopback, and `ailocal check` fails any off-loopback "
          "listener that serves plain HTTP or TLS the ailocal CA does not verify (#126)", repr(outcomes))


def managed_ollama() -> None:
    suite.section("managed Ollama runtime (#74)")
    with tempfile.TemporaryDirectory() as box:
        real = Path(box).resolve() / "Caskroom/ollama"  # lsof reports /private/var, not /var
        real.parent.mkdir()
        real.write_text("")
        (link := Path(box) / "ollama").symlink_to(real)
        plist = Path(box) / "com.ailocal.ollama.plist"
        app = "/Applications/Ollama.app/Contents/Resources/ollama"
        cases = {"unreadable plist": (None, [str(link), "serve"], str(real)),
                 "plist names another program": (str(link), [app, "serve"], str(real)),
                 "daemon runs another executable": (str(link), [str(link), "serve"], app),
                 "no listener": (str(link), [str(link), "serve"], ""),
                 "dangling cask symlink": (str(link), [str(link), "serve"], str(real)),
                 "selected binary on 11434": (str(link), [str(link), "serve"], str(real))}
        got = {}
        for label, (selected, program, running) in cases.items():
            plist.unlink(missing_ok=True)
            if selected:
                plist.write_bytes(plistlib.dumps({"ProgramArguments": program}))
            if label == "dangling cask symlink":
                real.unlink()
            elif not real.exists():
                real.write_text("")
            with patch.object(S, "MANAGED_OLLAMA", plist), \
                    patch.object(P, "ollama_binary", return_value=selected or str(link)), \
                    patch.object(S, "_listener_executable", return_value=running):
                r = S.check_managed_ollama_runtime()
            got[label] = r.status if r.status is PASS or r.remediation == "ailocal install" else "no-remediation"
    want = {label: FAIL for label in cases} | {"selected binary on 11434": PASS}

    # A client connected to :11434 lists before the daemon in plain lsof (#132).
    def lsof_ps(*argv: str, timeout: float | None = None) -> str:
        if argv[0] == "lsof":
            return "222" if "-sTCP:LISTEN" in argv else "111\n222"
        return {"111": "OLLAMA_MODELS=/client", "222": "OLLAMA_MODELS=/daemon"}.get(argv[-1], "")
    with patch("ailocal.primitives.command_output", lsof_ps), \
            patch.object(H, "command_output", lsof_ps):
        got["models dir"] = H._models_dir()
    want["models dir"] = "/daemon"
    check(got == want, "only the selected Ollama binary listening on 11434 passes, anything else "
          "fails with `ailocal install`, and the models dir is the listener's", repr(got))


def commands() -> None:
    suite.section("read-only commands")
    # `trace`/`status` raised NameError on a deleted helper past import checks.
    broken = {}
    for cmd in ("help", "--version", "status", "trace", "profile active-tier"):
        r = run([sys.executable, "-m", "ailocal.cli", *cmd.split()], timeout=120, cwd=REPO)
        if "Traceback" in r.stderr or r.returncode == 124:
            broken[cmd] = (r.stderr.strip().splitlines() or [""])[-1]
    check(not broken, "read-only commands run without a traceback", repr(broken))


def live() -> None:
    suite.section("`ailocal check` against the live installation")

    def rc(env: dict | None = None) -> int:
        return run([sys.executable, "-m", "ailocal.checks.run", "check"], timeout=900,
                   env={**os.environ, **(env or {})}).returncode

    unreachable, healthy = rc({"AILOCAL_PROXY": "http://127.0.0.1:1"}), rc()
    check((unreachable, healthy) == (1, 0),
          "check exits 1 when the proxy is unreachable and 0 on the healthy install",
          f"unreachable={unreachable} healthy={healthy}")


if __name__ == "__main__":
    for section in (services, managed_ollama, commands, live):
        try:
            section()
        except Exception as exc:  # noqa: BLE001 - one broken section must not hide the others
            suite.error(f"{section.__name__} section raised", exc)
    sys.exit(suite.report())
