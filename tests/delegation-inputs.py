#!/usr/bin/env python3
"""Session authority and explicit image inputs: fixtures are [APPROX]."""
import asyncio
import base64
import copy
import importlib.util
import http.server
import json
import os
from pathlib import Path
import runpy
import struct
import subprocess
import sys
import tempfile
import threading
import unittest
import uuid
import zlib
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'src'))
from ailocal import generation, policy

if importlib.util.find_spec('mcp') is None:
    runtime = policy.state_root() / 'local-agent/.venv/bin/python'
    if not runtime.is_file():
        sys.exit('local-agent runtime missing; run ailocal clients claude codex')
    os.execv(str(runtime), [str(runtime), __file__, *sys.argv[1:]])

sys.path.insert(0, str(ROOT / 'src/ailocal/resources/integrations/local-agent'))
import server as S
from mcp.server.mcpserver.exceptions import ToolError


def png(rgb=(224, 34, 51), width=64, height=64, broken=False):
    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data) & 0xffffffff)
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>2I5B', width, height, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', b'not-zlib' if broken else zlib.compress((b'\0' + bytes(rgb) * width) * height)) + chunk(b'IEND', b''))


class InputTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = self.enterContext(tempfile.TemporaryDirectory(prefix='delegate inputs ', dir='/private/tmp'))
        self.root = Path(self.tmp)
        self.work = self.root / 'work'; self.work.mkdir()
        self.home = self.root / 'worker'; self.home.mkdir()
        self.enterContext(patch.dict(os.environ, {
            'LOCAL_AGENT_STATE': str(self.root / 'state'),
            'LOCAL_AGENT_CLAUDE_HOME': str(self.home), 'LOCAL_AGENT_CODEX_HOME': str(self.home),
            'LOCAL_AGENT_BASE_URL': 'http://127.0.0.1:9', 'LOCAL_AGENT_KEY': 'not-a-key',
            'LOCAL_AGENT_DENY_READ': '[]', 'LOCAL_AGENT_CLAUDE_ENV': '{}',
        }))
        for key in (S.DEPTH_VAR, 'LOCAL_AGENT_CALLER'):
            os.environ.pop(key, None)
        self.guide = {'model': 'fixture-model', 'context_input': 16000, 'max_output': 512,
                      'reasoning': False, 'sampling': {}, 'source': None,
                      'input_modalities': ['text', 'image']}
        self.guides = {'tier': '64gb', 'models': {'ailocal-fast': self.guide}}
        self.enterContext(patch.object(S, 'model_guides', AsyncMock(return_value=self.guides)))
        self.ceilings = {'claude': 'edit', 'codex': 'edit'}
        self.enterContext(patch.object(S, 'mode_ceilings', AsyncMock(return_value=self.ceilings)))
        self.runs = []

        async def worker(prompt, model, mode, cwd, resume, guide, images=()):
            self.runs.append({'agent_mode': mode, 'cwd': cwd, 'images': copy.deepcopy(images)})
            return {'agent': 'fixture', 'mode': mode, 'model': model, 'session_id': resume or str(uuid.uuid4()),
                    'output': 'done', 'capabilities': {}}

        self.claude_patch = patch.object(S, 'delegate_claude', side_effect=worker)
        self.codex_patch = patch.object(S, 'delegate_codex', side_effect=worker)
        self.enterContext(self.claude_patch); self.enterContext(self.codex_patch)
        self.app = S.build_mcp()

    async def call(self, **args):
        return (await self.app.call_tool('delegate', {'prompt': 'task', **args})).structured_content

    async def refused(self, code, **args):
        before = len(self.runs)
        with self.assertRaises(ToolError) as error:
            await self.call(**args)
        self.assertIn(code + ':', str(error.exception))
        self.assertEqual(len(self.runs), before, 'refusal launched a worker')

    async def test_resume_inherits_and_refuses_explicit_scope_changes(self):
        first = await self.call(agent='codex', model='ailocal-fast', mode='edit', cwd=str(self.work))
        sid = first['session_id']
        again = await self.call(resume=sid.upper())
        self.assertEqual(again['session_id'], sid)
        self.assertEqual(self.runs[-1]['agent_mode'], 'edit')
        self.assertEqual(self.runs[-1]['cwd'], str(self.work))
        for changed in ({'agent': 'claude'}, {'mode': 'text'}, {'tools': False},
                        {'model': 'ailocal-cyber'}, {'cwd': str(self.root)}):
            await self.refused('SESSION_MISMATCH', resume=sid, **changed)
        self.assertEqual((self.root / 'state/sessions' / f'{sid}.json').stat().st_mode & 0o777, 0o600)
        self.assertEqual((self.root / 'state/sessions').stat().st_mode & 0o777, 0o700)
        binding = S.load_binding(sid)
        self.assertNotIn('task', json.dumps(binding))
        self.assertNotIn('done', json.dumps(binding))
        # A new server instance (no in-memory session map) resumes the same binding.
        self.app = S.build_mcp()
        self.assertEqual((await self.call(resume=sid))['session_id'], sid)

    async def test_resume_rechecks_ceiling_and_model_policy(self):
        sid = (await self.call(model='ailocal-fast', mode='edit', cwd=str(self.work)))['session_id']
        self.ceilings['claude'] = 'text'
        await self.refused('MODE_NOT_PERMITTED', resume=sid)
        self.ceilings['claude'] = 'edit'
        for key, value in (('context_input', 8000), ('model', 'replaced-model'),
                           ('source', {'sha256': 'f' * 64}), ('input_modalities', ['text'])):
            old = self.guide[key]; self.guide[key] = value
            await self.refused('SESSION_POLICY_CHANGED', resume=sid)
            self.guide[key] = old
        self.guides['tier'] = '128gb'
        await self.refused('SESSION_POLICY_CHANGED', resume=sid)
        self.guides['tier'] = '64gb'
        self.guides['models'] = {}
        await self.refused('SESSION_POLICY_CHANGED', resume=sid)

    async def test_completed_empty_reply_stays_an_error_with_bound_resume(self):
        for agent in ('claude', 'codex'):
            sid = str(uuid.uuid4())
            async def completed_empty(*args, **kwargs):
                raise S.CompletedWithoutReply(agent, sid)
            with patch.object(S, 'delegate_' + agent, side_effect=completed_empty):
                with self.assertRaises(ToolError) as caught:
                    await self.call(agent=agent, model='ailocal-fast', mode='edit', cwd=str(self.work))
            self.assertIn('DELEGATE_EMPTY:', str(caught.exception))
            self.assertIn('resume=' + sid, str(caught.exception))
            self.assertIn('task success is unverified', str(caught.exception))
            binding = S.load_binding(sid)
            self.assertEqual(binding['agent'], agent)
            self.assertNotIn('task', json.dumps(binding))
            self.ceilings[agent] = 'text'
            await self.refused('MODE_NOT_PERMITTED', resume=sid)
            self.ceilings[agent] = 'edit'
            again = await self.call(resume=sid)
            self.assertEqual(again['session_id'], sid)
            self.assertEqual(self.runs[-1]['agent_mode'], 'edit')
            self.assertEqual(self.runs[-1]['cwd'], str(self.work))

    async def test_failed_worker_does_not_create_recoverable_authority(self):
        for agent in ('claude', 'codex'):
            with patch.object(S, 'delegate_' + agent, side_effect=S.DelegateError('DELEGATE_FAILED: fixture')):
                with self.assertRaises(ToolError) as caught:
                    await self.call(agent=agent, model='ailocal-fast', mode='edit', cwd=str(self.work))
            self.assertNotIn('resume=', str(caught.exception))
        self.assertFalse((self.root / 'state/sessions').exists())

    async def test_empty_reply_binding_failure_does_not_advertise_resume(self):
        sid = str(uuid.uuid4())
        with patch.object(S, 'delegate_codex', side_effect=S.CompletedWithoutReply('codex', sid)), \
                patch.object(S.os, 'replace', side_effect=OSError('fixture')):
            with self.assertRaises(ToolError) as caught:
                await self.call(agent='codex', model='ailocal-fast', mode='edit', cwd=str(self.work))
        self.assertIn('SESSION_BINDING_FAILED:', str(caught.exception))
        self.assertNotIn('resume=', str(caught.exception))
        self.assertFalse((self.root / 'state/sessions' / f'{sid}.json').exists())

    async def test_unknown_corrupt_busy_and_legacy_sessions_are_refused(self):
        await self.refused('SESSION_NOT_MANAGED', resume=str(uuid.uuid4()))
        await self.refused('INVALID_SESSION', resume='../session')
        sid = (await self.call(model='ailocal-fast'))['session_id']
        with S.session_lock(sid):
            await self.refused('SESSION_BUSY', resume=sid)
        path = self.root / 'state/sessions' / f'{sid}.json'
        path.write_text('{}')
        await self.refused('SESSION_INVALID', resume=sid)
        path.unlink()
        path.symlink_to(self.root / 'missing')
        await self.refused('SESSION_INVALID', resume=sid)

    async def test_binding_write_failure_does_not_return_resumable_success(self):
        with patch.object(S.os, 'replace', side_effect=OSError('fixture')):
            with self.assertRaises(ToolError) as error:
                await self.call(model='ailocal-fast')
        self.assertIn('SESSION_BINDING_FAILED', str(error.exception))
        self.assertEqual(list((self.root / 'state/sessions').glob('.binding-*')), [])

    async def test_images_are_explicit_private_copies_without_tool_escalation(self):
        source = self.work / 'picture with spaces.png'; data = png(); source.write_bytes(data)
        first = await self.call(model='ailocal-fast', cwd=str(self.work), image_paths=[str(source)])
        run = self.runs[-1]
        self.assertEqual(run['agent_mode'], 'text')
        self.assertEqual(run['cwd'], str(self.root / 'state/empty'))
        self.assertEqual(run['images'][0]['data'], data)
        self.assertFalse(Path(run['images'][0]['path']).exists(), 'attachment copy survived completion')
        self.assertEqual(first['capabilities']['images_supplied'], 1)
        self.assertEqual(list((self.root / 'state/attachments').iterdir()), [])
        await self.call(resume=first['session_id'], image_paths=[str(source)])
        self.assertEqual(self.runs[-1]['images'][0]['data'], data)

    async def test_invalid_image_requests_launch_no_worker(self):
        image = self.work / 'image.png'; image.write_bytes(png())
        await self.refused('IMAGE_CWD_REQUIRED', model='ailocal-fast', image_paths=[str(image)])
        await self.refused('IMAGE_LIMIT', model='ailocal-fast', cwd=str(self.work), image_paths=[str(image)] * 5)
        self.guide['input_modalities'] = ['text']
        await self.refused('IMAGES_UNSUPPORTED', model='ailocal-fast', cwd=str(self.work), image_paths=[str(image)])
        self.guide['input_modalities'] = ['text', 'image']
        bad = self.work / 'not-an-image.png'; bad.write_text('not an image')
        await self.refused('IMAGE_INVALID', model='ailocal-fast', cwd=str(self.work), image_paths=[str(bad)])
        bad.write_bytes(b'\x89PNG\r\n\x1a\ninvalid')
        await self.refused('IMAGE_INVALID', model='ailocal-fast', cwd=str(self.work), image_paths=[str(bad)])
        bad.write_bytes(png(broken=True))
        await self.refused('IMAGE_INVALID', model='ailocal-fast', cwd=str(self.work), image_paths=[str(bad)])
        huge = self.work / 'huge.png'
        with huge.open('wb') as handle:
            handle.truncate(S.IMAGE_LIMIT + 1)
        await self.refused('IMAGE_LIMIT', model='ailocal-fast', cwd=str(self.work), image_paths=[str(huge)])
        image.write_bytes(png(width=S.IMAGE_SIDE + 1, height=1))
        await self.refused('IMAGE_LIMIT', model='ailocal-fast', cwd=str(self.work), image_paths=[str(image)])
        self.assertEqual(list((self.root / 'state/attachments').iterdir()), [])

    async def test_outside_paths_symlinks_keys_and_hardlinks_are_denied(self):
        outside = self.root / 'outside.png'; outside.write_bytes(png())
        link = self.work / 'escape.png'; link.symlink_to(outside)
        directory = self.work / 'escape'; directory.symlink_to(self.root, target_is_directory=True)
        key = self.work / 'key.png'; key.write_bytes(png())
        hard = self.work / 'hard.png'; os.link(key, hard)
        os.environ['LOCAL_AGENT_DENY_READ'] = json.dumps([str(key)])
        for path in (outside, link, directory / 'outside.png', key, hard, self.work / 'missing'):
            await self.refused('IMAGE_PATH_INVALID', model='ailocal-fast', cwd=str(self.work), image_paths=[str(path)])

    async def test_attachment_cleanup_after_worker_failure_and_cancellation(self):
        source = self.work / 'image.png'; source.write_bytes(png())
        for failure in (S.DelegateError('DELEGATE_FAILED: fixture'), asyncio.CancelledError()):
            with patch.object(S, 'delegate_claude', side_effect=failure):
                with self.assertRaises((ToolError, asyncio.CancelledError)):
                    await self.call(model='ailocal-fast', cwd=str(self.work), image_paths=[str(source)])
            self.assertEqual(list((self.root / 'state/attachments').iterdir()), [])

    async def test_jpeg_and_source_replacement_use_owned_bytes(self):
        source = self.work / 'input.png'; data = png(); source.write_bytes(data)
        async with S.prepared_images([str(source)], str(self.work), self.guide) as images:
            source.write_bytes(b'replaced')
            self.assertEqual(images[0]['data'], data)
            self.assertEqual(Path(images[0]['path']).read_bytes(), data)
            self.assertEqual(Path(images[0]['path']).stat().st_mode & 0o777, 0o600)
            self.assertEqual(Path(images[0]['path']).parent.stat().st_mode & 0o777, 0o700)
            source.write_bytes(data)
            jpeg = self.work / 'input.jpg'
            subprocess.run(['/usr/bin/sips', '-s', 'format', 'jpeg', str(source), '--out', str(jpeg)],
                           check=True, capture_output=True)
        async with S.prepared_images([str(jpeg)], str(self.work), self.guide) as images:
            self.assertEqual(images[0]['mime_type'], 'image/jpeg')

    async def test_real_claude_image_wire_on_fresh_and_bound_resume(self):
        """Real installed CLI, scripted endpoint: transport, not model quality."""
        self.claude_patch.__exit__(None, None, None)
        tests = runpy.run_path(str(ROOT / 'tests/model-guidance.py'))
        model = tests['ScriptedModel']([])
        self.addCleanup(model.close)
        captured = []
        handler = model.server.RequestHandlerClass
        original = handler._reply
        def capture(instance, body, block):
            captured.append(body)
            return original(instance, body, block)
        handler._reply = capture
        os.environ['LOCAL_AGENT_BASE_URL'] = model.url
        source = self.work / 'attachment.png'; source.write_bytes(png())
        first = await self.call(model='ailocal-fast', cwd=str(self.work), image_paths=[str(source)])
        second = png((20, 40, 230)); source.write_bytes(second)
        again = await self.call(resume=first['session_id'], image_paths=[str(source)])
        self.assertEqual(again['session_id'], first['session_id'])
        images = [block for message in captured[-1]['messages'] if isinstance(message.get('content'), list)
                  for block in message['content'] if block.get('type') == 'image']
        self.assertEqual(base64.b64decode(images[-1]['source']['data']), second)
        self.assertTrue(all(not request.get('tools') for request in captured))

    async def test_real_codex_image_wire_on_fresh_and_bound_resume(self):
        """Real Responses client and resume parser, scripted model endpoint."""
        self.codex_patch.__exit__(None, None, None)
        captured = []
        trace = []
        run = S._run
        async def traced(argv, env, cwd, on_line=None, input_data=None):
            def event(line):
                if argv[0] == 'codex':
                    trace.append(json.loads(line))
                if on_line:
                    on_line(line)
            code, out = await run(argv, env, cwd, event if on_line else None, input_data)
            return code, out
        self.enterContext(patch.object(S, '_run', side_effect=traced))
        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get('content-length', 0))))
                captured.append(body)
                item = {'id': 'msg_fixture', 'type': 'message', 'role': 'assistant', 'status': 'completed',
                        'content': [{'type': 'output_text', 'text': 'done', 'annotations': []}]}
                response = {'id': 'resp_fixture', 'object': 'response', 'created_at': 1,
                            'status': 'completed', 'model': body.get('model'), 'output': [item],
                            'usage': {'input_tokens': 10, 'output_tokens': 1, 'total_tokens': 11}}
                self.send_response(200)
                self.send_header('content-type', 'text/event-stream'); self.end_headers()
                events = [
                    {'type': 'response.created', 'response': {**response, 'status': 'in_progress', 'output': []}},
                    {'type': 'response.output_item.added', 'output_index': 0, 'item': {**item, 'status': 'in_progress', 'content': []}},
                    {'type': 'response.content_part.added', 'item_id': item['id'], 'output_index': 0,
                     'content_index': 0, 'part': {'type': 'output_text', 'text': '', 'annotations': []}},
                    {'type': 'response.output_text.delta', 'item_id': item['id'], 'output_index': 0,
                     'content_index': 0, 'delta': 'done'},
                    {'type': 'response.output_text.done', 'item_id': item['id'], 'output_index': 0,
                     'content_index': 0, 'text': 'done'},
                    {'type': 'response.output_item.done', 'output_index': 0, 'item': item},
                    {'type': 'response.completed', 'response': response},
                ]
                for index, event in enumerate(events):
                    event['sequence_number'] = index
                    self.wfile.write(f'event: {event["type"]}\ndata: {json.dumps(event)}\n\n'.encode())
                self.wfile.flush()
        endpoint = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=endpoint.serve_forever, daemon=True).start()
        self.addCleanup(endpoint.server_close); self.addCleanup(endpoint.shutdown)
        os.environ['LOCAL_AGENT_BASE_URL'] = f'http://127.0.0.1:{endpoint.server_port}'
        models = policy.profile_summary('64gb', ROOT / 'src/ailocal/resources')['roles']
        with patch.object(generation, 'stage') as staged:
            generation.regen_catalog(models)
        (self.home / 'catalog.json').write_text(staged.call_args.args[1])
        (self.home / 'config.toml').write_text(
            'model_provider = "ailocal"\nmodel_catalog_json = ' + json.dumps(str(self.home / 'catalog.json'))
            + '\n[model_providers.ailocal]\nname="fixture"\nwire_api="responses"\nenv_key="OPENAI_API_KEY"\nbase_url='
            + json.dumps(os.environ['LOCAL_AGENT_BASE_URL'] + '/v1')
            + '\n[mcp_servers.agent]\ncommand="/usr/bin/true"\nenabled=false\n')
        source = self.work / 'attachment.png'; source.write_bytes(png())
        try:
            first = await self.call(agent='codex', model='ailocal-fast', cwd=str(self.work), image_paths=[str(source)])
        except ToolError:
            self.fail(f'Controlled Codex fixture events: {trace!r}; requests: {len(captured)}')
        second = png((20, 40, 230)); source.write_bytes(second)
        again = await self.call(resume=first['session_id'], image_paths=[str(source)])
        self.assertEqual(again['session_id'], first['session_id'])
        images = [part for item in captured[-1]['input'] if isinstance(item.get('content'), list)
                  for part in item['content'] if part.get('type') == 'input_image']
        self.assertTrue(images)
        self.assertEqual(base64.b64decode(images[-1]['image_url'].split(',', 1)[1]), second)

    async def test_stdio_schema_images_and_id_only_resume(self):
        """Real MCP serialization, controlled CLI; native CLIs tested above."""
        from mcp import ClientSession
        from mcp.client.stdio import StdioServerParameters, stdio_client
        commands = self.root / 'bin'; commands.mkdir()
        command = commands / 'claude'
        command.write_text('#!/usr/bin/env python3\n'
                           'import json,sys\n'
                           'body=json.loads(sys.stdin.readline())\n'
                           'images=[x for x in body["message"]["content"] if x["type"]=="image"]\n'
                           'assert len(images)==1\n'
                           'key="--resume" if "--resume" in sys.argv else "--session-id"\n'
                           'print(json.dumps({"type":"result","result":"fixture","session_id":sys.argv[sys.argv.index(key)+1]}))\n')
        command.chmod(0o755)
        image = self.work / 'diagram.png'; image.write_bytes(png())
        env = dict(os.environ, PATH=str(commands) + os.pathsep + os.environ['PATH'])
        params = StdioServerParameters(command=sys.executable, args=[str(Path(S.__file__))], env=env)
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            tool = next(t for t in (await session.list_tools()).tools if t.name == 'delegate')
            self.assertIn('image_paths', tool.input_schema['properties'])
            result = await session.call_tool('delegate', {'prompt': 'fixture', 'model': 'ailocal-fast',
                'cwd': str(self.work), 'image_paths': [str(image)]})
            self.assertFalse(result.is_error, result)
            sid = result.structured_content['session_id']
            resumed = await session.call_tool('delegate', {'prompt': 'fixture', 'resume': sid, 'image_paths': [str(image)]})
            self.assertFalse(resumed.is_error, resumed)
            self.assertEqual(resumed.structured_content['session_id'], sid)
            refused = await session.call_tool('delegate', {'prompt': 'fixture', 'resume': sid, 'agent': 'codex'})
            self.assertTrue(refused.is_error)
            self.assertIn('SESSION_MISMATCH', refused.content[0].text)


class ProcessInputTests(unittest.IsolatedAsyncioTestCase):
    async def test_owned_pipe_is_closed_and_delivered(self):
        data = b'x' * (2 * 1024 * 1024)
        code, out = await S._run([sys.executable, '-c', 'import sys; print(len(sys.stdin.buffer.read()))'],
                                {'PATH': '/usr/bin:/bin'}, None, input_data=data)
        self.assertEqual((code, int(out)), (0, len(data)))

    async def test_timeout_reaps_child_while_input_writer_is_blocked(self):
        import anyio
        with tempfile.TemporaryDirectory() as tmp:
            pidfile = Path(tmp) / 'pid'
            program = 'import os,sys,time; open(sys.argv[1],"w").write(str(os.getpid())); time.sleep(60)'
            with self.assertRaises(TimeoutError):
                with anyio.fail_after(0.3):
                    await S._run([sys.executable, '-c', program, str(pidfile)], {'PATH': '/usr/bin:/bin'},
                                 None, input_data=b'x' * (2 * 1024 * 1024))
            pid = int(pidfile.read_text())
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)
            self.assertNotIn(pid, S._ACTIVE)


class CatalogTests(unittest.TestCase):
    def test_all_tiers_project_exact_backend_modalities(self):
        for tier in policy.TIERS:
            models = policy.profile_summary(tier, ROOT / 'src/ailocal/resources')['roles']
            with patch.object(generation, 'stage') as staged:
                generation.regen_catalog(models)
            entries = json.loads(staged.call_args.args[1])['models']
            for entry in entries:
                role = entry['slug'].removeprefix('ailocal-')
                expected = ['text'] if role == 'completion' else models[role]['guidance'].get('input_modalities', ['text'])
                self.assertEqual(entry['input_modalities'], expected)
            replacement = {**models['architecture'], 'active': 'replacement'}
            replacement['guidance'] = policy.guidance_for({'architecture': replacement}, 'architecture')
            models['architecture'] = replacement
            with patch.object(generation, 'stage') as staged:
                generation.regen_catalog({'architecture': models['architecture']})
            self.assertEqual(json.loads(staged.call_args.args[1])['models'][0]['input_modalities'], ['text'])


if __name__ == '__main__':
    unittest.main()
