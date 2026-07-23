---
applyTo: "**"
---

# Local AI Stack

You are connected to local Ollama models via a LiteLLM proxy at `http://localhost:4000`. No cloud
API calls are made. Models are exposed as role names — never use backend model names directly.

| Role | Purpose |
|---|---|
| `coder-main` | Primary implementation, code edits, generation (64k context) |
| `coder-agent` | Multi-step planning / agentic orchestration (64k, vision) |
| `coder-fast` | Fast small tasks; IDE autocomplete (16k context) |
| `deep-think` | Lighter reasoning, thinking merged into the answer (64k) |
| `deep-think-more` | Deep reasoning / decomposition (64k) |
| `supervisor` | Review, critique, approval gate (32k context) |
| `embed` | Semantic search only — not for chat |

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
./scripts/install.sh > /tmp/install.log 2>&1 &
docker compose up -d > /tmp/compose.log 2>&1 &
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
