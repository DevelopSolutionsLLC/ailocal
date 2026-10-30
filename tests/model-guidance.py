#!/usr/bin/env python3
"""Profile knowledge follows exact backends and native model selection."""
import asyncio
import contextlib
import io
import json
import os
import re
import shlex
import shutil
from pathlib import Path
import subprocess
import http.server
import threading
import sys
import tempfile
import unittest
import uuid
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'src'))
from ailocal import guidance, policy, generation, mcp

RES = ROOT / 'src/ailocal/resources'


class GuidanceTests(unittest.TestCase):
    def test_each_tier_has_exact_backend_knowledge(self):
        for tier in policy.TIERS:
            guides = policy.model_guides(tier, RES)
            self.assertEqual(set(guides['models']), {
                'ailocal-' + r for r, cfg in policy.profile_summary(tier, RES)['roles'].items()
                if cfg['enabled']})
            for alias, guide in guides['models'].items():
                role = policy.resolve_role(tier, alias.removeprefix('ailocal-'), RES)
                self.assertEqual(guide['model'], role['model'])
                self.assertEqual(guide['context_input'], role['context_input'])
                self.assertEqual(guide['sampling']['temperature'], role['temperature'])
                if role.get('draft_num_predict') is not None:
                    self.assertEqual(guide['sampling']['draft_num_predict'], role['draft_num_predict'])
                self.assertTrue(guide['sources'])
                self.assertTrue(guide['evidence'].startswith('['))

    def test_changed_backend_never_inherits_another_guide(self):
        data = policy.load_profile('16gb', RES)
        data['cyber']['active'] = 'operator-custom-model'
        guide = policy.guidance_for(data, 'cyber')
        self.assertIn('No model-specific guidance', guide['summary'])
        self.assertEqual(guide['sources'], [])

    def test_invalid_guidance_fails_closed(self):
        data = policy.load_profile('16gb', RES)
        data['model_guidance'][data['implementation']['active']]['prompting'] = 'wrong shape'
        with self.assertRaises(policy.ProfileError):
            policy._validate_guidance(data)

    def test_native_hook_uses_switched_model_not_launch_default(self):
        with patch.dict(os.environ, {'AILOCAL_CONFIG': str(RES)}), \
                patch.object(sys, 'stdin', io.StringIO(json.dumps({
                    'hook_event_name': 'PostModelSwitch', 'to_model': 'ailocal-cyber'}))), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(guidance.main(['--tier', '16gb', '--hook']), 0)
        self.assertIn(policy.resolve_role('16gb', 'cyber', RES)['model'], out.getvalue())
        self.assertNotIn('Lead architect', out.getvalue())

    def _hook(self, event: dict, tier: str = '16gb'):
        with patch.dict(os.environ, {'AILOCAL_CONFIG': str(RES)}), \
                patch.object(sys, 'stdin', io.StringIO(json.dumps(event))), \
                contextlib.redirect_stdout(io.StringIO()) as out, \
                contextlib.redirect_stderr(io.StringIO()) as err:
            rc = guidance.main(['--tier', tier, '--hook'])
        return rc, out.getvalue(), err.getvalue()

    def test_hook_directory_updates_without_stale_launch_context(self):
        for cwd, previous in (('/work/first', '/work/second'), ('/work/second', '/work/first')):
            rc, text, _ = self._hook({'model': 'ailocal-cyber', 'cwd': cwd})
            self.assertEqual(rc, 0)
            self.assertIn(json.dumps(cwd), text)
            self.assertNotIn(previous, text)
        rc, text, _ = self._hook({'model': 'ailocal-cyber', 'cwd': 'relative/path'})
        self.assertEqual(rc, 0)
        self.assertNotIn('Task working directory:', text)

    def test_hook_resolves_the_observed_gateway_router_id_on_every_tier(self):
        # [REAL] 2026-10-26, Claude Code 2.1.310: the PostModelSwitch hook ran
        # as `PostModelSwitch:claude-router-61696c6f63616c2d6379626572` after
        # selecting ailocal-cyber from the gateway-discovered rows, and the
        # hook exited 2 with MODEL_GUIDANCE_UNKNOWN_MODEL. The hex is the id.
        # Every tier owns a different cyber backend and guide, so each must
        # resolve the same payload to its own entry.
        router = 'claude-router-' + 'ailocal-cyber'.encode().hex()
        self.assertEqual(router, 'claude-router-61696c6f63616c2d6379626572')
        for tier in policy.TIERS:
            with self.subTest(tier=tier):
                cyber = policy.resolve_role(tier, 'cyber', RES)
                self.assertTrue(cyber['enabled'], f'{tier}: cyber is not enabled')
                guide = policy.guidance_for(policy.load_profile(tier, RES), 'cyber')
                self.assertNotIn('No model-specific guidance', guide['summary'],
                                 f'{tier}: cyber backend {cyber["model"]} has no guide')
                rc, out, err = self._hook({'hook_event_name': 'PostModelSwitch',
                                           'to_model': router, 'requested_model': router,
                                           'source': 'picker'}, tier)
                self.assertEqual((rc, err), (0, ''))
                self.assertIn(f'Local model guide: ailocal-cyber ({cyber["model"]})', out)
                self.assertIn(f'Profile limits: {cyber["context_input"]} input tokens, '
                              f'{cyber["max_output"]} maximum output tokens', out)
                self.assertNotIn('Lead architect', out)

    def test_hook_falls_back_to_the_documented_requested_model(self):
        # hooks#premodelswitch-input: `requested_model` is the alias or id the
        # request named. It is consulted when `to_model` resolves to nothing.
        rc, out, _ = self._hook({'hook_event_name': 'PostModelSwitch',
                                 'to_model': 'claude-router-zz', 'requested_model': 'opus'})
        self.assertEqual(rc, 0)
        self.assertIn('ailocal-architecture', out)
        self.assertEqual(guidance.hook_model_candidates(
            {'to_model': 'claude-router-zz', 'requested_model': 'opus'}),
            ['claude-router-zz', 'opus'])

    def test_hook_still_refuses_an_unknown_model(self):
        rc, _, err = self._hook({'hook_event_name': 'PostModelSwitch',
                                 'to_model': 'claude-router-' + 'not-served'.encode().hex()})
        self.assertEqual(rc, 2)
        self.assertIn('MODEL_GUIDANCE_UNKNOWN_MODEL', err)

    def test_missing_model_is_explicit_not_wrong_default(self):
        with patch.dict(os.environ, {'AILOCAL_CONFIG': str(RES)}), \
                patch.object(sys, 'stdin', io.StringIO('{}')), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(guidance.main(['--tier', '16gb', '--hook']), 0)
        self.assertIn('not supplied', out.getvalue())
        self.assertIn('apply only the entry matching', out.getvalue())
        cyber_guide = policy.model_guides('16gb', RES)['models']['ailocal-cyber']
        self.assertIn(cyber_guide['summary'], out.getvalue())
        self.assertIn(cyber_guide['model'], out.getvalue())
        stock_guide = policy.model_guides('16gb', RES)['models']['ailocal-implementation']
        self.assertEqual(out.getvalue().count(stock_guide['evidence']), 1)

    def test_generators_share_guidance_and_preserve_external_hooks(self):
        models = generation.load_models_yaml(RES / 'profiles/16gb.toml')
        self.assertNotIn('model_guidance', models)
        self.assertIn('completion', models)
        with tempfile.TemporaryDirectory() as disabled_root:
            path = Path(disabled_root) / '16gb.toml'
            path.write_text((RES / 'profiles/16gb.toml').read_text().replace(
                '[cyber]\n', '[cyber]\nenabled = false\n', 1))
            selected = generation.load_models_yaml(path)
            self.assertNotIn('cyber', selected)
            self.assertIn('implementation', selected)
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / 'settings.json'
            external = {'hooks': [{'type': 'command', 'command': 'external-hook'}]}
            target.write_text(json.dumps({'hooks': {'SessionStart': [external]}}))
            with patch.object(generation, 'CLAUDE_SETTINGS', target), \
                    patch.object(generation, 'stage') as stage:
                generation.regen_claude_settings(models, policy.load_client_policy(RES))
                data = json.loads(stage.call_args.args[1])
                self.assertIn(external, data['hooks']['SessionStart'])
                self.assertIn('PostModelSwitch', data['hooks'])
                target.write_text(json.dumps(data))
                generation.regen_claude_settings(models, policy.load_client_policy(RES))
                self.assertEqual(data, json.loads(stage.call_args.args[1]))
                # #39: auto memory's storage path in the system prompt is taken
                # for the working directory. Off by default; an explicit choice wins.
                self.assertIs(data['autoMemoryEnabled'], False)
                target.write_text(json.dumps(dict(data, autoMemoryEnabled=True)))
                generation.regen_claude_settings(models, policy.load_client_policy(RES))
                self.assertIs(json.loads(stage.call_args.args[1])['autoMemoryEnabled'], True)
            with patch.object(generation, 'stage') as stage:
                generation.regen_catalog(models)
                entries = json.loads(stage.call_args.args[1])['models']
                cyber = next(x for x in entries if x['slug'] == 'ailocal-cyber')
                self.assertIn(models['cyber']['guidance']['evidence'], cyber['base_instructions'])
                self.assertIn('Configured inference preset:', cyber['base_instructions'])
                self.assertIn('Current MTP draft tokens: 1.', cyber['base_instructions'])
                self.assertFalse(cyber['supports_parallel_tool_calls'])
                fim = next(x for x in entries if x['slug'] == 'ailocal-completion')
                self.assertFalse(fim['supports_parallel_tool_calls'])
                self.assertIn('supports_function_calling: false', generation.gen_role_block('completion', models['completion']))
                review = next(x for x in entries if x['slug'] == 'ailocal-review')
                self.assertNotIn('You review; you do not fix', review['base_instructions'])

    def test_real_sdk_exposes_readonly_guide_and_rejects_unknown_alias(self):
        py = policy.state_root() / 'local-agent/.venv/bin/python'
        # `ailocal install --yes` leaves this runtime to `ailocal clients`; the
        # gate refuses before any suite runs when it is absent (gate.py).
        self.assertTrue(py.is_file(), f'local-agent runtime missing at {py}; '
                        'run: ailocal clients claude codex')
        tmp = self.enterContext(tempfile.TemporaryDirectory())
        (Path(tmp) / 'active-profile').write_text('16gb\n')
        env = dict(os.environ, AILOCAL_CONFIG=str(RES), AILOCAL_STATE=tmp,
                   LOCAL_AGENT_STATE=tmp)
        script = '''
import asyncio, json, os, sys
sys.path.insert(0, sys.argv[1])
from server import build_mcp
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
async def run():
    params = StdioServerParameters(command=sys.executable, args=[sys.argv[1]+'/server.py'], env=dict(os.environ))
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        init = await session.initialize()
        assert 'call model_guide' in init.instructions
        tool = next(t for t in (await session.list_tools()).tools if t.name == 'model_guide')
        assert tool.annotations.read_only_hint
        result = await session.call_tool('model_guide', {'model':'ailocal-cyber'})
        assert not result.is_error, result
        data = result.structured_content
        assert data['tier'] == '16gb'
        guide = data['models']['ailocal-cyber']
        assert guide['model'] == 'qwen3.5:4b-unrestricted-mtp-q4_K_M'
        assert '[REAL]' in guide['evidence'] and '[APPROX]' in guide['evidence']
        failure = await session.call_tool('model_guide', {'model':'not-a-model'})
        assert failure.is_error
        loose = await session.call_tool('delegate', {'prompt':'x', 'tools': True})
        assert loose.is_error and 'INVALID_CWD' in loose.content[0].text, loose
        os.environ['LOCAL_AGENT_DEPTH'] = '1'
    params = StdioServerParameters(command=sys.executable, args=[sys.argv[1]+'/server.py'], env=dict(os.environ))
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        nested = await session.call_tool('delegate', {'prompt':'x'})
        assert nested.is_error and 'RECURSIVE_DELEGATION' in nested.content[0].text, nested
    os.environ.pop('LOCAL_AGENT_DEPTH')
    os.environ['LOCAL_AGENT_CALLER'] = 'local'
    params = StdioServerParameters(command=sys.executable, args=[sys.argv[1]+'/server.py'], env=dict(os.environ))
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        guide = await session.call_tool('model_guide', {'model':'ailocal-cyber'})
        assert not guide.is_error, guide
        for agent in ('claude', 'codex'):
            for tools in (False, True):
                blocked = await session.call_tool('delegate', {'prompt':'x', 'agent':agent, 'tools':tools})
                assert blocked.is_error and 'LOCAL_DELEGATION_DISABLED' in blocked.content[0].text, blocked
asyncio.run(run())
'''
        r = subprocess.run([str(py), '-c', script, str(RES / 'integrations/local-agent')],
                           env=env, capture_output=True, text=True, timeout=25)
        self.assertEqual(r.returncode, 0, r.stderr[-1800:])



def _server():
    sys.path.insert(0, str(RES / 'integrations/local-agent'))
    try:
        import server
    finally:
        sys.path.pop(0)
    return server


class DelegationContractTests(unittest.TestCase):
    """Argument and environment contract of the local-agent boundary; no CLI runs."""

    def setUp(self):
        self.S = _server()

    def test_child_env_drops_hosted_routing_and_applies_local_launch(self):
        hosted = {'ANTHROPIC_AUTH_TOKEN': 'hosted', 'CLAUDE_CODE_USE_BEDROCK': '1',
                  'CLAUDE_CODE_OAUTH_TOKEN': 'x', 'ANTHROPIC_MODEL': 'claude-opus',
                  'OPENAI_ORG_ID': 'org', 'CODEX_API_KEY': 'k', 'CLAUDECODE': '1',
                  'LOCAL_AGENT_BASE_URL': 'http://127.0.0.1:4000', 'LOCAL_AGENT_KEY': 'local',
                  'LOCAL_AGENT_CLAUDE_ENV': json.dumps({'ANTHROPIC_DEFAULT_HAIKU_MODEL': 'ailocal-fast',
                                                        'CLAUDE_CODE_DISABLE_1M_CONTEXT': '1'}),
                  'LOCAL_AGENT_CLAUDE_HOME': '/local/claude',
                  # What a hosted Claude Code session exports to its MCP servers.
                  'CLAUDE_CODE_MESSAGING_TOKEN': 't', 'CLAUDE_CODE_MESSAGING_SOCKET': '/s',
                  'CLAUDE_CODE_SESSION_ID': 'hosted', 'CLAUDE_CODE_ENTRYPOINT': 'cli',
                  'CLAUDE_PROJECT_DIR': '/hosted/repo', 'CLAUDE_CONFIG_DIR': '/hosted/root',
                  'SSH_AUTH_SOCK': '/agent', 'PATH': '/usr/bin'}
        with patch.dict(os.environ, hosted, clear=True):
            claude, codex = self.S._child_env('claude'), self.S._child_env('codex')
        for env in (claude, codex):
            self.assertFalse({'ANTHROPIC_AUTH_TOKEN', 'CLAUDE_CODE_USE_BEDROCK', 'CLAUDE_CODE_OAUTH_TOKEN',
                              'ANTHROPIC_MODEL', 'OPENAI_ORG_ID', 'CODEX_API_KEY', 'CLAUDECODE',
                              'CLAUDE_CODE_MESSAGING_TOKEN', 'CLAUDE_CODE_MESSAGING_SOCKET',
                              'CLAUDE_CODE_SESSION_ID', 'CLAUDE_CODE_ENTRYPOINT', 'CLAUDE_PROJECT_DIR',
                              'SSH_AUTH_SOCK'} & set(env))
        # The child's own CLAUDE_* values survive the filter; the hosted root does not.
        self.assertEqual((claude['CLAUDE_CONFIG_DIR'], claude['CLAUDE_CODE_DISABLE_1M_CONTEXT']),
                         ('/local/claude', '1'))
        self.assertNotIn('CLAUDE_CONFIG_DIR', codex)
        for env in (claude, codex):
            self.assertEqual(env[self.S.DEPTH_VAR], '1')
            self.assertEqual(env['PATH'], '/usr/bin')
            self.assertEqual([k for k in env if k.startswith('LOCAL_AGENT_') and k != self.S.DEPTH_VAR], [])
        self.assertEqual((claude['ANTHROPIC_BASE_URL'], claude['ANTHROPIC_API_KEY'],
                          claude['ANTHROPIC_DEFAULT_HAIKU_MODEL']),
                         ('http://127.0.0.1:4000', 'local', 'ailocal-fast'))
        self.assertEqual((codex['OPENAI_BASE_URL'], codex['OPENAI_API_KEY']),
                         ('http://127.0.0.1:4000/v1', 'local'))

    def test_launcher_projects_claude_local_slots(self):
        env = mcp.claude_launch_environment()
        slots = policy.load_client_policy()['claude']['slots']
        for slot, cap in slots.items():
            self.assertEqual(env[generation.SLOT_ENV[slot]], 'ailocal-' + cap)

    GUIDE = {'context_input': 253952, 'max_output': 8192}

    def test_default_mode_cannot_write_or_recurse(self):
        def settings(argv):
            return json.loads(argv[argv.index('--settings') + 1])
        argv = self.S._claude_argv('p', 'm', 'text', None, 'sid', self.S.claude_settings('text', self.GUIDE))
        self.assertEqual(argv[argv.index('--tools') + 1], '')
        self.assertIn('--strict-mcp-config', argv)
        self.assertNotIn('--mcp-config', argv)
        self.assertNotIn('--dangerously-skip-permissions', argv)
        full = self.S._claude_argv('p', 'm', 'edit', None, 'sid', self.S.claude_settings('edit', self.GUIDE))
        self.assertEqual(full[full.index('--disallowedTools') + 1], 'mcp__agent__delegate')
        # Below full, a project's settings, hooks and .mcp.json are not read.
        for each in (argv, full):
            self.assertEqual(each[each.index('--setting-sources') + 1], 'user')
        # Never lift the working-directory boundary; never widen it.
        self.assertNotIn('--dangerously-skip-permissions', full)
        self.assertNotIn('--add-dir', full)
        # [REAL] Claude 2.1.308 forces a scrubbed run to default mode, so edits
        # are granted by a rule for cwd, not by acceptEdits.
        self.assertNotIn('--permission-mode', full)
        self.assertNotIn('--allowedTools', full)   # no cwd, no grant
        tmp = os.path.realpath(tempfile.gettempdir())
        granted = self.S._claude_argv('p', 'm', 'edit', None, 'sid', self.S.claude_settings('edit', self.GUIDE), tmp)
        self.assertEqual(granted[granted.index('--allowedTools') + 1], f'Edit(/{tmp}/**)')
        sandbox = settings(full)['sandbox']
        self.assertEqual((sandbox['enabled'], sandbox['failIfUnavailable'], sandbox['allowUnsandboxedCommands']),
                         (True, True, False))
        # One --settings carries everything: a second would be a precedence question.
        for each in (argv, full):
            self.assertEqual(each.count('--settings'), 1)
            self.assertEqual(settings(each)['env']['CLAUDE_CODE_SUBPROCESS_ENV_SCRUB'], '1')
        for tools in (False, True):
            for resume in (None, 'sid'):
                codex = self.S._codex_argv('p', 'm', 'edit' if tools else 'text', '/work', resume, '/out')
                self.assertIn('mcp_servers.agent.disabled_tools=["delegate"]', codex)
                self.assertIn('shell_environment_policy.ignore_default_excludes=false', codex)
                self.assertEqual(codex[-2:], ['--', 'p'] if resume is None else ['sid', 'p'])
                base = ':workspace' if tools else ':read-only'
                # A permission profile, never --sandbox or sandbox_mode: either
                # selects the older settings and drops the profile's deny rules.
                self.assertIn(f'default_permissions="{self.S.CODEX_PROFILE}"', codex)
                self.assertTrue(any(a.startswith(f'permissions.{self.S.CODEX_PROFILE}={{extends="{base}"')
                                    for a in codex), codex)
                self.assertNotIn('--sandbox', codex)
                self.assertFalse([a for a in codex if a.startswith('sandbox_mode')])
                if resume:
                    # [REAL] Codex 0.165.0 `exec resume` has no -C: cwd is the process cwd.
                    self.assertEqual(codex[:3], ['codex', 'exec', 'resume'])
                    self.assertNotIn('-C', codex)
                    self.assertEqual(codex[-3], '--')
                else:
                    # Every fresh run names its directory, text included.
                    self.assertEqual(codex[codex.index('-C') + 1], '/work')

    def test_key_files_are_denied_to_the_child(self):
        paths = ['/home/u/.config/ailocal/.env.local', '/home/u/.local/state/ailocal/env']
        with patch.dict(os.environ, {'LOCAL_AGENT_DENY_READ': json.dumps(paths + ['relative/x'])}):
            text, edit = self.S.claude_settings('text', self.GUIDE), self.S.claude_settings('edit', self.GUIDE)
        # Read runs outside the sandbox: an absolute permission path needs `//`.
        for settings in (text, edit):
            self.assertEqual(settings['permissions']['deny'],
                             ['Read(//home/u/.config/ailocal/.env.local)', 'Read(//home/u/.local/state/ailocal/env)'])
        self.assertEqual(edit['sandbox']['credentials']['files'],
                         [{'path': p, 'mode': 'deny'} for p in paths])
        with patch.object(mcp.runtime, 'env_value', return_value='k'):
            launched = json.loads(mcp.agent_environment()['LOCAL_AGENT_DENY_READ'])
        from ailocal import environment
        self.assertIn(str(environment.user_file()), launched)
        self.assertIn(str(environment.generated_file()), launched)

    def test_selected_alias_sets_claude_limits(self):
        env = self.S.claude_settings('text', self.GUIDE)['env']
        self.assertEqual((env['CLAUDE_CODE_MAX_OUTPUT_TOKENS'], env['CLAUDE_CODE_MAX_CONTEXT_TOKENS']),
                         ('8192', '253952'))
        guides = {'models': {'ailocal-fast': self.GUIDE, 'ailocal-completion': self.GUIDE}}
        self.assertIs(self.S.selected_guide(guides, 'ailocal-fast'), self.GUIDE)
        for model, code in (('ailocal-completion', 'MODEL_NOT_DELEGABLE'), ('ailocal-coder', 'UNKNOWN_MODEL')):
            with self.assertRaises(self.S.DelegateError) as caught:
                self.S.selected_guide(guides, model)
            self.assertTrue(str(caught.exception).startswith(code))

    def test_resume_flags_exist_in_installed_codex(self):
        """Parser contract against the installed CLI's own help; no inference."""
        if not shutil.which('codex'):
            self.skipTest('codex not installed')
        fresh = subprocess.run(['codex', 'exec', '--help'], capture_output=True, text=True, timeout=30).stdout
        again = subprocess.run(['codex', 'exec', 'resume', '--help'], capture_output=True, text=True, timeout=30).stdout
        for help_text, resume in ((fresh, None), (again, 'sid')):
            for mode in self.S.CODEX_PLAN:
                flags = [a for a in self.S._codex_argv('p', 'm', mode, '/w', resume, '/o')
                         if a.startswith('-') and a != '--']
                for flag in flags:
                    self.assertRegex(help_text, rf'(?m)^\s+(-\w, )?{re.escape(flag)}\b|, {re.escape(flag)}\b',
                                     f'{flag} not accepted by `codex exec{" resume" if resume else ""}`')

    def test_mode_resolution_and_operator_ceiling(self):
        R = self.S.resolve_mode
        # The legacy flag keeps its meaning; an explicit mode wins when tools agrees or is omitted.
        self.assertEqual([R(None, None), R(None, False), R(None, True)], ['text', 'text', 'edit'])
        self.assertEqual([R('text', False), R('edit', True), R('full', True), R('full', None),
                          R('research', None)], ['text', 'edit', 'full', 'full', 'research'])
        for mode, tools, code in (('text', True, 'CONFLICTING_MODE'), ('edit', False, 'CONFLICTING_MODE'),
                                  ('full', False, 'CONFLICTING_MODE'), ('research', True, 'CONFLICTING_MODE'),
                                  ('research', False, 'CONFLICTING_MODE'), ('bogus', None, 'UNKNOWN_MODE')):
            with self.assertRaises(self.S.DelegateError) as caught:
                R(mode, tools)
            self.assertTrue(str(caught.exception).startswith(code), (mode, tools))
        ceilings = {'claude': 'full', 'codex': 'edit'}
        self.S.permit_mode('full', 'claude', ceilings)
        self.S.permit_mode('edit', 'codex', ceilings)
        self.S.permit_mode('research', 'claude', {'claude': 'research'})
        self.S.permit_mode('research', 'claude', {})
        self.S.supported_mode('research', 'claude')
        with self.assertRaises(self.S.DelegateError) as caught:
            self.S.supported_mode('research', 'codex')
        self.assertTrue(str(caught.exception).startswith('UNSUPPORTED_MODE'))
        for mode, agent, limits in (('full', 'codex', ceilings), ('edit', 'claude', {'claude': 'text'}),
                                    ('research', 'claude', {'claude': 'text'}),
                                    ('edit', 'claude', {'claude': 'research'}),
                                    ('edit', 'claude', {'claude': 'bogus'}), ('full', 'claude', {})):
            with self.assertRaises(self.S.DelegateError) as caught:
                self.S.permit_mode(mode, agent, limits)
            self.assertTrue(str(caught.exception).startswith('MODE_NOT_PERMITTED'), (mode, agent, limits))

    def test_policy_ceiling_defaults_to_edit_and_fails_closed(self):
        tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (tmp / 'profiles').mkdir()
        shipped = (RES / 'profiles/clients.toml').read_text()
        with patch.dict(os.environ, {'AILOCAL_CONFIG': str(tmp)}):
            (tmp / 'profiles/clients.toml').write_text(shipped)
            self.assertEqual(policy.delegate_ceilings(), {'claude': 'edit', 'codex': 'edit'})
            (tmp / 'profiles/clients.toml').write_text(
                shipped.replace('delegate_max_mode = "edit"', 'delegate_max_mode = "full"', 1))
            self.assertEqual(policy.delegate_ceilings(), {'claude': 'full', 'codex': 'edit'})
            (tmp / 'profiles/clients.toml').write_text(re.sub(r'(?m)^delegate_max_mode = "edit"\n', '', shipped))
            self.assertEqual(policy.delegate_ceilings(), {'claude': 'edit', 'codex': 'edit'})
            (tmp / 'profiles/clients.toml').write_text(
                shipped.replace('delegate_max_mode = "edit"', 'delegate_max_mode = "yolo"', 1))
            with self.assertRaises(policy.ProfileError):
                policy.delegate_ceilings()

    def test_instruction_budget_defaults_and_fails_closed(self):
        tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (tmp / 'profiles').mkdir()
        shipped = (RES / 'profiles/clients.toml').read_text()
        with patch.dict(os.environ, {'AILOCAL_CONFIG': str(tmp)}):
            (tmp / 'profiles/clients.toml').write_text(shipped)
            self.assertEqual(policy.instruction_budgets(), {'claude': 4096, 'codex': 4096})
            # An operator copy from before the key existed keeps the measured default.
            (tmp / 'profiles/clients.toml').write_text(re.sub(r'(?m)^instruction_max_bytes = 4096\n', '', shipped))
            self.assertEqual(policy.instruction_budgets(), {'claude': 4096, 'codex': 4096})
            for bad in ('0', '"4k"', 'true'):
                (tmp / 'profiles/clients.toml').write_text(
                    shipped.replace('instruction_max_bytes = 4096', f'instruction_max_bytes = {bad}', 1))
                with self.assertRaises(policy.ProfileError, msg=bad):
                    policy.instruction_budgets()

    def test_denied_calls_are_not_retried_and_are_reported(self):
        # #39: a delegated local model re-issued one denied Read 228 times, and
        # replied DONE after denied writes while the run still reported success.
        # research also needs a provisioned research server; its argv shares this path.
        for mode in ('text', 'edit', 'full'):
            argv = self.S._claude_argv('p', 'm', mode, None, 'sid', self.S.claude_settings(mode, self.GUIDE))
            self.assertEqual(argv[argv.index('--permission-prompts') + 1], 'none', mode)
        images = self.S._claude_argv('p', 'm', 'edit', None, 'sid', self.S.claude_settings('edit', self.GUIDE),
                                     images=True)
        self.assertNotIn('p', images[:3])
        self.assertIn('--permission-prompts', images)
        payload = {'permission_denials': [
            {'tool_name': 'Write', 'tool_input': {'file_path': '/x/clamp.py', 'content': 'secret'}},
            {'tool_name': 'Bash', 'tool_input': {'command': 'find / ' + 'a' * 400}},
            'not-a-dict']}
        self.assertEqual(self.S._permission_denials(payload),
                         [{'tool': 'Write', 'target': '/x/clamp.py'},
                          {'tool': 'Bash', 'target': ('find / ' + 'a' * 400)[:200]}])
        self.assertEqual(self.S._permission_denials({}), [])

    def test_full_mode_lifts_restrictions_but_not_routing(self):
        with patch.dict(os.environ, {'LOCAL_AGENT_DENY_READ': json.dumps(['/k/.env.local'])}):
            full = self.S.claude_settings('full', self.GUIDE)
        self.assertEqual(full['sandbox'], {'enabled': False})
        self.assertNotIn('permissions', full)
        self.assertNotIn('CLAUDE_CODE_SUBPROCESS_ENV_SCRUB', full['env'])
        self.assertEqual(full['env']['CLAUDE_CODE_MAX_OUTPUT_TOKENS'], '8192')
        argv = self.S._claude_argv('p', 'm', 'full', None, 'sid', full)
        self.assertEqual(argv[argv.index('--permission-mode') + 1], 'bypassPermissions')
        self.assertEqual(argv[argv.index('--disallowedTools') + 1], 'mcp__agent__delegate')
        self.assertNotIn('--setting-sources', argv)
        for resume in (None, 'sid'):
            codex = self.S._codex_argv('p', 'm', 'full', '/work', resume, '/out')
            self.assertIn('default_permissions=":danger-full-access"', codex)
            self.assertNotIn('shell_environment_policy.ignore_default_excludes=false', codex)
            self.assertIn('mcp_servers.agent.disabled_tools=["delegate"]', codex)
        hosted = {'LOCAL_AGENT_BASE_URL': 'http://127.0.0.1:4000', 'LOCAL_AGENT_KEY': 'k',
                  'SSH_AUTH_SOCK': '/agent', 'CLAUDE_CODE_MESSAGING_TOKEN': 't', 'ANTHROPIC_AUTH_TOKEN': 'h'}
        with patch.dict(os.environ, hosted, clear=True):
            for agent in ('claude', 'codex'):
                env = self.S._child_env(agent, 'full')
                self.assertEqual(env['SSH_AUTH_SOCK'], '/agent')
                self.assertFalse({'CLAUDE_CODE_MESSAGING_TOKEN', 'ANTHROPIC_AUTH_TOKEN', 'LOCAL_AGENT_KEY'} & set(env))
                self.assertNotIn('SSH_AUTH_SOCK', self.S._child_env(agent, 'edit'))
        self.assertNotIn('cannot write outside', self.S.scoped_prompt('x', '/w', 'full'))
        self.assertIn('cannot write outside', self.S.scoped_prompt('x', '/w', 'edit'))

    def test_edit_prompt_names_the_working_directory(self):
        self.assertTrue(self.S.scoped_prompt('fix it', '/work').startswith('Working directory: /work\n'))
        self.assertTrue(self.S.scoped_prompt('fix it', '/work').endswith('\n\nfix it'))

    def test_failed_runs_are_errors_not_answers(self):
        def ran(code, out):
            async def run(argv, *a):
                return code, out.encode()
            return patch.object(self.S, '_run', side_effect=run)
        sid = '0' * 8 + '-0000-0000-0000-' + '0' * 12
        env = {'LOCAL_AGENT_BASE_URL': 'http://127.0.0.1:4000', 'LOCAL_AGENT_KEY': 'k'}
        with patch.dict(os.environ, env):
            for code, out, expected, resumable in (
                    (0, json.dumps({'is_error': True, 'subtype': 'error_max_turns', 'result': 'x'}),
                     'DELEGATE_FAILED: local Claude run error_max_turns', False),
                    (1, json.dumps({'is_error': False, 'result': 'partial', 'session_id': sid}),
                     'DELEGATE_FAILED: local Claude run did not complete', False),
                    (0, json.dumps({'result': 'done', 'session_id': 'another'}),
                     'DELEGATE_FAILED: local Claude run did not complete', False),
                    (0, json.dumps({'type': 'result', 'subtype': 'success', 'is_error': False,
                                    'result': ' ', 'session_id': sid}),
                     'DELEGATE_EMPTY: local Claude run returned no answer', True),
                    (0, json.dumps({'result': ' ', 'session_id': sid}),
                     'DELEGATE_EMPTY: local Claude run returned no answer', False),
                    (0, 'not json', 'DELEGATE_FAILED: local Claude run did not complete', False)):
                with ran(code, out), self.assertRaises(self.S.DelegateError) as caught:
                    asyncio.run(self.S.delegate_claude('p', 'm', 'text', None, sid, self.GUIDE))
                self.assertEqual(str(caught.exception), expected)
                self.assertEqual(isinstance(caught.exception, self.S.CompletedWithoutReply), resumable)
            with ran(1, json.dumps({'is_error': True, 'subtype': 'success', 'session_id': sid})), \
                    self.assertRaises(self.S.DelegateError) as caught:
                asyncio.run(self.S.delegate_claude('p', 'm', 'text', None, sid, self.GUIDE))
            self.assertEqual(str(caught.exception), 'DELEGATE_FAILED: local Claude run did not complete')
            with ran(0, json.dumps({'result': 'done', 'session_id': sid})):
                done = asyncio.run(self.S.delegate_claude('p', 'm', 'text', None, sid, self.GUIDE))
                self.assertEqual((done['output'], done['session_id']), ('done', sid))

            thread = '0199a213-81c0-7800-8aa1-bbab2a035a53'
            started = json.dumps({'type': 'thread.started', 'thread_id': thread})
            item = json.dumps({'type': 'item.completed', 'item': {'id': sid, 'text': 'x'}})

            def codex(code, events, message='last message'):
                async def run(argv, env, cwd, on_line):
                    Path(argv[argv.index('-o') + 1]).write_text(message)
                    for line in events:
                        on_line(line.encode())
                    return code, b''
                return patch.object(self.S, '_run', side_effect=run)
            completed = json.dumps({'type': 'turn.completed'})
            failed = json.dumps({'type': 'turn.failed'})
            not_done = 'DELEGATE_FAILED: local Codex run did not complete'
            for code, events, message, expected in (
                    (1, [started, completed], 'x', not_done),
                    (0, [started], 'x', not_done),
                    (0, [started, failed, completed], 'x', not_done),
                    (0, [started, completed], ' \n', 'DELEGATE_EMPTY: local Codex run returned no answer')):
                with codex(code, events, message), self.assertRaises(self.S.DelegateError) as caught:
                    asyncio.run(self.S.delegate_codex('p', 'm', 'text', None, None, self.GUIDE))
                self.assertEqual(str(caught.exception), expected)
                self.assertEqual(isinstance(caught.exception, self.S.CompletedWithoutReply),
                                 expected.startswith('DELEGATE_EMPTY:'))
            # The id is the thread.started event's, not the first UUID in the stream.
            with codex(0, [item, started, completed]):
                done = asyncio.run(self.S.delegate_codex('p', 'm', 'text', None, None, self.GUIDE))
                self.assertEqual((done['output'], done['session_id']), ('last message', thread))

    def test_research_mode_reads_and_searches_only(self):
        entry = mcp.registration()
        env = {'LOCAL_AGENT_RESEARCH_MCP': json.dumps(entry),
               'LOCAL_AGENT_DENY_READ': json.dumps(['/k/.env.local'])}
        with patch.dict(os.environ, env):
            settings = self.S.claude_settings('research', self.GUIDE)
            argv = self.S._claude_argv('p', 'm', 'research', None, 'sid', settings, '/work')
        after = lambda flag: argv[argv.index(flag) + 1]
        self.assertEqual(after('--tools'), 'Read,Grep,Glob')
        self.assertEqual(after('--setting-sources'), 'user')
        self.assertIn('--strict-mcp-config', argv)
        # Only ailocal's research server, launched as claude-local registers it:
        # no --enable-answer, so its local synthesis tool is not offered.
        self.assertEqual(json.loads(after('--mcp-config')), {'mcpServers': {'research': entry}})
        self.assertEqual(entry['args'], ['mcp', 'research'])
        self.assertEqual(after('--allowedTools'), self.S.RESEARCH_TOOL)
        self.assertEqual(after('--disallowedTools').split(','), ['Agent', 'mcp__agent__delegate'])
        self.assertNotIn('--permission-mode', argv)
        self.assertNotIn('sandbox', settings)
        self.assertTrue(settings['permissions']['blockReadsOutsideWorkingDirectories'])
        self.assertEqual(settings['permissions']['deny'], ['Read(//k/.env.local)'])
        self.assertEqual(settings['env']['CLAUDE_CODE_SUBPROCESS_ENV_SCRUB'], '1')
        with patch.dict(os.environ, {'LOCAL_AGENT_RESEARCH_MCP': ''}), \
                self.assertRaises(self.S.DelegateError) as caught:
            self.S._claude_argv('p', 'm', 'research', None, 'sid', settings)
        self.assertTrue(str(caught.exception).startswith('RESEARCH_UNAVAILABLE'))
        with patch.object(mcp.runtime, 'env_value', return_value='k'):
            self.assertEqual(json.loads(mcp.agent_environment()['LOCAL_AGENT_RESEARCH_MCP']), entry)
        prompt = self.S.research_prompt('q', None)
        self.assertIn('search snippet', prompt)
        self.assertNotIn('Working directory', prompt)
        self.assertIn('you can read only inside it', self.S.research_prompt('q', '/work'))

    def test_capabilities_are_read_from_the_argv_table(self):
        """The reported capabilities and the argv come from one row per mode."""
        env = {'LOCAL_AGENT_RESEARCH_MCP': json.dumps(mcp.registration()),
               'LOCAL_AGENT_DENY_READ': json.dumps(['/k/.env.local'])}
        with patch.dict(os.environ, env):
            for mode in self.S.CLAUDE_PLAN:
                caps = self.S.capabilities('claude', mode)
                argv = self.S._claude_argv('p', 'm', mode, None, 'sid', self.S.claude_settings(mode, self.GUIDE), '/w')
                tools = argv[argv.index('--tools') + 1] if '--tools' in argv else None
                self.assertEqual(caps['built_in_tools'], 'all' if tools is None else [t for t in tools.split(',') if t])
                loads_none = '--strict-mcp-config' in argv and '--mcp-config' not in argv
                self.assertEqual(caps['mcp_servers'] == [], loads_none, mode)
                self.assertEqual(caps['settings_sources'] == 'user', '--setting-sources' in argv, mode)
                self.assertEqual(caps['hooks'] == 'worker root', '--setting-sources' in argv, mode)
                self.assertEqual(caps['denied_reads'], [] if mode == 'full' else ['/k/.env.local'])
                self.assertEqual('file edits allowed' in caps['permission_mode'], '--allowedTools' in argv
                                 and mode == 'edit', mode)
            for mode in self.S.CODEX_PLAN:
                caps = self.S.capabilities('codex', mode)
                argv = self.S._codex_argv('p', 'm', mode, '/w', None, '/o')
                base = self.S.CODEX_PLAN[mode]['profile']
                if mode == 'full':
                    self.assertEqual(caps['sandbox'], 'off')
                    self.assertIn(f'default_permissions="{base}"', argv)
                else:
                    self.assertIn(base, caps['sandbox'])
                    self.assertTrue(any(f'extends="{base}"' in a for a in argv))
                    self.assertTrue(any('"/k/.env.local"="deny"' in a for a in argv))

    def test_text_mode_runs_in_an_owned_empty_directory(self):
        tmp = self.enterContext(tempfile.TemporaryDirectory())
        with patch.dict(os.environ, {'LOCAL_AGENT_STATE': tmp}):
            first = self.S.empty_workdir()
            self.assertEqual(first, str(Path(tmp) / 'empty'))
            (Path(first) / 'planted').write_text('x')
            (Path(first) / 'dir').mkdir()
            (Path(first) / 'link').symlink_to(tmp)
            self.assertEqual(self.S.empty_workdir(), first)   # stable, so resume finds it
            self.assertEqual(list(Path(first).iterdir()), [])
            self.assertTrue(Path(tmp).is_dir())                 # a symlink is unlinked, not followed

    def test_codex_profile_denies_key_files_under_the_worker_config(self):
        """Model-free: the same -c overrides under `codex sandbox`, with the
        deployed worker root, whose config sets sandbox_mode for codex-local."""
        root = policy.deployed_client_root() / 'codex'
        if not shutil.which('codex') or not (root / 'config.toml').is_file() or sys.platform != 'darwin':
            self.skipTest('codex worker root or macOS Seatbelt unavailable')
        tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        key, work = tmp / 'key.env', tmp / 'work'
        key.write_text('not-a-key')
        work.mkdir()
        probe = f'cat {key} >/dev/null 2>&1 && echo READABLE || echo BLOCKED'

        def run(overrides):
            r = subprocess.run(['codex', 'sandbox', '-P', self.S.CODEX_PROFILE, '-C', str(work), *overrides,
                                '--', 'sh', '-c', probe], env=dict(os.environ, CODEX_HOME=str(root)),
                               capture_output=True, text=True, timeout=60)
            return r.stdout.strip().splitlines()[-1:] or [r.stderr.strip()[-200:]]
        with patch.dict(os.environ, {'LOCAL_AGENT_DENY_READ': json.dumps([str(key), str(tmp / 'absent')])}):
            for mode in ('text', 'edit'):
                with self.subTest(mode=mode):
                    self.assertEqual(run(self.S.codex_permissions(mode)), ['BLOCKED'])
        # The probe can tell: the older sandbox setting reads the same file.
        self.assertEqual(run(['-c', f'default_permissions="{self.S.CODEX_PROFILE}"',
                              '-c', f'permissions.{self.S.CODEX_PROFILE}={{extends=":read-only"}}']), ['READABLE'])

    def test_session_lock_refuses_concurrent_resume(self):
        tmp = self.enterContext(tempfile.TemporaryDirectory())
        with patch.dict(os.environ, {'LOCAL_AGENT_STATE': tmp}):
            with self.S.session_lock('s'):
                with self.assertRaises(self.S.DelegateError) as caught, self.S.session_lock('s'):
                    pass
                self.assertTrue(str(caught.exception).startswith('SESSION_BUSY'))
                with self.S.session_lock('t'):
                    pass
            with self.S.session_lock('s'):
                pass


class ScriptedModel:
    """A Messages endpoint that plays a fixed list of tool calls and records the
    tool_result Claude Code returned for each. No inference: what is allowed or
    denied is decided by the real CLI's permission engine."""
    def __init__(self, calls):
        self.calls, self.results, self.requests, self.offered = calls, [], 0, []
        owner = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a): pass
            def do_GET(self):
                self.send_response(404); self.end_headers()
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get('content-length') or 0)) or b'{}')
                owner.requests += 1
                if not self.path.startswith('/v1/messages') or 'count_tokens' in self.path:
                    return self._json({'input_tokens': 1})
                msgs = body.get('messages') or []
                results = [c for m in msgs if m.get('role') == 'user' and isinstance(m.get('content'), list)
                           for c in m['content'] if isinstance(c, dict) and c.get('type') == 'tool_result']
                owner.offered.append(sorted(t.get('name', '') for t in body.get('tools') or []))
                if body.get('tools') and owner.results is not None:
                    owner.results[:] = [(r.get('is_error', False), _text(r.get('content'))) for r in results]
                step = len(results)
                block = None
                if body.get('tools') and step < len(owner.calls):
                    name, args = owner.calls[step]
                    block = {'type': 'tool_use', 'id': f'toolu_{step:04d}', 'name': name, 'input': args}
                self._reply(body, block)
            def _json(self, obj, code=200):
                data = json.dumps(obj).encode()
                self.send_response(code); self.send_header('content-type', 'application/json')
                self.send_header('content-length', str(len(data))); self.end_headers(); self.wfile.write(data)
            def _reply(self, body, block):
                content = [block] if block else [{'type': 'text', 'text': 'done'}]
                stop = 'tool_use' if block else 'end_turn'
                msg = {'id': 'msg_1', 'type': 'message', 'role': 'assistant', 'model': body.get('model', 'x'),
                       'content': content, 'stop_reason': stop, 'stop_sequence': None,
                       'usage': {'input_tokens': 1, 'output_tokens': 1}}
                if not body.get('stream'):
                    return self._json(msg)
                self.send_response(200); self.send_header('content-type', 'text/event-stream'); self.end_headers()
                def ev(kind, data):
                    self.wfile.write(f'event: {kind}\ndata: {json.dumps(data)}\n\n'.encode())
                ev('message_start', {'type': 'message_start', 'message': {**msg, 'content': [], 'stop_reason': None}})
                if block:
                    ev('content_block_start', {'type': 'content_block_start', 'index': 0,
                                               'content_block': {**block, 'input': {}}})
                    ev('content_block_delta', {'type': 'content_block_delta', 'index': 0,
                                               'delta': {'type': 'input_json_delta', 'partial_json': json.dumps(block['input'])}})
                else:
                    ev('content_block_start', {'type': 'content_block_start', 'index': 0,
                                               'content_block': {'type': 'text', 'text': ''}})
                    ev('content_block_delta', {'type': 'content_block_delta', 'index': 0,
                                               'delta': {'type': 'text_delta', 'text': 'done'}})
                ev('content_block_stop', {'type': 'content_block_stop', 'index': 0})
                ev('message_delta', {'type': 'message_delta', 'delta': {'stop_reason': stop, 'stop_sequence': None},
                                     'usage': {'output_tokens': 1}})
                ev('message_stop', {'type': 'message_stop'})
                self.wfile.flush()

        self.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f'http://127.0.0.1:{self.server.server_port}'

    def close(self):
        self.server.shutdown(); self.server.server_close()

def _text(content):
    if isinstance(content, list):
        return ' '.join(c.get('text', '') for c in content if isinstance(c, dict))
    return str(content)


class DelegationBoundaryTests(unittest.TestCase):
    """What each Claude mode loads from a hostile project, with no inference.

    SessionStart hooks run and MCP servers start before the first model
    request, and the endpoint below refuses that request with a 400, which
    Claude does not retry. Each marker is a side effect that ran on this
    machine: a project hook, a project .mcp.json server, the worker root's
    own hook, and the research server research mode registers."""

    MODES = {'text': {'user-hook'}, 'research': {'user-hook', 'research-mcp'},
             'edit': {'user-hook'}, 'full': {'user-hook', 'project-hook', 'project-mcp'}}

    #: (tool, target) -> allowed, per mode. Text offers no tools at all.
    REACH = {'research': [True, False, False, False, True, False, False, False],
             'edit': [True, False, False, False, True, True, False, False],
             'full': [True] * 8}

    def test_each_mode_allows_exactly_its_declared_reach(self):
        """A scripted model asks for the same eight tool calls in every mode: a
        read inside cwd, a symlink in cwd pointing outside, a read outside, a
        denied key file, an in-cwd edit, and a Write and a Bash redirect
        outside cwd."""
        if not shutil.which('claude'):
            self.skipTest('claude not installed')
        S = _server()
        for mode in ('text', 'research', 'edit', 'full'):
            with self.subTest(mode=mode):
                tmp = Path(self.enterContext(tempfile.TemporaryDirectory(dir='/private/tmp')))
                work, outside, cfg = tmp / 'work', tmp / 'outside', tmp / 'cfg'
                for d in (work, outside, cfg):
                    d.mkdir()
                (work / 'calc.py').write_text('def add(a, b):\n    return a - b\n')
                (work / 'notes.txt').write_text('inside\n')
                (outside / 'canary.txt').write_text('canary\n')
                (outside / 'key.env').write_text('not-a-key\n')
                (work / 'escape.txt').symlink_to(outside / 'canary.txt')
                calls = [('Read', {'file_path': str(work / 'notes.txt')}),
                         ('Read', {'file_path': str(work / 'escape.txt')}),
                         ('Read', {'file_path': str(outside / 'canary.txt')}),
                         ('Read', {'file_path': str(outside / 'key.env')}),
                         ('Read', {'file_path': str(work / 'calc.py')}),
                         ('Edit', {'file_path': str(work / 'calc.py'), 'old_string': 'a - b', 'new_string': 'a + b'}),
                         ('Write', {'file_path': str(outside / 'written.txt'), 'content': 'x'}),
                         ('Bash', {'command': f'echo x > {outside}/bash.txt', 'description': 'write'})]
                model = ScriptedModel(calls)
                self.addCleanup(model.close)
                env = {'LOCAL_AGENT_BASE_URL': model.url, 'LOCAL_AGENT_KEY': 'not-a-key',
                       'LOCAL_AGENT_CLAUDE_HOME': str(cfg),
                       'LOCAL_AGENT_DENY_READ': json.dumps([str(outside / 'key.env')]),
                       'LOCAL_AGENT_RESEARCH_MCP': json.dumps({'type': 'stdio', 'command': 'true',
                                                               'args': ['mcp', 'research'], 'env': {}})}
                with patch.dict(os.environ, env):
                    argv = S._claude_argv('go', 'ailocal-fast', mode, None, str(uuid.uuid4()),
                                          S.claude_settings(mode, {'context_input': 200000, 'max_output': 4096}),
                                          str(work))
                    child = S._child_env('claude', mode)
                r = subprocess.run(argv, cwd=work, env=child, stdin=subprocess.DEVNULL,
                                   capture_output=True, text=True, timeout=120)
                self.assertEqual(r.returncode, 0, r.stdout[-300:])
                # Tool-search plumbing loads other tools; it reads and writes nothing.
                meta = {'DeferredToolPlaceholder', 'ToolSearch'}
                builtin = {name for names in model.offered for name in names
                           if not name.startswith('mcp__') and name not in meta}
                if mode == 'text':
                    self.assertEqual((builtin, model.results), (set(), []))
                    continue
                if mode == 'research':
                    self.assertEqual(builtin, {'Read', 'Grep', 'Glob'})
                allowed = [not denied for denied, _ in model.results]
                self.assertEqual(allowed, self.REACH[mode], [t[:80] for _, t in model.results])
                self.assertEqual('a + b' in (work / 'calc.py').read_text(), mode in ('edit', 'full'))
                self.assertEqual(sorted(p.name for p in outside.iterdir()),
                                 ['bash.txt', 'canary.txt', 'key.env', 'written.txt'] if mode == 'full'
                                 else ['canary.txt', 'key.env'])


    def test_project_hooks_and_mcp_load_only_in_full_mode(self):
        if not shutil.which('claude'):
            self.skipTest('claude not installed')
        class Refuse(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers.get('content-length') or 0))
                body = b'{"type":"error","error":{"type":"invalid_request_error","message":"fixture"}}'
                self.send_response(400)
                self.send_header('content-type', 'application/json')
                self.send_header('content-length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_GET = do_POST

            def log_message(self, *args):
                pass

        endpoint = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Refuse)
        threading.Thread(target=endpoint.serve_forever, daemon=True).start()
        self.addCleanup(endpoint.server_close)
        self.addCleanup(endpoint.shutdown)
        S = _server()
        for mode, expected in self.MODES.items():
            with self.subTest(mode=mode):
                tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
                proj, cfg, marks, bin_dir = tmp / 'proj', tmp / 'cfg', tmp / 'marks', tmp / 'bin'
                for d in (proj / '.claude', cfg, marks, bin_dir):
                    d.mkdir(parents=True)

                def hook(name):
                    return {'hooks': [{'type': 'command', 'command': f'touch {marks / name}'}]}
                (cfg / 'settings.json').write_text(json.dumps({'hooks': {'SessionStart': [hook('user-hook')]}}))
                (proj / '.claude/settings.json').write_text(json.dumps({'hooks': {'SessionStart': [hook('project-hook')]}}))
                fake = tmp / 'fake_mcp.py'
                fake.write_text(f'import sys\nopen({str(marks)!r} + "/" + sys.argv[1], "w").write("x")\n')
                (proj / '.mcp.json').write_text(json.dumps(
                    {'mcpServers': {'mutating': {'command': sys.executable, 'args': [str(fake), 'project-mcp']}}}))
                research = bin_dir / 'fake-ailocal'
                research.write_text('#!/bin/sh\nexec ' + shlex.join(
                    [sys.executable, str(fake), 'research-mcp']) + '\n')
                research.chmod(0o755)
                env = {'LOCAL_AGENT_BASE_URL': f'http://127.0.0.1:{endpoint.server_port}',
                       'LOCAL_AGENT_KEY': 'not-a-key', 'LOCAL_AGENT_CLAUDE_HOME': str(cfg),
                       'LOCAL_AGENT_RESEARCH_MCP': json.dumps({'type': 'stdio', 'command': str(research),
                                                               'args': ['mcp', 'research'], 'env': {}})}
                with patch.dict(os.environ, env):
                    argv = S._claude_argv('hi', 'ailocal-fast', mode, None, str(uuid.uuid4()),
                                          S.claude_settings(mode, {'context_input': 200000, 'max_output': 512}),
                                          str(proj))
                    child = S._child_env('claude', mode)
                r = subprocess.run(argv, cwd=proj, env=child, stdin=subprocess.DEVNULL,
                                   capture_output=True, text=True, timeout=90)
                self.assertEqual({p.name for p in marks.iterdir()}, expected, r.returncode)
                # Claude forces a scrubbed acceptEdits run back to default unless
                # tools are declared; edit mode must keep acceptEdits.
                self.assertNotIn('forced to default', r.stderr)


class DelegationLifecycleTests(unittest.TestCase):
    """Real subprocesses under the integration's own interpreter (anyio, mcp)."""

    def _venv(self):
        py = policy.state_root() / 'local-agent/.venv/bin/python'
        self.assertTrue(py.is_file(), f'local-agent runtime missing at {py}; '
                        'run: ailocal clients claude codex')
        return py

    def test_group_is_reaped_on_timeout_cancel_flood_and_held_pipe(self):
        tmp = self.enterContext(tempfile.TemporaryDirectory())
        script = r'''
import os, signal, sys, time
sys.path.insert(0, sys.argv[1])
import anyio, server
tmp = sys.argv[2]
# Leader starts a descendant in its own group, records both pids, then behaves per mode.
FAKE = """import os, subprocess, sys, time
mode, pids = sys.argv[1], sys.argv[2]
assert sys.stdin.read() == ''          # stdin is /dev/null, never the protocol pipe
child = subprocess.Popen(['sleep', '300'])
open(pids, 'w').write(f'{os.getpid()} {child.pid}')
if mode == 'flood':
    while True: sys.stdout.write('x' * 65536)
if mode == 'held':
    print('reply', flush=True); os._exit(0)   # child still holds stdout open
if mode == 'bigline':
    # One event line past the bound (a tool's output), then a terminal event.
    sys.stdout.write('{"type":"item.completed","text":"' + 'y' * (9 * 1024 * 1024) + '"}\\n')
    sys.stdout.write('{"type":"turn.completed"}\\n'); sys.stdout.flush(); os._exit(0)
if mode == 'graceful':
    # A descendant that needs a moment to finish on TERM; the leader exits first.
    subprocess.Popen([sys.executable, '-c', GRACE, pids + '.marker'], stdout=subprocess.DEVNULL)
    time.sleep(0.5); print('reply', flush=True); os._exit(0)
time.sleep(300)
"""
GRACE = """import signal, sys, time
def done(*_):
    time.sleep(0.2); open(sys.argv[1], 'w').write('done'); sys.exit(0)
signal.signal(signal.SIGTERM, done)
time.sleep(300)
"""
FAKE = FAKE.replace('import os, subprocess, sys, time\n', 'import os, subprocess, sys, time\nGRACE = ' + repr(GRACE) + '\n', 1)
fake = os.path.join(tmp, 'fake.py'); open(fake, 'w').write(FAKE)

def alive(pid):
    try: os.kill(pid, 0)
    except ProcessLookupError: return False
    return True

def gone(pids_file):
    pids = [int(p) for p in open(pids_file).read().split()]
    deadline = time.time() + 3
    while time.time() < deadline and any(alive(p) for p in pids):
        time.sleep(0.1)
    return not any(alive(p) for p in pids)

async def run(mode, wrap):
    pids = os.path.join(tmp, mode)
    argv = [sys.executable, fake, mode, pids]
    start = time.monotonic()
    outcome = await wrap(server._run(argv, dict(os.environ), None))
    return outcome, time.monotonic() - start, gone(pids)

async def with_timeout(coro):
    try:
        with anyio.fail_after(1.5): return await coro
    except TimeoutError: return 'timeout'

async def cancelled(coro):
    with anyio.move_on_after(1.5): return await coro
    return 'cancelled'

async def raised(coro):
    try: return await coro
    except server.DelegateError as error: return str(error).split(':')[0]

async def plain(coro):
    return await coro

async def main():
    outcome, took, ok = await run('sleep', with_timeout)
    assert outcome == 'timeout' and ok, ('timeout', outcome, ok)
    outcome, took, ok = await run('sleep', cancelled)
    assert outcome == 'cancelled' and ok, ('cancel', outcome, ok)
    outcome, took, ok = await run('flood', raised)
    assert outcome == 'DELEGATE_OUTPUT_LIMIT' and ok, ('flood', outcome, ok)
    outcome, took, ok = await run('held', plain)
    assert outcome == (0, b'reply\n') and ok and took < server.DRAIN_GRACE + 3, ('held', outcome, took, ok)
    # An oversized event line is skipped, not fatal, when lines are consumed.
    seen = []
    pids = os.path.join(tmp, 'bigline')
    code, rest = await server._run([sys.executable, fake, 'bigline', pids], dict(os.environ), None, seen.append)
    assert (code, rest, seen) == (0, b'', [b'{"type":"turn.completed"}']), (code, rest, [len(x) for x in seen])
    # After a normal exit the group still gets TERM and time to act on it.
    outcome, took, ok = await run('graceful', plain)
    marker = os.path.join(tmp, 'graceful.marker')
    assert outcome == (0, b'reply\n') and ok and os.path.exists(marker), ('graceful', outcome, ok)
anyio.run(main)
'''
        r = subprocess.run([str(self._venv()), '-c', script, str(RES / 'integrations/local-agent'), tmp],
                           capture_output=True, text=True, timeout=90)
        self.assertEqual(r.returncode, 0, r.stderr[-1800:])

    def test_host_stop_kills_a_child_that_ignores_sigterm(self):
        """Claude Code stops a stdio server with SIGINT, SIGTERM after 100 ms and
        SIGKILL 400 ms later (code.claude.com/docs/en/mcp). The child runs in its
        own session, so only the server can end it, inside that window."""
        tmp = self.enterContext(tempfile.TemporaryDirectory())
        (Path(tmp) / 'active-profile').write_text('16gb\n')
        bin_dir = Path(tmp) / 'bin'
        bin_dir.mkdir()
        pids = Path(tmp) / 'pids'
        fake = bin_dir / 'claude'
        fake.write_text(f'''#!{self._venv()}
import os, signal, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
open({str(pids)!r}, 'w').write(f'{{os.getpid()}} {{os.getppid()}}')
time.sleep(60)
''')
        fake.chmod(0o755)
        env = dict(os.environ, AILOCAL_CONFIG=str(RES), AILOCAL_STATE=tmp, LOCAL_AGENT_STATE=tmp,
                   PATH=f'{bin_dir}{os.pathsep}{os.environ["PATH"]}',
                   LOCAL_AGENT_BASE_URL='http://127.0.0.1:9', LOCAL_AGENT_KEY='not-a-key')
        script = '''
import asyncio, os, signal, sys, time
from pathlib import Path
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
pids = Path(sys.argv[2])
def alive(pid):
    try: os.kill(pid, 0)
    except ProcessLookupError: return False
    return True
async def run():
    params = StdioServerParameters(command=sys.executable, args=[sys.argv[1]+'/server.py'], env=dict(os.environ))
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        work = asyncio.create_task(session.call_tool('delegate', {'prompt': 'x', 'model': 'ailocal-fast'}))
        for _ in range(200):
            if pids.exists() and pids.read_text(): break
            await asyncio.sleep(0.05)
        child, server = map(int, pids.read_text().split())
        os.kill(server, signal.SIGINT); time.sleep(0.1)
        if alive(server): os.kill(server, signal.SIGTERM)
        time.sleep(0.4)
        if alive(server): os.kill(server, signal.SIGKILL)
        deadline = time.time() + 1
        while time.time() < deadline and alive(child):
            time.sleep(0.05)
        assert not alive(child), 'child survived the host stop sequence'
        work.cancel()
        raise SystemExit(0)
try:
    asyncio.run(run())
except BaseException as error:
    if not (isinstance(error, SystemExit) and error.code == 0) and not isinstance(error, BaseExceptionGroup):
        raise
    if isinstance(error, BaseExceptionGroup) and not error.subgroup(lambda e: isinstance(e, SystemExit) and e.code == 0):
        raise
'''
        r = subprocess.run([str(self._venv()), '-c', script, str(RES / 'integrations/local-agent'), str(pids)],
                           env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr[-1800:])

    def test_server_answers_while_a_delegate_runs(self):
        """A blocking handler would hold model_guide behind the delegate; also
        proves the selected alias's limits and a fresh session id reach the CLI."""
        tmp = self.enterContext(tempfile.TemporaryDirectory())
        (Path(tmp) / 'active-profile').write_text('16gb\n')
        bin_dir = Path(tmp) / 'bin'
        bin_dir.mkdir()
        fake = bin_dir / 'claude'
        fake.write_text(f'''#!{self._venv()}
import json, sys, time
argv = sys.argv
settings = json.loads(argv[argv.index('--settings') + 1])
time.sleep(3)
print(json.dumps({{'result': json.dumps(settings['env']), 'session_id': argv[argv.index('--session-id') + 1]}}))
''')
        fake.chmod(0o755)
        env = dict(os.environ, AILOCAL_CONFIG=str(RES), AILOCAL_STATE=tmp, LOCAL_AGENT_STATE=tmp,
                   PATH=f'{bin_dir}{os.pathsep}{os.environ["PATH"]}',
                   LOCAL_AGENT_BASE_URL='http://127.0.0.1:9', LOCAL_AGENT_KEY='not-a-key')
        script = '''
import asyncio, json, os, sys, time
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
async def run():
    params = StdioServerParameters(command=sys.executable, args=[sys.argv[1]+'/server.py'], env=dict(os.environ))
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        guides = (await session.call_tool('model_guide', {})).structured_content['models']
        for model, code in (('ailocal-completion', 'MODEL_NOT_DELEGABLE'), ('ailocal-coder', 'UNKNOWN_MODEL')):
            refused = await session.call_tool('delegate', {'prompt': 'x', 'model': model})
            assert refused.is_error and code in refused.content[0].text, refused
        named = await session.call_tool('delegate', {'prompt': 'x', 'resume': 'my-thread'})
        assert named.is_error and 'INVALID_SESSION' in named.content[0].text, named
        # The shipped policy stops at edit; full is refused before any CLI starts.
        for agent in ('claude', 'codex'):
            full = await session.call_tool('delegate', {'prompt': 'x', 'agent': agent, 'mode': 'full',
                                                        'cwd': os.environ['AILOCAL_STATE']})
            assert full.is_error and 'MODE_NOT_PERMITTED' in full.content[0].text, full
        clash = await session.call_tool('delegate', {'prompt': 'x', 'mode': 'text', 'tools': True})
        assert clash.is_error and 'CONFLICTING_MODE' in clash.content[0].text, clash
        start = time.monotonic()
        work = asyncio.create_task(session.call_tool('delegate', {'prompt': 'x', 'model': 'ailocal-fast'}))
        await asyncio.sleep(0.5)
        guide = await session.call_tool('model_guide', {'model': 'ailocal-fast'})
        answered = time.monotonic() - start
        done = await work
        assert not guide.is_error and answered < 2.5, ('model_guide waited for delegate', answered)
        assert not done.is_error, done
        result = done.structured_content
        env = json.loads(result['output'])
        fast = guides['ailocal-fast']
        assert env['CLAUDE_CODE_MAX_OUTPUT_TOKENS'] == str(fast['max_output']), env
        assert env['CLAUDE_CODE_MAX_CONTEXT_TOKENS'] == str(fast['context_input']), env
        assert len(result['session_id']) == 36, result
asyncio.run(run())
'''
        r = subprocess.run([str(self._venv()), '-c', script, str(RES / 'integrations/local-agent')],
                           env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr[-1800:])


if __name__ == '__main__':
    unittest.main()
