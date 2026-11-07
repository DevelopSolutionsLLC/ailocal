#!/usr/bin/env python3
"""`ailocal check` and the read-only commands.

The live section runs `ailocal check` against this installation; everything else is in-process.
"""
from __future__ import annotations

import os
import plistlib
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness import REPO, Suite, run  # noqa: E402

from ailocal import environment  # noqa: E402
from ailocal import policy as P  # noqa: E402
from ailocal.checks import FAIL, PASS, WARN  # noqa: E402
from ailocal.checks import services as S  # noqa: E402

suite = Suite("CHECKS AND READ-ONLY COMMANDS")
check = suite.check


def services() -> None:
    suite.section("service probes")
    # A reinstall leaves the container serving an empty bind mount while healthy; repair is `ailocal start`.
    verdicts = []
    for out in (SimpleNamespace(returncode=0, stdout="", stderr=""),
                SimpleNamespace(returncode=1, stdout="", stderr="ls: No such file or directory")):
        with patch.object(S, "container_state", return_value=("running", "")), \
                patch.object(S, "_run", return_value=out):
            r = S.check_config_mount()
        verdicts.append((r.status, r.remediation))
    check(verdicts == [(FAIL, "ailocal start")] * 2,
          "a detached config mount fails and points at `ailocal start`", repr(verdicts))

    # #103: the proxy may leave loopback only when `ailocal bind` says so, and then as a warning.
    def ports(mapping):
        return lambda argv, **_kw: SimpleNamespace(returncode=0, stdout=(
            mapping if argv[1] == "port" else "/" + S.CONTAINER if argv[-1] == "{{.Name}}" else "x"))
    outcomes = {}
    for bind, mapping in (("0.0.0.0", "4000/tcp -> 0.0.0.0:4000\n4000/tcp -> [::]:4000"),
                          ("0.0.0.0", "4000/tcp -> 127.0.0.1:4000"), ("127.0.0.1", "4000/tcp -> 0.0.0.0:4000"),
                          ("10.0.0.5", "4000/tcp -> 0.0.0.0:4000")):
        with patch.object(S, "_run", side_effect=ports(mapping)), patch.object(S, "_running_containers", lambda: ["x"]), \
                patch.object(environment, "bind", lambda: bind):
            outcomes[(bind, mapping.split()[-1])] = S.check_loopback()[0].status
    check(outcomes == {("0.0.0.0", "[::]:4000"): WARN, ("0.0.0.0", "127.0.0.1:4000"): WARN,
                       ("127.0.0.1", "0.0.0.0:4000"): FAIL, ("10.0.0.5", "0.0.0.0:4000"): FAIL},
          "the proxy leaves loopback only on a configured bind, and then as a warning (#103)", repr(outcomes))


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
    check(got == want, "only the selected Ollama binary listening on 11434 passes; anything else "
          "fails with `ailocal install`", repr(got))


def commands() -> None:
    suite.section("read-only commands")
    # `ailocal trace` and `status` once raised NameError on a deleted helper while every import check passed.
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
