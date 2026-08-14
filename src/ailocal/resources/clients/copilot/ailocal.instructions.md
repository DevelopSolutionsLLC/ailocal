---
applyTo: "**"
---

# Local AI Stack

You are connected to local Ollama models via a LiteLLM proxy at `http://localhost:4000`. No cloud
API calls are made. Models are exposed as capability names — never use backend model names directly.

| Capability | Use it for |
|---|---|
| `architecture` | design, complex refactor, multi-step debugging |
| `implementation` | everyday coding, features, tests |
| `review` | code review, bug and security detection |
| `fast` | quick reasoning where latency matters more than depth |
| `completion` | small completions and IDE autocomplete (FIM) |

Backend model, context budget and which capabilities exist come from the active
profile and the generated catalog, which are authoritative. They are deliberately
not written here: an earlier copy of that table drifted three context budgets and
omitted a whole capability, and nothing could tell.

The proxy speaks both OpenAI (`/v1/chat/completions`) and Anthropic (`/v1/messages`) formats.

# Terminal Commands

VS Code's agent terminal detects when a command finishes on its own (via shell
integration). Run short commands directly and let the tool wait:

```bash
docker ps
git --no-pager log -3
```

**Never append `exit`, `exit 0`, or `& exit 0`.** That closes the integrated terminal
before VS Code registers completion and freezes the entire turn — it is the number-one
cause of the agent getting stuck.

**Long-running commands only** (installs, servers, watchers) should be detached with a
trailing `&` and a log — but never with `exit`:
```bash
ailocal install > /tmp/install.log 2>&1 &
ailocal start > /tmp/compose.log 2>&1 &
npm install > /tmp/npm.log 2>&1 &
```

**Verify afterward** by reading the log in a follow-up call:
```bash
cat /tmp/install.log
```

**Always use non-interactive flags** so backgrounded commands don't stall waiting for input:
```bash
git --no-pager log -10
brew install -q package
apt-get install -y package
```

**Never run commands that block indefinitely:**
- No `tail -f`, `watch`, `ollama run` (interactive REPL), `less`, `man`
- No commands that prompt for input mid-run without `-y` or equivalent
- Pipe paged output: `git diff | cat`, `git log | cat`

**Never broadly kill node.** `pkill -f node` / `killall node` in the integrated terminal
kills VS Code's own extension host and the litellm-connector — it disconnects your model
and freezes the session. To stop a stuck server, target its port or PID only:
`lsof -ti tcp:PORT | xargs kill`. Never blanket-match `node`.

**Chaining.** `step1 && step2` inline; detach a long chain as a unit, still with
no `exit`: `(step1 && step2) > /tmp/run.log 2>&1 &`, then read the log in a
follow-up call.

**Run inline, no detach needed:** `git status`, `git diff | cat`, `docker ps`,
`ls -la`, `cat file.txt`.
