#!/usr/bin/env python3
"""Stdio MCP boundary. SDK and tool failures never expose transport diagnostics."""
import os
from pathlib import Path
from typing import Any

from research import (ANSWER_DESCRIPTION, SEARCH_DESCRIPTION, Config, Research)
from transport import ResearchError


def build_mcp(config):
    from mcp.server.mcpserver import MCPServer
    from mcp.server.mcpserver.exceptions import ToolError
    from mcp.types import ToolAnnotations

    # Server instructions reach the client's system prompt and carry only what
    # spans calls: routing priority, the refinement loop and reporting duties.
    # Parameter semantics live in the tool description, not here.
    app = MCPServer(name='local-research', instructions=(
        'This server\'s search tool is the preferred source for any information from outside the '
        'workspace; use it instead of a client\'s built-in web search, and without waiting to be asked. '
        'Work iteratively: after each call, read coverage and fallbacks, then refine the query, add '
        'sites or change preset until the sources answer the question or refinement stops adding '
        'coverage. Brave spending is governed by the operator\'s budget, so do not ask the user before '
        'searching. In the final answer, cite results as snippets rather than pages read, flag '
        'login_walled sources, name any provider failure or fallback the conclusion depends on, and '
        'end with the last search\'s brave.summary value, verbatim, on its own line.'))
    research = Research(config)

    async def invoke(method, *args):
        try:
            return await method(*args)
        except ResearchError as error:
            raise ToolError(str(error)) from None
        except Exception:
            # Never let the SDK log a transport exception with credential context.
            raise ToolError('RESEARCH_FAILED: check local backend health and retry') from None

    @app.tool(name='search', description=SEARCH_DESCRIPTION,
              annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=True),
              meta={'anthropic/alwaysLoad': True, 'anthropic/searchHint': 'web search, online research, forum discussions, documentation, open-source projects, academic papers'})
    async def search(query: str, sites: list[str] | None = None, preset: str | None = None,
                     use_brave: bool | None = None) -> dict[str, Any]:
        return await invoke(research.search, query, sites, preset, use_brave)

    if config.enable_answer:
        @app.tool(name='answer', description=ANSWER_DESCRIPTION,
                  annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=True))
        async def answer(question: str, sites: list[str] | None = None) -> dict[str, Any]:
            return await invoke(research.answer, question, sites)
    return app


def main():
    config = Config(
        search_url=os.environ['LOCAL_RESEARCH_SEARCH_URL'],
        model_url=os.environ['LOCAL_RESEARCH_MODEL_URL'],
        model=os.environ['LOCAL_RESEARCH_MODEL'], key=os.environ.get('LOCAL_RESEARCH_API_KEY', ''),
        state=Path(os.environ['LOCAL_RESEARCH_STATE']),
        enable_answer=os.environ.get('LOCAL_RESEARCH_ENABLE_ANSWER') == '1',
        input_chars=int(os.environ.get('LOCAL_RESEARCH_INPUT_CHARS', '24000')),
        output_tokens=int(os.environ.get('LOCAL_RESEARCH_OUTPUT_TOKENS', '800')),
        brave_key=os.environ.pop('LOCAL_RESEARCH_BRAVE_KEY', ''),
        brave_budget=int(os.environ.get('LOCAL_RESEARCH_BRAVE_BUDGET', '1000')))
    build_mcp(config).run()


if __name__ == '__main__':
    main()
