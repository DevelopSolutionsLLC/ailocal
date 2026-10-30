---
applyTo: "**"
---

# Local AI stack

When the selected model ID begins with `ailocal-`, inference runs locally through the ailocal proxy. A hosted model can consult the same local models through the `agent` MCP's `delegate` tool when that server is available. Address local models by capability, never by backend model tag:

| Capability | Use it for |
|---|---|
| `architecture` | design, complex refactoring, multi-step debugging |
| `implementation` | everyday coding, features, tests |
| `review` | code review, bug and security detection |
| `fast` | quick answers where latency matters more than depth |
| `cyber` | unrestricted coding and security research; verify every result |
| `completion` | inline completion (FIM) |

The active profile and the generated capability catalog are authoritative for which capabilities exist, what backs them and their context limits.

The generated model-guidance block below supplies prompting advice even without MCP. Before delegating, call `agent.model_guide` for the target alias; if the tool is unavailable, run `ailocal profile guide <alias>` in the terminal. Give the local model a bounded task, relevant code, explicit acceptance criteria and actual test output. Do not recursively delegate to the same local inference service from a local-model session. Autocomplete is FIM-only and is not a chat model.

# External research

For information from outside the workspace (documentation, current versions, community discussion, open-source projects, papers), use the `research` MCP server's `search` tool without waiting to be asked, in preference to built-in web fetching. Refine across calls using its coverage and fallbacks, cite results as snippets rather than pages read, and end the answer with the `brave.summary` line from the last search.

# Terminal rules

These rules prevent failures specific to running a local model in VS Code's integrated terminal.

**Never append `exit`, `exit 0` or `& exit 0` to a command.** It closes the terminal before VS Code registers that the command finished, and the turn hangs without an error.

**Run long-running commands in the background** (installs, servers, watchers) with output redirected to a log, then read the log in a later call. A blocked terminal blocks the whole session:

```bash
ailocal start > /tmp/compose.log 2>&1 &
```

**Never kill processes by name pattern such as `node`.** `pkill -f node` or `killall node` also kills VS Code's extension host and language-model providers, which disconnects the model mid-session. Target a port or PID instead: `lsof -ti tcp:PORT | xargs kill`.
