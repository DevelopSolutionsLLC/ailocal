#!/usr/bin/env python3
"""client-targets.py — WHICH clients get configured, and what VS Code reports.

Two contracts, both of them user-visible and both regressed once:

  * target selection — `all` is every supported client, no argument is only the
    ones this machine has, a name is honoured whether or not it is installed.
  * install_vscode's THREE outcomes — success, absence, and an actual failure —
    because the caller does different things with each, and collapsing failure
    into absence silently drops the Copilot instruction files.

Nothing here touches the real machine: the per-client configurators, generation
and the shell rewrite are all replaced with recorders.
"""
from __future__ import annotations

import sys
import json
import os
import tempfile
import tomllib
from types import SimpleNamespace
from unittest.mock import patch
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from harness import Suite  # noqa: E402
from ailocal import clients as C  # noqa: E402
from ailocal import generation  # noqa: E402

_suite = Suite()
check = _suite.check


def _configured(argv, present: set[str]) -> tuple[int, list[str]]:
    """Run clients.main with every side effect stubbed; return (rc, targets)."""
    order: list[str] = []
    saved = (dict(C._TARGETS), C.present, C._master_key,
             C.ensure_shell_sourcing, C.report_missing, generation.main,
             C.provision_documentation)
    try:
        C._TARGETS.update({n: (lambda n=n: order.append(n)) for n in C._TARGETS})
        C.present = lambda name: name in present
        C._master_key = lambda: "sk-test"
        C.ensure_shell_sourcing = lambda key: None
        C.report_missing = lambda names: None
        generation.main = lambda argv: 0
        C.provision_documentation = lambda names: True
        return C.main(argv), order
    finally:
        (targets, C.present, C._master_key, C.ensure_shell_sourcing,
         C.report_missing, generation.main, C.provision_documentation) = saved
        C._TARGETS.clear()
        C._TARGETS.update(targets)


def _vscode(user_dir, which_code: bool, writes_fail: bool = False) -> int:
    """Run install_vscode against a stubbed VS Code; return its outcome code."""
    saved = (C._vscode_user_dir, C.shutil.which, C._code, C._extensions,
             C._install_extension, C._provider_group, C._prune_deprecated,
             C._ensure_recommended, C.provision_vscode_research)

    def boom(*a, **k):
        raise OSError(13, "Permission denied")

    try:
        C._vscode_user_dir = lambda: user_dir
        C.shutil.which = lambda name: "/usr/local/bin/code" if which_code else None
        C._code = lambda *a: "1.99.0\nabc\nx64" if which_code else None
        C._extensions = lambda: []
        C._install_extension = lambda ext, installed: None
        C._provider_group = boom if writes_fail else (lambda p, dry: None)
        C._prune_deprecated = lambda p, dry: None
        C._ensure_recommended = lambda p: None
        C.provision_vscode_research = lambda p: True
        return C.install_vscode([])
    finally:
        (C._vscode_user_dir, C.shutil.which, C._code, C._extensions,
         C._install_extension, C._provider_group, C._prune_deprecated,
         C._ensure_recommended, C.provision_vscode_research) = saved


def main() -> None:
    _suite.section("DOCUMENTATION MCP NATIVE REGISTRATION (SIMULATED CLIENT WRITES)")
    with tempfile.TemporaryDirectory() as box:
        root = Path(box)
        hosted = root / "hosted"
        local = root / "local"
        hosted.mkdir()
        (hosted / ".claude.json").write_text(json.dumps({"session": "preserve", "mcpServers": {
            "foreign": {"command": "keep"}}}))
        (hosted / "config.toml").write_text('model = "keep"\n[mcp_servers.foreign]\ncommand = "keep"\n')
        calls = []

        def native(command, **kwargs):
            client = command[0]
            env = kwargs["env"]
            if client == "claude":
                path = Path(env["CLAUDE_CONFIG_DIR"]) / ".claude.json"
                data = json.loads(path.read_text()) if path.exists() else {}
                data.setdefault("mcpServers", {})[command[-2]] = json.loads(command[-1])
                path.write_text(json.dumps(data))
            else:
                path = Path(env["CODEX_HOME"]) / "config.toml"
                body = path.read_text() if path.exists() else ""
                path.write_text(body + f'\n[mcp_servers.{command[3]}]\nurl = "{command[5]}"\n')
            calls.append(command)
            return SimpleNamespace(returncode=0)

        with patch.dict(os.environ, {"CODEX_HOME": str(hosted), "CLAUDE_CONFIG_DIR": str(hosted)}), \
                patch.object(C.policy, "deployed_client_root", return_value=local), \
                patch.object(C, "present", return_value=True), \
                patch.object(C.subprocess, "run", side_effect=native):
            check(C.provision_documentation(), "fresh roots register documentation through native CLIs")
            check(len(calls) == 12, "three vendor docs registered in both clients and both roots")
            check(C.provision_documentation() and len(calls) == 12,
                  "reinstall is idempotent without rewriting existing registrations")
            check(json.loads((hosted / ".claude.json").read_text())["session"] == "preserve"
                  and tomllib.loads((hosted / "config.toml").read_text())["model"] == "keep",
                  "hosted session and model settings survive")
            check(json.loads((hosted / ".claude.json").read_text())["mcpServers"]["foreign"]
                  == {"command": "keep"}, "foreign MCP server survives")
            path = local / "claude/.claude.json"
            data = json.loads(path.read_text())
            data["mcpServers"]["openai-docs"] = {"command": "operator-server"}
            path.write_text(json.dumps(data))
            check(not C.provision_documentation(), "foreign same-name collision returns failure")
            check(json.loads(path.read_text())["mcpServers"]["openai-docs"]
                  == {"command": "operator-server"}, "colliding server remains untouched")
            path.unlink()
            with patch.object(C.subprocess, "run", return_value=SimpleNamespace(returncode=1)):
                check(not C.provision_documentation(), "native CLI failure propagates")
            with patch.object(C, "present", return_value=False), \
                    patch.object(C.subprocess, "run") as run:
                check(C.provision_documentation() and not run.called, "missing optional clients are skipped")
            with patch.object(C.subprocess, "run") as run:
                check(C.provision_documentation(["vscode"]) and not run.called,
                      "Copilot documentation provisioning is deferred")
        with patch.dict(os.environ, {}, clear=True), patch.object(Path, "home", return_value=root):
            check(C.policy.client_mcp_path("claude", local=False) == root / ".claude.json",
                  "default hosted Claude registration uses native ~/.claude.json location")

    _suite.section("TARGET SELECTION")

    with patch.object(generation, "main") as generate, \
            patch.object(C, "ensure_shell_sourcing") as shell, \
            patch.object(C, "_master_key") as key, \
            patch.object(C, "present", return_value=True), \
            patch.dict(C._TARGETS, {n: (lambda: check(False, "dry-run wrote client state"))
                                    for n in C.TARGETS}):
        rc = C.main(["all", "--dry-run"])
        check(rc == 0 and not generate.called and not shell.called and not key.called,
              "dry-run skips generation, secrets, shell edits and client setup")

    rc, order = _configured(["all"], present={"claude"})
    check(rc == 0 and order == list(C.TARGETS),
          "`all` configures every supported client", f"rc={rc} order={order}")

    rc, order = _configured([], present={"claude", "codex"})
    check(rc == 0 and set(order) == {"claude", "codex"},
          "no argument configures only the DETECTED clients", f"order={order}")

    rc, order = _configured(["codex"], present=set())
    check(rc == 0 and order == ["codex"],
          "a named target is configured even when it is not installed",
          f"rc={rc} order={order}")

    rc, order = _configured(["nope"], present={"claude"})
    check(rc == 1 and order == [],
          "an unknown target still fails and configures nothing",
          f"rc={rc} order={order}")

    rc, order = _configured([], present=set())
    check(rc == 0 and order == [],
          "no client installed and no argument: clean exit, nothing configured",
          f"rc={rc} order={order}")

    _suite.section("install_vscode REPORTS WHICH KIND OF OUTCOME IT WAS")

    got = _vscode(Path("/tmp/does-not-need-to-exist"), which_code=True)
    check(got == C.VSCODE_OK, "a successful configuration returns VSCODE_OK",
          f"got {got}")

    got = _vscode(None, which_code=False)
    check(got == C.VSCODE_ABSENT, "VS Code absent returns VSCODE_ABSENT",
          f"got {got}")

    got = _vscode(None, which_code=True)
    check(got == C.VSCODE_ABSENT,
          "VS Code installed but never launched returns VSCODE_ABSENT",
          f"got {got}")

    got = _vscode(Path("/tmp/does-not-need-to-exist"), which_code=True,
                  writes_fail=True)
    check(got == C.VSCODE_FAILED,
          "a write failure returns VSCODE_FAILED, NOT absence", f"got {got}")

    check(C.VSCODE_ABSENT != C.VSCODE_FAILED != C.VSCODE_OK,
          "the three outcomes are distinct values")

    _suite.section("AND THE CALLER ACTS ON THE DIFFERENCE")

    def _ran_instructions(outcome: int) -> bool:
        seen = []
        saved = (C.install_vscode, C._copilot_instructions)
        try:
            C.install_vscode = lambda argv: outcome
            C._copilot_instructions = lambda: seen.append(True)
            C.target_vscode()
        finally:
            (C.install_vscode, C._copilot_instructions) = saved
        return bool(seen)

    check(_ran_instructions(C.VSCODE_OK),
          "on success the Copilot instruction files ARE deployed")
    check(not _ran_instructions(C.VSCODE_ABSENT),
          "on absence they are skipped — there is nothing to read them")
    check(not _ran_instructions(C.VSCODE_FAILED),
          "on a real failure they are skipped, and the failure is reported")

    sys.exit(_suite.report())


if __name__ == "__main__":
    main()
