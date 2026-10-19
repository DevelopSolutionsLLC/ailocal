"""Launch bundled research without importing its SDK into stdlib-only core."""
from __future__ import annotations

import ipaddress
import os
from pathlib import Path
import sys
from urllib.parse import urlsplit

from . import policy, runtime


def registration() -> dict:
    return {'type': 'stdio', 'command': 'ailocal',
            'args': ['mcp', 'research'], 'env': {}}


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
    env = dict(os.environ)
    env.update({'LOCAL_RESEARCH_SEARCH_URL': f"http://127.0.0.1:{os.environ.get('AILOCAL_SEARXNG_PORT', '8080')}",
                'LOCAL_RESEARCH_MODEL_URL': endpoint + '/v1',
                'LOCAL_RESEARCH_MODEL': 'ailocal-fast',
                'LOCAL_RESEARCH_API_KEY': runtime.env_value('LITELLM_MASTER_KEY'),
                'LOCAL_RESEARCH_STATE': str(policy.state_root() / 'local-research'),
                'LOCAL_RESEARCH_ENABLE_ANSWER': '1' if enable_answer else '0',
                'LOCAL_RESEARCH_INPUT_CHARS': str(chars),
                'LOCAL_RESEARCH_OUTPUT_TOKENS': str(min(800, cfg['max_output']))})
    return env


def main(argv: list[str]) -> int:
    if not argv or argv[0] != 'research' or any(a != '--enable-answer' for a in argv[1:]):
        print('usage: ailocal mcp research [--enable-answer]', file=sys.stderr)
        return 2
    py = policy.state_root() / 'local-research/.venv/bin/python'
    source = policy.data_root() / 'integrations/local-research/server.py'
    if not py.is_file() or not source.is_file():
        print('research runtime missing; run: ailocal clients claude', file=sys.stderr)
        return 1
    try:
        env = launch_environment('--enable-answer' in argv)
        os.execve(str(py), [str(py), str(source)], env)
    except (OSError, RuntimeError, policy.ProfileError):
        print('research launch refused; check the active profile, loopback proxy and provisioned runtime', file=sys.stderr)
        return 1
    return 0
