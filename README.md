# ailocal

Local AI infrastructure for macOS Apple Silicon. Ollama runs natively for Metal GPU acceleration; everything else runs in Docker Compose.

## Requirements

- macOS 13+ (M1/M2/M3/M4)
- 64 GB RAM recommended — 32 GB minimum with smaller models
- ~80 GB free disk for the full model set

## Prerequisites

**Install Homebrew** if you don't have it:

```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
```

**Install all dependencies in one shot:**

```bash
brew install git jq
brew install --cask docker ollama
```

> After installing Docker Desktop, open it once to accept the license agreement and let it finish its first-run setup. You can then enable "Start at Login" in Docker Desktop → Settings → General.

`curl` and `openssl` are already included with macOS. If you want the GitHub CLI for managing this repo: `brew install gh`.

## Setup

```bash
./scripts/install.sh         # install host deps, generate .env
ollama serve                 # start Ollama (or open Ollama.app)
./scripts/install-models.sh  # pull models (~65 GB, takes a while)
./scripts/start.sh           # start Docker services
./scripts/healthcheck.sh     # verify everything is up
```

## Security model

All ports bind to `127.0.0.1` (localhost-only). The stack is designed for single-user local use.

**If you expose this stack on a LAN** (changing port binds to `0.0.0.0`):
- Set `WEBUI_AUTH=true` in `docker-compose.yml` for Open WebUI (OWASP A07:2021 — Authentication Failures)
- Add Caddy `basic_auth` middleware on `/v1/*` and `/metrics*` in the Caddyfile
- Rotate `LITELLM_MASTER_KEY` and `ADMIN_PASSWORD` to strong unique values before doing so

`WEBUI_AUTH=false` is intentional for the default local-only setup and is not a vulnerability in that context.

## Services

| Container | Port | What it does |
|---|---|---|
| **litellm** | 4000 | AI proxy. Single endpoint for all model requests. Routes to the right local model based on task type, caches responses in Redis, logs usage to Postgres. Speaks both OpenAI format (`/v1/chat/completions`) and Anthropic format (`/v1/messages`) — so Claude Code, Cowork, Codex, and any OpenAI SDK all work by just changing the base URL. |
| **open-webui** | 8081 | Chat interface. Routes through LiteLLM so all requests share caching and routing. Good for testing models without writing code. |
| **postgres** | — | LiteLLM's database. Stores API keys, spend logs, and model config. Internal only. |
| **redis** | — | Response cache for LiteLLM. Identical requests within 10 minutes return instantly from cache instead of hitting Ollama. Internal only. |
| **caddy** | 80, 443 | Reverse proxy. `http://localhost/v1/*` routes to LiteLLM; everything else to Open WebUI. All services are also accessible directly on their own ports. Provides the future path to local TLS. |
| **prometheus** | 9090 | Scrapes LiteLLM `/metrics` every 15 seconds — request counts, latency, and token usage per model. |
| **grafana** | 3000 | Dashboard UI for Prometheus. Prometheus is pre-wired as the default datasource on first boot. |

**Ollama** runs on the host at port 11434 — not in Docker. Containerized Ollama on Apple Silicon loses Metal GPU access, so it runs natively and all containers reach it via `host.docker.internal:11434`.

## Model routing

LiteLLM routes based on the model name in the request. All Anthropic and OpenAI model names are aliased to local equivalents — no cloud calls.

| Requested model | Routes to | Best for |
|---|---|---|
| `claude-sonnet-4-6` / `gpt-4o` / `qwen3-coder:30b` | qwen3-coder:30b | Coding, agentic tasks, the daily driver |
| `claude-opus-4-8` / `deepseek-r1:32b` | deepseek-r1:32b | Complex reasoning, hard problems |
| `claude-haiku-4-5` / `gpt-4o-mini` / `gpt-3.5-turbo` / `qwen3:8b` | qwen3:8b → qwen2.5:7b fallback | Fast tasks, chat, quick completions |
| `nomic-embed-text` | nomic-embed-text | Embeddings |

Claude Code defaults to `claude-sonnet-4-6`. Codex defaults to `gpt-4o`. Both map to `qwen3-coder:30b` locally.

## Client integration

Source this once to configure your shell for all tools:

```bash
source config/clients/env.sh
```

Add that line to `~/.zprofile` to make it permanent — every tool below picks up the env vars automatically on next launch.

---

### Claude Code

**Per-session:**
```bash
export ANTHROPIC_BASE_URL=http://localhost:4000
export ANTHROPIC_API_KEY=$(grep LITELLM_MASTER_KEY .env | cut -d= -f2)
claude
```

**Permanent** — copy and fill in your key once:
```bash
cp config/clients/claude-code.json ~/.claude/settings.json
# edit ~/.claude/settings.json and replace <LITELLM_MASTER_KEY>
```

Claude Code defaults to `claude-sonnet-4-6`, which routes to `qwen3-coder:30b` locally.

---

### Claude Desktop (Cowork)

The Claude Desktop app connects to Anthropic's servers directly — it can't be redirected to a local proxy. However, any subprocess it spawns (cadence agents, skill runs, shell scripts) inherits your shell environment, so adding the env file to `~/.zprofile` means all agent work routes through ailocal automatically.

---

### Codex CLI

**Per-session:**
```bash
export OPENAI_BASE_URL=http://localhost:4000/v1
export OPENAI_API_KEY=$(grep LITELLM_MASTER_KEY .env | cut -d= -f2)
codex --model gpt-4o "refactor this to use async/await"
```

**Permanent** — copy and fill in your key once:
```bash
cp config/clients/codex-config.yaml ~/.codex/config.yaml
# edit ~/.codex/config.yaml and replace <LITELLM_MASTER_KEY>
```

`gpt-4o` routes to `qwen3-coder:30b`. You can also call local models directly: `codex --model qwen3-coder:30b`.

---

### VS Code

Launch VS Code with ailocal env vars active so all extensions pick them up:

```bash
source config/clients/env.sh && code .
```

**Continue extension** — copy config and fill in your key:
```bash
cp config/clients/vscode-continue.json ~/.continue/config.json
# edit and replace <LITELLM_MASTER_KEY>
```

Gives you three models in the Continue panel, tab autocomplete via qwen3:8b, and codebase embeddings via nomic-embed-text.

Any extension that supports a custom OpenAI-compatible endpoint (Cline, Copilot alternatives, etc.) works the same way — point it at `http://localhost:4000/v1` with your `LITELLM_MASTER_KEY`.

---

### Any OpenAI SDK

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://localhost:4000/v1",
    api_key="<your LITELLM_MASTER_KEY>",
)

response = client.chat.completions.create(
    model="qwen3-coder:30b",   # or gpt-4o, claude-sonnet-4-6, etc.
    messages=[{"role": "user", "content": "Hello"}],
)
```

```typescript
import OpenAI from "openai";

const client = new OpenAI({
  baseURL: "http://localhost:4000/v1",
  apiKey: process.env.LITELLM_MASTER_KEY,
});
```

---

### Any Anthropic SDK

```python
import anthropic

client = anthropic.Anthropic(
    base_url="http://localhost:4000",
    api_key="<your LITELLM_MASTER_KEY>",
)

message = client.messages.create(
    model="claude-sonnet-4-6",   # maps to qwen3-coder:30b locally
    max_tokens=1024,
    messages=[{"role": "user", "content": "Hello"}],
)
```

---

## Operations

```bash
./scripts/start.sh           # start services
./scripts/stop.sh            # stop (preserves volumes)
./scripts/stop.sh --volumes  # stop and wipe all volume data
./scripts/teardown.sh        # full removal of containers, volumes, network
./scripts/teardown.sh --images  # also remove pulled Docker images
./scripts/update.sh          # backup → pull new images → restart
./scripts/backup.sh          # config + postgres dump to ./backups/
./scripts/restore.sh         # restore from most recent backup
./scripts/healthcheck.sh     # check all services and endpoints
```

## Cloud fallback

Disabled by default — LiteLLM runs entirely on local models without any API keys.

To enable for a specific model:
1. Add your key to `.env` (`ANTHROPIC_API_KEY` or `OPENAI_API_KEY`)
2. Set `ENABLE_CLOUD=true` in `.env`
3. Uncomment the relevant block at the bottom of `config/litellm/config.yaml`
4. `docker compose restart litellm`

The cloud entry takes over that model name from the local alias — comment out the local entry above it if you want cloud as primary rather than fallback.

## Troubleshooting

**LiteLLM won't start** — depends on Postgres being healthy first. Check: `docker logs ailocal_postgres`. Usually a wrong or missing `POSTGRES_PASSWORD` in `.env`.

**Models not responding / 404 on model name** — either Ollama isn't running (`ollama serve`), the model isn't pulled (`ollama list`), or the model name isn't aliased in `config/litellm/config.yaml`.

**Open WebUI shows no models** — LiteLLM takes up to 45 seconds on first start (DB migrations). Check: `docker logs ailocal_litellm`.

**Containers restart-looping** — `docker logs <container>` is fastest. Most common cause: a required `.env` variable is empty.

**Getting your API key:**
```bash
grep LITELLM_MASTER_KEY .env | cut -d= -f2
```
