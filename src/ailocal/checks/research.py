"""Research integration diagnostics do not issue searches or inference."""
import hashlib
import json
from ailocal import policy
from ailocal.mcp import registration
from ailocal.checks import failed, passed, warning


def check_research():
    home = policy.deployed_client_root() / 'claude'
    cj = home / '.claude.json'
    if not cj.is_file():
        return warning('research', 'research MCP not provisioned for claude-local',
                       'ailocal clients claude')
    try:
        data = json.loads(cj.read_text())
        entry = data.get('mcpServers', {}).get('research')
        if entry != registration():
            return warning('research', 'research MCP registration missing or owned by another provider',
                           'inspect the research entry, then run: ailocal clients claude')
        state = policy.state_root() / 'local-research/.venv'
        stamp = state / '.requirements.sha'
        req = policy.data_root() / 'integrations/local-research/requirements.txt'
        if not (state / 'bin/python').is_file() or not stamp.is_file() or stamp.read_text().strip() != hashlib.sha256(req.read_bytes()).hexdigest():
            return failed('research', 'research MCP runtime missing or outdated', 'ailocal clients claude')
    except (OSError, ValueError, AttributeError):
        return failed('research', 'research MCP configuration cannot be read', 'repair claude-local .claude.json, then: ailocal clients claude')
    return passed('research', 'research MCP provisioned; search enabled, local answer opt-in (no query issued)')
