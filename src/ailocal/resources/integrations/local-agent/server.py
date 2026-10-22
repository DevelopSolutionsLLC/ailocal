#!/usr/bin/env python3
"""Stdio MCP boundary that delegates a task to a local ailocal agent.

A hosted Claude or Codex session calls `delegate` to hand a prompt to
claude-local or codex-local running on a local model (ailocal-cyber by default),
the way it would spawn a subagent. The local CLIs are driven headless; the
loopback proxy base URL, key and the isolated config root are injected by the
`ailocal mcp agent` launcher, so a hosted config is never read or written here.

Two capability modes, chosen per call: text-only (the default) runs the local
agent with no tool authority, so it reasons and returns prose; full-agent mode is
opt-in and scoped to a caller-supplied working directory. No credential or raw
CLI diagnostic crosses the MCP boundary -- a failure returns a constant, named
error and any captured text is scoped to the delegated run.
"""
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

log = logging.getLogger('local-agent')

AGENTS = ('claude', 'codex')
DEFAULT_MODEL = os.environ.get('LOCAL_AGENT_DEFAULT_MODEL', 'ailocal-cyber')
MAX_OUTPUT_CHARS = 60000
_UUID = re.compile(r'[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-'
                   r'[0-9a-fA-F]{4}-[0-9a-fA-F]{12}')


class DelegateError(Exception):
    """Only constant, credential-free messages cross the MCP boundary."""


def configure_logging(state: Path) -> None:
    """Record lifecycle and fatal exits off the stdout protocol channel.

    stdout carries the MCP stream and must stay byte-clean, so records go to a
    file under the state directory and to stderr -- the same discipline the
    local-research server uses, so a silent death leaves a diagnostic behind.
    """
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    try:
        state.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(state / 'server.log'))
    except OSError:
        pass
    logging.basicConfig(level=logging.INFO, handlers=handlers, force=True,
                        format='%(asctime)s %(levelname)s %(name)s %(message)s')


def _child_env(agent: str) -> dict[str, str]:
    """Point the local CLI at the loopback proxy and its isolated config root.

    The launcher injects LOCAL_AGENT_* so this boundary never reads a hosted
    config. For Claude the base URL and key are the gateway contract; for Codex
    the same reach the CLI through its own env and the isolated CODEX_HOME.
    """
    env = dict(os.environ)
    base, key = os.environ.get('LOCAL_AGENT_BASE_URL'), os.environ.get('LOCAL_AGENT_KEY')
    if not base or not key:
        raise DelegateError('LOCAL_AGENT_NOT_PROVISIONED: run ailocal clients')
    if agent == 'claude':
        home = os.environ.get('LOCAL_AGENT_CLAUDE_HOME')
        if home:
            env['CLAUDE_CONFIG_DIR'] = home
        env['ANTHROPIC_BASE_URL'] = base
        env['ANTHROPIC_API_KEY'] = key
        # The gateway must discover ailocal-* models for --model to resolve them.
        env['CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY'] = '1'
    else:
        home = os.environ.get('LOCAL_AGENT_CODEX_HOME')
        if home:
            env['CODEX_HOME'] = home
        env['OPENAI_BASE_URL'] = base.rstrip('/') + '/v1'
        env['OPENAI_API_KEY'] = key
    return env


def _claude_argv(prompt, model, tools, cwd, resume) -> list[str]:
    argv = ['claude', '-p', prompt, '--model', model, '--output-format', 'json']
    if resume:
        argv += ['--resume', resume]
    if tools:
        # Opt-in full agent: accept its edits/commands, scoped to the directory.
        argv += ['--dangerously-skip-permissions']
        if cwd:
            argv += ['--add-dir', cwd]
    # Text-only is the default: with no skip-permissions flag, headless Claude
    # auto-denies any tool it would have to prompt for, so the run stays advisory.
    return argv


def _codex_argv(prompt, model, tools, cwd, resume, out_file) -> list[str]:
    head = ['codex', 'exec', '--model', model, '--json', '--skip-git-repo-check',
            '-o', out_file, '--sandbox', 'workspace-write' if tools else 'read-only']
    if tools and cwd:
        head += ['-C', cwd]
    if resume:
        # `exec resume <id>` takes the prompt positionally after the id.
        return head[:2] + ['resume', resume] + head[2:] + [prompt]
    return head + [prompt]


def _run(argv, env, cwd, timeout) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(argv, env=env, cwd=cwd, capture_output=True,
                              text=True, timeout=timeout)
    except FileNotFoundError:
        raise DelegateError(f'AGENT_CLI_MISSING: {argv[0]} is not installed') from None
    except subprocess.TimeoutExpired:
        raise DelegateError(f'DELEGATE_TIMEOUT: no reply within {timeout}s') from None


def _clip(text: str) -> tuple[str, bool]:
    if len(text) <= MAX_OUTPUT_CHARS:
        return text, False
    return text[:MAX_OUTPUT_CHARS], True


def delegate_claude(prompt, model, tools, cwd, resume, timeout) -> dict[str, Any]:
    proc = _run(_claude_argv(prompt, model, tools, cwd, resume),
                _child_env('claude'), cwd if tools else None, timeout)
    try:
        payload = json.loads(proc.stdout)
        text = payload.get('result', '')
        session = payload.get('session_id')
    except (ValueError, AttributeError):
        if proc.returncode != 0:
            raise DelegateError('DELEGATE_FAILED: local Claude run did not complete') from None
        text, session = proc.stdout, None
    clipped, truncated = _clip(text)
    return {'agent': 'claude', 'model': model, 'session_id': session,
            'output': clipped, 'truncated': truncated}


def delegate_codex(prompt, model, tools, cwd, resume, timeout) -> dict[str, Any]:
    with tempfile.NamedTemporaryFile('r', suffix='.txt', delete=False) as handle:
        out_file = handle.name
    try:
        proc = _run(_codex_argv(prompt, model, tools, cwd, resume, out_file),
                    _child_env('codex'), cwd if tools else None, timeout)
        try:
            text = Path(out_file).read_text()
        except OSError:
            text = ''
        if not text and proc.returncode != 0:
            raise DelegateError('DELEGATE_FAILED: local Codex run did not complete')
        # Session id lives in the --json event stream; take the first UUID seen.
        match = _UUID.search(proc.stdout or '')
        session = match.group(0) if match else None
    finally:
        try:
            os.unlink(out_file)
        except OSError:
            pass
    clipped, truncated = _clip(text)
    return {'agent': 'codex', 'model': model, 'session_id': session,
            'output': clipped, 'truncated': truncated}


DELEGATE_DESCRIPTION = (
    'Delegate a task to a local ailocal agent (claude-local or codex-local) '
    'running on a local model, and return its reply. Use it to consult a local '
    'model such as ailocal-cyber the way you would spawn a subagent. By default '
    'the local agent runs text-only with no tool authority; set tools=true (and '
    'a cwd) only when it must edit files or run commands. Pass the returned '
    'session_id back as resume to continue the same local session.')


def build_mcp():
    from mcp.server.mcpserver import MCPServer
    from mcp.server.mcpserver.exceptions import ToolError
    from mcp.types import ToolAnnotations

    app = MCPServer(name='local-agent', instructions=(
        'This server delegates a task to a local ailocal agent and returns its '
        'text. Prefer it when a prompt should be answered by a local model such '
        'as ailocal-cyber rather than by you. Keep tools=false unless the task '
        'requires the local agent to edit files or run commands, in which case '
        'pass tools=true with the working directory as cwd.'))

    @app.tool(name='delegate', description=DELEGATE_DESCRIPTION,
              annotations=ToolAnnotations(destructive_hint=False, open_world_hint=True))
    async def delegate(prompt: str, agent: str = 'claude', model: str | None = None,
                       tools: bool = False, cwd: str | None = None,
                       resume: str | None = None, timeout: int = 600) -> dict[str, Any]:
        try:
            if agent not in AGENTS:
                raise DelegateError(f'UNKNOWN_AGENT: choose one of {", ".join(AGENTS)}')
            if tools and cwd and not Path(cwd).is_dir():
                raise DelegateError('INVALID_CWD: working directory does not exist')
            model = model or DEFAULT_MODEL
            timeout = max(10, min(int(timeout), 3600))
            runner = delegate_claude if agent == 'claude' else delegate_codex
            return runner(prompt, model, bool(tools), cwd, resume, timeout)
        except DelegateError as error:
            raise ToolError(str(error)) from None
        except Exception:
            raise ToolError('DELEGATE_FAILED: check local agent runtime and retry') from None

    return app


def main():
    configure_logging(Path(os.environ.get('LOCAL_AGENT_STATE', '.')))
    log.info('local-agent starting (default model=%s)', DEFAULT_MODEL)
    try:
        build_mcp().run()
    except BaseException:
        log.exception('local-agent exited abnormally')
        raise


if __name__ == '__main__':
    main()
