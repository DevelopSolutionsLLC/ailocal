# ailocal

Local AI infrastructure for macOS Apple Silicon. Ollama runs natively for Metal GPU acceleration; everything else runs in Docker Compose.

## Requirements

- macOS 13+ (Apple Silicon M1+)
- 64 GB RAM recommended — 32 GB minimum with smaller models
- ~60 GB free disk for the full model set

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

## Setup

```bash
./scripts/install.sh         # install host deps, generate .env
ollama serve                 # start Ollama (or open Ollama.app)
./scripts/install-models.sh  # pull models (~45+ GB, takes a while)
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
| **litellm** | 4000 | AI proxy. Single endpoint for all model requests. Exposes role-based model names only — clients never reference backend models directly. Caches responses in Redis, logs usage to Postgres. Speaks both OpenAI format (`/v1/chat/completions`) and Anthropic format (`/v1/messages`). |
| **open-webui** | 8081 | Chat interface. Routes through LiteLLM so all requests share caching and routing. Good for testing roles without writing code. |
| **postgres** | — | LiteLLM's database. Stores API keys, spend logs, and model config. Internal only. |
| **redis** | — | Response cache for LiteLLM. Identical requests within 10 minutes return instantly from cache instead of hitting Ollama. Internal only. |
| **caddy** | 80, 443 | Reverse proxy. `http://localhost/v1/*` routes to LiteLLM; everything else to Open WebUI. All services are also accessible directly on their own ports. |
| **prometheus** | 9090 | Scrapes LiteLLM `/metrics` every 15 seconds — request counts, latency, and token usage per role. |
| **grafana** | 3000 | Dashboard UI for Prometheus. Prometheus is pre-wired as the default datasource on first boot. |

**Ollama** runs on the host at port 11434 — not in Docker. Containerized Ollama on Apple Silicon loses Metal GPU access, so it runs natively and all containers reach it via `host.docker.internal:11434`.

## Role-based routing

LiteLLM exposes **role names only** — no backend model names are visible to external clients. All orchestration is external.

| Role | Backend model | Purpose |
|---|---|---|
| `router` | qwen3:8b | Fast classification, trivial tasks, autocomplete |
| `reasoner` | deepseek-r1:32b | Planning, decomposition, deep reasoning |
| `coder` | qwen3.6:27b | Implementation, generation, coding tasks |
| `supervisor` | gemma4:31b | Review, critique, approval gate |
| `embed` | nomic-embed-text | Semantic retrieval and memory — not for chat |

**Never use backend model names directly in client configs or scripts.** Use role names only.

## Client integration

Source this once to configure your shell for all tools:

```bash
source config/clients/env.sh
```

Add that line to `~/.zprofile` to make it permanent.

---

### Claude Code

**Per-session:**
```bash
export ANTHROPIC_BASE_URL=http://localhost:4000
export ANTHROPIC_API_KEY=$(grep LITELLM_MASTER_KEY .env | cut -d= -f2)
claude
```

**Permanent** — add the source line to `~/.zprofile`. To set env vars in `~/.claude/settings.json`, merge the contents of `config/clients/claude-code.json` into your existing file. Claude Code is configured to use the `coder` role by default.

---

### Claude Desktop (Cowork)

The Claude Desktop app connects to Anthropic's servers directly — it can't be redirected to a local proxy. However, any subprocess it spawns inherits your shell environment, so adding the env file to `~/.zprofile` means all agent work routes through ailocal automatically.

---

### Codex CLI

**Per-session:**
```bash
export OPENAI_BASE_URL=http://localhost:4000/v1
export OPENAI_API_KEY=$(grep LITELLM_MASTER_KEY .env | cut -d= -f2)
codex
```

**Permanent** — the `~/.zprofile` source line covers Codex too. To use a config file, see `config/clients/codex-config.yaml` — copy it to `~/.codex/config.yaml` only if that file doesn't already exist.

Codex is configured to use the `coder` role by default.

---

### VS Code

Launch VS Code with ailocal env vars active so all extensions pick them up:

```bash
source config/clients/env.sh && code .
```

**Continue extension** — if you don't have an existing `~/.continue/config.json`, use `config/clients/vscode-continue.json` as a starting point. Replace `<LITELLM_MASTER_KEY>` with your key from `.env`. If you already have a Continue config, add the role entries manually rather than overwriting.

Gives you four roles in the Continue panel (`coder`, `reasoner`, `supervisor`, `router`), tab autocomplete via `router`, and codebase embeddings via `embed`.

Any extension that supports a custom OpenAI-compatible endpoint (Cline, etc.) works the same way — point it at `http://localhost:4000/v1` with your `LITELLM_MASTER_KEY` and use a role name as the model.

---

### Any OpenAI SDK

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://localhost:4000/v1",
    api_key="<your LITELLM_MASTER_KEY>",
)

response = client.chat.completions.create(
    model="coder",   # role names: router | reasoner | coder | supervisor | embed
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
    model="coder",   # role names: router | reasoner | coder | supervisor | embed
    max_tokens=1024,
    messages=[{"role": "user", "content": "Hello"}],
)
```

---

## Operations

```bash
./scripts/start.sh              # start services
./scripts/stop.sh               # stop (preserves volumes)
./scripts/stop.sh --volumes     # stop and wipe all volume data
./scripts/teardown.sh           # full removal of containers, volumes, network
./scripts/teardown.sh --images  # also remove pulled Docker images
./scripts/update.sh             # backup → pull new images → restart
./scripts/backup.sh             # config + postgres dump to ./backups/
./scripts/restore.sh            # restore from most recent backup
./scripts/healthcheck.sh        # check all services and endpoints
```

## Cloud fallback

Disabled by default. The `.env` generated by `install.sh` includes `ENABLE_CLOUD`, `ANTHROPIC_API_KEY`, and `OPENAI_API_KEY` fields, and `docker-compose.yml` passes them to LiteLLM — but no cloud-backed role aliases exist in `config/litellm/config.yaml`.

To enable cloud for a role, add a second entry with the same role name pointing to a cloud model:

```yaml
- model_name: reasoner
  litellm_params:
    model: anthropic/claude-opus-4-8
    api_key: os.environ/ANTHROPIC_API_KEY
```

LiteLLM will load-balance or fall back between the two entries for that role. Add your key to `.env` and restart: `docker compose restart litellm`.

## Troubleshooting

**LiteLLM won't start** — depends on Postgres being healthy first. Check: `docker logs ailocal_postgres`. Usually a wrong or missing `POSTGRES_PASSWORD` in `.env`.

**404 on role name** — either Ollama isn't running (`ollama serve`), the backend model for that role isn't pulled (`ollama list`), or the role isn't defined in `config/litellm/config.yaml`.

**Open WebUI shows no models** — LiteLLM takes up to 45 seconds on first start (DB migrations). Check: `docker logs ailocal_litellm`.

**Containers restart-looping** — `docker logs <container>` is fastest. Most common cause: a required `.env` variable is empty.

**Getting your API key:**
```bash
grep LITELLM_MASTER_KEY .env | cut -d= -f2
```
