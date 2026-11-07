#!/usr/bin/env python3
"""Model guidance (native hook delivery, co-owned Claude settings, instruction budgets) and the local-agent
delegation boundary: argv/env contract, real-CLI reach per mode against scripted endpoints (no inference) and
the stdio server end to end. Temp roots only; re-executes under the local-agent venv."""
import asyncio, contextlib, http.server, importlib.util, io, json, os, re, shlex, shutil
import struct, subprocess, sys, tempfile, threading, unittest, uuid, zlib
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

from ailocal import environment, generation, guidance, mcp as launcher
sys.path.insert(0, str(AGENT))
import server as S
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

GUIDE = {'context_input': 253952, 'max_output': 8192}
after = lambda argv, flag: argv[argv.index(flag) + 1]


def hook(event, tier='16gb'):
    with patch.dict(os.environ, {'AILOCAL_CONFIG': str(RES)}), patch.object(sys, 'stdin', io.StringIO(json.dumps(event))), \
            contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()) as err:
        rc = guidance.main(['--tier', tier, '--hook'])
    return rc, out.getvalue(), err.getvalue()


def png(rgb=(224, 34, 51), width=64, height=64):
    chunk = lambda kind, data: (struct.pack('>I', len(data)) + kind + data
                                + struct.pack('>I', zlib.crc32(kind + data) & 0xffffffff))
    pixels = zlib.compress((b'\0' + bytes(rgb) * width) * height)
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>2I5B', width, height, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', pixels) + chunk(b'IEND', b''))


class ScriptedModel:
    """A streaming Messages endpoint that plays fixed tool calls and records each request; no inference."""
    def __init__(self, calls):
        self.calls, self.results, self.offered, self.bodies = calls, [], [], []
        owner = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a): pass
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get('content-length') or 0)) or b'{}')
                if not self.path.startswith('/v1/messages') or 'count_tokens' in self.path:
                    return self.send(json.dumps({'input_tokens': 1}).encode(), 'application/json')
                owner.bodies.append(body)
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
    def test_native_hook_follows_the_selected_model(self):
        rc, out, _ = hook({'hook_event_name': 'PostModelSwitch', 'to_model': 'ailocal-cyber'})
        self.assertTrue(policy.resolve_role('16gb', 'cyber', RES)['model'] in out and 'ailocal-primary' not in out)
        # [REAL] Claude Code 2.1.310 named a gateway-picked model `claude-router-<hex alias>`; the hook exited 2.
        router = 'claude-router-' + 'ailocal-cyber'.encode().hex()
        for tier in policy.TIERS:
            with self.subTest(tier=tier):
                cyber = policy.resolve_role(tier, 'cyber', RES)
                self.assertNotIn('No model-specific guidance', policy.guidance_for(policy.load_profile(tier, RES), 'cyber')['summary'])
                rc, out, err = hook({'hook_event_name': 'PostModelSwitch', 'to_model': router, 'requested_model': router}, tier)
                self.assertEqual((rc, err), (0, ''))
                self.assertIn(f'Local model guide: ailocal-cyber ({cyber["model"]})', out)
                self.assertIn(f'Profile limits: {cyber["context_input"]} input tokens, {cyber["max_output"]} maximum output tokens', out)
        rc, out, _ = hook({'hook_event_name': 'PostModelSwitch', 'to_model': 'claude-router-zz', 'requested_model': 'opus'})
        self.assertEqual((rc, 'ailocal-primary' in out), (0, True))   # falls back to requested_model
        # #89: a slot naming a role the tier omits resolves through policy's one rule (16gb has no fast).
        rc, out, _ = hook({'hook_event_name': 'PostModelSwitch', 'requested_model': 'haiku'})
        self.assertEqual((rc, 'ailocal-primary' in out, 'ailocal-fast' in out), (0, True, False))
        rc, out, _ = hook({'hook_event_name': 'PostModelSwitch', 'requested_model': 'haiku'}, '64gb')
        self.assertEqual((rc, 'ailocal-fast' in out), (0, True))
        rc, _, err = hook({'hook_event_name': 'PostModelSwitch', 'to_model': 'claude-router-' + 'not-served'.encode().hex()})
        self.assertEqual((rc, 'MODEL_GUIDANCE_UNKNOWN_MODEL' in err), (2, True))
        # #60: no model named: every alias's limits on one line each, never a guessed
        # default, and no full guide per alias.
        rc, out, _ = hook({})
        guides = policy.model_guides('16gb', RES)['models']
        self.assertTrue(rc == 0 and 'not supplied' in out and 'model_guide' in out)
        for name, g in guides.items():
            self.assertIn(f"{name}: {g['model']}; input {g['context_input']}, output {g['max_output']}", out)
        self.assertNotIn(guides['ailocal-cyber']['summary'], out)
        self.assertNotIn(guides['ailocal-cyber']['limitations'][0], out)
        # #60: the always-on hook leaves reference data to model_guide; the on-demand guide keeps it.
        cyber = guides['ailocal-cyber']
        _, brief, _ = hook({'model': 'ailocal-cyber'})
        self.assertTrue('Profile limits:' in brief and cyber['limitations'][0] in brief and guidance.REFERENCE_POINTER in brief)
        for absent in (cyber['evidence'], cyber['summary'], 'Configured inference preset', 'Current MTP draft tokens'):
            self.assertNotIn(absent, brief)
        with patch.dict(os.environ, {'AILOCAL_CONFIG': str(RES)}), contextlib.redirect_stdout(io.StringIO()) as full:
            self.assertEqual(guidance.main(['--tier', '16gb', 'ailocal-cyber']), 0)
        self.assertTrue(cyber['evidence'] in full.getvalue() and 'Configured inference preset' in full.getvalue())
        for cwd, previous in (('/work/first', '/work/second'), ('/work/second', '/work/first')):
            rc, out, _ = hook({'model': 'ailocal-cyber', 'cwd': cwd})
            self.assertTrue(rc == 0 and json.dumps(cwd) in out and previous not in out)
        self.assertNotIn('Task working directory:', hook({'model': 'ailocal-cyber', 'cwd': 'relative/path'})[1])

    def test_claude_settings_keep_external_hooks_and_user_choices(self):
        """settings.json is co-owned: regeneration keeps foreign hooks and an operator's explicit values."""
        models = generation.load_models_yaml(RES / 'profiles/16gb.toml')
        tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        target, external = tmp / 'settings.json', {'hooks': [{'type': 'command', 'command': 'external-hook'}]}
        target.write_text(json.dumps({'hooks': {'SessionStart': [external]}}))
        with patch.object(generation, 'CLAUDE_SETTINGS', target), patch.object(generation, 'stage') as stage:
            regen = lambda: (generation.regen_claude_settings(models, policy.load_client_policy(RES)), json.loads(stage.call_args.args[1]))[1]
            data = regen()
            self.assertTrue(external in data['hooks']['SessionStart'] and 'PostModelSwitch' in data['hooks'])
            target.write_text(json.dumps(data))
            self.assertEqual(regen(), data)
            # #39: auto memory's storage path was taken for the working directory. Off by default; an explicit choice wins.
            self.assertIs(data['autoMemoryEnabled'], False)
            target.write_text(json.dumps(dict(data, autoMemoryEnabled=True)))
            self.assertIs(regen()['autoMemoryEnabled'], True)
            # #48: no assistant trailer, PR footer or session link by default; an explicit choice wins.
            self.assertEqual(data['attribution'], {'commit': '', 'pr': '', 'sessionUrl': False})
            target.write_text(json.dumps(dict(data, attribution={'commit': 'x'})))
            self.assertEqual(regen()['attribution'], {'commit': 'x'})

    def test_client_policy_defaults_and_fails_closed(self):
        tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (tmp / 'profiles').mkdir()
        shipped = (RES / 'profiles/clients.toml').read_text()
        edit, budget = {'claude': 'edit', 'codex': 'edit'}, {'claude': 4096, 'codex': 4096}
        mode = lambda value: shipped.replace('delegate_max_mode = "edit"', f'delegate_max_mode = "{value}"', 1)
        cases = [(shipped, policy.delegate_ceilings, edit), (mode('full'), policy.delegate_ceilings, {'claude': 'full', 'codex': 'edit'}),
                 (re.sub(r'(?m)^delegate_max_mode = "edit"\n', '', shipped), policy.delegate_ceilings, edit),
                 (mode('yolo'), policy.delegate_ceilings, None), (shipped, policy.instruction_budgets, budget),
                 # An operator copy from before the key existed keeps the measured default.
                 (re.sub(r'(?m)^instruction_max_bytes = 4096\n', '', shipped), policy.instruction_budgets, budget)]
        cases += [(shipped.replace('instruction_max_bytes = 4096', f'instruction_max_bytes = {bad}', 1), policy.instruction_budgets, None)
                  for bad in ('0', '"4k"', 'true')]
        # #47: a tier override applies to its tier only, and is validated like the default.
        tiered = lambda tier: lambda: policy.instruction_budgets(tier=tier)
        cases += [(shipped, tiered('128gb'), {'claude': 8192, 'codex': 4096}), (shipped, tiered('64gb'), budget)]
        cases += [(shipped.replace('{ "128gb" = 8192 }', bad, 1), tiered('128gb'), None)
                  for bad in ('{ "256gb" = 8192 }', '{ "128gb" = 0 }', '8192')]
        with patch.dict(os.environ, {'AILOCAL_CONFIG': str(tmp)}):
            for text, reader, expected in cases:
                (tmp / 'profiles/clients.toml').write_text(text)
                if expected is None:
                    self.assertRaises(policy.ProfileError, reader)
                else:
                    self.assertEqual(reader(), expected)

    def test_instruction_budget_counts_the_guide_hook(self):
        """Claude Code adds the SessionStart guide to the context CLAUDE.md is in."""
        from ailocal.checks import config as C
        tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (tmp / 'profiles').mkdir()
        shutil.copy(RES / 'profiles/clients.toml', tmp / 'profiles/clients.toml')
        shutil.copy(RES / 'profiles/64gb.toml', tmp / 'profiles/64gb.toml')
        event = {'hook_event_name': 'SessionStart', 'cwd': '/'}
        with patch.dict(os.environ, {'AILOCAL_CONFIG': str(tmp)}), \
                patch('sys.stdin', io.StringIO(json.dumps(event))), \
                contextlib.redirect_stdout(io.StringIO()) as printed:
            self.assertEqual(guidance.main(['--hook', '--tier', '64gb']), 0)
            self.assertEqual(guidance.hook_bytes(event, '64gb'), len(printed.getvalue().encode()))
        # #60: the shipped scratchpad hook is measured from its source, in both roots.
        self.assertTrue(all(isinstance(C._scratchpad_hook_bytes(t), int) and C._scratchpad_hook_bytes(t) > 200
                            for t in ('claude', 'codex')))
        (tmp / 'claude').mkdir()
        (tmp / 'claude/CLAUDE.md').write_text('owner: cadence\n' + 'x' * 2985)
        with patch.object(policy, 'deployed_client_root', return_value=tmp), \
                patch.dict(os.environ, {'AILOCAL_CONFIG': str(tmp)}), \
                patch.object(C, '_scratchpad_hook_bytes', return_value=50), \
                patch.object(policy, 'resolve_active_tier', return_value='64gb'):
            budget = lambda: next(r for r in C.check_instruction_budget() if r.name == 'claude-instructions')
            with patch.object(C, '_guide_hook_bytes', return_value=(1000, 300)):
                claude = budget()
                self.assertEqual(claude.status, C.PASS)
                self.assertIn('CLAUDE.md 3000 + guide hook 1000 + scratchpad hook 50', claude.summary)
            # The no-model case is counted when it is the larger: "fits" is never claimed for it.
            with patch.object(C, '_guide_hook_bytes', return_value=(1000, 1200)):
                claude = budget()
                self.assertEqual(claude.status, C.WARN)
                self.assertIn('4250/4096', claude.summary)
                self.assertIn('1200 when the event names none', claude.detail)
                self.assertIn('fits alone', claude.remediation)

    def test_scratchpad_is_private_and_unescapable(self):
        """#78: the scratchpad is private, never under a pre-claimed base, and the session id cannot escape it."""
        hook = RES / 'clients/scratchpad-hook.sh'
        with tempfile.TemporaryDirectory() as box:
            root = Path(box).resolve()

            def run_hook(sid):
                out = subprocess.run(['bash', str(hook), 'claude'], input=json.dumps({'session_id': sid}),
                                     capture_output=True, text=True, timeout=10,
                                     env=dict(os.environ, AILOCAL_SCRATCH_ROOT=str(root)))
                self.assertEqual((out.returncode, out.stderr), (0, ''))
                return Path(re.search(r'Session scratchpad: (\S+)', out.stdout)[1])

            made = run_hook('../../escape/x')
            base = root / f'ailocal-scratchpad-{os.getuid()}'
            self.assertEqual(made.parent, base)
            self.assertTrue(made.is_dir() and made.stat().st_mode & 0o777 == 0o700
                            and base.stat().st_mode & 0o777 == 0o700)
            self.assertFalse((root / 'escape').exists())
            shutil.rmtree(base)
            (elsewhere := root / 'attacker').mkdir()
            base.symlink_to(elsewhere)
            made = run_hook('s1')
            self.assertNotEqual(made.parent, base)
            self.assertTrue(made.parent.name.startswith('ailocal-scratchpad.') and not any(elsewhere.iterdir()))


class Contract(unittest.TestCase):
    """Argument and environment contract of the local-agent boundary; no inference."""

    def test_child_env_drops_hosted_routing_and_applies_local_launch(self):
        # Includes what a hosted Claude Code session exports to its MCP servers.
        leaked = {'ANTHROPIC_AUTH_TOKEN': 'h', 'CLAUDE_CODE_USE_BEDROCK': '1', 'CLAUDE_CODE_OAUTH_TOKEN': 'x', 'ANTHROPIC_MODEL': 'opus',
                  'OPENAI_ORG_ID': 'org', 'CODEX_API_KEY': 'k', 'CLAUDECODE': '1', 'CLAUDE_CODE_MESSAGING_TOKEN': 't',
                  'CLAUDE_CODE_MESSAGING_SOCKET': '/s', 'CLAUDE_CODE_SESSION_ID': 'h', 'CLAUDE_CODE_ENTRYPOINT': 'cli',
                  'CLAUDE_PROJECT_DIR': '/hosted/repo', 'SSH_AUTH_SOCK': '/agent'}
        local = {'LOCAL_AGENT_BASE_URL': 'http://127.0.0.1:4000', 'LOCAL_AGENT_KEY': 'local', 'LOCAL_AGENT_CLAUDE_HOME': '/local/claude',
                 'LOCAL_AGENT_CLAUDE_ENV': json.dumps({'ANTHROPIC_DEFAULT_HAIKU_MODEL': 'ailocal-fast', 'CLAUDE_CODE_DISABLE_1M_CONTEXT': '1'}),
                 'CLAUDE_CONFIG_DIR': '/hosted/root', 'PATH': '/usr/bin'}
        with patch.dict(os.environ, leaked | local, clear=True):
            env = {(a, m): S._child_env(a, m) for a in S.AGENTS for m in ('text', 'full')}
        for (agent, mode), child in env.items():
            self.assertFalse((set(leaked) - ({'SSH_AUTH_SOCK'} if mode == 'full' else set())) & set(child), (agent, mode))
            self.assertEqual((child[S.DEPTH_VAR], child['PATH']), ('1', '/usr/bin'))
            self.assertEqual([k for k in child if k.startswith('LOCAL_AGENT_') and k != S.DEPTH_VAR], [])
        claude, codex = env['claude', 'text'], env['codex', 'text']
        # The child's own CLAUDE_* values survive the filter; the hosted root does not. Full mode keeps the SSH agent.
        self.assertEqual((claude['CLAUDE_CONFIG_DIR'], claude['CLAUDE_CODE_DISABLE_1M_CONTEXT'], env['codex', 'full']['SSH_AUTH_SOCK']),
                         ('/local/claude', '1', '/agent'))
        self.assertNotIn('CLAUDE_CONFIG_DIR', codex)
        self.assertEqual((claude['ANTHROPIC_BASE_URL'], claude['ANTHROPIC_API_KEY'], claude['ANTHROPIC_DEFAULT_HAIKU_MODEL']),
                         ('http://127.0.0.1:4000', 'local', 'ailocal-fast'))
        self.assertEqual((codex['OPENAI_BASE_URL'], codex['OPENAI_API_KEY']), ('http://127.0.0.1:4000/v1', 'local'))
        slots, launch = policy.load_client_policy()['claude']['slots'], launcher.claude_launch_environment()
        self.assertEqual({s: launch[generation.SLOT_ENV[s]] for s in slots}, {s: 'ailocal-' + c for s, c in slots.items()})
        with patch.object(launcher.runtime, 'env_value', return_value='k'):
            agent_env = launcher.agent_environment()
        self.assertEqual(json.loads(agent_env['LOCAL_AGENT_RESEARCH_MCP']), launcher.registration())
        self.assertTrue({str(environment.user_file()), str(environment.generated_file())} <= set(json.loads(agent_env['LOCAL_AGENT_DENY_READ'])))

    def test_claude_argv_per_mode(self):
        """RealClaudeReach proves the behaviour; this pins what it cannot observe."""
        deny = ['/home/u/.config/ailocal/.env.local', '/home/u/.local/state/ailocal/env']
        with patch.dict(os.environ, {'LOCAL_AGENT_DENY_READ': json.dumps(deny + ['relative/x']),
                                     'LOCAL_AGENT_RESEARCH_MCP': json.dumps(launcher.registration())}):
            settings = {m: S.claude_settings(m, GUIDE) for m in S.CLAUDE_PLAN}
            argv = {m: S._claude_argv('p', 'm', m, None, 'sid', settings[m], '/w') for m in S.CLAUDE_PLAN}
            caps = {m: S.capabilities('claude', m) for m in S.CLAUDE_PLAN}
            bare = S._claude_argv('p', 'm', 'edit', None, 'sid', settings['edit'])
            images = S._claude_argv('p', 'm', 'edit', None, 'sid', settings['edit'], images=True)
        self.assertEqual(after(argv['research'], '--disallowedTools'), 'Agent,mcp__agent__delegate')
        self.assertEqual({after(argv[m], '--disallowedTools') for m in ('edit', 'full')}, {'mcp__agent__delegate'})
        # [REAL] Claude 2.1.308 forces a scrubbed run to default mode: edits are a rule for cwd, never acceptEdits.
        self.assertEqual((after(argv['edit'], '--allowedTools'), '--allowedTools' in bare), ('Edit(//w/**)', False))
        self.assertTrue('p' not in images[:3] and '--permission-prompts' in images)
        for mode, each in argv.items():
            self.assertEqual(each.count('--settings'), 1)   # a second would be a precedence question
            self.assertEqual(after(each, '--permission-prompts'), 'none', mode)   # #39: a denied Read was re-issued 228 times
            if mode != 'full':
                self.assertFalse({'--permission-mode', '--dangerously-skip-permissions', '--add-dir'} & set(each))
                self.assertEqual(settings[mode]['env']['CLAUDE_CODE_SUBPROCESS_ENV_SCRUB'], '1')
            tools = after(each, '--tools') if '--tools' in each else None   # capabilities come from the argv's plan row
            self.assertEqual(caps[mode]['built_in_tools'], 'all' if tools is None else [t for t in tools.split(',') if t])
            self.assertEqual(caps[mode]['mcp_servers'] == [], '--strict-mcp-config' in each and '--mcp-config' not in each)
            self.assertEqual(caps[mode]['denied_reads'], [] if mode == 'full' else deny)
        sandbox = settings['edit']['sandbox']
        self.assertEqual((sandbox['enabled'], sandbox['failIfUnavailable'], sandbox['allowUnsandboxedCommands']), (True, True, False))
        self.assertEqual(sandbox['credentials']['files'], [{'path': p, 'mode': 'deny'} for p in deny])
        self.assertEqual((settings['full']['sandbox'], 'permissions' in settings['full']), ({'enabled': False}, False))
        self.assertNotIn('CLAUDE_CODE_SUBPROCESS_ENV_SCRUB', settings['full']['env'])
        with patch.dict(os.environ, {'LOCAL_AGENT_RESEARCH_MCP': ''}), self.assertRaisesRegex(S.DelegateError, '^RESEARCH_UNAVAILABLE'):
            S._claude_argv('p', 'm', 'research', None, 'sid', settings['research'])
        payload = {'permission_denials': [{'tool_name': 'Write', 'tool_input': {'file_path': '/x/clamp.py', 'content': 'secret'}},
                                          {'tool_name': 'Bash', 'tool_input': {'command': 'find / ' + 'a' * 400}}, 'not-a-dict']}
        self.assertEqual(S._permission_denials(payload), [{'tool': 'Write', 'target': '/x/clamp.py'},
                                                          {'tool': 'Bash', 'target': ('find / ' + 'a' * 400)[:200]}])

    def test_codex_argv_per_mode_and_installed_cli(self):
        with patch.dict(os.environ, {'LOCAL_AGENT_DENY_READ': json.dumps(['/k/.env.local'])}):
            for mode in S.CODEX_PLAN:
                base, caps = S.CODEX_PLAN[mode]['profile'], S.capabilities('codex', mode)
                for resume in (None, 'sid'):
                    argv = S._codex_argv('p', 'm', mode, '/work', resume, '/out')
                    self.assertIn('mcp_servers.agent.disabled_tools=["delegate"]', argv)
                    # A permission profile, never --sandbox or sandbox_mode: either drops the profile's deny rules.
                    self.assertFalse('--sandbox' in argv or [a for a in argv if a.startswith('sandbox_mode')])
                    # [REAL] Codex 0.165.0 `exec resume` has no -C: cwd is the process cwd. A fresh run always names it.
                    self.assertEqual(argv[:3] == ['codex', 'exec', 'resume'], resume is not None)
                    self.assertEqual(argv[-3:], ['--', 'sid', 'p'] if resume else ['/work', '--', 'p'])
                    self.assertEqual('-C' in argv, resume is None)
                    self.assertEqual('shell_environment_policy.ignore_default_excludes=false' in argv, mode != 'full')
                    if mode == 'full':
                        self.assertTrue(caps['sandbox'] == 'off' and 'default_permissions=":danger-full-access"' in argv)
                    else:
                        self.assertIn(f'default_permissions="{S.CODEX_PROFILE}"', argv)
                        self.assertTrue(any(a.startswith(f'permissions.{S.CODEX_PROFILE}={{extends="{base}"') for a in argv))
                        self.assertTrue(base in caps['sandbox'] and any('"/k/.env.local"="deny"' in a for a in argv))
        if not shutil.which('codex'):
            self.skipTest('codex not installed')
        for resume in (None, 'sid'):   # every flag, --image included, parses under fresh and resumed exec
            help_text = subprocess.run(['codex', 'exec', *(['resume'] if resume else []), '--help'],
                                       capture_output=True, text=True, timeout=30).stdout
            for mode in S.CODEX_PLAN:
                for flag in {a for a in S._codex_argv('p', 'm', mode, '/w', resume, '/o', [{'path': '/i.png'}]) if a[0] == '-' and a != '--'}:
                    self.assertRegex(help_text, rf'(?m)^\s+(-\w, )?{re.escape(flag)}\b|, {re.escape(flag)}\b', (flag, resume))
        root = policy.deployed_client_root() / 'codex'
        if not (root / 'config.toml').is_file() or sys.platform != 'darwin':
            self.skipTest('codex worker root or macOS Seatbelt unavailable')
        tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        key, work = tmp / 'key.env', tmp / 'work'
        key.write_text('not-a-key'); work.mkdir()
        def probe(overrides):   # model-free: the same overrides under `codex sandbox` with the worker root's config
            r = subprocess.run(['codex', 'sandbox', '-P', S.CODEX_PROFILE, '-C', str(work), *overrides, '--', 'sh', '-c',
                                f'cat {key} >/dev/null 2>&1 && echo READABLE || echo BLOCKED'],
                               env=dict(os.environ, CODEX_HOME=str(root)), capture_output=True, text=True, timeout=60)
            return r.stdout.strip().splitlines()[-1:] or [r.stderr.strip()[-200:]]
        with patch.dict(os.environ, {'LOCAL_AGENT_DENY_READ': json.dumps([str(key), str(tmp / 'absent')])}):
            self.assertEqual([probe(S.codex_permissions(m)) for m in ('text', 'edit')], [['BLOCKED']] * 2)
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

    def fake_claude(self, body):
        (self.tmp / 'bin/claude').write_text(f'#!{sys.executable}\n{body}')
        (self.tmp / 'bin/claude').chmod(0o755)

    async def test_stdio_server_guides_refuses_and_delegates(self):
        self.fake_claude('''import json, sys, time
argv, line = sys.argv, sys.stdin.readline()
images = [x for x in json.loads(line)['message']['content'] if x['type'] == 'image'] if line else []
time.sleep(0 if images else 1.5)
env = json.loads(argv[argv.index('--settings') + 1])['env']
key = '--resume' if '--resume' in argv else '--session-id'
print(json.dumps({'type': 'result', 'result': json.dumps({'env': env, 'images': len(images)}), 'session_id': argv[argv.index(key) + 1]}))
''')
        image = self.tmp / 'diagram.png'
        image.write_bytes(png())
        error = lambda result, code: self.assertTrue(result.is_error and code in result.content[0].text, (code, result))
        async with agent_session(self.env) as (session, _):
            tools = {t.name: t for t in (await session.list_tools()).tools}
            self.assertTrue(tools['model_guide'].annotations.read_only_hint and 'image_paths' in tools['delegate'].input_schema['properties'])
            guide = (await session.call_tool('model_guide', {'model': 'ailocal-cyber'})).structured_content
            self.assertEqual((guide['tier'], guide['models']['ailocal-cyber']['model']), ('16gb', policy.resolve_role('16gb', 'cyber', RES)['model']))
            error(await session.call_tool('model_guide', {'model': 'not-a-model'}), 'MODEL_GUIDANCE_UNKNOWN_MODEL')
            for args, code in (({'tools': True}, 'INVALID_CWD'), ({'model': 'ailocal-completion'}, 'MODEL_NOT_DELEGABLE'),
                               ({'model': 'ailocal-coder'}, 'UNKNOWN_MODEL'), ({'resume': 'my-thread'}, 'INVALID_SESSION'),
                               # #89: 16gb declares no fast, and a deprecated proxy alias is not a delegable model.
                               ({'model': 'ailocal-fast'}, 'UNKNOWN_MODEL'), ({'model': 'ailocal-architecture'}, 'UNKNOWN_MODEL'),
                               ({'mode': 'text', 'tools': True}, 'CONFLICTING_MODE'),
                               # The shipped policy stops at edit; full is refused before any CLI starts.
                               ({'agent': 'claude', 'mode': 'full', 'cwd': str(self.tmp)}, 'MODE_NOT_PERMITTED'),
                               ({'agent': 'codex', 'mode': 'full', 'cwd': str(self.tmp)}, 'MODE_NOT_PERMITTED')):
                error(await session.call_tool('delegate', {'prompt': 'x', **args}), code)
            work = asyncio.create_task(session.call_tool('delegate', {'prompt': 'x', 'model': 'ailocal-primary'}))
            await asyncio.sleep(0.3)
            fast = (await session.call_tool('model_guide', {'model': 'ailocal-primary'})).structured_content['models']['ailocal-primary']
            self.assertFalse(work.done(), 'model_guide waited for the running delegate')
            done = (await work).structured_content
            env = json.loads(done['output'])['env']   # the selected alias's limits and a fresh session id reach the CLI
            self.assertEqual((env['CLAUDE_CODE_MAX_OUTPUT_TOKENS'], env['CLAUDE_CODE_MAX_CONTEXT_TOKENS'], len(done['session_id'])),
                             (str(fast['max_output']), str(fast['context_input']), 36))
            first = await session.call_tool('delegate', {'prompt': 'x', 'model': 'ailocal-primary', 'cwd': str(self.tmp), 'image_paths': [str(image)]})
            self.assertFalse(first.is_error, first)
            sid = first.structured_content['session_id']
            resumed = await session.call_tool('delegate', {'prompt': 'x', 'resume': sid, 'image_paths': [str(image)]})
            self.assertEqual([json.loads(r.structured_content['output'])['images'] for r in (first, resumed)], [1, 1])
            self.assertEqual(resumed.structured_content['session_id'], sid)
            error(await session.call_tool('delegate', {'prompt': 'x', 'resume': sid, 'agent': 'codex'}), 'SESSION_MISMATCH')
        async with agent_session(dict(self.env, LOCAL_AGENT_DEPTH='1')) as (session, _):
            error(await session.call_tool('delegate', {'prompt': 'x'}), 'RECURSIVE_DELEGATION')
        async with agent_session(dict(self.env, LOCAL_AGENT_CALLER='local')) as (session, _):
            self.assertFalse((await session.call_tool('model_guide', {'model': 'ailocal-cyber'})).is_error)
            for agent in S.AGENTS:
                for tools in (False, True):
                    error(await session.call_tool('delegate', {'prompt': 'x', 'agent': agent, 'tools': tools}), 'LOCAL_DELEGATION_DISABLED')


if __name__ == '__main__':
    import harness
    harness.unittest_main()
