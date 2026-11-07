#!/usr/bin/env python3
"""Test the claude-local and codex-local zsh launchers against stub clients.

Offline: stub claude, codex and curl on PATH; a scratch AILOCAL_STATE key.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness import REPO, Suite  # noqa: E402

suite = Suite("CLAUDE-LOCAL AND CODEX-LOCAL LAUNCHERS")
check = suite.check
TEMPLATE = REPO / "src/ailocal/resources/clients/configure.template.zsh"
KEY = "sk-test=with=equals"
STUB = '#!/bin/sh\necho "$0"; env | sort; printf "arg:%s\\n" "$@"\n'


def _box(key: bool) -> dict[str, str]:
    root = Path(tempfile.mkdtemp(prefix="ailocal-launch-"))
    (bin_ := root / "bin").mkdir()
    for name in ("claude", "codex"):
        (bin_ / name).write_text(STUB)
        (bin_ / name).chmod(0o755)
    # The override check reads /v1/models; serve only ailocal-primary.
    (bin_ / "curl").write_text('#!/bin/sh\necho \'{"data":[{"id":"ailocal-primary"}]}\'\n')
    (bin_ / "curl").chmod(0o755)
    (state := root / "state").mkdir()
    if key:
        (state / "env").write_text(f"OTHER=1\nLITELLM_MASTER_KEY={KEY}\n")
    return {"HOME": str(root), "PATH": f"{bin_}:/usr/bin:/bin", "AILOCAL_STATE": str(state)}


def _zsh(script: str, env: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run(["zsh", "-fc", f"source {TEMPLATE}\n{script}"], env=env,
                          capture_output=True, text=True, timeout=10)


def _vars(out: str) -> dict[str, str]:
    return dict(line.split("=", 1) for line in out.splitlines() if "=" in line
                and not line.startswith("arg:"))


def environment() -> None:
    suite.section("each launcher hands its client the proxy, key and config")
    env = _box(key=True)
    home = env["HOME"]
    claude = _zsh("claude-local a 'b c'", env)
    codex = _zsh("codex-local x", env)
    got_c, got_x = _vars(claude.stdout), _vars(codex.stdout)
    want_c = {"ANTHROPIC_BASE_URL": "http://127.0.0.1:4000", "ANTHROPIC_API_KEY": KEY,
              "CLAUDE_CONFIG_DIR": f"{home}/.config/ailocal/claude",
              "LOCAL_AGENT_CALLER": "local", "CLAUDE_CODE_DISABLE_1M_CONTEXT": "1",
              "ENABLE_TOOL_SEARCH": "true", "CLAUDE_CODE_DISABLE_WORKFLOWS": "1",
              "API_TIMEOUT_MS": "900000"}
    want_x = {"OPENAI_BASE_URL": "http://127.0.0.1:4000/v1", "OPENAI_API_KEY": KEY,
              "CODEX_HOME": f"{home}/.config/ailocal/codex"}
    bad = {k: got_c.get(k) for k, v in want_c.items() if got_c.get(k) != v}
    bad |= {k: got_x.get(k) for k, v in want_x.items() if got_x.get(k) != v}
    slots = {k: v for k, v in got_c.items() if k.startswith("ANTHROPIC_DEFAULT_")}
    if not slots or not all(v.startswith("ailocal-") for v in slots.values()):
        bad["slots"] = repr(slots)
    if "arg:a\narg:b c" not in claude.stdout:
        bad["claude args"] = claude.stdout[-200:]
    if 'arg:mcp_servers.agent.env.LOCAL_AGENT_CALLER="local"' not in codex.stdout \
            or not codex.stdout.rstrip().endswith("arg:x"):
        bad["codex args"] = codex.stdout[-300:]
    proxied = _vars(_zsh("claude-local", env | {"AILOCAL_PROXY": "http://h:9"}).stdout)
    if proxied.get("ANTHROPIC_BASE_URL") != "http://h:9":
        bad["AILOCAL_PROXY"] = proxied.get("ANTHROPIC_BASE_URL")
    check(claude.returncode == codex.returncode == 0 and not bad,
          "claude-local and codex-local pass base URL, key, config dir, slots and args", repr(bad))


def failures() -> None:
    suite.section("a launcher fails closed and leaks nothing")
    missing = _box(key=False)
    results = {}
    for fn in ("claude-local", "codex-local"):
        r = _zsh(fn, missing)
        results[fn] = (r.returncode, f"{fn}: no key in" in r.stderr, "arg:" in r.stdout)
    env = _box(key=True)
    r = _zsh("claude-local", env | {"AILOCAL_PRIMARY_ALIAS_OVERRIDE": "ailocal-bogus"})
    results["unserved override"] = (r.returncode, "is not served" in r.stderr, "arg:" in r.stdout)
    # Locals must stay inside the functions, or a sourced rc file leaks the key.
    leak = _zsh('claude-local >/dev/null; codex-local >/dev/null\n'
                'print -r -- "${key-unset}|${base-unset}|${state-unset}|${slots-unset}"', env)
    results["leak"] = leak.stdout.strip()
    want = {"claude-local": (1, True, False), "codex-local": (1, True, False),
            "unserved override": (1, True, False), "leak": "unset|unset|unset|unset"}
    check(results == want, "no key or an unserved alias exits 1 before the client runs, "
          "and no launcher variable reaches the caller", repr(results))


def main() -> int:
    if not TEMPLATE.is_file() or not os.access("/bin/zsh", os.X_OK):
        suite.skip("launchers", "zsh or the template is missing")
        return suite.report()
    for section in (environment, failures):
        try:
            section()
        except Exception as exc:  # noqa: BLE001
            suite.error(section.__name__, exc)
    return suite.report()


if __name__ == "__main__":
    sys.exit(main())
