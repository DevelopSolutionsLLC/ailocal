#!/usr/bin/env python3
"""Live local research measurements, JSON lines for human review. Out of the gate.

    python3 tests/measure_research.py [--quick]           # answer quality through the real MCP server
    python3 tests/measure_research.py --routing [--runs N]  # does the model pick search from the schemas?

Needs the research runtime (the `mcp` package) and a running stack. No hosted or
Brave queries; --routing stages retrieval and never executes a tool.
"""
import argparse
import asyncio
import json
from pathlib import Path
import sys
import time

REPO = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(REPO / "src"), str(REPO / "src/ailocal/resources/integrations/local-research")]

QUESTIONS = [
    ("When one Python asyncio TaskGroup task raises, what happens to its sibling tasks?", ["docs.python.org"]),
    ("Does asyncio.shield prevent cancellation of the calling coroutine?", ["docs.python.org"]),
    ("Why does Rust error E0507 reject moving a value out of borrowed content?", ["doc.rust-lang.org"]),
    ("What does Rust Option.take leave in the original Option?", ["doc.rust-lang.org"]),
    ("Can SQLite WAL mode allow readers and a writer at the same time?", ["sqlite.org"]),
    ("What does the HTTP 429 Retry-After header mean?", ["developer.mozilla.org"]),
    ("How does publishing a Docker port to 127.0.0.1 differ from the default binding?", ["docs.docker.com"]),
    ("Which format parameter requests JSON from the SearXNG search API?", ["docs.searxng.org"]),
    ("Does GitHub REST code search require authentication?", ["docs.github.com"]),
    ("What does Python asyncio.wait_for do when its timeout expires?", ["docs.python.org"]),
]
# (expected_search, acceptable presets or None, prompt); no prompt names a tool, preset or site.
PROMPTS = [
    (True, {"forums"}, "Search the forums online for challenges people face developing cybersecurity tools with AI"),
    (True, {"forums"}, "What do developers on Reddit and Hacker News think about Bun vs Node in production?"),
    (True, {"web", None}, "What changed in the latest Python asyncio TaskGroup documentation?"),
    (True, {"research"}, "Find recent papers on structured concurrency."),
    (True, {"code"}, "Are there open-source GitHub projects that fuzz MCP servers?"),
    (False, None, "What is 2 + 2?"),
    (False, None, "Rewrite this sentence more simply: The cat was situated upon the mat."),
    (False, None, "In this repository, which function in src/ailocal/mcp.py builds the launch environment?"),
]


async def answers(quick):
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client
    from ailocal import mcp as launcher
    params = StdioServerParameters(command="ailocal", args=["mcp", "research", "--enable-answer"],
                                   env=launcher.launch_environment(True))
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        for question, sites in QUESTIONS[:1] if quick else QUESTIONS:
            start = time.monotonic()
            response = await session.call_tool("answer", {"question": question, "sites": sites},
                                               read_timeout_seconds=130)
            result = response.structured_content or {"error": [getattr(c, "text", "") for c in response.content]}
            print(json.dumps({"evidence": "REAL local path; factual quality requires human assessment",
                              "question": question, "seconds": round(time.monotonic() - start, 3),
                              "is_error": response.is_error, **result}), flush=True)


async def routing(runs):
    from ailocal import mcp as launcher, policy
    from research import Config
    from server import build_mcp
    from transport import request
    env = launcher.launch_environment()
    cfg = Config(env["LOCAL_RESEARCH_SEARCH_URL"], env["LOCAL_RESEARCH_MODEL_URL"], env["LOCAL_RESEARCH_MODEL"],
                 env["LOCAL_RESEARCH_API_KEY"], policy.state_root() / "local-research")
    app = build_mcp(cfg)
    tools = [{"type": "function", "function": {"name": "mcp__research__" + t.name, "description": t.description,
                                               "parameters": t.input_schema}} for t in await app.list_tools()]
    # Repository-tool stand-ins, so the repository negative control has a correct target.
    for name, text, arg in (("Read", "Read a file from the local repository.", "file_path"),
                            ("Grep", "Search file contents in the local repository with a regular expression.",
                             "pattern")):
        tools.append({"type": "function", "function": {"name": name, "description": text, "parameters": {
            "type": "object", "properties": {arg: {"type": "string"}}}}})
    totals = dict.fromkeys(("positive_hits", "preset_hits", "positive_runs", "negative_clean", "negative_runs"), 0)
    for expected, presets, prompt in PROMPTS:
        for _ in range(runs):
            payload = {"model": cfg.model, "max_tokens": 500, "stream": False, "tools": tools, "messages": [
                {"role": "system", "content": "You are a coding assistant. " + (app.instructions or "")},
                {"role": "user", "content": prompt}]}
            status, _, data = await request(cfg.model_url + "/chat/completions", payload=payload,
                                            headers={"Authorization": "Bearer " + cfg.key}, timeout=180)
            if status != 200:
                sys.exit("routing measurement failed: local proxy returned an HTTP error")
            calls = [c.get("function", {}) for c in json.loads(data)["choices"][0]["message"].get("tool_calls") or []]
            search = [c for c in calls if c.get("name") == "mcp__research__search"]
            try:
                preset = json.loads(search[0].get("arguments") or "{}").get("preset") if search else None
            except ValueError:
                preset = "UNPARSEABLE"
            totals["positive_runs" if expected else "negative_runs"] += 1
            totals["positive_hits"] += expected and bool(search)
            totals["preset_hits"] += expected and bool(search) and preset in presets
            totals["negative_clean"] += not expected and not search
            print(json.dumps({"expected_search": expected, "selected_search": bool(search), "preset": preset,
                              "prompt": prompt}), flush=True)
    print(json.dumps({**totals, "scope": "REAL local inference; APPROX reduced surface (research search plus "
                      "Read/Grep stand-ins); tools not executed; full client surface unmeasured"}))


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--quick", action="store_true", help="answer mode: first question only")
    ap.add_argument("--routing", action="store_true", help="measure tool selection instead of answers")
    ap.add_argument("--runs", type=int, default=1, help="routing mode: repetitions per prompt")
    args = ap.parse_args()
    asyncio.run(routing(args.runs) if args.routing else answers(args.quick))
