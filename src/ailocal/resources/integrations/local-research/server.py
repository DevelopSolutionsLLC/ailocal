#!/usr/bin/env python3
"""Serve research over stdio MCP; failures never expose transport details."""
from __future__ import annotations

import logging
import os
import socket
import sys
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from research import (ANSWER_DESCRIPTION, SEARCH_DESCRIPTION, Config, Research)
from transport import ResearchError

log = logging.getLogger('local-research')


def configure_logging(state: Path) -> None:
    """Log lifecycle and fatal exits to a state file and stderr, never stdout.

    stdout is the MCP channel; a silent death reads as a random drop.
    """
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    try:
        state.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(state / 'server.log'))
    except OSError:
        pass  # stderr still carries the record even if the state dir is unwritable
    logging.basicConfig(
        level=logging.INFO, handlers=handlers, force=True,
        format='%(asctime)s %(levelname)s %(name)s %(message)s')


def _endpoint(url: str) -> tuple[str, int]:
    parts = urlsplit(url)
    return parts.hostname or '127.0.0.1', parts.port or (443 if parts.scheme == 'https' else 80)


def wait_for_backends(dependencies: list[tuple[str, str]], *,
                      timeout: float = 30.0, interval: float = 1.0) -> None:
    """Block until each loopback backend accepts TCP, else exit with a reason.

    Backends start independently, so an early call would fail as a random drop.
    Logs only host:port, never a URL or key.
    """
    for label, url in dependencies:
        host, port = _endpoint(url)
        deadline = time.monotonic() + timeout
        while True:
            try:
                with socket.create_connection((host, port), timeout=2.0):
                    log.info('dependency ready: %s at %s:%d', label, host, port)
                    break
            except OSError as error:
                if time.monotonic() >= deadline:
                    log.error('dependency unavailable after %.0fs: %s at %s:%d (%s)',
                              timeout, label, host, port, error.__class__.__name__)
                    raise SystemExit(f'{label} backend unreachable at {host}:{port}')
                time.sleep(interval)


def build_mcp(config: Config) -> Any:
    from mcp.server.mcpserver import MCPServer
    from mcp.server.mcpserver.exceptions import ToolError
    from mcp.types import ToolAnnotations

    # Instructions reach the system prompt: cross-call duties only; parameter
    # semantics live in the tool description.
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

    async def invoke(method: Callable[..., Awaitable[dict[str, Any]]],
                     *args: object) -> dict[str, Any]:
        try:
            return await method(*args)
        except ResearchError as error:
            raise ToolError(str(error)) from None
        except Exception:
            # Never let the SDK log a transport exception with credentials.
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


def main() -> None:
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
    configure_logging(config.state)
    log.info('local-research starting (answer=%s)', config.enable_answer)
    try:
        wait_for_backends([('search', config.search_url), ('model', config.model_url)])
        build_mcp(config).run()
    except SystemExit:
        raise  # already logged with a named reason
    except BaseException:
        # A stdio server otherwise dies silently, seen only as a closed pipe.
        log.exception('local-research exited abnormally')
        raise


if __name__ == '__main__':
    main()
