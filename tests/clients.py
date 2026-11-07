#!/usr/bin/env python3
"""Test what ailocal writes into, and removes from, co-owned client cfg.

Writes only temp roots.
"""
from __future__ import annotations

import json
import os
import re
import sys
import tomllib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness import RESOURCES, Suite, temp_dir  # noqa: E402
from ailocal import clients as C, generation, policy as P  # noqa: E402

S = Suite("ailocal clients")
check = S.check


def vscode() -> None:
    S.section("VS CODE MODEL PROVIDERS ARE CO-OWNED")
    models = generation.load_models_yaml(P.profile_path("64gb", RESOURCES))
    foreign = {"vendor": "customendpoint", "name": "Other endpoint", "models": []}
    stranger = {"vendor": "litellm-connector", "name": "LiteLLM", "baseUrl": "http://10.0.0.5:4000"}
    old = {"vendor": "litellm-connector", "name": "LiteLLM", "baseUrl": C.BASE_URL, "apiKey": "${k}"}
    result = C.vscode_provider_document([foreign, stranger, old], models)
    merged = (result[:2] == [foreign, stranger] and result[-1]["apiKey"] == "${k}"
              and C.vscode_provider_document(result, models) == result)
    kept = []
    with temp_dir() as user, patch.object(C, "_vscode_user_dir", lambda: user):
        target = user / "chatLanguageModels.json"
        for text in ("[", json.dumps([7, {"vendor": C.VSCODE_VENDOR, "name": C.VSCODE_PROVIDER_NAME}]),
                     json.dumps([stranger])):
            target.write_text(text)
            with patch.dict(generation._STAGE, clear=True), patch.dict(generation._STAGED_TEXT, clear=True):
                generation.regen_vscode(models)
                kept.append(target.read_text() == text and target not in generation._STAGE)
    check(merged and all(kept),
          "foreign providers, another proxy's group and the stored key reference survive; an unreadable file is "
          "left untouched and never aborts start", f"merged={merged} kept={kept}")


def _native_mcp(calls: list):
    """Fake `claude mcp`/`codex mcp`, writing only under the configured root."""
    def fake(command, **kwargs):
        var = "CLAUDE_CONFIG_DIR" if command[0] == "claude" else "CODEX_HOME"
        calls.append((command, kwargs["env"][var]))
        root = Path(kwargs["env"][var])
        if command[0] == "claude":
            path = root / ".claude.json"
            data = json.loads(path.read_text()) if path.exists() else {}
            servers = data.setdefault("mcpServers", {})
            if command[2] == "remove":
                servers.pop(command[-1], None)
            else:
                servers[command[-2]] = json.loads(command[-1])
            path.write_text(json.dumps(data, indent=2))
        else:
            path = root / "config.toml"
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
    return fake


def copilot_mcp() -> None:
    S.section("COPILOT PORTABLE MCP")
    with temp_dir() as box:
        path = box / "mcp-config.json"
        agent = {"command": "/old/ailocal", "args": ["mcp", "agent"], "timeout": 123}
        path.write_text(json.dumps({"version": 1, "mcpServers": {"foreign": {"x": 1}, "agent": agent}}))
        path.chmod(0o600)
        with patch.object(C.policy, "client_mcp_path", return_value=path), \
                patch.object(C.shutil, "which", return_value="/new/ailocal"):
            ok = C.provision_copilot_mcp()
            data = json.loads(path.read_text())
            merged = (ok and path.stat().st_mode & 0o777 == 0o600 and data["mcpServers"]["foreign"] == {"x": 1}
                      and data["mcpServers"]["agent"] == {**agent, "command": "/new/ailocal"}
                      and data["mcpServers"]["research"]["args"] == ["mcp", "research"])
            data["mcpServers"]["research"] = {"command": "foreign"}
            refused = []
            for text in (json.dumps(data), "{invalid"):
                path.write_text(text)
                refused.append(not C.provision_copilot_mcp() and path.read_text() == text)
        check(merged and all(refused),
              "Copilot MCP keeps foreign servers, options and 0600; a name collision or invalid JSON fails "
              "and preserves the file", f"merged={merged} refused={refused}")



def agent_mcp() -> None:
    # local-agent was once registered in hosted roots and outlived uninstall.
    S.section("AGENT MCP: LOCAL ROOTS ONLY, HOSTED BYTE-IDENTICAL")
    with temp_dir() as root:
        hosted, local = root / "hosted", root / "local"
        for d in (hosted, local / "claude", local / "codex"):
            d.mkdir(parents=True)
        ours = {"type": "stdio", "command": "ailocal", "args": ["mcp", "agent"], "env": {}}
        (hosted / ".claude.json").write_text(json.dumps({"mcpServers": {"local-agent": ours}}))
        (hosted / "config.toml").write_text(
            'model = "keep"\n[mcp_servers.local-agent] # managed by cadence - x\n'
            'command = "ailocal"\nargs = ["mcp", "agent"]\n')
        cl, cx = local / "claude/.claude.json", local / "codex/config.toml"
        cl.write_text(json.dumps({"session": "keep", "mcpServers": {
            "foreign": {"command": "keep"}, "local-agent": ours}}))
        cx.write_text('# Generated by ailocal. Do not edit.\nmodel = "ailocal-primary"\n'
                      '[mcp_servers.foreign]\ncommand = "keep"\n'
                      '[mcp_servers.local-agent]\ncommand = "/opt/bin/ailocal"\nargs = ["mcp", "agent"]\n')
        before = {p: p.read_bytes() for p in hosted.iterdir()}
        calls: list = []
        problems = []
        with patch.dict(os.environ, {"CODEX_HOME": str(hosted), "CLAUDE_CONFIG_DIR": str(hosted)}), \
                patch.object(C.policy, "deployed_client_root", return_value=local), \
                patch.object(C, "present", return_value=True), \
                patch.object(C, "provision_mcp_runtime", return_value=root / "py"), \
                patch.object(C.subprocess, "run", side_effect=_native_mcp(calls)):
            if not C.provision_local_agent():
                problems.append("provision failed")
            for name, got in (("claude", json.loads(cl.read_text())["mcpServers"]),
                              ("codex", tomllib.loads(cx.read_text())["mcp_servers"])):
                a = got.get(C.AGENT_MCP, {})
                if not (Path(a.get("command", "")).name == "ailocal" and a.get("args") == ["mcp", "agent"]
                        and C.LEGACY_AGENT_MCP not in got and got.get("foreign") == {"command": "keep"}):
                    problems.append(f"{name}: {got}")
            if not (json.loads(cl.read_text())["session"] == "keep"
                    and tomllib.loads(cx.read_text())["model"] == "ailocal-primary"):
                problems.append("local session/model settings lost")
            if not all(w == str(local / c[0]) for c, w in calls):
                problems.append(f"native call outside a local root: {[w for _, w in calls]}")
            data = json.loads(cl.read_text())
            data["mcpServers"][C.AGENT_MCP] = {"command": "operator-server"}
            cl.write_text(json.dumps(data))
            if C.provision_local_agent(["claude"]) or json.loads(cl.read_text()) != data:
                problems.append("a foreign `agent` server was replaced")
        if not all(p.read_bytes() == b for p, b in before.items()):
            problems.append("hosted roots changed")
        check(not problems, "agent MCP writes only local roots, keeps foreign servers and refuses a foreign "
              "`agent`; hosted roots stay byte-identical", "; ".join(problems))


def codex() -> None:
    # Dropped [hooks.state] re-armed "Hooks need review" each start; a DOTALL
    # `.*` once captured the rest of the file, writing later tables twice.
    S.section("CODEX CONFIG.TOML: CO-OWNED RECORDS SURVIVE REGENERATION")
    T = 'model = "ailocal-primary"\n\n[model_providers.ailocal]\nname = "x"\n'
    tpl = T + '\n[mcp_servers.research]\ncommand = "ailocal"\n'
    live = (T + "stale_hand_edit = true\n"
            '\n[projects."/x/repo"]\ntrust_level = "trusted"\n'
            "\n[tui]\ntheme = \"hand-edit\"\nscreen_reader_detection_done = true\n"
            '\n[hooks.state."/x/codex/config.toml:session_start:0:0"]\ntrusted_hash = "sha256:6ef5bb"\n'
            '\n[mcp_servers.research]\ncommand = "stale"\n'
            '\n[mcp_servers.grepai] # managed by external registrar\ncommand = "grepai"\n'
            '\n[mcp_servers.grepai.env]\nEXAMPLE = "value"\n'
            '\n[mcp_servers."server.with.dots"]\ncommand = "other"\n'
            '\n[marketplaces.cadence]\nsource_type = "local"\nsource = "/x/cadence-plugin"\n'
            '\n[plugins."cadence@cadence"]\nenabled = true\n'
            "\n# ── Model Providers ──\n")
    with temp_dir() as td, patch.object(generation, "CODEX_CONFIG", td / "config.toml") as path:
        path.write_text(live)
        out = generation._keep_codex_projects(tpl)
        path.write_text(out)
        again = generation._keep_codex_projects(tpl)
    doc = tomllib.loads(out)
    got = {"projects": doc.get("projects"), "tui": doc.get("tui"), "hooks": len(doc.get("hooks", {}).get("state", {})),
           "servers": doc.get("mcp_servers"), "plugins": doc.get("plugins"),
           "marketplace": "cadence" in doc.get("marketplaces", {}), "stale": "stale_hand_edit" in out,
           "next table's comment": "Model Providers" in out, "fixed point": again == out}
    want = {"projects": {"/x/repo": {"trust_level": "trusted"}}, "tui": {"screen_reader_detection_done": True},
            "hooks": 1, "servers": {"research": {"command": "ailocal"},
                                    "grepai": {"command": "grepai", "env": {"EXAMPLE": "value"}},
                                    "server.with.dots": {"command": "other"}},
            "plugins": {"cadence@cadence": {"enabled": True}}, "marketplace": True, "stale": False,
            "next table's comment": False, "fixed point": True}
    check(got == want, "trust, hook, external MCP and plugin records survive regeneration once; the template "
          "owns its own tables; the merge is a fixed point", f"{got}")


def removal() -> None:
    S.section("CLIENTS --remove: REVERSES ONLY PROVABLE WRITES (#91)")
    with temp_dir() as box:
        b = Path(box)
        zd, cop, vs = b / "zsh", b / "copilot", b / "vscode"
        (cop / "instructions").mkdir(parents=True), zd.mkdir(), vs.mkdir()
        rc = zd / ".zshrc"
        rc.write_text(f"{C._CONFIGURE_LINE}\nexport FOO=1\n\n{C._FINALIZE_LINE}\n")
        head = (P.data_root() / "clients" / "copilot" / "ailocal.instructions.md").read_text()
        (cop / "instructions" / "ailocal.instructions.md").write_text(head + "\nguidance\n")
        (cop / "mcp-config.json").write_text(json.dumps({"mcpServers": {
            "research": {"command": "/x/ailocal", "args": ["mcp", "research"]},
            "agent": {"command": "/x/other", "args": ["mcp", "agent"]}, "mine": {"command": "/y"}}}))
        (vs / "mcp.json").write_text(json.dumps({"servers": {"agent": {"command": "/x/ailocal", "args": ["mcp", "agent"]}}}))
        (vs / "chatLanguageModels.json").write_text(json.dumps(
            [{"name": C.VSCODE_PROVIDER_NAME, "vendor": C.VSCODE_VENDOR, "models": []}, {"name": "other", "vendor": "v"}]))
        (vs / "settings.json").write_text(json.dumps({**C.RECOMMENDED_SETTINGS, "editor.fontSize": 13}))
        env = {"ZDOTDIR": str(zd), "COPILOT_HOME": str(cop), "AILOCAL_VSCODE_USER": str(vs)}
        with patch.dict(os.environ, env), patch.object(C, "backup", lambda p: True):
            lines = C.remove_client_writes()
        check(rc.read_text() == "export FOO=1\n\n" and not (cop / "instructions" / "ailocal.instructions.md").exists()
              and set(json.loads((cop / "mcp-config.json").read_text())["mcpServers"]) == {"agent", "mine"}
              and json.loads((vs / "mcp.json").read_text())["servers"] == {}
              and json.loads((vs / "chatLanguageModels.json").read_text()) == [{"name": "other", "vendor": "v"}]
              and json.loads((vs / "settings.json").read_text()) == {"editor.fontSize": 13}
              and any(l.startswith("KEPT `agent`") for l in lines),
              "removal takes only what ailocal provably wrote; a foreign `agent`, other servers and the user's "
              "own values stay", lines)


def main() -> int:
    for section in (vscode, copilot_mcp, agent_mcp, codex, removal):
        try:
            section()
        except Exception as exc:  # noqa: BLE001
            S.error(section.__name__, exc)
    return S.report()


if __name__ == "__main__":
    sys.exit(main())
