---
applyTo: "**"
---

# Local AI stack

This chat runs on a local model served by Ollama through the ailocal LiteLLM proxy at `http://localhost:4000`. Models are addressed by capability, never by backend model tag:

| Capability | Use it for |
|---|---|
| `architecture` | design, complex refactoring, multi-step debugging |
| `implementation` | everyday coding, features, tests |
| `review` | code review, bug and security detection |
| `fast` | quick answers where latency matters more than depth |
| `completion` | inline completion (FIM) |

The active profile and the generated capability catalog are authoritative for which capabilities exist, what backs them and their context limits.

# Terminal rules

These rules prevent failures specific to running a local model in VS Code's integrated terminal.

**Never append `exit`, `exit 0` or `& exit 0` to a command.** It closes the terminal before VS Code registers that the command finished, and the turn hangs without an error.

**Run long-running commands in the background** (installs, servers, watchers) with output redirected to a log, then read the log in a later call. A blocked terminal blocks the whole session:

```bash
ailocal start > /tmp/compose.log 2>&1 &
```

**Never kill processes by name pattern such as `node`.** `pkill -f node` or `killall node` also kills VS Code's extension host and the LiteLLM connector, which disconnects the model mid-session. Target a port or PID instead: `lsof -ti tcp:PORT | xargs kill`.
