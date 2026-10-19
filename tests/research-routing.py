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

# Natural phrasing: no prompt names the tool, a preset, a provider or a site.
# (expected_search, acceptable presets or None, prompt)
PROMPTS = [
    (True, {'forums'}, 'Search the forums online for challenges people face developing cybersecurity tools with AI'),
    (True, {'forums'}, 'What do developers on Reddit and Hacker News think about Bun vs Node in production?'),
    (True, {'web', None}, 'What changed in the latest Python asyncio TaskGroup documentation?'),
    (True, {'research'}, 'Find recent papers on structured concurrency.'),
    (True, {'code'}, 'Are there open-source GitHub projects that fuzz MCP servers?'),
    (False, None, 'What is 2 + 2?'),
    (False, None, 'Rewrite this sentence more simply: The cat was situated upon the mat.'),
    (False, None, 'In this repository, which function in src/ailocal/mcp.py builds the launch environment?'),
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
    # Stand-ins for the client's own repository tools, so a repository question
    # has somewhere correct to go. Without them the negative control is unfair.
    for name, text in (('Read', 'Read a file from the local repository.'),
                       ('Grep', 'Search file contents in the local repository with a regular expression.')):
        wire_tools.append({'type': 'function', 'function': {'name': name, 'description': text, 'parameters': {
            'type': 'object', 'properties': {'pattern' if name == 'Grep' else 'file_path': {'type': 'string'}}}}})
    hits = positives = negatives_clean = negatives = 0
    runs = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    preset_hits = 0
    for expected, presets, prompt in PROMPTS:
        for _ in range(runs):
            payload = {'model': cfg.model, 'max_tokens': 500, 'stream': False,
                       # A client places server instructions in its system prompt; nothing else steers.
                       'messages': [{'role': 'system', 'content': 'You are a coding assistant. ' + (app.instructions or '')},
                                    {'role': 'user', 'content': prompt}], 'tools': wire_tools}
            status, _, data = await request(cfg.model_url+'/chat/completions', payload=payload,
                                           headers={'Authorization': 'Bearer '+cfg.key}, timeout=180)
            if status != 200:
                sys.exit('routing measurement failed: local proxy returned an HTTP error')
            result = json.loads(data)
            calls = [c.get('function', {}) for c in result['choices'][0]['message'].get('tool_calls') or []]
            search = [c for c in calls if c.get('name') == 'mcp__research__search']
            selected = bool(search)
            try:
                preset = json.loads(search[0].get('arguments') or '{}').get('preset') if search else None
            except ValueError:
                preset = 'UNPARSEABLE'
            preset_hits += int(expected and selected and preset in presets)
            positives += int(expected)
            hits += int(expected and selected)
            negatives += int(not expected)
            negatives_clean += int(not expected and not selected)
            print(json.dumps({'expected_search': expected, 'selected_search': selected, 'preset': preset,
                              'prompt': prompt}), flush=True)
    print(json.dumps({'positive_hits': hits, 'preset_hits': preset_hits, 'positive_runs': positives,
                      'negative_clean': negatives_clean,
                      'negative_runs': negatives, 'scope': 'REAL local inference; APPROX reduced surface (research search plus Read/Grep stand-ins); tools not executed; full client surface unmeasured'}))


if __name__ == '__main__':
    asyncio.run(main())
