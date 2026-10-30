"""Launch bundled research without importing its SDK into stdlib-only core."""
from __future__ import annotations

import ipaddress
import json
import os
from pathlib import Path
import sys
from urllib.parse import urlsplit

from . import environment, policy, runtime


def registration() -> dict:
    return {'type': 'stdio', 'command': 'ailocal',
            'args': ['mcp', 'research'], 'env': {}}


def agent_registration() -> dict:
    return {'type': 'stdio', 'command': 'ailocal',
            'args': ['mcp', 'agent'], 'env': {}}


def agent_environment() -> dict:
    """Env for the local-agent server: the loopback proxy, its key and the two
    isolated client roots the delegated CLIs run under. Hosted configs are never
    read here -- only the ailocal-owned deployed roots are named."""
    endpoint = runtime.proxy_url().rstrip('/')
    parsed = urlsplit(endpoint)
    try:
        if (parsed.scheme not in ('http', 'https') or not parsed.hostname
                or not ipaddress.ip_address(parsed.hostname).is_loopback
                or parsed.username or parsed.password or parsed.query or parsed.fragment):
            raise ValueError
    except ValueError:
        raise RuntimeError('local-agent requires a loopback proxy endpoint; hosted inference is refused') from None
    roots = policy.deployed_client_root()
    env = dict(os.environ)
    env.update({'LOCAL_AGENT_BASE_URL': endpoint,
                'LOCAL_AGENT_KEY': runtime.env_value('LITELLM_MASTER_KEY'),
                'LOCAL_AGENT_CLAUDE_HOME': str(roots / 'claude'),
                'LOCAL_AGENT_CODEX_HOME': str(roots / 'codex'),
                'LOCAL_AGENT_CLAUDE_ENV': json.dumps(claude_launch_environment()),
                # Every file that can hold the proxy key; a delegated child is
                # denied reads of them (the sandbox otherwise reads most paths).
                'LOCAL_AGENT_DENY_READ': json.dumps([str(p) for p in (
                    environment.user_file(), environment.generated_file(),
                    environment.legacy_file(), environment.legacy_projection())]),
                # Research mode starts this server, as claude-local registers it.
                'LOCAL_AGENT_RESEARCH_MCP': json.dumps(registration()),
                'LOCAL_AGENT_DEFAULT_MODEL': 'ailocal-cyber',
                'LOCAL_AGENT_STATE': str(policy.state_root() / 'local-agent')})
    return env


def claude_launch_environment() -> dict:
    """What claude-local sets besides URL, key and config root: the built-in
    slots from client policy plus its timeout and context switches."""
    from .generation import SLOT_ENV, mn
    slots = (policy.load_client_policy().get('claude') or {}).get('slots', {})
    env = {SLOT_ENV[slot]: mn(cap) for slot, cap in slots.items() if slot in SLOT_ENV}
    env.update({'CLAUDE_CODE_DISABLE_1M_CONTEXT': '1', 'ENABLE_TOOL_SEARCH': 'true',
                'CLAUDE_CODE_DISABLE_WORKFLOWS': '1', 'API_TIMEOUT_MS': '900000'})
    return env


def launch_environment(enable_answer: bool = False) -> dict:
    endpoint = runtime.proxy_url().rstrip('/')
    parsed = urlsplit(endpoint)
    try:
        if (parsed.scheme not in ('http', 'https') or not parsed.hostname
                or not ipaddress.ip_address(parsed.hostname).is_loopback
                or parsed.username or parsed.password or parsed.query or parsed.fragment):
            raise ValueError
    except ValueError:
        raise RuntimeError('research requires a loopback LiteLLM endpoint; hosted inference is refused') from None
    cfg = policy.effective_role('fast')
    # Approximate character accounting with room for source metadata and prompt.
    chars = max(256, min(24000, (cfg['context_input'] - 4096)))
    search_port = (os.environ.get('AILOCAL_SEARXNG_PORT')
                   or runtime.env_value('AILOCAL_SEARXNG_PORT') or '8080')
    env = dict(os.environ)
    env.update({'LOCAL_RESEARCH_SEARCH_URL': f'http://127.0.0.1:{search_port}',
                'LOCAL_RESEARCH_MODEL_URL': endpoint + '/v1',
                'LOCAL_RESEARCH_MODEL': 'ailocal-fast',
                'LOCAL_RESEARCH_API_KEY': runtime.env_value('LITELLM_MASTER_KEY'),
                'LOCAL_RESEARCH_STATE': str(policy.state_root() / 'local-research'),
                'LOCAL_RESEARCH_ENABLE_ANSWER': '1' if enable_answer else '0',
                'LOCAL_RESEARCH_INPUT_CHARS': str(chars),
                'LOCAL_RESEARCH_OUTPUT_TOKENS': str(min(800, cfg['max_output'])),
                # The user's allowance per Brave billing period, not the provider's.
                'LOCAL_RESEARCH_BRAVE_BUDGET': str(brave_budget()),
                'LOCAL_RESEARCH_BRAVE_KEY': runtime.env_value('BRAVE_API')})
    return env


def brave_budget() -> int:
    """AILOCAL_BRAVE_BUDGET from the user's .env.local; 0 disables Brave."""
    value = runtime.env_value('AILOCAL_BRAVE_BUDGET') or '1000'
    try:
        budget = int(value)
    except ValueError:
        raise RuntimeError('AILOCAL_BRAVE_BUDGET must be a whole number of requests') from None
    if not 0 <= budget <= 100000:
        raise RuntimeError('AILOCAL_BRAVE_BUDGET must be between 0 and 100000')
    return budget


def _exec_server(name: str, env: dict) -> int:
    """Hand off to a provisioned integration's own interpreter and server.py."""
    py = policy.state_root() / name / '.venv/bin/python'
    source = policy.data_root() / 'integrations' / name / 'server.py'
    if not py.is_file() or not source.is_file():
        print(f'{name} runtime missing; run: ailocal clients claude', file=sys.stderr)
        return 1
    try:
        os.execve(str(py), [str(py), str(source)], env)
    except (OSError, RuntimeError, policy.ProfileError):
        print(f'{name} launch refused; check the active profile, loopback proxy and provisioned runtime',
              file=sys.stderr)
        return 1
    return 0


def main(argv: list[str]) -> int:
    if argv and argv[0] == 'research' and all(a == '--enable-answer' for a in argv[1:]):
        try:
            return _exec_server('local-research', launch_environment('--enable-answer' in argv))
        except (RuntimeError, policy.ProfileError):
            print('research launch refused; check the active profile, loopback proxy and provisioned runtime',
                  file=sys.stderr)
            return 1
    if argv == ['agent-policy']:
        # Read by the agent server on every delegate, so an operator who lowers
        # a ceiling does not have to restart a long-lived MCP process.
        try:
            print(json.dumps({'ceilings': policy.delegate_ceilings()}))
        except policy.ProfileError as error:
            print(f'agent policy unavailable: {error}', file=sys.stderr)
            return 1
        return 0
    if argv == ['agent']:
        try:
            return _exec_server('local-agent', agent_environment())
        except (RuntimeError, policy.ProfileError):
            print('agent launch refused; check the active profile and loopback proxy', file=sys.stderr)
            return 1
    print('usage: ailocal mcp (research [--enable-answer] | agent | agent-policy)', file=sys.stderr)
    return 2
