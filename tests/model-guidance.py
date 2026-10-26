#!/usr/bin/env python3
"""Profile knowledge follows exact backends and native model selection."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
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
        self.assertIn('Unrestricted Qwen3.5 4B reasoning-distilled MLX', out.getvalue())
        stock_guide = policy.model_guides('16gb', RES)['models']['ailocal-implementation']
        self.assertEqual(out.getvalue().count(stock_guide['evidence']), 1)

    def test_generators_share_guidance_and_preserve_external_hooks(self):
        models = generation.load_models_yaml(RES / 'profiles/16gb.toml')
        self.assertNotIn('model_guidance', models)
        self.assertNotIn('completion', models)
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
            with patch.object(generation, 'stage') as stage:
                generation.regen_catalog(models)
                entries = json.loads(stage.call_args.args[1])['models']
                cyber = next(x for x in entries if x['slug'] == 'ailocal-cyber')
                self.assertIn(models['cyber']['guidance']['evidence'], cyber['base_instructions'])
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
        assert data['models']['ailocal-cyber']['evidence'].startswith('[APPROX]')
        failure = await session.call_tool('model_guide', {'model':'not-a-model'})
        assert failure.is_error
asyncio.run(run())
'''
        r = subprocess.run([str(py), '-c', script, str(RES / 'integrations/local-agent')],
                           env=env, capture_output=True, text=True, timeout=25)
        self.assertEqual(r.returncode, 0, r.stderr[-1800:])


if __name__ == '__main__':
    unittest.main()
