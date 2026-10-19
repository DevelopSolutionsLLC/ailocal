#!/usr/bin/env python3
"""Real local model tool selection with MCP schemas; staged retrieval, no credits.

This isolates schema routing from search quality and never executes a tool.
It is an aggregate observation, not a deterministic gate or a Claude UI test.
"""
import asyncio
import json
from pathlib import Path
import sys

REPO = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(REPO/'src'), str(REPO/'src/ailocal/resources/integrations/local-research')]
from ailocal import mcp as launcher, policy
from research import Config
from server import build_mcp
from transport import request

PROMPTS = [
    (True, 'Find current Python TaskGroup documentation on docs.python.org. Search the web with the keyless web preset and cite sources.'),
    (True, 'Search doc.rust-lang.org for E0507 and return source links. Use keyless web search.'),
    (True, 'Find papers about structured concurrency using the research preset.'),
    (False, 'What is 2 + 2? Answer directly.'),
    (False, 'Rewrite this sentence more simply: The cat was situated upon the mat.'),
]


async def main():
    env = launcher.launch_environment()
    cfg = Config(env['LOCAL_RESEARCH_SEARCH_URL'], env['LOCAL_RESEARCH_MODEL_URL'],
                 env['LOCAL_RESEARCH_MODEL'], env['LOCAL_RESEARCH_API_KEY'],
                 policy.state_root()/'local-research')
    app = build_mcp(cfg)
    tools = await app.list_tools()
    wire_tools = [{'type': 'function', 'function': {'name': 'mcp__research__'+t.name, 'description': t.description,
                                                  'parameters': t.input_schema}} for t in tools]
    hits = positives = negatives_clean = negatives = 0
    runs = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    for expected, prompt in PROMPTS:
        for _ in range(runs):
            payload = {'model': cfg.model, 'max_tokens': 500, 'stream': False,
                       'messages': [{'role': 'system', 'content': 'Use available tools when external evidence is needed. Answer directly otherwise.'},
                                    {'role': 'user', 'content': prompt}], 'tools': wire_tools}
            status, _, data = await request(cfg.model_url+'/chat/completions', payload=payload,
                                           headers={'Authorization': 'Bearer '+cfg.key}, timeout=180)
            if status != 200:
                sys.exit('routing measurement failed: local proxy returned an HTTP error')
            result = json.loads(data)
            names = [c.get('function', {}).get('name') for c in result['choices'][0]['message'].get('tool_calls', [])]
            selected = 'mcp__research__search' in names
            positives += int(expected)
            hits += int(expected and selected)
            negatives += int(not expected)
            negatives_clean += int(not expected and not selected)
            print(json.dumps({'expected_search': expected, 'selected_search': selected, 'prompt': prompt}), flush=True)
    print(json.dumps({'positive_hits': hits, 'positive_runs': positives, 'negative_clean': negatives_clean,
                      'negative_runs': negatives, 'scope': 'REAL local inference; APPROX reduced MCP search-only surface; tools not executed; full Claude surface unmeasured'}))


if __name__ == '__main__':
    asyncio.run(main())
