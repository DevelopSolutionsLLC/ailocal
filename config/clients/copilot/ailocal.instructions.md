---
applyTo: "**"
---

# Local AI Stack

You are connected to local Ollama models via a LiteLLM proxy at `http://localhost:4000`. No cloud
API calls are made. Models are exposed as role names — never use backend model names directly.

| Role | Purpose |
|---|---|
| `router` | Fast classification, trivial tasks, autocomplete |
| `coder` | Implementation, code edits, generation (256k context) |
| `reasoner` | Deep analysis, planning, debugging (128k context, thinking model) |
| `supervisor` | Review, critique, multimodal (128k context) |
| `embed` | Semantic search only — not for chat |

The proxy speaks both OpenAI (`/v1/chat/completions`) and Anthropic (`/v1/messages`) formats.

# Terminal Commands

Local models have higher first-token latency than cloud models. To keep the terminal snappy
and avoid VS Code hanging waiting for command completion, run commands detached:

**Standard pattern — background the command, log output, exit immediately:**
```bash
some-command > /tmp/cmd.log 2>&1 & exit 0
```

**To verify the result afterward**, read the log in a follow-up terminal call:
```bash
cat /tmp/cmd.log
```

**Concrete examples:**
```bash
./scripts/install.sh > /tmp/install.log 2>&1 & exit 0
docker compose up -d > /tmp/compose.log 2>&1 & exit 0
npm install > /tmp/npm.log 2>&1 & exit 0
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
