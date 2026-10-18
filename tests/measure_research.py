#!/usr/bin/env python3
"""Opt-in real local research measurement; no hosted or Brave queries.

Run with the research runtime: python tests/measure_research.py [--quick].
Outputs JSON lines for human assessment; this is not an assertion of quality.
"""
import asyncio
import json
from pathlib import Path
import sys
import time

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / 'src'))
from ailocal import mcp as launcher
from ailocal import policy

QUESTIONS = [
    ('When one Python asyncio TaskGroup task raises, what happens to its sibling tasks?', ['docs.python.org']),
    ('Does asyncio.shield prevent cancellation of the calling coroutine?', ['docs.python.org']),
    ('Why does Rust error E0507 reject moving a value out of borrowed content?', ['doc.rust-lang.org']),
    ('What does Rust Option.take leave in the original Option?', ['doc.rust-lang.org']),
    ('Can SQLite WAL mode allow readers and a writer at the same time?', ['sqlite.org']),
    ('What does the HTTP 429 Retry-After header mean?', ['developer.mozilla.org']),
    ('How does publishing a Docker port to 127.0.0.1 differ from the default binding?', ['docs.docker.com']),
    ('Which format parameter requests JSON from the SearXNG search API?', ['docs.searxng.org']),
    ('Does GitHub REST code search require authentication?', ['docs.github.com']),
    ('What does Python asyncio.wait_for do when its timeout expires?', ['docs.python.org']),
]


async def main():
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client
    config = launcher.launch_environment(True)
    params = StdioServerParameters(command='ailocal', args=['mcp', 'research', '--enable-answer'], env=config)
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        for question, sites in QUESTIONS[:1] if '--quick' in sys.argv else QUESTIONS:
            start = time.monotonic()
            response = await session.call_tool('answer', {'question': question, 'sites': sites}, read_timeout_seconds=130)
            result = response.structured_content or {'error': [getattr(c, 'text', '') for c in response.content]}
            print(json.dumps({'evidence': 'REAL local path; factual quality requires human assessment',
                              'question': question, 'seconds': round(time.monotonic()-start, 3),
                              'is_error': response.is_error, **result}), flush=True)


if __name__ == '__main__':
    asyncio.run(main())
