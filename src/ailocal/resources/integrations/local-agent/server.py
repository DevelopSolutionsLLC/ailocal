#!/usr/bin/env python3
"""Stdio MCP boundary that delegates a task to a local ailocal agent.

A hosted Claude or Codex session calls `delegate` to hand a prompt to
claude-local or codex-local running on a local model (ailocal-cyber by default),
the way it would spawn a subagent. The local CLIs are driven headless; the
loopback proxy base URL, key and the isolated config root are injected by the
`ailocal mcp agent` launcher, so a hosted config is never read or written here.

Four modes, chosen per call, weakest first. The default, text, cannot edit
files: it runs in an ailocal-owned empty directory, local Claude with no tools
and no MCP servers, local Codex in its read-only sandbox (it may read files and
run commands, and loads its root's MCP servers, because Codex has no tool-free
mode). Research (Claude only) reads inside its working directory and searches
the web through ailocal's research server, and nothing else. Edit is confined
to an existing working directory: Claude by an edit rule for cwd plus its OS
Bash sandbox, Codex by its workspace permission profile. Full lifts each CLI's own
sandbox and permission checks. Everything above text is bounded by the
operator's per-worker `delegate_max_mode` in clients.toml, read on every call.
Below full, a project's own Claude settings, hooks and .mcp.json are not
loaded; ailocal's worker-root hooks run in every mode. The argv, settings and
the `capabilities` block every result reports are built from one table per
CLI, so the report cannot drift from what ran. A delegated agent cannot
delegate again: both share one local inference service, so recursion only
queues work behind itself. No credential or raw CLI diagnostic crosses the MCP
boundary -- a failure returns a constant, named error and any captured text is
scoped to the delegated run.
"""
import contextlib
import base64
import fcntl
import hashlib
import json
import logging
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

log = logging.getLogger('local-agent')

AGENTS = ('claude', 'codex')
DEFAULT_MODEL = os.environ.get('LOCAL_AGENT_DEFAULT_MODEL', 'ailocal-cyber')
#: Enabled in the policy view but not an agent: fill-in-the-middle completion.
NOT_DELEGABLE = ('ailocal-completion',)
MAX_OUTPUT_CHARS = 60000
#: stdout held while a child runs. Claude's single JSON result is read whole;
#: Codex's event stream is consumed a line at a time and never accumulated.
STDOUT_LIMIT = 8 * 1024 * 1024
#: After the CLI exits, how long a descendant may hold its stdout open before
#: the group is killed and the reply read as it stands.
DRAIN_GRACE = 2
#: TERM, then KILL, each bounded, so cleanup cannot outlive the call by more.
KILL_GRACE = 5
#: After a normal exit the leader is already gone, so waiting on it gives the
#: rest of the group no time at all; this is how long they get after TERM.
GROUP_GRACE = 1
GUIDE_TIMEOUT = 10
IMAGE_LIMIT = 5 * 1024 * 1024
IMAGE_COUNT = 4
IMAGE_PIXELS = 16_000_000
IMAGE_SIDE = 8192
BINDING_VERSION = 1
#: Inherited variables that would reroute or re-authenticate a child away from
#: the loopback proxy. Claude Code ranks cloud-provider selection and
#: ANTHROPIC_AUTH_TOKEN above ANTHROPIC_API_KEY (code.claude.com/docs/en/authentication).
#: Every CLAUDE_* name, because a hosted session also exports its own messaging
#: token and socket, session id, entrypoint and project dir; the child's own
#: CLAUDE_* values are applied after this filter. SSH_AUTH_SOCK would hand the
#: child the parent's SSH agent.
_HOSTED_PREFIXES = ('ANTHROPIC_', 'CLAUDE_', 'OPENAI_', 'CODEX_')
_HOSTED_NAMES = ('AWS_BEARER_TOKEN_BEDROCK', 'CLAUDECODE', 'SSH_AUTH_SOCK')
DEPTH_VAR = 'LOCAL_AGENT_DEPTH'
_UUID = re.compile(r'[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-'
                   r'[0-9a-fA-F]{4}-[0-9a-fA-F]{12}')


class DelegateError(Exception):
    """Fixed error codes and public session IDs; never raw CLI diagnostics."""


class CompletedWithoutReply(DelegateError):
    """Native completion permits an explicit resume, never a success claim."""

    def __init__(self, agent: str, session_id: str):
        if agent not in AGENTS or not isinstance(session_id, str) or not _UUID.fullmatch(session_id):
            raise DelegateError('DELEGATE_FAILED: invalid completed session')
        self.session_id = str(uuid.UUID(session_id))
        super().__init__(f'DELEGATE_EMPTY: local {agent.capitalize()} run returned no answer')


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


def _child_env(agent: str, mode: str = 'text') -> dict[str, str]:
    """Point the local CLI at the loopback proxy and its isolated config root.

    The launcher injects LOCAL_AGENT_* so this boundary never reads a hosted
    config. For Claude the base URL and key are the gateway contract; for Codex
    the same reach the CLI through its own env and the isolated CODEX_HOME.
    Routing and session names are dropped in every mode -- they would point the
    child at the hosted provider or the parent's session. Full mode keeps the
    parent's SSH agent, which the restricted modes withhold.
    """
    # LOCAL_AGENT_* is this boundary's own launch contract, and LOCAL_AGENT_KEY
    # is the proxy key under a name no CLI scrubs; the child gets only the
    # credential its CLI authenticates with, below.
    withheld = _HOSTED_NAMES if mode != 'full' else tuple(n for n in _HOSTED_NAMES if n != 'SSH_AUTH_SOCK')
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(_HOSTED_PREFIXES + ('LOCAL_AGENT_',)) and k not in withheld}
    env[DEPTH_VAR] = '1'
    base, key = os.environ.get('LOCAL_AGENT_BASE_URL'), os.environ.get('LOCAL_AGENT_KEY')
    if not base or not key:
        raise DelegateError('LOCAL_AGENT_NOT_PROVISIONED: run ailocal clients')
    if agent == 'claude':
        home = os.environ.get('LOCAL_AGENT_CLAUDE_HOME')
        if home:
            env['CLAUDE_CONFIG_DIR'] = home
        # The same slots, timeout and context switches claude-local applies,
        # projected by the launcher from client policy.
        try:
            launch = json.loads(os.environ.get('LOCAL_AGENT_CLAUDE_ENV') or '{}')
        except ValueError:
            launch = {}
        env.update({k: str(v) for k, v in launch.items() if isinstance(k, str)})
        env['ANTHROPIC_BASE_URL'] = base
        env['ANTHROPIC_API_KEY'] = key
        # No gateway discovery: it only fills the /model picker. Behind a custom
        # ANTHROPIC_BASE_URL, --model passes any non-empty ID through unchanged.
    else:
        home = os.environ.get('LOCAL_AGENT_CODEX_HOME')
        if home:
            env['CODEX_HOME'] = home
        env['OPENAI_BASE_URL'] = base.rstrip('/') + '/v1'
        env['OPENAI_API_KEY'] = key
    return env


#: Weakest first; a call may not exceed the operator's ceiling for its agent.
MODES = ('text', 'research', 'edit', 'full')

#: One row per mode: everything argv, settings and the reported capabilities
#: are built from. `tools` is the built-in tool list (None: the CLI default);
#: `mcp` is "none", "research" (only ailocal's research server, its search
#: tool) or "worker" (the worker root's user-scope servers); `sources` is the
#: settings files Claude reads (None: all, including the project's).
#:
#: Edit confinement: --dangerously-skip-permissions lifts the working directory
#: boundary entirely, and --add-dir only widens it: a delegated model that
#: misread its working directory once searched $HOME and rewrote an unrelated
#: file. An allow rule approves file edits only under cwd and a headless run
#: denies every other prompt; the sandbox confines Bash writes to cwd and
#: $TMPDIR and refuses to start rather than run unsandboxed. [REAL] Claude Code
#: 2.1.308 forces the permission mode to default whenever the credential scrub
#: is on, ignoring --permission-mode acceptEdits (and warning "Permission mode
#: forced to default"), so the edit grant is the rule, not the mode.
#: Project settings: a headless run loads a repository's .mcp.json servers
#: without asking, and its settings can add hooks or widen the sandbox, all of
#: which run outside the confinement above; `--setting-sources user` reads
#: neither (code.claude.com/docs/en/mcp, /en/permissions). It also skips the
#: project's CLAUDE.md, so the caller puts what the task needs in the prompt.
#: Auto memory is off in every mode because its storage path in the system
#: prompt is what one model mistook for its working directory.
#: https://code.claude.com/docs/en/permission-modes, /en/sandboxing
CLAUDE_PLAN = {
    'text': {'tools': '', 'mcp': 'none', 'sources': 'user', 'permission_mode': None,
             'sandbox': None, 'reads': 'none', 'writes': 'none', 'workdir': 'empty'},
    'research': {'tools': 'Read,Grep,Glob', 'mcp': 'research', 'sources': 'user',
                 'permission_mode': None, 'sandbox': None, 'reads': 'working directory',
                 'writes': 'none', 'workdir': 'cwd or empty'},
    'edit': {'tools': None, 'mcp': 'worker', 'sources': 'user', 'permission_mode': None,
             'allow_edits': True, 'sandbox': 'confined',
             'reads': 'file tools: working directory; sandboxed commands: most of the machine',
             'writes': 'working directory', 'workdir': 'cwd'},
    'full': {'tools': None, 'mcp': 'worker and project', 'sources': None,
             'permission_mode': 'bypassPermissions', 'sandbox': 'off', 'reads': 'unrestricted',
             'writes': 'unrestricted', 'workdir': 'cwd'},
}
#: Research may call only this tool; a headless run would deny it otherwise.
RESEARCH_TOOL = 'mcp__research__search'
#: Edit and full keep the worker root's MCP servers; the delegate tool among
#: them is removed so a child cannot delegate again. Research loads no agent
#: server, and neither it nor text has the Agent tool.
CLAUDE_DENIED_TOOLS = {'research': ['Agent', 'mcp__agent__delegate'],
                       'edit': ['mcp__agent__delegate'], 'full': ['mcp__agent__delegate']}


def claude_settings(mode: str, guide: dict) -> dict:
    """The one --settings object for a Claude child, for the SELECTED alias.

    The worker root's settings.json carries the launch-default role's limits in
    its `env` block, and a settings `env` value replaces the inherited process
    value, so exporting the selected alias's limits does nothing. Command-line
    settings outrank user settings (code.claude.com/docs/en/env-vars#precedence,
    /en/settings#settings-precedence).
    """
    plan = CLAUDE_PLAN[mode]
    settings: dict[str, Any] = {'autoMemoryEnabled': False}
    if plan['sandbox'] == 'confined':
        settings['sandbox'] = {'enabled': True, 'failIfUnavailable': True,
                               'allowUnsandboxedCommands': False}
    elif plan['sandbox'] == 'off':
        settings['sandbox'] = {'enabled': False}
    settings['env'] = {'CLAUDE_CODE_MAX_OUTPUT_TOKENS': str(guide['max_output']),
                       'CLAUDE_CODE_MAX_CONTEXT_TOKENS': str(guide['context_input'])}
    if mode == 'full':
        # An unsandboxed child can read any file it likes, so a read rule here
        # would only look like protection; scrubbing would break the tokens a
        # full-mode task is granted to use.
        return settings
    # The scrub keeps the proxy key out of Bash, hooks and stdio MCP servers;
    # Claude keeps it for its own requests (/en/env-vars).
    settings['env']['CLAUDE_CODE_SUBPROCESS_ENV_SCRUB'] = '1'
    # [REAL] 2026-10-28: a tools=true child's Bash read the key file, because the
    # sandbox allows reads of most of the machine. credentials.files denies the
    # path to sandboxed commands; Read runs outside the sandbox and needs its own
    # rule, where `//` anchors an absolute path. Deny entries only narrow, from
    # any scope. code.claude.com/docs/en/sandboxing#protect-credentials,
    # /en/permissions
    denied = _denied_paths()
    settings['permissions'] = {'deny': [f'Read(/{p})' for p in denied]}
    if plan['reads'] == 'working directory':
        # Read, Grep and Glob refuse paths outside the working directories,
        # following symlinks (/en/settings-reference#permissions-blockreadsoutsideworkingdirectories).
        settings['permissions']['blockReadsOutsideWorkingDirectories'] = True
    if plan['sandbox'] == 'confined':
        settings['sandbox']['credentials'] = {'files': [{'path': p, 'mode': 'deny'} for p in denied]}
    return settings


def _denied_paths() -> list[str]:
    """Key-bearing files the launcher names; never restated here."""
    try:
        paths = json.loads(os.environ.get('LOCAL_AGENT_DENY_READ') or '[]')
    except ValueError:
        paths = []
    return [p for p in paths if isinstance(p, str) and os.path.isabs(p)] if isinstance(paths, list) else []


def research_mcp_config() -> str:
    """ailocal's own research registration, projected by the launcher from
    mcp.registration(), so research mode starts the same server claude-local
    does -- search only, its local synthesis tool left disabled."""
    try:
        entry = json.loads(os.environ.get('LOCAL_AGENT_RESEARCH_MCP') or 'null')
    except ValueError:
        entry = None
    if not isinstance(entry, dict) or entry.get('args') != ['mcp', 'research']:
        raise DelegateError('RESEARCH_UNAVAILABLE: run ailocal clients to provision the research server')
    return json.dumps({'mcpServers': {'research': entry}})


def _claude_argv(prompt, model, mode, resume, session, settings, cwd=None, images=False) -> list[str]:
    plan = CLAUDE_PLAN[mode]
    # Nobody can answer a prompt in a delegated run. `--permission-prompts none`
    # also tells the model so and not to retry the denied call; without it a
    # local model re-issued one denied Read 228 times until the timeout [REAL, #39].
    # Requires Claude Code 2.1.259 (/en/headless#turn-off-permission-prompts-in-unattended-runs).
    argv = ['claude', '-p', prompt, '--model', model, '--output-format', 'json',
            '--settings', json.dumps(settings), '--permission-prompts', 'none']
    if images:
        del argv[2]
        argv[argv.index('--output-format') + 1] = 'stream-json'
        argv += ['--input-format', 'stream-json', '--verbose']
    # A fresh run is given its identity rather than having it parsed back out;
    # a resumed run keeps the id it was resumed by.
    argv += ['--resume', resume] if resume else ['--session-id', session]
    if plan['sources']:
        argv += ['--setting-sources', plan['sources']]
    if plan['tools'] is not None:
        # Headless Claude would still run read-only tools such as Read without
        # a prompt; `--tools` names the whole built-in set ("" for none).
        argv += ['--tools', plan['tools']]
    if plan['mcp'] == 'none':
        # --strict-mcp-config with no --mcp-config loads no MCP server.
        argv += ['--strict-mcp-config']
    elif plan['mcp'] == 'research':
        argv += ['--strict-mcp-config', '--mcp-config', research_mcp_config(),
                 '--allowedTools', RESEARCH_TOOL]
    if plan['permission_mode']:
        argv += ['--permission-mode', plan['permission_mode']]
    if plan.get('allow_edits') and cwd:
        # Edit rules cover every file-editing tool; `//` anchors an absolute
        # path. An allow rule must match both the requested path and the file
        # it resolves to, so a symlinked cwd (/tmp on macOS) is named both ways
        # (/en/permissions#symlinks).
        roots = dict.fromkeys((os.path.abspath(cwd), os.path.realpath(cwd)))
        argv += ['--allowedTools', *(f'Edit(/{root}/**)' for root in roots)]
    if mode in CLAUDE_DENIED_TOOLS:
        argv += ['--disallowedTools', ','.join(CLAUDE_DENIED_TOOLS[mode])]
    return argv


#: Codex: research is not offered -- its read-only sandbox still executes
#: whatever code the model writes, so it cannot honour a read-only research
#: contract. Every other mode is a permission profile, not `sandbox_mode`.
CODEX_PLAN = {
    'text': {'profile': ':read-only', 'reads': 'most of the machine', 'writes': 'none',
             'workdir': 'empty'},
    'edit': {'profile': ':workspace', 'reads': 'most of the machine',
             'writes': 'working directory and temp directories', 'workdir': 'cwd'},
    'full': {'profile': ':danger-full-access', 'reads': 'unrestricted', 'writes': 'unrestricted',
             'workdir': 'cwd'},
}
#: The per-run profile name; defined on the command line, never in a config file.
CODEX_PROFILE = 'ailocal-delegate'


def codex_permissions(mode: str) -> list[str]:
    """-c overrides selecting this mode's permission profile.

    [REAL] 2026-10-28, Codex 0.165.0: `--sandbox read-only` let a delegated
    shell read the key file; a profile extending :read-only with a `deny`
    entry blocked it, both under the worker config (which sets sandbox_mode for
    interactive codex-local) and without it. --sandbox or a sandbox_mode
    override would select the older settings and drop the profile, so neither
    is passed. learn.chatgpt.com/docs/permissions
    """
    base = CODEX_PLAN[mode]['profile']
    if base == ':danger-full-access':
        return ['-c', f'default_permissions="{base}"']
    profile = {'extends': json.dumps(base)}
    denied = _denied_paths()
    if denied:
        profile['filesystem'] = '{' + ','.join(f'{json.dumps(p)}="deny"' for p in denied) + '}'
    table = '{' + ','.join(f'{k}={v}' for k, v in profile.items()) + '}'
    return ['-c', f'default_permissions="{CODEX_PROFILE}"', '-c', f'permissions.{CODEX_PROFILE}={table}']


def _codex_argv(prompt, model, mode, cwd, resume, out_file, images=()) -> list[str]:
    common = ['--model', model, '--json', '--skip-git-repo-check', '-o', out_file,
              '-c', 'mcp_servers.agent.disabled_tools=["delegate"]',
              # exec never prompts; stated so a profile cannot make a run wait.
              '-c', 'approval_policy="never"', *codex_permissions(mode)]
    for image in images:
        common += ['--image', image['path']]
    if mode != 'full':
        # Codex passes *KEY*/*SECRET*/*TOKEN* variables to the commands it
        # spawns unless default excludes are applied, and the child inherits
        # the hosted parent's environment. [REAL] 2026-10-28, 0.165.0: the
        # provider key was absent from the shell with or without this. Full
        # mode keeps them: a granted task may need its tokens.
        # learn.chatgpt.com/docs/config-file/config-advanced#shell-environment-policy
        common += ['-c', 'shell_environment_policy.ignore_default_excludes=false']
    if resume:
        # [REAL] Codex 0.165.0: `exec resume` has no -C; cwd is the process cwd,
        # which is the same directory the session started in.
        return ['codex', 'exec', 'resume', *common, '--', resume, prompt]
    return ['codex', 'exec', *common, '-C', cwd, '--', prompt]


def capabilities(agent: str, mode: str) -> dict[str, Any]:
    """What the child was given, read from the same rows that built its argv."""
    full = mode == 'full'
    env = ('parent environment minus hosted routing and session variables' if full else
           'hosted routing, session variables and SSH agent removed; '
           + ('credentials scrubbed from subprocesses' if agent == 'claude'
              else 'KEY/SECRET/TOKEN variables withheld from commands'))
    denied = [] if full else _denied_paths()
    if agent == 'claude':
        plan = CLAUDE_PLAN[mode]
        tools = {'': [], None: 'all'}.get(plan['tools'], plan['tools'].split(',') if plan['tools'] else [])
        servers = {'none': [], 'research': ['research (search only)'],
                   'worker': 'worker root user scope (delegate removed)',
                   'worker and project': 'worker root and project (delegate removed)'}[plan['mcp']]
        hooks = 'worker root' if plan['sources'] == 'user' else 'worker root and project'
        sandbox = {None: 'none (no command tools)', 'confined': 'Bash in the OS sandbox, writes confined',
                   'off': 'off'}[plan['sandbox']]
        permission = plan['permission_mode'] or 'default (headless denies prompts)'
        if plan.get('allow_edits'):
            permission += '; file edits allowed under the working directory'
        elif plan['mcp'] == 'research':
            permission += '; research search allowed'
        return {'built_in_tools': tools, 'mcp_servers': servers, 'hooks': hooks,
                'settings_sources': plan['sources'] or 'user, project, local',
                'permission_mode': permission,
                'sandbox': sandbox, 'reads': plan['reads'], 'writes': plan['writes'],
                'denied_reads': denied, 'working_directory': plan['workdir'], 'env_policy': env}
    plan = CODEX_PLAN[mode]
    return {'built_in_tools': 'all (Codex has no tool-free mode)',
            'mcp_servers': 'worker root (delegate disabled)', 'hooks': 'worker root',
            'sandbox': f'permission profile extending {plan["profile"]}' if not full else 'off',
            'reads': plan['reads'], 'writes': plan['writes'], 'denied_reads': denied,
            'working_directory': plan['workdir'], 'env_policy': env}


def scoped_prompt(prompt: str, cwd: str, mode: str = 'edit') -> str:
    """[REAL] 2026-10-28: a 9B child resolved files against its config root or
    memory path instead of the process cwd (0/3 edits); naming it gave 3/3.
    Edit and research are confined, so they say so."""
    bound = {'edit': '; you cannot write outside it', 'research': '; you can read only inside it'}.get(mode, '')
    context = ('\nThe client already loaded its instruction files. Use the supplied instructions '
               'and task context; do not reread the client configuration directory to discover '
               'your task. Read the task files in this working directory instead.'
               if mode in ('edit', 'research') else '')
    return f'Working directory: {cwd}\nResolve every relative path against it{bound}.{context}\n\n{prompt}'


#: Research results are search snippets, not pages the model read.
RESEARCH_CONTRACT = ('Use the search tool for anything outside the working directory. Attribute '
                     'each claim to the result it came from, as a search snippet; do not say you '
                     'read or opened a page.')


def research_prompt(prompt: str, cwd: str | None) -> str:
    body = f'{RESEARCH_CONTRACT}\n\n{prompt}'
    return scoped_prompt(body, cwd, 'research') if cwd else body


def empty_workdir() -> str:
    """The ailocal-owned directory text runs (and research runs without a cwd)
    start in, so a hosted session's repository is neither the child's project
    nor its default read target. One fixed path, under this server's state
    root: a resumed session starts in the directory it began in (Codex filters
    resume by cwd, Claude stores sessions per directory), and the worker root
    gains one project entry rather than one per run. No mode that runs there
    can write, so anything found in it is cleared."""
    state = os.environ.get('LOCAL_AGENT_STATE')
    root = Path(state) / 'empty' if state else Path(tempfile.gettempdir()) / 'local-agent-empty'
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    for entry in root.iterdir():
        if entry.is_dir() and not entry.is_symlink():
            shutil.rmtree(entry, ignore_errors=True)
        else:
            with contextlib.suppress(OSError):
                entry.unlink()
    return str(root)


#: Modes each worker CLI offers.
AGENT_MODES = {'claude': tuple(CLAUDE_PLAN), 'codex': tuple(CODEX_PLAN)}


def resolve_mode(mode: str | None, tools: bool | None) -> str:
    """The legacy `tools` flag and the explicit `mode`, reconciled before launch.

    Omitted both, or tools=false alone: text (Codex: its read-only sandbox).
    tools=true alone: edit. An explicit mode wins when tools agrees or is
    omitted; tools=false agrees only with text and tools=true with edit or
    full, so research takes no tools flag. A contradiction is refused rather
    than resolved either way.
    """
    if mode is None:
        return 'edit' if tools else 'text'
    if mode not in MODES:
        raise DelegateError(f'UNKNOWN_MODE: choose one of {", ".join(MODES)}')
    if tools is not None and mode not in (('edit', 'full') if tools else ('text',)):
        raise DelegateError('CONFLICTING_MODE: tools contradicts mode; pass mode alone')
    return mode


def supported_mode(mode: str, agent: str) -> None:
    if mode not in AGENT_MODES[agent]:
        raise DelegateError(f'UNSUPPORTED_MODE: {agent} offers {", ".join(AGENT_MODES[agent])}; '
                            'Codex has no read-only research mode because its sandbox still runs code')


def permit_mode(mode: str, agent: str, ceilings: dict) -> None:
    ceiling = ceilings.get(agent, 'edit')
    if ceiling not in MODES or MODES.index(mode) > MODES.index(ceiling):
        raise DelegateError(f'MODE_NOT_PERMITTED: {agent} delegation is limited to {ceiling} '
                            f'by {agent}.delegate_max_mode in the operator\'s clients.toml')


#: Process groups of children still running, for the signal handler.
_ACTIVE: set[int] = set()
#: Claude Code stops a stdio server with SIGINT, SIGTERM 100 ms later and
#: SIGKILL 400 ms after that (code.claude.com/docs/en/mcp); kill inside it.
SHUTDOWN_GRACE = 0.3


def _signal_group(pgid: int, sig: int) -> None:
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    return True


def _shutdown(signum, frame) -> None:
    """The host is stopping this server. Each child runs in its own session, so
    the host's signal never reaches it: kill every live group, then exit. A
    SIGKILLed server cannot run this, and its children are not claimed."""
    for pgid in list(_ACTIVE):
        _signal_group(pgid, signal.SIGTERM)
    deadline = time.monotonic() + SHUTDOWN_GRACE
    while time.monotonic() < deadline and any(_group_alive(p) for p in _ACTIVE):
        time.sleep(0.02)
    for pgid in list(_ACTIVE):
        _signal_group(pgid, signal.SIGKILL)
    os._exit(128 + signum)


async def _reap(proc) -> None:
    """TERM then KILL the whole group, each wait bounded. Runs on every exit, so
    a tool subprocess the CLI left behind does not outlive the call either."""
    import anyio
    _signal_group(proc.pid, signal.SIGTERM)
    with anyio.move_on_after(KILL_GRACE):
        await proc.wait()
    # A normal exit reaches here with the leader already gone, so the wait above
    # returned at once; descendants still in the group get their own short grace.
    with anyio.move_on_after(GROUP_GRACE):
        while _group_alive(proc.pid):
            await anyio.sleep(0.05)
    _signal_group(proc.pid, signal.SIGKILL)
    with anyio.move_on_after(KILL_GRACE):
        await proc.aclose()
    _ACTIVE.discard(proc.pid)


async def _run(argv, env, cwd, on_line=None, input_data=None) -> tuple[int, bytes]:
    """Run one owned CLI in its own process group; return (exit code, stdout).

    The caller bounds the wall clock with anyio.fail_after. Timeout, MCP
    cancellation and normal exit all leave through the same shielded cleanup.
    A descendant that starts its own session escapes the group and is not
    claimed here. stdout is bounded while it is read: with on_line each
    complete line is handed over and dropped, and a single line longer than
    STDOUT_LIMIT is skipped -- the events read from it are small, and an item
    that large is a tool's output, not an answer. Without on_line at most
    STDOUT_LIMIT bytes are held, and more fails the run. stderr is drained and
    discarded -- it can quote the child's environment.
    """
    import anyio
    try:
        # stdin is this server's JSON-RPC pipe. `codex exec` and `claude -p`
        # both read a non-TTY stdin as extra prompt input, so inheriting it
        # would consume protocol messages and block until the host closes it.
        proc = await anyio.open_process(argv, stdin=subprocess.PIPE if input_data is not None else subprocess.DEVNULL,
                                        stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, cwd=cwd, env=env,
                                        start_new_session=True)
    except FileNotFoundError:
        raise DelegateError(f'AGENT_CLI_MISSING: {argv[0]} is not installed') from None
    _ACTIVE.add(proc.pid)
    out, overflow, drained = bytearray(), False, anyio.Event()

    async def feed():
        try:
            await proc.stdin.send(input_data)
        except (anyio.BrokenResourceError, anyio.ClosedResourceError):
            pass  # The child's exit/result decides failure, not a raw pipe diagnostic.
        finally:
            await proc.stdin.aclose()

    async def pump():
        nonlocal overflow
        skipping = False
        try:
            async for chunk in proc.stdout:
                if on_line is None:
                    out.extend(chunk)
                    if len(out) > STDOUT_LIMIT:
                        overflow = True
                        _signal_group(proc.pid, signal.SIGKILL)
                        return
                    continue
                if skipping:
                    cut = chunk.find(b'\n')
                    if cut < 0:
                        continue
                    chunk, skipping = chunk[cut + 1:], False
                out.extend(chunk)
                *lines, rest = out.split(b'\n')
                for line in lines:
                    on_line(line)
                out[:] = rest
                if len(out) > STDOUT_LIMIT:
                    out.clear()
                    skipping = True
        finally:
            drained.set()

    async def discard():
        async for _ in proc.stderr:
            pass

    try:
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(pump)
            tasks.start_soon(discard)
            if input_data is not None:
                tasks.start_soon(feed)
            code = await proc.wait()
            with anyio.move_on_after(DRAIN_GRACE):
                await drained.wait()
            tasks.cancel_scope.cancel()
    finally:
        with anyio.CancelScope(shield=True):
            await _reap(proc)
    if overflow:
        raise DelegateError('DELEGATE_OUTPUT_LIMIT: the local run produced more output than this tool returns')
    if on_line is not None and out:
        on_line(bytes(out))
    return code, bytes(out)


async def model_guides() -> dict:
    """The public policy view, read on every call: a long-lived MCP must not
    retain the previous hardware profile or operator's old advice."""
    import anyio
    try:
        with anyio.fail_after(GUIDE_TIMEOUT):
            code, out = await _run(['ailocal', 'profile', 'guide', '--json'], dict(os.environ), None)
        guides = json.loads(out)
        if code or not isinstance(guides.get('models'), dict):
            raise ValueError
    except (DelegateError, TimeoutError, OSError, ValueError, AttributeError):
        raise DelegateError('MODEL_GUIDANCE_UNAVAILABLE: check ailocal profile policy') from None
    return guides


async def mode_ceilings() -> dict:
    """The operator's per-worker ceilings, read through policy on every call so
    lowering one takes effect without restarting this server. Fails closed."""
    import anyio
    try:
        with anyio.fail_after(GUIDE_TIMEOUT):
            code, out = await _run(['ailocal', 'mcp', 'agent-policy'], dict(os.environ), None)
        ceilings = json.loads(out)['ceilings']
        if code or not isinstance(ceilings, dict):
            raise ValueError
    except (DelegateError, TimeoutError, OSError, ValueError, KeyError, TypeError):
        raise DelegateError('AGENT_POLICY_UNAVAILABLE: check delegate_max_mode in clients.toml') from None
    return ceilings


def selected_guide(guides: dict, model: str) -> dict:
    """Refuse before launch what the active policy does not serve as an agent."""
    if model in NOT_DELEGABLE:
        raise DelegateError(f'MODEL_NOT_DELEGABLE: {model} serves code completion, not agents')
    guide = guides['models'].get(model)
    if not isinstance(guide, dict):
        raise DelegateError('UNKNOWN_MODEL: use an enabled ailocal alias listed by model_guide')
    return guide


def private_directory(name: str) -> Path:
    state = os.environ.get('LOCAL_AGENT_STATE')
    if not state or not Path(state).is_absolute():
        raise DelegateError('LOCAL_AGENT_NOT_PROVISIONED: run ailocal clients')
    root = Path(state) / name
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if root.is_symlink():
        raise DelegateError('LOCAL_AGENT_STATE_INVALID: restore the owned local-agent state directory')
    root.chmod(0o700)
    return root


def canonical_cwd(cwd: str | None) -> str | None:
    if cwd is None:
        return None
    try:
        path = Path(cwd)
        if not path.is_absolute() or not path.is_dir():
            raise ValueError
        return str(path.resolve(strict=True))
    except (OSError, ValueError, RuntimeError):
        raise DelegateError('INVALID_CWD: cwd must be an existing absolute directory') from None


def session_binding(session, agent, model, mode, scope, cwd, guides, guide) -> dict:
    identity = {k: guide.get(k) for k in ('model', 'source', 'context_input', 'max_output',
                                         'reasoning', 'sampling', 'input_modalities')}
    identity['tier'] = guides.get('tier')
    home = os.environ.get(f'LOCAL_AGENT_{agent.upper()}_HOME')
    return {'version': BINDING_VERSION, 'session_id': session, 'agent': agent, 'mode': mode,
            'model': model, 'scope': scope, 'cwd': cwd,
            'worker_home': str(Path(home).resolve()) if home else None,
            'policy': hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()}


def load_binding(session: str) -> dict:
    try:
        fd = os.open(private_directory('sessions') / f'{session}.json', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd) as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise ValueError
            text = handle.read(16385)
        if len(text) > 16384:
            raise ValueError
        binding = json.loads(text)
        if (not isinstance(binding, dict)
                or set(binding) != {'version', 'session_id', 'agent', 'mode', 'model', 'scope', 'cwd', 'worker_home', 'policy'}
                or binding.get('version') != BINDING_VERSION
                or binding.get('session_id') != session or binding.get('agent') not in AGENTS
                or binding.get('mode') not in MODES
                or not isinstance(binding.get('model'), str)
                or not isinstance(binding.get('cwd'), str) or not os.path.isabs(binding['cwd'])
                or not isinstance(binding.get('policy'), str)
                or not re.fullmatch('[a-f0-9]{64}', binding['policy'])
                or any(binding.get(k) is not None and (not isinstance(binding[k], str)
                       or not os.path.isabs(binding[k])) for k in ('scope', 'worker_home'))):
            raise ValueError
        return binding
    except FileNotFoundError:
        raise DelegateError('SESSION_NOT_MANAGED: start a new delegation; this session has no binding') from None
    except (OSError, ValueError, TypeError):
        raise DelegateError('SESSION_INVALID: session binding is unreadable or unsupported; start a new delegation') from None


def save_binding(binding: dict) -> None:
    root = private_directory('sessions')
    temporary = None
    try:
        with tempfile.NamedTemporaryFile('w', dir=root, prefix='.binding-', delete=False) as handle:
            temporary = handle.name
            json.dump(binding, handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, root / f'{binding["session_id"]}.json')
    except OSError:
        raise DelegateError('SESSION_BINDING_FAILED: could not record the local session binding') from None
    finally:
        if temporary:
            with contextlib.suppress(OSError):
                os.unlink(temporary)


def bound_arguments(binding, agent, model, mode, tools, cwd):
    """An ID-only resume inherits; an explicit change never changes its scope."""
    effective = (binding['agent'] if agent is None else agent,
                 binding['model'] if model is None else model,
                 binding['mode'] if mode is None and tools is None else resolve_mode(mode, tools),
                 binding['scope'] if cwd is None else canonical_cwd(cwd))
    if effective != (binding['agent'], binding['model'], binding['mode'], binding['scope']):
        raise DelegateError('SESSION_MISMATCH: keep the original agent, model, mode and cwd; start a new delegation to change them')
    return effective


@contextlib.contextmanager
def session_lock(session: str):
    """One run per native session at a time: two resumes of one transcript
    would interleave its writes. The file exists only to be locked."""
    root = private_directory('sessions')
    fd = os.open(root / f'{session}.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'a') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise DelegateError('SESSION_BUSY: that local session is already running') from None
        yield


def image_bytes(path: str, scope: str) -> bytes:
    """Read once through a no-symlink directory walk; the worker gets a copy."""
    try:
        if not Path(path).is_absolute():
            raise ValueError
        source = Path(path).resolve(strict=True)
        relative = source.relative_to(scope)
        if not relative.parts or any(p in ('.', '..') for p in relative.parts):
            raise ValueError
        with contextlib.ExitStack() as held:
            directory = os.open(scope, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            held.callback(os.close, directory)
            for part in relative.parts[:-1]:
                directory = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
                held.callback(os.close, directory)
            fd = os.open(relative.parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            handle = held.enter_context(os.fdopen(fd, 'rb'))
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise ValueError
            for denied in _denied_paths():
                with contextlib.suppress(FileNotFoundError):
                    key = os.stat(denied)
                    if (info.st_dev, info.st_ino) == (key.st_dev, key.st_ino):
                        raise ValueError
            if info.st_size > IMAGE_LIMIT:
                raise DelegateError('IMAGE_LIMIT: each image must be at most 5 MiB')
            data = handle.read(IMAGE_LIMIT + 1)
            if len(data) > IMAGE_LIMIT:
                raise DelegateError('IMAGE_LIMIT: each image must be at most 5 MiB')
            return data
    except (OSError, ValueError, RuntimeError):
        raise DelegateError('IMAGE_PATH_INVALID: use regular image files inside cwd; symlink escapes and credential files are denied') from None


@contextlib.asynccontextmanager
async def prepared_images(paths, scope, guide):
    """Bound explicit attachments, without granting the model file tools."""
    if not paths:
        yield []
        return
    if 'image' not in guide.get('input_modalities', ['text']):
        raise DelegateError('IMAGES_UNSUPPORTED: this backend has no qualified image input')
    if len(paths) > IMAGE_COUNT:
        raise DelegateError('IMAGE_LIMIT: supply at most four images')
    if scope is None:
        raise DelegateError('IMAGE_CWD_REQUIRED: supply cwd to scope image_paths, including in text mode')
    import anyio
    with tempfile.TemporaryDirectory(prefix='call-', dir=private_directory('attachments')) as tmp:
        images = []
        for index, path in enumerate(paths):
            data = image_bytes(path, scope)
            if data.startswith(b'\x89PNG\r\n\x1a\n'):
                kind, mime = 'png', 'image/png'
            elif data.startswith(b'\xff\xd8\xff'):
                kind, mime = 'jpeg', 'image/jpeg'
            else:
                raise DelegateError('IMAGE_INVALID: supply a valid PNG or JPEG')
            target = Path(tmp) / f'{index}.{kind}'
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'wb') as handle:
                handle.write(data)
            try:
                with anyio.fail_after(10):
                    code, out = await _run(['/usr/bin/sips', '-g', 'format', '-g', 'pixelWidth',
                                             '-g', 'pixelHeight', str(target)], {'PATH': '/usr/bin:/bin'}, tmp)
                fields = dict(re.findall(r'^\s*(format|pixelWidth|pixelHeight):\s*(\S+)', out.decode(), re.M))
                width, height = int(fields['pixelWidth']), int(fields['pixelHeight'])
                if code or fields['format'] != kind or width <= 0 or height <= 0:
                    raise ValueError
            except (DelegateError, TimeoutError, ValueError, KeyError, UnicodeError):
                raise DelegateError('IMAGE_INVALID: native image validation failed') from None
            if max(width, height) > IMAGE_SIDE or width * height > IMAGE_PIXELS:
                raise DelegateError('IMAGE_LIMIT: images must fit 8192 pixels per side and 16 million pixels total')
            # Metadata can survive a corrupt pixel stream. Force a native decode
            # only after dimensions are bounded; discard its output and deliver
            # the original validated bytes without conversion to the worker.
            try:
                with anyio.fail_after(10):
                    code, _ = await _run(['/usr/bin/sips', '-s', 'format', 'png', str(target),
                                           '--out', str(Path(tmp) / 'decoded.png')], {'PATH': '/usr/bin:/bin'}, tmp)
                if code:
                    raise ValueError
            except (DelegateError, TimeoutError, ValueError):
                raise DelegateError('IMAGE_INVALID: native image decoding failed') from None
            images.append({'path': str(target), 'mime_type': mime, 'data': data})
        yield images


def _clip(text: str) -> tuple[str, bool]:
    if len(text) <= MAX_OUTPUT_CHARS:
        return text, False
    return text[:MAX_OUTPUT_CHARS], True


async def delegate_claude(prompt, model, mode, cwd, resume, guide, images=()) -> dict[str, Any]:
    session = resume or str(uuid.uuid4())
    argv = _claude_argv(prompt, model, mode, resume, session, claude_settings(mode, guide), cwd, bool(images))
    if images:
        content = [{'type': 'text', 'text': prompt}] + [
            {'type': 'image', 'source': {'type': 'base64', 'media_type': image['mime_type'],
                                       'data': base64.b64encode(image['data']).decode('ascii')}} for image in images]
        message = {'type': 'user', 'message': {'role': 'user', 'content': content},
                   'parent_tool_use_id': None, 'session_id': session}
        terminal = []

        def on_event(line):
            try:
                event = json.loads(line)
                if isinstance(event, dict) and event.get('type') == 'result':
                    terminal[:] = [event]
            except ValueError:
                pass

        code, _ = await _run(argv, _child_env('claude', mode), cwd, on_event,
                             input_data=(json.dumps(message) + '\n').encode())
        out = json.dumps(terminal[-1] if terminal else {}).encode()
    else:
        code, out = await _run(argv, _child_env('claude', mode), cwd)
    try:
        payload = json.loads(out)
        text = payload.get('result', '')
        failed = bool(payload.get('is_error')) or payload.get('session_id') != session
        subtype = str(payload.get('subtype') or '')
    except (ValueError, AttributeError):
        text, failed, subtype = '', True, ''
    if failed or code != 0:
        # The subtype is a fixed CLI vocabulary (e.g. error_max_turns); the
        # result text may quote the child's environment, so it stays here.
        # [REAL] Claude Code 2.1.308 reports an API error as is_error with
        # subtype "success", which names nothing.
        reason = (subtype if subtype != 'success' and re.fullmatch(r'[a-z_]{1,40}', subtype)
                  else 'did not complete')
        raise DelegateError(f'DELEGATE_FAILED: local Claude run {reason}')
    if not isinstance(text, str) or not text.strip():
        if (payload.get('type') == 'result' and subtype == 'success'
                and payload.get('is_error') is False):
            raise CompletedWithoutReply('claude', session)
        raise DelegateError('DELEGATE_EMPTY: local Claude run returned no answer')
    clipped, truncated = _clip(text)
    return {'agent': 'claude', 'model': model, 'mode': mode, 'session_id': session,
            'output': clipped, 'truncated': truncated,
            'permission_denials': _permission_denials(payload),
            'capabilities': {**capabilities('claude', mode), 'working_directory': cwd}}


def _permission_denials(payload) -> list[dict[str, str]]:
    """The tool calls Claude Code refused during the run, from its result message.

    A headless run whose calls were denied still ends `subtype: success` with
    exit 0 (anthropics/claude-code#76867, #90578), and a local model can then
    reply that the task is done [REAL, #39]. `permission_denials` is the CLI's
    authoritative record (/en/agent-sdk/typescript#sdkpermissiondeniedmessage);
    reporting it lets the caller see a claim of success beside the refusals.
    It is not a verdict on the task. Targets are clipped, never interpreted.
    """
    out = []
    for entry in payload.get('permission_denials') or ():
        if not isinstance(entry, dict):
            continue
        tool_input = entry.get('tool_input') if isinstance(entry.get('tool_input'), dict) else {}
        target = next((tool_input[k] for k in ('file_path', 'path', 'command', 'pattern', 'url')
                       if isinstance(tool_input.get(k), str)), '')
        out.append({'tool': str(entry.get('tool_name') or '')[:64], 'target': target[:200]})
    return out


async def delegate_codex(prompt, model, mode, cwd, resume, guide, images=()) -> dict[str, Any]:
    # Codex limits come from its catalog entry for `model`; guide is not needed.
    events = {'thread': None, 'completed': False, 'failed': False}

    def on_event(line: bytes) -> None:
        # learn.chatgpt.com/docs/non-interactive-mode#make-output-machine-readable
        try:
            event = json.loads(line)
        except ValueError:
            return
        kind = event.get('type') if isinstance(event, dict) else None
        if kind == 'thread.started' and events['thread'] is None:
            thread = event.get('thread_id')
            if isinstance(thread, str) and _UUID.fullmatch(thread):
                events['thread'] = thread
        elif kind == 'turn.completed':
            events['completed'] = True
        elif kind == 'turn.failed':
            events['failed'] = True

    with tempfile.NamedTemporaryFile('r', suffix='.txt', delete=False) as handle:
        out_file = handle.name
    try:
        code, _ = await _run(_codex_argv(prompt, model, mode, cwd, resume, out_file, images),
                             _child_env('codex', mode), cwd, on_event)
        if (code != 0 or events['failed'] or not events['completed'] or not events['thread']
                or (resume and events['thread'].lower() != resume)):
            # A last message written before a failure is not a completed answer.
            raise DelegateError('DELEGATE_FAILED: local Codex run did not complete')
        try:
            with open(out_file, encoding='utf-8', errors='replace') as reply:
                text = reply.read(MAX_OUTPUT_CHARS + 1)
        except OSError:
            text = ''
    finally:
        try:
            os.unlink(out_file)
        except OSError:
            pass
    if not text.strip():
        raise CompletedWithoutReply('codex', events['thread'])
    clipped, truncated = _clip(text)
    return {'agent': 'codex', 'model': model, 'mode': mode, 'session_id': events['thread'] or resume,
            'output': clipped, 'truncated': truncated, 'capabilities': {**capabilities('codex', mode), 'working_directory': cwd}}


DELEGATE_DESCRIPTION = (
    'Delegate a task to a local ailocal agent (claude-local or codex-local, a full native '
    'CLI agent) running on a local model, and return its reply. model must be an enabled '
    'alias listed by model_guide. mode: "text" (the default) cannot edit files and runs in '
    'an empty directory -- Claude with no tools, Codex in a read-only sandbox that can still '
    'read files and run commands; "research" (Claude only) reads files inside cwd, if given, '
    'and searches the web, returning attributed search snippets, with no write or command '
    'tools; "edit" edits and runs commands sandboxed and confined to cwd; "full" runs '
    'without a sandbox or permission prompts, with the parent environment, and only where '
    'the operator allowed it (MODE_NOT_PERMITTED otherwise). edit and full need an existing '
    'absolute cwd. Below full, the project\'s own Claude settings, hooks and .mcp.json are '
    'not loaded, so put what the task needs in the prompt. tools=true is the older spelling '
    'of mode="edit". The result\'s capabilities field states what the child was given. A '
    'Claude result lists permission_denials, the tool calls Claude Code refused; a reply '
    'claiming success beside refused writes is not evidence the task was done. Pass '
    'the returned session_id back as resume to continue that local session. If a native turn '
    'completed without a final reply, DELEGATE_EMPTY remains an error but includes resume=UUID; '
    'use that bound ID for an explicit follow-up. No retry or task-success claim is automatic. Omitted options '
    'inherit its bound agent, model, mode and cwd; conflicting options or a changed profile '
    'are refused. Sessions created before binding support must start fresh. '
    'image_paths optionally attaches up to four local PNG/JPEG files (5 MiB each, 8192 pixels '
    'per side, 16 million pixels each) inside an explicitly supplied cwd, including text '
    'mode, whose worker still runs in an empty directory. No tools are enabled by images. '
    'Parent conversation images are not forwarded automatically; supply local file paths.')


def build_mcp():
    from mcp.server.mcpserver import MCPServer
    from mcp.server.mcpserver.exceptions import ToolError
    from mcp.types import ToolAnnotations

    app = MCPServer(name='local-agent', instructions=(
        'Before delegating to a local model, call model_guide for its ailocal alias. '
        'It returns the active memory tier, limits, prompting advice and measured weaknesses '
        'from the same profiles direct local clients use. Give the child a bounded task, '
        'relevant code, exact acceptance criteria and the actual failure from a test when repairing. '
        'Do not treat guidance or a small perfect score as a guarantee of correctness. '
        'This server delegates a task to a local ailocal agent and returns its '
        'text. Prefer it when a prompt should be answered by a local model such '
        'as ailocal-cyber rather than by you. Use the weakest mode that does the task: '
        'text by default, research when Claude must search the web or read files in cwd, '
        'edit with the working directory as cwd when it must edit files or run commands, '
        'full only when it needs access a sandbox blocks (docker, git over SSH, package '
        'installs). Put the needed code in the prompt: in text mode Claude cannot read files.'))

    @app.tool(name='model_guide', description=(
        'Read local model prompting guidance, limits and evaluation caveats before choosing '
        'or delegating to a model. Omit model to list all aliases in the active memory profile. '
        'No inference, model loading, network calls or edits.'),
        annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False,
                                    open_world_hint=False),
        meta={'anthropic/alwaysLoad': True})
    async def model_guide(model: str | None = None) -> dict[str, Any]:
        try:
            guides = await model_guides()
        except DelegateError as error:
            raise ToolError(str(error)) from None
        if model is not None:
            if model not in guides['models']:
                raise ToolError('MODEL_GUIDANCE_UNKNOWN_MODEL: use an active ailocal alias')
            guides['models'] = {model: guides['models'][model]}
        return guides

    # No destructive hint: annotations are per tool, and edit/full modes edit files.
    @app.tool(name='delegate', description=DELEGATE_DESCRIPTION,
              annotations=ToolAnnotations(open_world_hint=True))
    async def delegate(prompt: str, agent: str | None = None, model: str | None = None,
                       mode: str | None = None, tools: bool | None = None,
                       cwd: str | None = None, resume: str | None = None,
                       timeout: int = 600, image_paths: list[str] | None = None) -> dict[str, Any]:
        import anyio
        try:
            if agent is not None and agent not in AGENTS:
                raise DelegateError(f'UNKNOWN_AGENT: choose one of {", ".join(AGENTS)}')
            if os.environ.get(DEPTH_VAR):
                raise DelegateError('RECURSIVE_DELEGATION: already running as a local delegate')
            if os.environ.get('LOCAL_AGENT_CALLER') == 'local':
                raise DelegateError('LOCAL_DELEGATION_DISABLED: use this local session directly; model_guide remains available')
            # Codex would also resolve a thread NAME; only ids this tool returned.
            if resume is not None and not _UUID.fullmatch(resume):
                raise DelegateError('INVALID_SESSION: resume takes a session_id returned by delegate')
            resume = str(uuid.UUID(resume)) if resume else None
            timeout = max(10, min(int(timeout), 3600))
            with contextlib.ExitStack() as held:
                if resume:
                    held.enter_context(session_lock(resume))
                    binding = load_binding(resume)
                    agent, model, mode, cwd = bound_arguments(binding, agent, model, mode, tools, cwd)
                else:
                    binding = None
                    agent, model, mode = agent or 'claude', model or DEFAULT_MODEL, resolve_mode(mode, tools)
                    cwd = canonical_cwd(cwd)
                supported_mode(mode, agent)
                if mode in ('edit', 'full') and cwd is None:
                    raise DelegateError(f'INVALID_CWD: {mode} mode requires an existing absolute cwd')
                scope = canonical_cwd(cwd)
                cwd = str(Path(empty_workdir()).resolve()) if mode == 'text' or scope is None else scope
                if mode in ('edit', 'full'):
                    prompt = scoped_prompt(prompt, cwd, mode)
                elif mode == 'research':
                    prompt = research_prompt(prompt, scope)
                runner = delegate_claude if agent == 'claude' else delegate_codex
                # The published deadline covers policy lookup, startup and the run;
                # group cleanup after it is bounded separately (KILL_GRACE).
                with anyio.fail_after(timeout):
                    # Text is always permitted; anything stronger is checked
                    # against the operator's current ceiling for this agent.
                    if mode != 'text':
                        permit_mode(mode, agent, await mode_ceilings())
                    guides = await model_guides()
                    if binding and model not in guides['models']:
                        raise DelegateError('SESSION_POLICY_CHANGED: the model is no longer enabled; start a new delegation')
                    guide = selected_guide(guides, model)
                    expected = session_binding(resume, agent, model, mode, scope, cwd, guides, guide)
                    if binding and binding != expected:
                        raise DelegateError('SESSION_POLICY_CHANGED: the worker root or model policy changed; start a new delegation')
                    try:
                        async with prepared_images(image_paths, scope, guide) as images:
                            result = await runner(prompt, model, mode, cwd, resume, guide, images)
                    except CompletedWithoutReply as error:
                        # The runner verified native completion and identity. Bind
                        # only that authority; no reply or transcript is stored.
                        save_binding(session_binding(error.session_id, agent, model, mode,
                                                     scope, cwd, guides, guide))
                        raise DelegateError(str(error) + '; resume=' + error.session_id
                                            + ' for an explicit follow-up; task success is unverified') from None
                    session = result['session_id']
                    if not isinstance(session, str) or not _UUID.fullmatch(session):
                        raise DelegateError('DELEGATE_FAILED: local run did not return a valid session')
                    session = result['session_id'] = str(uuid.UUID(session))
                    save_binding(session_binding(session, agent, model, mode, scope, cwd, guides, guide))
                    result['capabilities'].update({'input_modalities': guide.get('input_modalities', ['text']),
                                                   'images_supplied': len(image_paths or [])})
                    return result
        except DelegateError as error:
            raise ToolError(str(error)) from None
        except TimeoutError:
            raise ToolError(f'DELEGATE_TIMEOUT: no reply within {timeout}s') from None
        except Exception:
            raise ToolError('DELEGATE_FAILED: check local agent runtime and retry') from None

    return app


def main():
    configure_logging(Path(os.environ.get('LOCAL_AGENT_STATE', '.')))
    log.info('local-agent starting (default model=%s)', DEFAULT_MODEL)
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, _shutdown)
    try:
        build_mcp().run()
    except BaseException:
        log.exception('local-agent exited abnormally')
        raise


if __name__ == '__main__':
    main()
