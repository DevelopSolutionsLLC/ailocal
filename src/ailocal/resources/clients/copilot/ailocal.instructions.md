---
applyTo: "**"
---

# Local AI stack

You are connected to local Ollama models through a LiteLLM proxy at
`http://localhost:4000`. No cloud API calls are made. Models are addressed by
capability name, never by backend model name.

| Capability | Use it for |
|---|---|
| `architecture` | design, complex refactor, multi-step debugging |
| `implementation` | everyday coding, features, tests |
| `review` | code review, bug and security detection |
| `fast` | quick reasoning where latency matters more than depth |
| `completion` | small completions and IDE autocomplete (FIM) |

Which capabilities exist, what backs them and how much context they have come
from the active profile and the generated catalog, which are authoritative and
deliberately not copied here — an earlier copy of that table drifted three
context budgets and dropped a capability without anything noticing.

The proxy speaks both OpenAI (`/v1/chat/completions`) and Anthropic
(`/v1/messages`) formats.

# Terminal rules that exist because of this setup

Only three, each for a failure specific to running a local model inside VS
Code's agent terminal. General shell etiquette is not repeated here.

**Never append `exit`, `exit 0`, or `& exit 0`.** It closes the integrated
terminal before VS Code registers that the command finished, and the turn hangs
with no error. This is the most common way the agent gets stuck.

**Detach anything long-running** — installs, servers, watchers — with a trailing
`&` and a log, then read the log in a follow-up call. A local model's turn can
outlast the terminal's patience, and a blocked terminal blocks the session:

```bash
ailocal start > /tmp/compose.log 2>&1 &
```

**Never broadly kill node.** `pkill -f node` or `killall node` in the integrated
terminal kills VS Code's own extension host *and* the litellm-connector, which
disconnects your model mid-session. Target a port or PID instead:
`lsof -ti tcp:PORT | xargs kill`.
