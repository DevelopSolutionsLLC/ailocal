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
import hashlib
import os
import re
import shutil
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


def _native_mcp(calls: list):
    """A stand-in for `claude mcp` / `codex mcp` that writes where the real CLIs
    do: the root named by CLAUDE_CONFIG_DIR or CODEX_HOME, nowhere else."""
    def run(command, **kwargs):
        env = kwargs["env"]
        calls.append((command, env["CLAUDE_CONFIG_DIR" if command[0] == "claude" else "CODEX_HOME"]))
        if command[0] == "claude":
            path = Path(env["CLAUDE_CONFIG_DIR"]) / ".claude.json"
            data = json.loads(path.read_text()) if path.exists() else {}
            servers = data.setdefault("mcpServers", {})
            if command[2] == "remove":
                servers.pop(command[-1])
            else:
                servers[command[-2]] = json.loads(command[-1])
            path.write_text(json.dumps(data, indent=2))
        else:
            path = Path(env["CODEX_HOME"]) / "config.toml"
            body = path.read_text() if path.exists() else ""
            if command[2] == "remove":
                body = re.sub(rf"(?ms)^\[mcp_servers\.{re.escape(command[3])}\][^\n]*\n(?:(?!^\[).*?\n)*",
                              "", body)
            else:
                sep = command.index("--")
                body += (f'\n[mcp_servers.{command[3]}]\ncommand = "{command[sep + 1]}"\n'
                         f"args = {json.dumps(command[sep + 2:])}\n")
            path.write_text(body)
        return SimpleNamespace(returncode=0)
    return run


def _agent_lifecycle() -> None:
    """The agent MCP and the codex instruction file, in throwaway roots.

    The defect this pins: ailocal registered `local-agent` in the HOSTED
    ~/.claude.json and ~/.codex/config.toml with no owner marker, so deleting
    ailocal's roots -- its documented uninstall -- left hosted entries pointing
    at a removed command, and Cadence's same-name declaration could not tell
    them apart from its own."""
    _suite.section("AGENT MCP: LOCAL ROOTS ONLY, HOSTED BYTE-IDENTICAL (THROWAWAY ROOTS)")
    with tempfile.TemporaryDirectory() as box:
        root = Path(box)
        hosted, local = root / "hosted", root / "local"
        hosted.mkdir()
        (local / "claude").mkdir(parents=True)
        (local / "codex").mkdir(parents=True)
        ours = {"type": "stdio", "command": "ailocal", "args": ["mcp", "agent"], "env": {}}
        # Hosted: Cadence's marked declaration and a foreign server. ailocal must
        # not touch either, even though `local-agent` has ailocal's exact shape.
        (hosted / ".claude.json").write_text(json.dumps({"numStartups": 7, "mcpServers": {
            "foreign": {"command": "keep"}, "local-agent": ours}}))
        (hosted / "config.toml").write_text(
            'model = "keep"\n[mcp_servers.local-agent] # managed by cadence - x\n'
            'command = "ailocal"\nargs = ["mcp", "agent"]\n')
        # Local: ailocal's former registration beside a foreign server.
        cl, cx = local / "claude/.claude.json", local / "codex/config.toml"
        cl.write_text(json.dumps({"session": "keep", "mcpServers": {
            "foreign": {"command": "keep"}, "local-agent": ours}}))
        cx.write_text('# Generated by ailocal. Do not edit.\nmodel = "ailocal-implementation"\n'
                      '[mcp_servers.foreign]\ncommand = "keep"\n'
                      '[mcp_servers.local-agent]\ncommand = "/opt/bin/ailocal"\nargs = ["mcp", "agent"]\n')
        before = {p: p.read_bytes() for p in hosted.iterdir()}
        calls: list = []
        with patch.dict(os.environ, {"CODEX_HOME": str(hosted), "CLAUDE_CONFIG_DIR": str(hosted)}), \
                patch.object(C.policy, "deployed_client_root", return_value=local), \
                patch.object(C, "present", return_value=True), \
                patch.object(C, "provision_mcp_runtime", return_value=root / "py"), \
                patch.object(C.subprocess, "run", side_effect=_native_mcp(calls)):
            check(C.provision_local_agent(), "install: agent MCP provisioned for claude and codex")
            servers = {"claude": json.loads(cl.read_text())["mcpServers"],
                       "codex": tomllib.loads(cx.read_text())["mcp_servers"]}
            for name, got in servers.items():
                check(C.AGENT_MCP in got and Path(got[C.AGENT_MCP]["command"]).name == "ailocal"
                      and got[C.AGENT_MCP]["args"] == ["mcp", "agent"],
                      f"install: {name}-local registers `{C.AGENT_MCP}` -> ailocal mcp agent")
                check(C.LEGACY_AGENT_MCP not in got,
                      f"install: {name}-local's former `{C.LEGACY_AGENT_MCP}` is retired")
                check(got.get("foreign") == {"command": "keep"},
                      f"install: {name}-local's foreign server survives")
            check(json.loads(cl.read_text())["session"] == "keep"
                  and tomllib.loads(cx.read_text())["model"] == "ailocal-implementation",
                  "install: local session and model settings survive")
            check(all(p.read_bytes() == b for p, b in before.items()),
                  "install: hosted ~/.claude.json and ~/.codex/config.toml are byte-identical")
            check(calls and all(where == str(local / c[0]) for c, where in calls),
                  "install: every native call targets an isolated root",
                  str([where for _, where in calls]))
            check(not any(c[2] in ("add", "add-json") and C.LEGACY_AGENT_MCP in c for c, _ in calls),
                  "install: nothing is registered under the hosted alias")

            snapshot = {p: p.read_bytes() for p in (cl, cx)}
            n = len(calls)
            check(C.provision_local_agent() and len(calls) == n,
                  "reinstall: no native call when the registration is current")
            check(all(p.read_bytes() == b for p, b in snapshot.items()),
                  "reinstall: local roots are byte-identical")

            data = json.loads(cl.read_text())
            data["mcpServers"][C.AGENT_MCP] = {"command": "operator-server"}
            data["mcpServers"][C.LEGACY_AGENT_MCP] = {"command": "operator-local-agent"}
            cl.write_text(json.dumps(data))
            check(not C.provision_local_agent(["claude"]),
                  "collision: a foreign `agent` server returns failure")
            data = json.loads(cl.read_text())["mcpServers"]
            check(data[C.AGENT_MCP] == {"command": "operator-server"}
                  and data[C.LEGACY_AGENT_MCP] == {"command": "operator-local-agent"},
                  "collision: foreign `agent` and foreign-shaped `local-agent` are left untouched")

        # ailocal's uninstall is `ailocal teardown` plus deleting its roots
        # (docs/architecture.md). Nothing ailocal wrote may outlive that.
        shutil.rmtree(local)
        check(all(p.read_bytes() == b for p, b in before.items()),
              "uninstall: deleting ailocal's roots leaves hosted roots exactly as seeded")

    _suite.section("CODEX ROOT: NO INSTRUCTION FILE; A PRIOR RENDERING IS RETIRED ONLY BY DIGEST")
    with tempfile.TemporaryDirectory() as box:
        local = Path(box) / "local"
        home = local / "codex"
        home.mkdir(parents=True)
        mine = "# ailocal local runtime\nrendered by a previous ailocal\n"
        agents = home / "AGENTS.md"
        agents.write_text(mine)
        digest = hashlib.sha256(mine.encode()).hexdigest()
        with patch.object(C, "RETIRED_CODEX_INSTRUCTIONS", frozenset({digest})), \
                patch.object(C.policy, "deployed_client_root", return_value=local), \
                patch.object(C, "provision_mcp_runtime", return_value=None):
            C.target_codex()
            check(not agents.exists(), "a byte-identical prior ailocal rendering is retired")
            C.target_codex()
            check(not agents.exists(), "reinstall: target_codex writes no AGENTS.md")
            agents.write_text(mine + "an operator's edit\n")
            C.target_codex()
            check(agents.read_text() == mine + "an operator's edit\n",
                  "an edited or composed AGENTS.md (Cadence's) is left in place")
            agents.unlink()
            elsewhere = Path(box) / "elsewhere.md"
            elsewhere.write_text(mine)
            agents.symlink_to(elsewhere)
            C.target_codex()
            check(agents.is_symlink() and elsewhere.read_text() == mine,
                  "a symlinked AGENTS.md is never removed or written through")
    check(len(C.RETIRED_CODEX_INSTRUCTIONS) == 9
          and all(re.fullmatch(r"[0-9a-f]{64}", d) for d in C.RETIRED_CODEX_INSTRUCTIONS),
          "every historical rendering digest is a well-formed SHA-256")
    check(not (C.policy.data_root() / "clients/codex/AGENTS.md.template").exists(),
          "the codex instruction template is no longer shipped")


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

    _agent_lifecycle()

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

    _suite.section("VS CODE PRESENCE IS THE APP OR ITS CLI, NEVER LEFTOVER SETTINGS")
    saved = (C._vscode_app, C._vscode_user_dir, C.shutil.which)
    try:
        C._vscode_user_dir = lambda: Path("/leftover/Code/User")
        C.shutil.which = lambda name: None
        C._vscode_app = lambda: None
        check(not C.present("vscode"), "a user directory with no app and no CLI is absent")
        C._vscode_app = lambda: Path("/Applications/Visual Studio Code.app")
        check(C.present("vscode"), "an intact app bundle without the CLI is present")
        C._vscode_app = lambda: None
        C.shutil.which = lambda name: "/usr/local/bin/code" if name == "code" else None
        check(C.present("vscode"), "the code CLI alone is present")
    finally:
        C._vscode_app, C._vscode_user_dir, C.shutil.which = saved
    with tempfile.TemporaryDirectory() as box:
        empty = Path(box) / "Applications" / "Visual Studio Code.app"
        empty.mkdir(parents=True)
        home = Path.home
        try:
            Path.home = classmethod(lambda cls: Path(box))
            found = C._vscode_app()
        finally:
            Path.home = home
        check(found != empty, "an emptied .app directory is not a bundle")

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
