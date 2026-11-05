#!/usr/bin/env python3
"""Model guidance (exact backend, native hook delivery, MCP model_guide) and the local-agent delegation
boundary: argv/env contract, real-CLI reach per mode against scripted endpoints (no inference), process
lifecycle, session binding and explicit image inputs. Temp roots only; re-executes under the local-agent venv."""
import asyncio, base64, contextlib, copy, http.server, importlib.util, io, json, os, re, shlex, shutil, signal
import struct, subprocess, sys, tempfile, threading, time, unittest, uuid, zlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parent.parent
RES = ROOT / 'src/ailocal/resources'
AGENT = RES / 'integrations/local-agent'
sys.path.insert(0, str(ROOT / 'src'))
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
from mcp.server.mcpserver.exceptions import ToolError

GUIDE = {'context_input': 253952, 'max_output': 8192}
after = lambda argv, flag: argv[argv.index(flag) + 1]


def hook(event, tier='16gb'):
    with patch.dict(os.environ, {'AILOCAL_CONFIG': str(RES)}), patch.object(sys, 'stdin', io.StringIO(json.dumps(event))), \
            contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()) as err:
        rc = guidance.main(['--tier', tier, '--hook'])
    return rc, out.getvalue(), err.getvalue()


def png(rgb=(224, 34, 51), width=64, height=64, broken=False):
    chunk = lambda kind, data: (struct.pack('>I', len(data)) + kind + data
                                + struct.pack('>I', zlib.crc32(kind + data) & 0xffffffff))
    pixels = b'not-zlib' if broken else zlib.compress((b'\0' + bytes(rgb) * width) * height)
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
    def test_guides_are_keyed_by_exact_backend(self):
        for tier in policy.TIERS:
            guides = policy.model_guides(tier, RES)['models']
            self.assertEqual(set(guides), {'ailocal-' + r for r, c in policy.profile_summary(tier, RES)['roles'].items() if c['enabled']})
            for alias, guide in guides.items():
                role = policy.resolve_role(tier, alias.removeprefix('ailocal-'), RES)
                self.assertEqual((guide['model'], guide['context_input'], guide['sampling']['temperature']),
                                 (role['model'], role['context_input'], role['temperature']))
                if role.get('draft_num_predict') is not None:
                    self.assertEqual(guide['sampling']['draft_num_predict'], role['draft_num_predict'])
                self.assertTrue(guide['sources'] and guide['evidence'].startswith('['))
        data = policy.load_profile('16gb', RES)
        data['cyber']['active'] = 'operator-custom-model'   # a changed backend never inherits another's guide
        guide = policy.guidance_for(data, 'cyber')
        self.assertEqual(('No model-specific guidance' in guide['summary'], guide['sources']), (True, []))
        data = policy.load_profile('16gb', RES)
        data['model_guidance'][data['implementation']['active']]['prompting'] = 'wrong shape'
        self.assertRaises(policy.ProfileError, policy._validate_guidance, data)

    def test_native_hook_follows_the_selected_model(self):
        rc, out, _ = hook({'hook_event_name': 'PostModelSwitch', 'to_model': 'ailocal-cyber'})
        self.assertTrue(policy.resolve_role('16gb', 'cyber', RES)['model'] in out and 'Lead architect' not in out)
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
        self.assertEqual((rc, 'ailocal-architecture' in out), (0, True))   # falls back to requested_model
        rc, _, err = hook({'hook_event_name': 'PostModelSwitch', 'to_model': 'claude-router-' + 'not-served'.encode().hex()})
        self.assertEqual((rc, 'MODEL_GUIDANCE_UNKNOWN_MODEL' in err), (2, True))
        rc, out, _ = hook({})   # no model named: every entry once, never a guessed default
        guides = policy.model_guides('16gb', RES)['models']
        self.assertTrue('not supplied' in out and guides['ailocal-cyber']['summary'] in out and guides['ailocal-cyber']['model'] in out)
        self.assertEqual(out.count(guides['ailocal-implementation']['evidence']), 1)
        for cwd, previous in (('/work/first', '/work/second'), ('/work/second', '/work/first')):
            rc, out, _ = hook({'model': 'ailocal-cyber', 'cwd': cwd})
            self.assertTrue(rc == 0 and json.dumps(cwd) in out and previous not in out)
        self.assertNotIn('Task working directory:', hook({'model': 'ailocal-cyber', 'cwd': 'relative/path'})[1])

    def test_generators_share_guidance_and_preserve_external_hooks(self):
        models = generation.load_models_yaml(RES / 'profiles/16gb.toml')
        self.assertTrue('model_guidance' not in models and 'completion' in models)
        tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (tmp / '16gb.toml').write_text((RES / 'profiles/16gb.toml').read_text().replace('[cyber]\n', '[cyber]\nenabled = false\n', 1))
        self.assertNotIn('cyber', generation.load_models_yaml(tmp / '16gb.toml'))
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
        catalog = lambda roles: (generation.regen_catalog(roles), {e['slug']: e for e in json.loads(stage.call_args.args[1])['models']})[1]
        with patch.object(generation, 'stage') as stage:
            for tier in policy.TIERS:
                roles = policy.profile_summary(tier, RES)['roles']
                for slug, entry in catalog(roles).items():
                    role = slug.removeprefix('ailocal-')
                    expected = ['text'] if role == 'completion' else roles[role]['guidance'].get('input_modalities', ['text'])
                    self.assertEqual(entry['input_modalities'], expected, (tier, slug))
                replacement = {**roles['architecture'], 'active': 'replacement'}
                replacement['guidance'] = policy.guidance_for({'architecture': replacement}, 'architecture')
                self.assertEqual(catalog({'architecture': replacement})['ailocal-architecture']['input_modalities'], ['text'])
            entries = catalog(models)
        self.assertIn(models['cyber']['guidance']['evidence'], entries['ailocal-cyber']['base_instructions'])
        self.assertIn('Current MTP draft tokens: 1.', entries['ailocal-cyber']['base_instructions'])
        self.assertFalse(entries['ailocal-cyber']['supports_parallel_tool_calls'] or entries['ailocal-completion']['supports_parallel_tool_calls'])
        self.assertIn('supports_function_calling: false', generation.gen_role_block('completion', models['completion']))

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
        (tmp / 'claude').mkdir()
        (tmp / 'claude/CLAUDE.md').write_text('owner: cadence\n' + 'x' * 2985)
        with patch.object(policy, 'deployed_client_root', return_value=tmp), \
                patch.dict(os.environ, {'AILOCAL_CONFIG': str(tmp)}):
            with patch.object(C, '_guide_hook_bytes', return_value=(1000, 9000)):
                claude = next(r for r in C.check_instruction_budget() if r.name == 'claude-instructions')
                self.assertEqual(claude.status, C.PASS)
                self.assertIn('CLAUDE.md 3000 + guide hook 1000', claude.summary)
            with patch.object(C, '_guide_hook_bytes', return_value=(1200, 9000)):
                claude = next(r for r in C.check_instruction_budget() if r.name == 'claude-instructions')
                self.assertEqual(claude.status, C.WARN)
                self.assertIn('4200/4096', claude.summary)
                self.assertIn('9000 bytes', claude.detail)
                self.assertIn('fits alone', claude.remediation)

    def test_contract_publishes_the_full_budget(self):
        """#56: subtracting the hook left Claude less than Cadence's compact policy."""
        with patch.object(generation, 'stage') as stage, contextlib.redirect_stderr(io.StringIO()):
            generation.write_integration_contract({'fast': {}})
        published = {c: e['max_bytes'] for c, e in json.loads(stage.call_args.args[1])['instructions'].items()}
        self.assertEqual(published, policy.instruction_budgets())


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

    def test_mode_resolution_and_operator_ceiling(self):
        R, ceilings = S.resolve_mode, {'claude': 'full', 'codex': 'edit'}
        # The legacy flag keeps its meaning; an explicit mode wins when tools agrees or is omitted.
        self.assertEqual([R(None, None), R(None, False), R(None, True), R('text', False), R('edit', True), R('full', None),
                          R('research', None)], ['text', 'text', 'edit', 'text', 'edit', 'full', 'research'])
        S.permit_mode('full', 'claude', ceilings); S.permit_mode('edit', 'codex', ceilings)
        S.permit_mode('research', 'claude', {}); S.supported_mode('research', 'claude')
        guides = {'models': {'ailocal-fast': GUIDE, 'ailocal-completion': GUIDE}}
        self.assertIs(S.selected_guide(guides, 'ailocal-fast'), GUIDE)
        refusals = [('CONFLICTING_MODE', R, args) for args in (('text', True), ('edit', False), ('full', False), ('research', True),
                                                               ('research', False))]
        refusals += [('UNKNOWN_MODE', R, ('bogus', None)), ('UNSUPPORTED_MODE', S.supported_mode, ('research', 'codex')),
                     ('MODEL_NOT_DELEGABLE', S.selected_guide, (guides, 'ailocal-completion')),
                     ('UNKNOWN_MODEL', S.selected_guide, (guides, 'ailocal-coder'))]
        refusals += [('MODE_NOT_PERMITTED', S.permit_mode, args) for args in (
            ('full', 'codex', ceilings), ('edit', 'claude', {'claude': 'text'}), ('research', 'claude', {'claude': 'text'}),
            ('edit', 'claude', {'claude': 'research'}), ('edit', 'claude', {'claude': 'bogus'}), ('full', 'claude', {}))]
        for code, call, args in refusals:
            with self.assertRaisesRegex(S.DelegateError, '^' + code, msg=args):
                call(*args)

    def test_failed_runs_are_errors_not_answers(self):
        sid, thread = str(uuid.UUID(int=0)), '0199a213-81c0-7800-8aa1-bbab2a035a53'
        ev = lambda kind, **kw: json.dumps({'type': kind, **kw})
        def claude(code, out):
            async def run(argv, *a):
                return code, out.encode()
            return 'claude', run
        def codex(code, events, message='x'):
            async def run(argv, env, cwd, on_line):
                Path(after(argv, '-o')).write_text(message)
                [on_line(line.encode()) for line in events]
                return code, b''
            return 'codex', run
        started, completed = ev('thread.started', thread_id=thread), ev('turn.completed')
        failed, empty = 'DELEGATE_FAILED: local {} run did not complete', 'DELEGATE_EMPTY: local {} run returned no answer'
        cases = [(claude(0, ev('result', is_error=True, subtype='error_max_turns', result='x')), 'DELEGATE_FAILED: local Claude run error_max_turns'),
                 (claude(1, json.dumps({'is_error': False, 'result': 'partial', 'session_id': sid})), failed),
                 (claude(1, json.dumps({'is_error': True, 'subtype': 'success', 'session_id': sid})), failed),
                 (claude(0, json.dumps({'result': 'done', 'session_id': 'another'})), failed),
                 (claude(0, json.dumps({'result': ' ', 'session_id': sid})), empty), (claude(0, 'not json'), failed),
                 (claude(0, ev('result', subtype='success', is_error=False, result=' ', session_id=sid)), empty + ' resumable'),
                 (codex(1, [started, completed]), failed), (codex(0, [started]), failed),
                 (codex(0, [started, ev('turn.failed'), completed]), failed), (codex(0, [started, completed], ' \n'), empty + ' resumable')]
        with patch.dict(os.environ, {'LOCAL_AGENT_BASE_URL': 'http://127.0.0.1:4000', 'LOCAL_AGENT_KEY': 'k'}):
            for (agent, run), expected in cases:
                name = agent.capitalize()
                with patch.object(S, '_run', side_effect=run), self.assertRaises(S.DelegateError) as caught:
                    asyncio.run(getattr(S, 'delegate_' + agent)('p', 'm', 'text', None, sid if agent == 'claude' else None, GUIDE))
                resumable = isinstance(caught.exception, S.CompletedWithoutReply)
                self.assertEqual(str(caught.exception) + (' resumable' if resumable else ''), expected.format(name))
            for (agent, run), session, output in ((claude(0, json.dumps({'result': 'done', 'session_id': sid})), sid, 'done'),
                                                  # The id is the thread.started event's, not the first UUID in the stream.
                                                  (codex(0, [ev('item.completed', item={'id': sid}), started, completed], 'last'), thread, 'last')):
                with patch.object(S, '_run', side_effect=run):
                    done = asyncio.run(getattr(S, 'delegate_' + agent)('p', 'm', 'text', None, sid if agent == 'claude' else None, GUIDE))
                self.assertEqual((done['output'], done['session_id']), (output, session))

    def test_text_mode_runs_in_an_owned_empty_directory(self):
        tmp = self.enterContext(tempfile.TemporaryDirectory())
        with patch.dict(os.environ, {'LOCAL_AGENT_STATE': tmp}):
            first = S.empty_workdir()
            (Path(first) / 'planted').write_text('x'); (Path(first) / 'dir').mkdir(); (Path(first) / 'link').symlink_to(tmp)
            self.assertEqual((first, S.empty_workdir(), list(Path(first).iterdir())), (str(Path(tmp) / 'empty'), first, []))
            self.assertTrue(Path(tmp).is_dir())   # stable for resume; a symlink is unlinked, not followed


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


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


async def gone(pids, within=3):
    deadline = time.monotonic() + within
    while time.monotonic() < deadline and any(map(alive, pids)):
        await asyncio.sleep(0.05)
    return not any(map(alive, pids))


@contextlib.asynccontextmanager
async def agent_session(env):
    params = StdioServerParameters(command=sys.executable, args=[str(AGENT / 'server.py')], env=env)
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        yield session, await session.initialize()


class Lifecycle(unittest.IsolatedAsyncioTestCase):
    """Owned process groups and the stdio server under real subprocesses and a fake claude; no inference."""

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

    async def test_group_is_reaped_on_timeout_cancel_flood_held_pipe_and_input(self):
        import anyio
        (self.tmp / 'fake.py').write_text('''import os, signal, subprocess, sys, time
mode, pids = sys.argv[1], sys.argv[2]
assert mode == 'input' or sys.stdin.read() == ''   # stdin is /dev/null, never the protocol pipe
open(pids, 'w').write(f"{os.getpid()} {subprocess.Popen(['sleep', '300']).pid}")
if mode == 'flood':
    while True: sys.stdout.write('x' * 65536)
if mode == 'held':
    print('reply', flush=True); os._exit(0)   # the child still holds stdout open
if mode == 'bigline':   # one event line past the bound (a tool's output), then a terminal event
    sys.stdout.write('{"type":"item.completed","text":"' + 'y' * (9 * 1024 * 1024) + '"}\\n{"type":"turn.completed"}\\n')
    sys.stdout.flush(); os._exit(0)
if mode == 'graceful':   # a descendant that needs a moment on TERM; the leader exits first
    def done(*_): time.sleep(0.2); open(pids + '.marker', 'w').write('done'); sys.exit(0)
    if os.fork() == 0:
        os.dup2(os.open(os.devnull, os.O_WRONLY), 1); signal.signal(signal.SIGTERM, done); time.sleep(300)
    time.sleep(0.5); print('reply', flush=True); os._exit(0)
time.sleep(300)
''')
        async def bounded(coro, cancel=False, limit=1.5):
            try:
                with anyio.move_on_after(limit) if cancel else anyio.fail_after(limit):
                    return await coro
                return 'cancelled'
            except TimeoutError:
                return 'timeout'
            except S.DelegateError as error:
                return str(error).split(':')[0]
        async def case(mode, cancel=False, limit=1.5, **kw):
            pids, start = str(self.tmp / mode), time.monotonic()
            run = S._run([sys.executable, str(self.tmp / 'fake.py'), mode, pids], dict(os.environ), None, **kw)
            outcome = await bounded(run, cancel, limit)
            return outcome, time.monotonic() - start, await gone([int(p) for p in open(pids).read().split()])
        seen = []
        cases = {'sleep': case('sleep'), 'cancel': case('cancel', True), 'flood': case('flood', limit=20), 'held': case('held', limit=20),
                 'bigline': case('bigline', limit=20, on_line=seen.append), 'graceful': case('graceful', limit=20),
                 'input': case('input', input_data=b'x' * (2 * 1024 * 1024))}   # reaped while the input writer is blocked
        results = dict(zip(cases, await asyncio.gather(*cases.values())))
        self.assertTrue(all(ok for _, _, ok in results.values()), results)
        self.assertEqual({m: r[0] for m, r in results.items()},
                         {'sleep': 'timeout', 'cancel': 'cancelled', 'flood': 'DELEGATE_OUTPUT_LIMIT', 'held': (0, b'reply\n'),
                          'bigline': (0, b''), 'graceful': (0, b'reply\n'), 'input': 'timeout'})
        self.assertLess(results['held'][1], S.DRAIN_GRACE + 3)
        self.assertEqual(seen, [b'{"type":"turn.completed"}'])   # an oversized line is skipped, not fatal
        self.assertTrue((self.tmp / 'graceful.marker').exists())   # after exit the group gets TERM and time to act on it
        code, out = await S._run([sys.executable, '-c', 'import sys; print(len(sys.stdin.buffer.read()))'],
                                 {'PATH': '/usr/bin:/bin'}, None, input_data=b'x' * (2 * 1024 * 1024))
        self.assertEqual((code, int(out), S._ACTIVE), (0, 2 * 1024 * 1024, type(S._ACTIVE)()))

    async def test_host_stop_kills_a_child_that_ignores_sigterm(self):
        """Claude Code stops a stdio server with SIGINT, SIGTERM after 100 ms and SIGKILL 400 ms later; the
        child runs in its own session, so only the server can end it, inside that window."""
        pids = self.tmp / 'pids'
        self.fake_claude('import os, signal, time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\n'
                         f'open({str(pids)!r}, "w").write(f"{{os.getpid()}} {{os.getppid()}}")\ntime.sleep(60)\n')
        class Done(Exception):
            pass
        try:
            async with agent_session(self.env) as (session, _):
                work = asyncio.create_task(session.call_tool('delegate', {'prompt': 'x', 'model': 'ailocal-fast'}))
                while not (pids.exists() and pids.read_text()):
                    await asyncio.sleep(0.05)
                child, server = map(int, pids.read_text().split())
                os.kill(server, signal.SIGINT); time.sleep(0.1)
                alive(server) and os.kill(server, signal.SIGTERM); time.sleep(0.4)
                alive(server) and os.kill(server, signal.SIGKILL)
                self.assertTrue(await gone([child], 1), 'child survived the host stop sequence')
                work.cancel()
                raise Done
        except BaseException as error:
            if not (isinstance(error, Done) or isinstance(error, BaseExceptionGroup) and error.subgroup(Done)):
                raise

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
                               ({'mode': 'text', 'tools': True}, 'CONFLICTING_MODE'),
                               # The shipped policy stops at edit; full is refused before any CLI starts.
                               ({'agent': 'claude', 'mode': 'full', 'cwd': str(self.tmp)}, 'MODE_NOT_PERMITTED'),
                               ({'agent': 'codex', 'mode': 'full', 'cwd': str(self.tmp)}, 'MODE_NOT_PERMITTED')):
                error(await session.call_tool('delegate', {'prompt': 'x', **args}), code)
            work = asyncio.create_task(session.call_tool('delegate', {'prompt': 'x', 'model': 'ailocal-fast'}))
            await asyncio.sleep(0.3)
            fast = (await session.call_tool('model_guide', {'model': 'ailocal-fast'})).structured_content['models']['ailocal-fast']
            self.assertFalse(work.done(), 'model_guide waited for the running delegate')
            done = (await work).structured_content
            env = json.loads(done['output'])['env']   # the selected alias's limits and a fresh session id reach the CLI
            self.assertEqual((env['CLAUDE_CODE_MAX_OUTPUT_TOKENS'], env['CLAUDE_CODE_MAX_CONTEXT_TOKENS'], len(done['session_id'])),
                             (str(fast['max_output']), str(fast['context_input']), 36))
            first = await session.call_tool('delegate', {'prompt': 'x', 'model': 'ailocal-fast', 'cwd': str(self.tmp), 'image_paths': [str(image)]})
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


class Sessions(unittest.IsolatedAsyncioTestCase):
    """Session authority and explicit images through build_mcp with fixture workers."""

    async def asyncSetUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix='delegate inputs ', dir='/private/tmp')))
        self.work, self.home, self.state = self.root / 'work', self.root / 'worker', self.root / 'state'
        self.work.mkdir(); self.home.mkdir()
        self.enterContext(patch.dict(os.environ, {
            'LOCAL_AGENT_STATE': str(self.state), 'LOCAL_AGENT_CLAUDE_HOME': str(self.home), 'LOCAL_AGENT_CODEX_HOME': str(self.home),
            'LOCAL_AGENT_BASE_URL': 'http://127.0.0.1:9', 'LOCAL_AGENT_KEY': 'not-a-key', 'LOCAL_AGENT_DENY_READ': '[]',
            'LOCAL_AGENT_CLAUDE_ENV': '{}'}))
        for key in (S.DEPTH_VAR, 'LOCAL_AGENT_CALLER'):
            os.environ.pop(key, None)
        self.guide = {'model': 'fixture-model', 'context_input': 16000, 'max_output': 512, 'reasoning': False, 'sampling': {},
                      'source': None, 'input_modalities': ['text', 'image']}
        self.guides, self.ceilings = {'tier': '64gb', 'models': {'ailocal-fast': self.guide}}, {'claude': 'edit', 'codex': 'edit'}
        self.enterContext(patch.object(S, 'model_guides', AsyncMock(return_value=self.guides)))
        self.enterContext(patch.object(S, 'mode_ceilings', AsyncMock(return_value=self.ceilings)))
        self.runs = []
        async def worker(prompt, model, mode, cwd, resume, guide, images=()):
            self.runs.append({'mode': mode, 'cwd': cwd, 'images': copy.deepcopy(images)})
            return {'agent': 'fixture', 'mode': mode, 'model': model, 'session_id': resume or str(uuid.uuid4()),
                    'output': 'done', 'capabilities': {}}
        self.workers = [patch.object(S, 'delegate_' + a, side_effect=worker) for a in S.AGENTS]
        [self.enterContext(p) for p in self.workers]
        self.app = S.build_mcp()

    async def call(self, **args):
        return (await self.app.call_tool('delegate', {'prompt': 'task', 'model': 'ailocal-fast', **args})).structured_content

    async def refused(self, code, **args):
        before = len(self.runs)
        with self.assertRaises(ToolError) as error:
            await self.call(**args)
        self.assertIn(code + ':', str(error.exception))
        self.assertEqual(len(self.runs), before, 'a refusal launched a worker')

    async def test_resume_inherits_binding_and_rechecks_policy(self):
        sid = (await self.call(agent='codex', mode='edit', cwd=str(self.work)))['session_id']
        self.assertEqual((await self.call(resume=sid.upper(), model=None))['session_id'], sid)
        self.assertEqual((self.runs[-1]['mode'], self.runs[-1]['cwd']), ('edit', str(self.work)))
        for changed in ({'agent': 'claude'}, {'mode': 'text'}, {'tools': False}, {'model': 'ailocal-cyber'}, {'cwd': str(self.root)}):
            await self.refused('SESSION_MISMATCH', resume=sid, **({'model': None} | changed))
        sessions = self.state / 'sessions'
        self.assertEqual(((sessions / f'{sid}.json').stat().st_mode & 0o777, sessions.stat().st_mode & 0o777), (0o600, 0o700))
        self.assertFalse({'task', 'done'} & set(re.findall(r'\w+', json.dumps(S.load_binding(sid)))))   # no prompt or reply stored
        self.app = S.build_mcp()   # a new server instance resumes the same binding
        self.assertEqual((await self.call(resume=sid, model=None))['session_id'], sid)
        self.ceilings['codex'] = 'text'
        await self.refused('MODE_NOT_PERMITTED', resume=sid, model=None)
        self.ceilings['codex'] = 'edit'
        for target, key, value in ((self.guide, 'context_input', 8000), (self.guide, 'model', 'replaced'),
                                   (self.guide, 'source', {'sha256': 'f' * 64}), (self.guide, 'input_modalities', ['text']),
                                   (self.guides, 'tier', '128gb'), (self.guides, 'models', {})):
            old, target[key] = target[key], value
            await self.refused('SESSION_POLICY_CHANGED', resume=sid, model=None)
            target[key] = old

    async def test_failed_empty_unknown_and_unsaved_sessions(self):
        for agent in S.AGENTS:
            sid = str(uuid.uuid4())
            with patch.object(S, 'delegate_' + agent, side_effect=S.CompletedWithoutReply(agent, sid)), self.assertRaises(ToolError) as caught:
                await self.call(agent=agent, mode='edit', cwd=str(self.work))
            self.assertTrue('DELEGATE_EMPTY:' in str(caught.exception) and 'resume=' + sid in str(caught.exception))
            self.assertEqual(S.load_binding(sid)['agent'], agent)   # an empty reply stays an error with a bound resume
            self.assertEqual((await self.call(resume=sid, model=None))['session_id'], sid)
            with patch.object(S, 'delegate_' + agent, side_effect=S.DelegateError('DELEGATE_FAILED: fixture')), \
                    self.assertRaises(ToolError) as caught:
                await self.call(agent=agent, mode='edit', cwd=str(self.work))
            self.assertNotIn('resume=', str(caught.exception))   # a failed worker creates no recoverable authority
        with patch.object(S, 'delegate_codex', side_effect=S.CompletedWithoutReply('codex', str(uuid.uuid4()))), \
                patch.object(S.os, 'replace', side_effect=OSError('fixture')), self.assertRaises(ToolError) as caught:
            await self.call(agent='codex', mode='edit', cwd=str(self.work))
        self.assertTrue('SESSION_BINDING_FAILED:' in str(caught.exception) and 'resume=' not in str(caught.exception))
        with patch.object(S.os, 'replace', side_effect=OSError('fixture')), self.assertRaisesRegex(ToolError, 'SESSION_BINDING_FAILED'):
            await self.call()
        self.assertEqual(list((self.state / 'sessions').glob('.binding-*')), [])
        await self.refused('SESSION_NOT_MANAGED', resume=str(uuid.uuid4()))
        await self.refused('INVALID_SESSION', resume='../session')
        sid = (await self.call())['session_id']
        with S.session_lock(sid):
            await self.refused('SESSION_BUSY', resume=sid)
        path = self.state / 'sessions' / f'{sid}.json'
        path.write_text('{}')
        await self.refused('SESSION_INVALID', resume=sid)
        path.unlink(); path.symlink_to(self.root / 'missing')
        await self.refused('SESSION_INVALID', resume=sid)

    async def test_images_are_validated_private_copies_without_tool_escalation(self):
        source = self.work / 'picture with spaces.png'; data = png(); source.write_bytes(data)
        first = await self.call(cwd=str(self.work), image_paths=[str(source)])
        run = self.runs[-1]
        self.assertEqual((run['mode'], run['cwd'], run['images'][0]['data']), ('text', str(self.state / 'empty'), data))
        self.assertFalse(Path(run['images'][0]['path']).exists(), 'attachment copy survived completion')
        self.assertEqual((first['capabilities']['images_supplied'], list((self.state / 'attachments').iterdir())), (1, []))
        async with S.prepared_images([str(source)], str(self.work), self.guide) as images:
            source.write_bytes(b'replaced')   # the owned copy, not the replaced source, is sent
            owned = Path(images[0]['path'])
            self.assertEqual((images[0]['data'], owned.read_bytes()), (data, data))
            self.assertEqual((owned.stat().st_mode & 0o777, owned.parent.stat().st_mode & 0o777), (0o600, 0o700))
        source.write_bytes(data)
        jpeg = self.work / 'input.jpg'
        subprocess.run(['/usr/bin/sips', '-s', 'format', 'jpeg', str(source), '--out', str(jpeg)], check=True, capture_output=True)
        async with S.prepared_images([str(jpeg)], str(self.work), self.guide) as images:
            self.assertEqual(images[0]['mime_type'], 'image/jpeg')
        bad, huge, wide, outside, key = (self.work / 'bad.png', self.work / 'huge.png', self.work / 'wide.png',
                                         self.root / 'outside.png', self.work / 'key.png')
        with huge.open('wb') as handle:
            handle.truncate(S.IMAGE_LIMIT + 1)
        wide.write_bytes(png(width=S.IMAGE_SIDE + 1, height=1))
        outside.write_bytes(data); key.write_bytes(data); os.link(key, self.work / 'hard.png')
        (self.work / 'escape.png').symlink_to(outside)
        (self.work / 'escape').symlink_to(self.root, target_is_directory=True)
        os.environ['LOCAL_AGENT_DENY_READ'] = json.dumps([str(key)])
        cases = [('IMAGE_CWD_REQUIRED', None, [source]), ('IMAGE_LIMIT', self.work, [source] * 5),
                 ('IMAGE_LIMIT', self.work, [huge]), ('IMAGE_LIMIT', self.work, [wide])]
        cases += [('IMAGE_PATH_INVALID', self.work, [p]) for p in (outside, self.work / 'escape.png', self.work / 'escape/outside.png',
                                                                   key, self.work / 'hard.png', self.work / 'missing')]
        for code, cwd, paths in cases:
            await self.refused(code, cwd=cwd and str(cwd), image_paths=[str(p) for p in paths])
        for content in (b'not an image', b'\x89PNG\r\n\x1a\ninvalid', png(broken=True)):
            bad.write_bytes(content)
            await self.refused('IMAGE_INVALID', cwd=str(self.work), image_paths=[str(bad)])
        self.guide['input_modalities'] = ['text']
        await self.refused('IMAGES_UNSUPPORTED', cwd=str(self.work), image_paths=[str(source)])
        self.guide['input_modalities'] = ['text', 'image']
        for failure in (S.DelegateError('DELEGATE_FAILED: fixture'), asyncio.CancelledError()):
            with patch.object(S, 'delegate_claude', side_effect=failure), self.assertRaises((ToolError, asyncio.CancelledError)):
                await self.call(cwd=str(self.work), image_paths=[str(source)])
        self.assertEqual(list((self.state / 'attachments').iterdir()), [])   # cleaned after failure and cancellation

    async def test_real_claude_image_wire_on_fresh_and_bound_resume(self):
        """Real installed CLI, scripted endpoint: transport, not model quality."""
        if not shutil.which('claude'):
            self.skipTest('claude not installed')
        self.workers[0].__exit__(None, None, None)
        model = ScriptedModel([])
        self.addCleanup(model.close)
        os.environ['LOCAL_AGENT_BASE_URL'] = model.url
        source = self.work / 'attachment.png'; source.write_bytes(png())
        first = await self.call(cwd=str(self.work), image_paths=[str(source)])
        second = png((20, 40, 230)); source.write_bytes(second)
        again = await self.call(resume=first['session_id'], model=None, image_paths=[str(source)])
        self.assertEqual(again['session_id'], first['session_id'])
        sent = [b['source']['data'] for m in model.bodies[-1]['messages'] if isinstance(m.get('content'), list)
                for b in m['content'] if b.get('type') == 'image']
        self.assertEqual(base64.b64decode(sent[-1]), second)
        self.assertTrue(all(not body.get('tools') for body in model.bodies))


if __name__ == '__main__':
    unittest.main()
