#!/usr/bin/env python3
"""Co-owned Claude settings and the local-agent delegation boundary: the child's environment, real-CLI reach
per mode against scripted endpoints (no inference) and the stdio server end to end. Temp roots only;
re-executes under the local-agent venv."""
import contextlib, http.server, importlib.util, json, os, shlex, shutil
import subprocess, sys, tempfile, threading, unittest, uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
RES = ROOT / 'src/ailocal/resources'
AGENT = RES / 'integrations/local-agent'
sys.path.insert(0, str(ROOT / 'src'))
sys.path.insert(0, str(ROOT / 'tests'))
from ailocal import policy

if importlib.util.find_spec('mcp') is None:
    venv = policy.state_root() / 'local-agent/.venv/bin/python'
    if not venv.is_file() or os.environ.get('AILOCAL_GUIDANCE_REEXEC'):
        sys.exit(f'FAIL: local-agent runtime missing or lacks mcp at {venv}; run: ailocal clients claude codex')
    os.environ['AILOCAL_GUIDANCE_REEXEC'] = '1'
    os.execv(str(venv), [str(venv), __file__, *sys.argv[1:]])

from ailocal import environment, generation, mcp as launcher
sys.path.insert(0, str(AGENT))
import server as S
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

class ScriptedModel:
    """A streaming Messages endpoint that plays fixed tool calls and records each request; no inference."""
    def __init__(self, calls):
        self.calls, self.results, self.offered = calls, [], []
        owner = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a): pass
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get('content-length') or 0)) or b'{}')
                if not self.path.startswith('/v1/messages') or 'count_tokens' in self.path:
                    return self.send(json.dumps({'input_tokens': 1}).encode(), 'application/json')
                results = [c for m in body.get('messages') or [] if m.get('role') == 'user' and isinstance(m.get('content'), list)
                           for c in m['content'] if isinstance(c, dict) and c.get('type') == 'tool_result']
                owner.offered.append({t.get('name', '') for t in body.get('tools') or []})
                if body.get('tools'):
                    owner.results[:] = [(r.get('is_error', False), str(r.get('content'))) for r in results]
                step, block = len(results), None
                if body.get('tools') and step < len(owner.calls):
                    block = {'type': 'tool_use', 'id': f'toolu_{step:04d}', 'name': owner.calls[step][0], 'input': owner.calls[step][1]}
                msg = {'id': 'msg_1', 'type': 'message', 'role': 'assistant', 'model': body.get('model', 'x'), 'content': [],
                       'stop_reason': None, 'stop_sequence': None, 'usage': {'input_tokens': 1, 'output_tokens': 1}}
                delta = ({'type': 'input_json_delta', 'partial_json': json.dumps(block['input'])} if block
                         else {'type': 'text_delta', 'text': 'done'})
                events = [('message_start', {'message': msg}),
                          ('content_block_start', {'index': 0, 'content_block': {**block, 'input': {}} if block else {'type': 'text', 'text': ''}}),
                          ('content_block_delta', {'index': 0, 'delta': delta}), ('content_block_stop', {'index': 0}),
                          ('message_delta', {'delta': {'stop_reason': 'tool_use' if block else 'end_turn', 'stop_sequence': None},
                                             'usage': {'output_tokens': 1}}), ('message_stop', {})]
                self.send(b''.join(f'event: {k}\ndata: {json.dumps({"type": k, **d})}\n\n'.encode() for k, d in events),
                          'text/event-stream')
            def send(self, data, kind):
                self.send_response(200); self.send_header('content-type', kind)
                self.send_header('content-length', str(len(data))); self.end_headers(); self.wfile.write(data)

        self.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f'http://127.0.0.1:{self.server.server_port}'

    def close(self):
        self.server.shutdown(); self.server.server_close()


class Guidance(unittest.TestCase):
    def test_claude_settings_keep_foreign_hooks_and_operator_values(self):
        """settings.json is co-owned: regeneration keeps a foreign hook and an operator's explicit value."""
        models = generation.load_models_yaml(RES / 'profiles/16gb.toml')
        tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        target, external = tmp / 'settings.json', {'hooks': [{'type': 'command', 'command': 'external-hook'}]}
        target.write_text(json.dumps({'hooks': {'SessionStart': [external]}, 'autoMemoryEnabled': True}))
        with patch.object(generation, 'CLAUDE_SETTINGS', target), patch.object(generation, 'stage') as stage:
            regen = lambda: (generation.regen_claude_settings(models, policy.load_client_policy(RES)), json.loads(stage.call_args.args[1]))[1]
            data = regen()
            self.assertIn(external, data['hooks']['SessionStart'])
            self.assertIs(data['autoMemoryEnabled'], True)
            target.write_text(json.dumps(data))
            self.assertEqual(regen(), data)   # a second start adds nothing


class Contract(unittest.TestCase):
    """What the local-agent boundary hands a delegated CLI; no inference."""

    def test_child_env_carries_no_hosted_credential_and_denies_the_key_files(self):
        # Includes what a hosted Claude Code session exports to its MCP servers.
        leaked = {'ANTHROPIC_AUTH_TOKEN': 'h', 'CLAUDE_CODE_USE_BEDROCK': '1', 'CLAUDE_CODE_OAUTH_TOKEN': 'x', 'ANTHROPIC_MODEL': 'opus',
                  'OPENAI_ORG_ID': 'org', 'CODEX_API_KEY': 'k', 'CLAUDECODE': '1', 'CLAUDE_CODE_SESSION_ID': 'h',
                  'CLAUDE_PROJECT_DIR': '/hosted/repo', 'SSH_AUTH_SOCK': '/agent'}
        local = {'LOCAL_AGENT_BASE_URL': 'http://127.0.0.1:4000', 'LOCAL_AGENT_KEY': 'local', 'LOCAL_AGENT_CLAUDE_HOME': '/local/claude',
                 'CLAUDE_CONFIG_DIR': '/hosted/root', 'PATH': '/usr/bin'}
        with patch.dict(os.environ, leaked | local, clear=True):
            env = {(a, m): S._child_env(a, m) for a in S.AGENTS for m in ('text', 'full')}
        for (agent, mode), child in env.items():
            self.assertFalse((set(leaked) - ({'SSH_AUTH_SOCK'} if mode == 'full' else set())) & set(child), (agent, mode))
        claude, codex = env['claude', 'text'], env['codex', 'text']
        self.assertEqual((claude['CLAUDE_CONFIG_DIR'], claude['ANTHROPIC_BASE_URL'], claude['ANTHROPIC_API_KEY']),
                         ('/local/claude', 'http://127.0.0.1:4000', 'local'))
        self.assertEqual((codex['OPENAI_BASE_URL'], codex['OPENAI_API_KEY']), ('http://127.0.0.1:4000/v1', 'local'))
        # The launcher names ailocal's own key files so every restricted mode denies them.
        with patch.object(launcher.runtime, 'env_value', return_value='k'):
            denied = set(json.loads(launcher.agent_environment()['LOCAL_AGENT_DENY_READ']))
        self.assertLessEqual({str(environment.user_file()), str(environment.generated_file())}, denied)

    def test_codex_worker_cannot_read_a_denied_file(self):
        """[REAL] 2026-10-28, Codex 0.165.0: `--sandbox read-only` let a delegated shell read the key file."""
        root = policy.deployed_client_root() / 'codex'
        if not shutil.which('codex') or not (root / 'config.toml').is_file() or sys.platform != 'darwin':
            self.skipTest('codex, its worker root or macOS Seatbelt unavailable')
        tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        key, work = tmp / 'key.env', tmp / 'work'
        key.write_text('not-a-key'); work.mkdir()

        def probe(overrides):   # model-free: the run's own -c overrides under `codex sandbox` with the worker root's config
            r = subprocess.run(['codex', 'sandbox', '-P', S.CODEX_PROFILE, '-C', str(work), *overrides, '--', 'sh', '-c',
                                f'cat {key} >/dev/null 2>&1 && echo READABLE || echo BLOCKED'],
                               env=dict(os.environ, CODEX_HOME=str(root)), capture_output=True, text=True, timeout=60)
            return r.stdout.strip().splitlines()[-1:] or [r.stderr.strip()[-200:]]
        with patch.dict(os.environ, {'LOCAL_AGENT_DENY_READ': json.dumps([str(key), str(tmp / 'absent')])}):
            got = {}
            for mode in ('text', 'edit'):
                argv = S._codex_argv('p', 'm', mode, str(work), None, '/o')
                # --sandbox or sandbox_mode selects the older settings and drops the profile's deny entries.
                self.assertFalse('--sandbox' in argv or [a for a in argv if a.startswith('sandbox_mode')], mode)
                got[mode] = probe([x for i, a in enumerate(argv) if a == '-c' and 'permissions' in argv[i + 1]
                                   for x in ('-c', argv[i + 1])])
        self.assertEqual(got, {'text': ['BLOCKED'], 'edit': ['BLOCKED']})
        self.assertEqual(probe(['-c', f'default_permissions="{S.CODEX_PROFILE}"',   # the probe can tell
                                '-c', f'permissions.{S.CODEX_PROFILE}={{extends=":read-only"}}']), ['READABLE'])


class RealClaudeReach(unittest.TestCase):
    """The installed claude CLI's permission engine against a hostile project and eight scripted tool calls."""
    LOADS = {'text': {'user-hook'}, 'research': {'user-hook', 'research-mcp'}, 'edit': {'user-hook'},
             'full': {'user-hook', 'project-hook', 'project-mcp'}}
    #: in-cwd read, symlink out, outside read, denied key, read calc.py, edit it, Write and Bash redirect outside.
    REACH = {'research': [True, False, False, False, True, False, False, False],
             'edit': [True, False, False, False, True, True, False, False], 'full': [True] * 8}

    def run_mode(self, mode):
        tmp = Path(tempfile.mkdtemp(dir='/private/tmp'))
        self.addCleanup(shutil.rmtree, tmp, True)
        work, outside, cfg, marks = tmp / 'work', tmp / 'outside', tmp / 'cfg', tmp / 'marks'
        for d in (work / '.claude', outside, cfg, marks):
            d.mkdir(parents=True)
        for path, text in ((work / 'calc.py', 'def add(a, b):\n    return a - b\n'), (work / 'notes.txt', 'inside\n'),
                           (outside / 'canary.txt', 'canary\n'), (outside / 'key.env', 'not-a-key\n'),
                           (tmp / 'mark.py', f'import sys\nopen({str(marks)!r} + "/" + sys.argv[1], "w").write("x")\n')):
            path.write_text(text)
        (work / 'escape.txt').symlink_to(outside / 'canary.txt')
        touch = lambda name: json.dumps({'hooks': {'SessionStart': [{'hooks': [{'type': 'command', 'command': f'touch {marks / name}'}]}]}})
        (cfg / 'settings.json').write_text(touch('user-hook'))
        (work / '.claude/settings.json').write_text(touch('project-hook'))
        (work / '.mcp.json').write_text(json.dumps({'mcpServers': {'m': {'command': sys.executable, 'args': [str(tmp / 'mark.py'), 'project-mcp']}}}))
        research = tmp / 'fake-ailocal'
        research.write_text('#!/bin/sh\nexec ' + shlex.join([sys.executable, str(tmp / 'mark.py'), 'research-mcp']) + '\n')
        research.chmod(0o755)
        read = lambda path: ('Read', {'file_path': str(path)})
        model = ScriptedModel([read(work / 'notes.txt'), read(work / 'escape.txt'), read(outside / 'canary.txt'), read(outside / 'key.env'),
                               read(work / 'calc.py'), ('Edit', {'file_path': str(work / 'calc.py'), 'old_string': 'a - b', 'new_string': 'a + b'}),
                               ('Write', {'file_path': str(outside / 'written.txt'), 'content': 'x'}),
                               ('Bash', {'command': f'echo x > {outside}/bash.txt', 'description': 'write'})])
        self.addCleanup(model.close)
        env = {'LOCAL_AGENT_BASE_URL': model.url, 'LOCAL_AGENT_KEY': 'not-a-key', 'LOCAL_AGENT_CLAUDE_HOME': str(cfg),
               'LOCAL_AGENT_DENY_READ': json.dumps([str(outside / 'key.env')]),
               'LOCAL_AGENT_RESEARCH_MCP': json.dumps({'type': 'stdio', 'command': str(research), 'args': ['mcp', 'research'], 'env': {}})}
        with patch.dict(os.environ, env):
            argv = S._claude_argv('go', 'ailocal-fast', mode, None, str(uuid.uuid4()),
                                  S.claude_settings(mode, {'context_input': 200000, 'max_output': 4096}), str(work))
            child = S._child_env('claude', mode)
        r = subprocess.run(argv, cwd=work, env=child, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=120)
        return r, model, work, outside, {p.name for p in marks.iterdir()}

    def test_each_mode_loads_and_reaches_exactly_its_declared_scope(self):
        if not shutil.which('claude'):
            self.skipTest('claude not installed')
        with ThreadPoolExecutor(4) as pool:
            runs = dict(zip(self.LOADS, pool.map(self.run_mode, self.LOADS)))
        for mode, (r, model, work, outside, marks) in runs.items():
            with self.subTest(mode=mode):
                self.assertEqual((r.returncode, marks), (0, self.LOADS[mode]), r.stdout[-300:])
                # Claude forces a scrubbed acceptEdits run back to default unless tools are declared.
                self.assertNotIn('forced to default', r.stderr)
                builtin = {n for names in model.offered for n in names
                           if not n.startswith('mcp__') and n not in ('DeferredToolPlaceholder', 'ToolSearch')}
                if mode == 'text':
                    self.assertEqual((builtin, model.results), (set(), []))
                    continue
                if mode == 'research':
                    self.assertEqual(builtin, {'Read', 'Grep', 'Glob'})
                self.assertEqual([not denied for denied, _ in model.results], self.REACH[mode], [t[:80] for _, t in model.results])
                self.assertEqual('a + b' in (work / 'calc.py').read_text(), mode in ('edit', 'full'))
                self.assertEqual(sorted(p.name for p in outside.iterdir()), ['bash.txt', 'canary.txt', 'key.env', 'written.txt']
                                 if mode == 'full' else ['canary.txt', 'key.env'])


@contextlib.asynccontextmanager
async def agent_session(env):
    params = StdioServerParameters(command=sys.executable, args=[str(AGENT / 'server.py')], env=env)
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        yield session, await session.initialize()


class StdioServer(unittest.IsolatedAsyncioTestCase):
    """The stdio server under a real MCP client and a fake claude; no inference."""

    async def asyncSetUp(self):
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (self.tmp / 'active-profile').write_text('16gb\n')
        (self.tmp / 'bin').mkdir()
        self.env = dict(os.environ, AILOCAL_CONFIG=str(RES), AILOCAL_STATE=str(self.tmp), LOCAL_AGENT_STATE=str(self.tmp),
                        LOCAL_AGENT_BASE_URL='http://127.0.0.1:9', LOCAL_AGENT_KEY='not-a-key',
                        PATH=f'{self.tmp / "bin"}{os.pathsep}{os.environ["PATH"]}')
        # A run that "succeeds" with a refused Write, or that the CLI itself marks failed.
        (self.tmp / 'bin/claude').write_text(f'''#!{sys.executable}
import json, sys
argv = sys.argv
print(json.dumps({{'type': 'result', 'subtype': 'success', 'is_error': argv[2] == 'fail', 'result': 'done',
                  'session_id': argv[argv.index('--session-id') + 1],
                  'permission_denials': [{{'tool_name': 'Write', 'tool_input': {{'file_path': '/x/out.py', 'content': 's'}}}}]}}))
''')
        (self.tmp / 'bin/claude').chmod(0o755)

    async def test_refused_and_failed_delegations_are_never_reported_as_success(self):
        error = lambda result, code: self.assertTrue(result.is_error and code in result.content[0].text, (code, result))
        async with agent_session(self.env) as (session, _):
            # The shipped policy stops at edit; full is refused before any CLI starts.
            for agent in S.AGENTS:
                error(await session.call_tool('delegate', {'prompt': 'x', 'agent': agent, 'mode': 'full', 'cwd': str(self.tmp)}),
                      'MODE_NOT_PERMITTED')
            error(await session.call_tool('delegate', {'prompt': 'fail'}), 'DELEGATE_FAILED')
            done = await session.call_tool('delegate', {'prompt': 'x'})
            self.assertFalse(done.is_error, done)
            self.assertEqual(done.structured_content['permission_denials'], [{'tool': 'Write', 'target': '/x/out.py'}])
        async with agent_session(dict(self.env, LOCAL_AGENT_DEPTH='1')) as (session, _):
            error(await session.call_tool('delegate', {'prompt': 'x'}), 'RECURSIVE_DELEGATION')
        async with agent_session(dict(self.env, LOCAL_AGENT_CALLER='local')) as (session, _):
            error(await session.call_tool('delegate', {'prompt': 'x'}), 'LOCAL_DELEGATION_DISABLED')


if __name__ == '__main__':
    import harness
    harness.unittest_main()
