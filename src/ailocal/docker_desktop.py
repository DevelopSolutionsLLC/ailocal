"""Check, apply and restore the Docker Desktop settings ailocal recommends.

No documented write path exists for personal installs (admin-settings.json is
Business only; the CLI toggles Model Runner alone), so the undocumented
settings-store.json is written only while stopped, then read back; a value that
did not take is rolled back. Unlisted settings are never written; originals
are recorded once and `restore` puts back those the operator has not changed.
"""

from __future__ import annotations

import http.client
import json
import os
import socket
import subprocess
import sys
import time
import tomllib
from collections.abc import Callable
from pathlib import Path

from . import policy
from .report import dim, ok, step, warn

STORE = Path.home() / "Library/Group Containers/group.com.docker/settings-store.json"
BACKEND = Path.home() / "Library/Containers/com.docker.docker/Data/backend.sock"
#: Outlives disposable state, so teardown after a reinstall still restores.
RECEIPT = Path.home() / "Library/LaunchAgents/com.ailocal.docker-desktop.restore.json"
GUI_PATH = "Docker Desktop > Settings"


def recommended() -> list[tuple[tuple[str, ...], str, object, str]]:
    """Return (backend path, store key, value, why) per ledger entry."""
    path = policy.data_root() / "deploy/docker-desktop/settings.toml"
    with open(path, "rb") as fh:
        entries = tomllib.load(fh)["setting"]
    return [(tuple(e["backend"].split(".")), e["key"], e["recommended"], e["why"]) for e in entries]


class _UnixHTTP(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float) -> None:
        super().__init__("localhost", timeout=timeout)
        self._path = path

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self._path)


def effective(timeout: float = 5) -> dict | None:
    """Return effective settings, or None when the backend is down."""
    if not BACKEND.is_socket():
        return None
    try:
        conn = _UnixHTTP(str(BACKEND), timeout)
        conn.request("GET", "/app/settings")
        resp = conn.getresponse()
        return json.loads(resp.read()) if resp.status == 200 else None
    except (OSError, ValueError, http.client.HTTPException):
        return None


def _value(settings: dict, path: tuple[str, ...]) -> object:
    node = settings
    for key in path:
        node = node.get(key, {}) if isinstance(node, dict) else {}
    return node.get("value") if isinstance(node, dict) else node


def drift(settings: dict) -> list[tuple[str, object, object, str]]:
    """Return (store key, effective, recommended, why) per difference."""
    return [(key, _value(settings, path), want, why) for path, key, want, why in recommended()
            if _value(settings, path) != want]


def _desktop(action: str) -> bool:
    try:
        return subprocess.run(["docker", "desktop", action], capture_output=True,
                              text=True, timeout=180).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _wait(up: bool, seconds: int = 180) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if (effective(timeout=2) is not None) == up:
            return True
        time.sleep(2)
    return False


def _write(values: dict) -> None:
    data = json.loads(STORE.read_text())
    data.update(values)
    tmp = STORE.with_name(STORE.name + ".ailocal-tmp")
    tmp.write_text(json.dumps(data, indent=2))
    os.chmod(tmp, STORE.stat().st_mode & 0o777)
    os.replace(tmp, STORE)


def _restart_with(values: dict | None, backup: bytes | None = None) -> bool:
    """Stop Docker Desktop, write `values` (or restore `backup`), start it."""
    if not (_desktop("stop") and _wait(False)):
        return False
    if backup is not None:
        STORE.write_bytes(backup)
    else:
        _write(values or {})
    return _desktop("start") and _wait(True)


def _save_receipt(originals: dict) -> None:
    if RECEIPT.is_symlink():
        raise RuntimeError("Docker Desktop settings receipt is a symlink; refusing to follow it")
    if RECEIPT.exists():
        # A recorded original is the operator's and is never replaced; a setting
        # the ledger gained since is added, or restore could not put it back.
        record = json.loads(RECEIPT.read_text())
        added = {k: v for k, v in originals.items() if k not in record["settings"]}
        if added:
            record["settings"].update(added)
            tmp = RECEIPT.with_name(RECEIPT.name + ".tmp")
            tmp.write_text(json.dumps(record))
            os.chmod(tmp, 0o600)
            os.replace(tmp, RECEIPT)
        return
    RECEIPT.parent.mkdir(parents=True, exist_ok=True)
    with RECEIPT.open("x") as stream:
        os.fchmod(stream.fileno(), 0o600)
        json.dump({"owner": "ailocal", "schema": 1, "settings": originals}, stream)


def apply(assume_yes: bool, ask: Callable[[str], str]) -> int:
    """Offer the recommended settings; 0 when they are in effect afterwards."""
    step("Docker Desktop settings")
    settings = effective()
    if settings is None or not STORE.is_file():
        warn(f"Docker Desktop's settings could not be read; review them in {GUI_PATH}")
        return 1
    changes = drift(settings)
    if not changes:
        ok("recommended settings already in effect (memory and disk are yours and never changed)")
        return 0
    for key, have, want, why in changes:
        print(f"    {key}: {have} -> {want}  ({why})")
    if not assume_yes and not ask("Apply these and restart Docker Desktop? [y/N]").lower().startswith("y"):
        dim(f"left unchanged; `ailocal docker apply` offers them again, or change them in {GUI_PATH}")
        return 1
    backup = STORE.read_bytes()
    fresh = not RECEIPT.exists()
    _save_receipt({key: have for key, have, _, _ in changes})
    if not _restart_with({key: want for key, _, want, _ in changes}):
        warn("Docker Desktop did not restart cleanly; open it, then run ailocal docker apply")
        return 1
    left = drift(effective() or {})
    if left:
        _restart_with(None, backup)
        if fresh:
            RECEIPT.unlink(missing_ok=True)   # nothing of ours remains to restore
        warn(f"Docker did not accept {', '.join(k for k, *_ in left)}; the previous settings were "
             f"restored. Change them in {GUI_PATH}")
        return 1
    ok(f"applied {len(changes)} recommended setting(s); originals recorded for restore")
    return 0


def restore() -> int:
    """Put back each recorded original the operator has not changed since."""
    if not RECEIPT.is_file() or RECEIPT.is_symlink():
        return 0
    record = json.loads(RECEIPT.read_text())
    if record.get("owner") != "ailocal" or record.get("schema") != 1:
        raise RuntimeError("invalid Docker Desktop settings receipt; settings preserved")
    step("Restoring Docker Desktop settings")
    settings = effective()
    if settings is None:
        warn("Docker Desktop is not running; its settings receipt is kept for the next restore")
        return 1
    ours = {key: want for _, key, want, _ in recommended()}
    now = {key: _value(settings, path) for path, key, _, _ in recommended()}
    back = {k: v for k, v in record["settings"].items() if k in ours and now.get(k) == ours[k]}
    if back and not _restart_with(back):
        warn("Docker Desktop did not restart; its settings receipt is kept for the next restore")
        return 1
    RECEIPT.unlink(missing_ok=True)
    ok(f"restored {len(back)} Docker Desktop setting(s) to their recorded values")
    return 0


USAGE = """usage: ailocal docker [check|apply [--yes]|restore]

  check     list recommended Docker Desktop settings not in effect (exit 1 if any)
  apply     offer them; restarts Docker Desktop. Memory and disk are never changed
  restore   put back the values recorded before the first apply"""


def main(argv: list[str]) -> int:
    action = argv[0] if argv else "check"
    if action in ("-h", "--help") or action not in ("check", "apply", "restore"):
        print(USAGE)
        return 0 if action in ("-h", "--help") else 2
    if action == "check":
        current = effective()
        if current is None:
            warn("Docker Desktop is not running; open -a Docker")
            return 1
        changes = drift(current)
        for key, have, want, why in changes:
            warn(f"{key} is {have}, recommended {want}: {why}")
        if not changes:
            ok("recommended Docker Desktop settings in effect")
        return 1 if changes else 0
    if action == "apply":
        def ask(prompt: str) -> str:
            try:
                return input(f"  {prompt}: ")
            except EOFError:
                return ""
        return apply("--yes" in argv, ask)
    try:
        return restore()
    except (OSError, ValueError, RuntimeError) as error:
        warn(f"not restored ({error}); the receipt is kept")
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
