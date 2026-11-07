#!/usr/bin/env python3
"""Research MCP: the network boundary, the Brave key and budget, cited answers, and client
registration. Staged search/model backends on loopback; no paid queries; temp state only."""
import asyncio, contextlib, io, json, socket, subprocess, sys, tempfile, threading
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlsplit

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / 'src/ailocal/resources/integrations/local-research'
sys.path[:0] = [str(SRC), str(REPO / 'src'), str(REPO / 'tests')]

import brave as B
import research as R
import transport as T
from ailocal import clients as C, mcp as launcher
from harness import unittest_main

# [REAL] shape of this account's Brave headers, 2026-10-19: no monthly cap.
HEADERS = {'x-ratelimit-limit': '50, 0', 'x-ratelimit-policy': '50;w=1, 0;w=2678400',
           'x-ratelimit-remaining': '49, 0', 'x-ratelimit-reset': '1, 1120380'}
DOCS = ['docs.example.org']
BRAVE_KEY = 'fixture-brave-key'


class Backend:
    """One HTTP server playing SearXNG (/config, /search), Brave (/brave) and the model."""
    def __init__(self):
        self.brave, self.prompt = [], None
        self.answer = 'Supported by the supplied evidence [S1].'

    async def handle(self, reader, writer):
        try:
            lines = (await reader.readuntil(b'\r\n\r\n')).decode().split('\r\n')
            path = lines[0].split()[1]
            headers = dict(line.split(': ', 1) for line in lines[1:] if ': ' in line)
            body = await reader.readexactly(int(headers.get('Content-Length', '0')))
            extra, args = b'', parse_qs(urlsplit(path).query)
            q = args.get('q', [''])[0]
            domain = q.split('site:')[-1] if 'site:' in q else 'docs.example.org'
            if path == '/config':
                data = {'engines': [{'name': e} for e in ('yahoo', 'yep', 'github', 'stackoverflow', 'hackernews',
                                                          'meta.discourse', 'openalex', 'arxiv')]}
            elif path.startswith('/brave?'):
                self.brave.append(headers)
                data = {'web': {'results': [{'url': f'https://{domain}/brave/page', 'title': 't',
                                             'description': 'Brave.', 'data': {}}]}}
                extra = ''.join(f'{k}: {v}\r\n' for k, v in HEADERS.items()).encode()
            elif path.startswith('/search?'):
                engine = args['engines'][0].split(',')[0]
                data = {'results': [{'url': f'https://{domain}/{engine}/page', 'title': 's',
                                     'content': 'Useful.', 'engines': [engine]}]}
            else:
                self.prompt = json.loads(body)
                data = {'choices': [{'message': {'content': self.answer}}], 'usage': {'prompt_tokens': 1, 'completion_tokens': 1}}
            wire = json.dumps(data).encode()
            writer.write(b'HTTP/1.1 200 X\r\n' + extra
                         + f'Content-Type: application/json\r\nContent-Length: {len(wire)}\r\n\r\n'.encode() + wire)
            await writer.drain()
        finally:
            writer.close()


def connection(raw, *addresses):
    """Pinned DNS answers plus one canned HTTP response, for transport.request."""
    class Writer:
        def write(self, data): pass
        async def drain(self): pass
        def close(self): pass
    reader = asyncio.StreamReader()
    reader.feed_data(raw)
    reader.feed_eof()
    return (AsyncMock(side_effect=[[(socket.AF_INET, socket.SOCK_STREAM, 6, '', (a, 443))] for a in addresses]),
            AsyncMock(return_value=(reader, Writer())))


class Search(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = self.enterContext(tempfile.TemporaryDirectory())
        self.backend = Backend()
        self.server = await asyncio.start_server(self.backend.handle, '127.0.0.1', 0)
        self.url = 'http://127.0.0.1:' + str(self.server.sockets[0].getsockname()[1])
        self.config = R.Config(self.url, self.url + '/v1', 'fixture-model', 'fixture-secret', Path(self.tmp), True,
                               brave_key=BRAVE_KEY, brave_url=self.url + '/brave')
        self.r = R.Research(self.config)

    async def asyncTearDown(self):
        self.r.cache.db and self.r.cache.db.close()
        self.server.close()
        await self.server.wait_closed()

    async def test_an_answer_citing_a_source_it_was_not_given_is_refused(self):
        page = AsyncMock(return_value={'text': 'Evidence. ' * 20, 'evidence': 'page_text', 'fetch_status': 'read'})
        with patch.object(self.r, 'page', page):
            self.assertIn('[S1]', (await self.r.answer('What is documented?', DOCS))['answer'])
            for i, invented in enumerate(('Invented support [S999].', 'See [S1] at https://invented.example.org/page.')):
                self.backend.answer = invented
                with self.assertRaisesRegex(T.ResearchError, 'INVALID_CITATIONS'):
                    await self.r.answer(f'q{i}', DOCS)

    async def test_the_brave_key_leaves_only_in_the_brave_request_header(self):
        result = await self.r.search('task groups', DOCS)
        self.assertEqual([h.get('X-Subscription-Token') for h in self.backend.brave], [BRAVE_KEY])
        self.assertNotIn(BRAVE_KEY, json.dumps(result))
        keyed = Path(self.tmp) / 'keyed.sqlite3'
        with self.assertRaises(T.ResearchError) as error:   # nothing listens on port 9
            await B.Brave(BRAVE_KEY, B.Ledger(keyed, 1000), 'http://127.0.0.1:9').search('query')
        self.assertNotIn(BRAVE_KEY, str(error.exception))
        for ledger in (keyed, Path(self.tmp) / 'brave-ledger.sqlite3'):
            self.assertNotIn(BRAVE_KEY.encode(), ledger.read_bytes())

    async def test_transport_never_connects_to_a_private_address(self):
        self.assertFalse(any(map(T.public_address, ('127.0.0.1', '10.0.0.1', '169.254.169.254', '::1', '::ffff:127.0.0.1',
                                                    '224.0.0.1', '64:ff9b::7f00:1', '2002:7f00:1::'))))
        with self.assertRaisesRegex(T.ResearchError, 'PRIVATE_ADDRESS'):
            await T.request(self.url, public=True)
        loop = asyncio.get_running_loop()
        # DNS is resolved once and the connection pinned to that answer (no rebinding); TLS still names the host.
        resolve, connect = connection(b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok', '8.8.8.8', '127.0.0.1')
        with patch.object(loop, 'getaddrinfo', resolve), patch('transport.asyncio.open_connection', connect):
            await T.request('https://docs.example.org/page', public=True)
        self.assertEqual((resolve.await_count, connect.call_args.args[0], connect.call_args.kwargs['server_hostname']),
                         (1, '8.8.8.8', 'docs.example.org'))
        resolve, connect = connection(b'HTTP/1.1 302 Found\r\nLocation: http://127.0.0.1/p\r\nContent-Length: 0\r\n\r\n',
                                      '8.8.8.8', '127.0.0.1')
        with patch.object(loop, 'getaddrinfo', resolve), patch('transport.asyncio.open_connection', connect), \
                self.assertRaisesRegex(T.ResearchError, 'PRIVATE_ADDRESS'):
            await T.request('http://docs.example.org/page', public=True)
        self.assertEqual(connect.await_count, 1)   # the redirect is refused before a second connection


class BraveLedger(unittest.TestCase):
    def test_spend_never_exceeds_the_budget_even_concurrently_or_with_lost_responses(self):
        path = Path(self.enterContext(tempfile.TemporaryDirectory())) / 'ledger.sqlite3'
        first = B.Ledger(path, 1000).reserve(1000)
        B.Ledger(path, 1000).settle(first, 200, HEADERS, now=1000)
        lost = B.Ledger(path, 1000).reserve(1000)
        B.Ledger(path, 1000).settle(lost, None, now=1000)   # sent, response lost: may be billed
        grants, barrier = [], threading.Barrier(20)
        def reserve():
            barrier.wait()
            grants.append(B.Ledger(path, 6).reserve(1001))
        threads = [threading.Thread(target=reserve) for _ in range(20)]
        [t.start() for t in threads]; [t.join() for t in threads]
        self.assertEqual(sorted(str(g.refusal) for g in grants), ['BRAVE_BUDGET_EXHAUSTED'] * 16 + ['None'] * 4)


class Provisioning(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(contextlib.redirect_stdout(io.StringIO()))
        self.enterContext(contextlib.redirect_stderr(io.StringIO()))

    def test_registration_writes_only_the_given_home_and_never_displaces_a_foreign_server(self):
        cj = self.root / '.claude.json'
        def native(args, **kwargs):   # `claude mcp add-json`, which owns the write
            # Without CLAUDE_CONFIG_DIR the native CLI writes the hosted client's real home.
            self.assertEqual((args[:6], kwargs['env']['CLAUDE_CONFIG_DIR']),
                             (['claude', 'mcp', 'add-json', '--scope', 'user', 'research'], str(self.root)))
            data = json.loads(cj.read_text())
            data.setdefault('mcpServers', {})['research'] = json.loads(args[6])
            cj.write_text(json.dumps(data))
            return subprocess.CompletedProcess(args, 0)
        with patch.object(C, 'provision_mcp_runtime', return_value=self.root / 'python'), \
                patch.object(C.policy, 'data_root', return_value=SRC.parent.parent), \
                patch.object(C.subprocess, 'run', side_effect=native):
            cj.write_text('{}')
            self.assertTrue(C.provision_local_research(self.root))
            self.assertEqual(json.loads(cj.read_text())['mcpServers']['research'], launcher.registration())
            # A file it cannot parse, or a `research` entry it did not write, is refused untouched.
            for original in ('{broken', json.dumps({'mcpServers': {'research': {'command': 'foreign'}}})):
                cj.write_text(original)
                self.assertFalse(C.provision_local_research(self.root))
                self.assertEqual(cj.read_text(), original)

if __name__ == '__main__':
    unittest_main()
