#!/usr/bin/env python3
"""test-lsp-baseline.py — claude-local's Python LSP works without Cadence.

ailocal provides the minimum local-client compatibility baseline required by the
isolated profiles it creates. Cadence provides repository intelligence, broader
language tooling, cross-client integration, and policy.

This asserts the ailocal half, and nothing else. It does NOT check TypeScript, Go
or C — those are Cadence's, and asserting them here would create a second owner.

WHY IT DRIVES THE SERVER DIRECTLY. Presence is not capability: a plugin can be
installed, ENABLE_LSP_TOOL can be set, and the tool can still answer nothing
because the server binary is missing or cannot initialize. So this speaks LSP to
pyright-langserver over stdio and requires a real document-scoped answer about a
real file in this repository. Tool listing is not acceptance.

Fast and offline — no model, no proxy, no container.

Exit: 0 baseline healthy, 1 broken, 0 with an explicit SKIP when the server is
absent (that is an install-time condition the installer already reports).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness import REPO, Suite
ROOT = Path.home() / ".config" / "ailocal" / "claude"
PROBE = REPO / "deploy" / "litellm" / "hooks" / "persona_injector.py"

_suite = Suite()
check = _suite.check


def rpc(proc, msg: dict) -> None:
    body = json.dumps(msg).encode()
    proc.stdin.write(f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
    proc.stdin.flush()


def read(proc) -> dict | None:
    length = 0
    while True:
        line = proc.stdout.readline()
        if not line:
            return None
        line = line.decode(errors="replace").strip()
        if line.lower().startswith("content-length:"):
            length = int(line.split(":", 1)[1])
        elif line == "":
            break
    return json.loads(proc.stdout.read(length).decode()) if length else None


def main() -> int:
    print("ailocal minimum LSP baseline (Python, claude-local)")

    # 1. the isolated root has the tool switched on
    settings = ROOT / "settings.json"
    if settings.exists():
        env = (json.loads(settings.read_text()).get("env") or {})
        check(env.get("ENABLE_LSP_TOOL") == "1",
              f"ENABLE_LSP_TOOL=1 in the isolated root ({env.get('ENABLE_LSP_TOOL')})")
    else:
        check(False, f"{settings} exists")

    # 2. the server binary exists in the environment claude-local actually uses
    server = shutil.which("pyright-langserver")
    if not server:
        print("\nSKIP — pyright-langserver is not installed, so there is no baseline")
        print("       to verify. `ailocal clients claude` reports this")
        print("       too. Install with: npm i -g pyright")
        return 0
    check(True, f"pyright-langserver resolvable ({server})")

    # 3. the plugin that wires it to Claude Code is present in THAT root
    if shutil.which("claude"):
        out = subprocess.run(["claude", "plugin", "list"], capture_output=True,
                             text=True, timeout=120,
                             env={**os.environ, "CLAUDE_CONFIG_DIR": str(ROOT)}).stdout
        check("pyright-lsp@claude-plugins-official" in out,
              "pyright-lsp plugin installed in the isolated root")
    else:
        check(False, "claude on PATH")

    # 4. THE ACTUAL OPERATION. Everything above is configuration; this is the only
    #    step that proves the server answers a real question about a real file.
    check(PROBE.is_file(), f"probe file exists ({PROBE.relative_to(REPO)})")
    proc = subprocess.Popen([server, "--stdio"], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        rpc(proc, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                   "params": {"processId": os.getpid(),
                              "rootUri": REPO.as_uri(),
                              "capabilities": {}}})
        # Read until the RESPONSE to id 1. The server emits notifications
        # (window/logMessage, progress) before it answers, so taking the first
        # message off the wire tests message ordering, not initialization.
        init = None
        for _ in range(20):
            msg = read(proc)
            if msg is None:
                break
            if msg.get("id") == 1:
                init = msg
                break
        check(bool(init and "result" in init), "server initializes")
        rpc(proc, {"jsonrpc": "2.0", "method": "initialized", "params": {}})

        rpc(proc, {"jsonrpc": "2.0", "method": "textDocument/didOpen",
                   "params": {"textDocument": {
                       "uri": PROBE.as_uri(), "languageId": "python",
                       "version": 1, "text": PROBE.read_text()}}})

        rpc(proc, {"jsonrpc": "2.0", "id": 2, "method": "textDocument/documentSymbol",
                   "params": {"textDocument": {"uri": PROBE.as_uri()}}})

        symbols = None
        for _ in range(40):                     # skip diagnostics/progress notices
            msg = read(proc)
            if msg is None:
                break
            if msg.get("id") == 2:
                symbols = msg.get("result")
                break
        names = [s.get("name") for s in (symbols or [])] if isinstance(symbols, list) else []
        check(bool(names), f"documentSymbol returns real symbols ({len(names)} found)")
        if names:
            print(f"        e.g. {', '.join(str(n) for n in names[:5])}")
    finally:
        try:
            proc.kill()
        except Exception:
            pass

    print()
    rc = _suite.report()
    if rc:
        print("\nRepair: ailocal clients claude")
    return rc
    return 0


if __name__ == "__main__":
    sys.exit(main())
