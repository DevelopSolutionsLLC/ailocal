#!/usr/bin/env python3
"""Test the LAN TLS sidecar: certificates, bind refusal, compose, audit (#126).

Scratch roots only; nothing here starts a container or touches the live stack.
"""
from __future__ import annotations

import contextlib
import io
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness import RESOURCES, Suite  # noqa: E402

from ailocal import audit, environment, runtime, tls  # noqa: E402
from ailocal.checks import BLOCKED, SKIP  # noqa: E402
from ailocal.checks import services as S  # noqa: E402

suite = Suite("LAN TLS SIDECAR")
check = suite.check
LAN = "10.11.12.13"


@contextlib.contextmanager
def roots():
    with tempfile.TemporaryDirectory() as box, \
            patch.dict(os.environ, {"AILOCAL_STATE": f"{box}/state", "AILOCAL_CONFIG": f"{box}/config"}), \
            contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        yield Path(box)


def mode(path: Path) -> int:
    return path.stat().st_mode & 0o777


def certificate() -> None:
    suite.section("certificate material")
    with roots():
        tls.ensure(LAN)
        ca = tls.ca_cert().read_bytes()
        sans = {name for kind, name in tls.handshake(LAN).get("subjectAltName", ())}
        by_name = tls.handshake(tls.hostname()) if tls.hostname() else {}
        modes = {p.name: mode(p) for p in (tls.ca_key(), tls.server_key(), tls.ca_cert().parent,
                                           tls.server_cert().parent)}
        tls.issue_server("10.11.12.14")
        again = tls.ensure(LAN)
        kept = tls.ca_cert().read_bytes() == ca and not tls.problems(LAN)
    check(LAN in sans and (by_name or not tls.hostname()) and kept
          and again == [f"issued a {tls.SERVER_DAYS}-day server certificate for {LAN}"]
          and modes == {"ca.key": 0o600, "server.key": 0o600, "ca": 0o700, "server": 0o700},
          "the server certificate verifies by TLS handshake for the LAN IP and mDNS name, keys are 0600 "
          "in 0700 directories, and a reissue for a changed address keeps the CA", repr((sans, modes, again)))

    with roots():
        tls.ensure(LAN)
        found = {"sound": tls.problems(LAN), "other address": tls.problems("10.11.12.14")}
        tls.issue_server("8.8.8.8")
        found["public address"] = tls.problems("8.8.8.8")
        with patch.object(tls, "SERVER_DAYS", 10):
            tls.issue_server(LAN)
        found["expiring"] = tls.problems(LAN)
        tls.issue_server(LAN)
        tls.server_key().chmod(0o644)
        found["readable key"] = tls.problems(LAN)
    check(not found.pop("sound") and all(found.values()),
          "a SAN that misses the address, an address outside the CA's name constraints, a certificate "
          "inside the renewal window and a key that is not 0600 are each a problem", repr(found))


def refusal() -> None:
    suite.section("binding off loopback without TLS is refused")
    outcomes = {}
    with roots():
        environment.ensure_user()
        before = environment.user_file().read_bytes()
        for label, argv, patches in (
                ("every interface", ["0.0.0.0"], {}),
                ("public address", ["8.8.8.8"], {}),
                ("address not on this host", [LAN], {}),
                ("no openssl", [LAN], {"address_error": lambda _a: "", "shutil": SimpleNamespace(which=lambda _n: None)})):
            with contextlib.ExitStack() as stack:
                for name, value in patches.items():
                    stack.enter_context(patch.object(tls, name, value))
                outcomes[label] = runtime.cmd_bind(argv)
        unchanged = environment.user_file().read_bytes() == before

        # A pre-#126 bind (0.0.0.0) or missing material stops start first.
        refused = {}
        for label, address, patches in (
                ("legacy every-interface bind", "0.0.0.0", {}),
                ("material missing", LAN, {"address_error": lambda _a: "", "ensure": lambda _a: []})):
            environment.set_bind(address)
            with contextlib.ExitStack() as stack:
                for name, value in patches.items():
                    stack.enter_context(patch.object(tls, name, value))
                stack.enter_context(patch.object(runtime, "_preflight", side_effect=AssertionError("started")))
                stack.enter_context(patch.object(runtime, "compose", side_effect=AssertionError("composed")))
                try:
                    runtime.cmd_start([])
                    refused[label] = "started"
                except SystemExit as e:
                    refused[label] = "TLS_BIND_REFUSED" in str(e)
                except AssertionError as e:
                    refused[label] = str(e)

        with patch.object(tls, "lan_address", lambda: LAN), patch.object(tls, "address_error", lambda _a: ""):
            outcomes["lan with TLS"] = runtime.cmd_bind(["lan"])
        bound = environment.bind()
    check(outcomes == {"every interface": 1, "public address": 1, "address not on this host": 1,
                       "no openssl": 1, "lan with TLS": 0}
          and unchanged and bound == LAN and all(v is True for v in refused.values()),
          "`ailocal bind` refuses an address it cannot serve over TLS and writes nothing; `ailocal start` "
          "refuses a bind without verified TLS before touching Docker", repr((outcomes, unchanged, refused)))


def compose() -> None:
    suite.section("compose")
    sidecar = (RESOURCES / "deploy/tls/compose.yaml").read_text()
    files = {}
    with roots():
        for bind in (environment.BIND_LOCAL, LAN):
            with patch.object(environment, "bind", lambda b=bind: b):
                files[bind] = [a for a in runtime.compose_argv(["config"]) if a.endswith("compose.yaml")]
    image = next(line.split("image:", 1)[1].strip() for line in sidecar.splitlines()
                 if line.strip().startswith("image:"))
    volumes = [line.strip() for line in sidecar.splitlines() if ":/tls" in line]
    check(not any("deploy/tls" in f for f in files[environment.BIND_LOCAL])
          and any(f.endswith("deploy/tls/compose.yaml") for f in files[LAN])
          and "@sha256:" in image and volumes == ["- ${AILOCAL_STATE:?run via `ailocal`}/tls/server:/tls:ro"]
          and '"${AILOCAL_BIND:?run via `ailocal bind`}:${AILOCAL_TLS_PORT:-4443}:4443"' in sidecar
          and "--target=litellm:4000" in sidecar,
          "the sidecar is merged only off loopback, pinned by digest, publishes only on the bound address, "
          "and mounts the server pair alone, never the CA key", repr((files, image, volumes)))


def supply_chain() -> None:
    suite.section("audit and update cover the sidecar image")
    pins = {p.name: p for p in audit.pins(lambda: {"ollama": None, "images": {}})}
    sidecar = pins.get(S.TLS_REPO)
    releases = [{"tag_name": "v1.11.5", "published_at": "2026-10-21T17:38:50Z"}]
    with patch.object(S, "http_json", return_value=releases), \
            patch.object(S, "_run", return_value=SimpleNamespace(stdout="", returncode=0)), \
            patch("ailocal.update.advisory_fixes", return_value=([], "")):
        update = [r for r in S.check_updates(S.declared_images()) if S.TLS_REPO in r.summary]
    with patch.object(environment, "bind", lambda: environment.BIND_LOCAL), \
            patch.object(S, "_running_digest", return_value=("", "")):
        drift = [r.status for r in S.check_no_drift(S.declared_images()) if S.TLS_REPO in r.summary]
    check(sidecar is not None and sidecar.github == "ghostunnel/ghostunnel" and sidecar.version == "1.11.5"
          and update and update[0].status is not BLOCKED and drift == [SKIP],
          "`ailocal audit` checks the sidecar against ghostunnel's advisories, `ailocal update` applies the "
          "version policy to it, and drift skips it on loopback", repr((sidecar, update, drift)))


if __name__ == "__main__":
    for section in (certificate, refusal, compose, supply_chain):
        try:
            section()
        except Exception as exc:  # noqa: BLE001 - one broken section must not hide the others
            suite.error(f"{section.__name__} section raised", exc)
    sys.exit(suite.report())
