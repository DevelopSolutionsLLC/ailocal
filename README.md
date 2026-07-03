# ailocal

Run AI coding tools — Claude Code, Codex, VS Code Copilot Chat — against local models on Apple Silicon. No cloud API costs, no data leaving your machine, no code changes to your tools.

**How it works:** Ollama runs your models natively for full Metal/MLX GPU access. LiteLLM sits in front as an OpenAI/Anthropic-compatible proxy, exposing role names (`coder`, `reasoner`, `supervisor`) instead of raw model names. Your tools point at `localhost:4000` instead of Anthropic or OpenAI — everything else stays the same.

**Why this over bare Ollama:**
- Single endpoint for all tools — configure once, works everywhere
- Role names decouple your client configs from backend models — swap `gemma4:31b-mxfp8` for something better without touching a single config file
- Response caching, usage logging, and fallback chains built in
- Optional cloud fallback per role — route `reasoner` to Claude Opus when the local model isn't enough

---

## Requirements

- macOS 13+ (Apple Silicon M1+)
- 64 GB RAM recommended — 32 GB minimum with smaller models
- ~85 GB free disk for the 64 GB profile's model set (varies 13–135 GB by hardware tier)

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
ollama serve                  # start Ollama first (or open Ollama.app)
./scripts/install.sh          # does everything: deps, .env, models (~85 GB on 64 GB tier), services, healthcheck, then prompts for client configs
./scripts/smoke-test.sh       # verify a real model request succeeds
```

## Security model

All ports bind to `127.0.0.1` (localhost-only). The stack is designed for single-user local use.

**If you expose this stack on a LAN** (changing port binds to `0.0.0.0`):
- Set `WEBUI_AUTH=true` in `docker-compose.yml` for Open WebUI (OWASP A07:2021 — Authentication Failures)
- Add Caddy `basic_auth` middleware on `/v1/*` in the Caddyfile
- Rotate `LITELLM_MASTER_KEY` and `ADMIN_PASSWORD` to strong unique values before doing so

`WEBUI_AUTH=false` is intentional for the default local-only setup and is not a vulnerability in that context.

## Services

| Container | Port | What it does |
|---|---|---|
| **litellm** | 4000 | AI proxy. Single endpoint for all model requests. Exposes role-based model names only — clients never reference backend models directly. Caches responses in Redis, logs usage to Postgres. Speaks both OpenAI format (`/v1/chat/completions`) and Anthropic format (`/v1/messages`). |
| **open-webui** | 8081 | Chat interface. Routes through LiteLLM so all requests share caching and routing. Good for testing roles without writing code. |
| **postgres** | — | LiteLLM's database. Stores API keys, spend logs, and model config. Internal only. |
| **redis** | — | Response cache for LiteLLM. Identical requests within the cache TTL (1 hour) return instantly from cache instead of hitting Ollama. Internal only. |
| **caddy** | 80, 443 | Reverse proxy. `http://localhost/v1/*` routes to LiteLLM; everything else to Open WebUI. All services are also accessible directly on their own ports. |

**Ollama** runs on the host at port 11434 — not in Docker. Containerized Ollama on Apple Silicon loses Metal GPU access, so it runs natively and all containers reach it via `host.docker.internal:11434`.

## Docker Desktop resource tuning

By default Docker Desktop allocates as much RAM as the VM needs, which can balloon to 14 GB+. After setup, lock it down:

1. Open **Docker Desktop → Settings → Resources**
2. Set **CPUs** to `2` — LiteLLM runs a single async worker; the real compute is in Ollama on the host
3. Set **Memory** to `4 GB` — actual container usage is ~1.7 GB; this gives 2× headroom
4. Set **Swap** to `2 GB` — safety net for VM overhead
5. Click **Apply & Restart**

Expected container memory usage after tuning:

| Container | Typical |
|---|---|
| litellm | ~800 MiB |
| open-webui | ~600 MiB |
| postgres | ~80 MiB |
| redis | ~12 MiB |
| caddy | ~16 MiB |

## Role-based routing

LiteLLM exposes **role names only** — no backend model names are visible to external clients. All orchestration is external.

The table below shows the **64 GB** profile's backends. The specific backend per role varies by hardware tier — see [Changing models](#changing-models).

| Role | Backend model (64 GB) | Purpose |
|---|---|---|
| `router` | qwen3.5:9b-mlx | Fast classification, trivial tasks, autocomplete |
| `reasoner` | deepseek-r1:32b | Planning, decomposition, deep reasoning |
| `coder` | qwen3.6:35b-mlx | Implementation, generation, coding tasks |
| `supervisor` | gemma4:31b-mxfp8 | Review, critique, approval gate |
| `embed` | nomic-embed-text | Semantic retrieval and memory — not for chat |

**Never use backend model names directly in client configs or scripts.** Use role names only.

### Changing models

Model choices live in **one place**: `config/models.yaml` (the active profile). The per-hardware presets are in `config/profiles/{16,32,64,128}gb.yaml`, and `install.sh` copies the tier matching your RAM into `models.yaml`.

To change a model or a capability:

```bash
# 1. Edit the role's backend / num_ctx / vision flag
$EDITOR config/models.yaml

# 2. Propagate to every generated file
./scripts/sync-models.sh

# 3. Reload the proxy so it serves the new model_info
docker compose restart litellm     # or ./scripts/start.sh
```

`sync-models.sh` regenerates, from `models.yaml`, the canonical role blocks in `config/litellm/config.yaml` (backend, `num_ctx`, and the `model_info` capability flags — tool calling, vision/PDF, reasoning, token budgets), the Codex `model_catalog.json`, and the backend names in this README / `AGENTS.md` / `CLAUDE.md`. **Do not hand-edit those generated files** — capability flags in `config.yaml` and the role table above are produced by the generator. Capabilities per role: tool calling everywhere; parallel tool calls everywhere except `reasoner` (DeepSeek-R1); reasoning everywhere except `supervisor` (Gemma is not a thinking model); vision/PDF on multimodal backends (`coder` except 16 GB, `supervisor` on all tiers), driven by the `vision:` flag.

## Client integration

The quickest path is the install script — it deploys configs to all three tools at once, handles merging with existing configs, and backs up before touching anything:

```bash
./scripts/install-clients.sh              # deploy all three
./scripts/install-clients.sh vscode       # VS Code only
./scripts/install-clients.sh codex        # Codex only
./scripts/install-clients.sh claude       # Claude Code only
```

For manual setup or per-session use, source the env helper first:

```bash
source config/clients/env.sh
```

Add that line to `~/.zprofile` to make it permanent.

---

### Claude Code

`install-clients.sh claude` writes `~/.claude/settings.json` and `~/.claude/CLAUDE.md`. Claude Code is configured to use the `coder` role by default; use `/model` to switch roles mid-session.

**Manual / per-session:**
```bash
export ANTHROPIC_BASE_URL=http://localhost:4000
export ANTHROPIC_API_KEY=$(grep LITELLM_MASTER_KEY .env | cut -d= -f2)
claude
```

> To switch back to real Anthropic: `unset ANTHROPIC_BASE_URL` in your session, or remove the `env.ANTHROPIC_BASE_URL` entry from `~/.claude/settings.json`.

---

### Codex CLI

`install-clients.sh codex` merges ailocal provider settings into `~/.codex/config.toml` without overwriting existing computer-use, plugin, or MCP entries. It also copies `model_catalog.json` so Codex's model picker shows the role names.

**Manual / per-session:**
```bash
export OPENAI_BASE_URL=http://localhost:4000/v1
export OPENAI_API_KEY=$(grep LITELLM_MASTER_KEY .env | cut -d= -f2)
codex
```

**Config file:** `config/clients/codex-config.toml` is the source template — do not copy it directly to `~/.codex/config.toml` if you already have one there; use `install-clients.sh` instead to merge safely.

---

### VS Code (Copilot Chat)

VS Code connects through the **[LiteLLM Connector for Copilot](https://marketplace.visualstudio.com/items?itemName=Gethnet.litellm-connector-copilot)** extension (`Gethnet.litellm-connector-copilot`, requires VS Code 1.120+). The extension stores the Base URL + API key in VS Code's encrypted SecretStorage — a boundary no script can write to — so the key is entered **once**, by hand:

```bash
code --install-extension Gethnet.litellm-connector-copilot
```

1. Copilot Chat → model-picker dropdown → **Manage Models…** (or `Cmd+Shift+P` → **Chat: Manage Language Models**)
2. Pick **LiteLLM Connector**, then enter:
   - **Base URL:** `http://localhost:4000`
   - **API Key:** your `LITELLM_MASTER_KEY` (from `.env`)
3. `Cmd+Shift+P` → **LiteLLM: Reload Models**

Models **and their capabilities** (vision, tool calling, context window) are auto-discovered from LiteLLM's `/v1/model/info` — nothing is listed manually. `install-clients.sh vscode` no longer writes a config file (the old `chatLanguageModels.json` "custom endpoint" ignores the key and sends an empty Bearer); it only clears that stale entry if present and prints these steps.

Any extension that supports a custom OpenAI-compatible endpoint (Cline, Continue, etc.) also works — point it at `http://localhost:4000/v1` with your `LITELLM_MASTER_KEY` and use a role name as the model.

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
./scripts/start.sh             # start services
./scripts/stop.sh              # stop (preserves volumes)
./scripts/stop.sh --volumes    # stop and wipe all volume data
./scripts/teardown.sh          # full removal of containers, volumes, network
./scripts/teardown.sh --images # also remove pulled Docker images
./scripts/update.sh            # backup → pull new images → restart
./scripts/backup.sh            # config + postgres dump to ./backups/
./scripts/restore.sh           # restore from most recent backup
./scripts/doctor.sh            # one-command preflight + health summary
./scripts/smoke-test.sh        # verify a real model request succeeds
./scripts/healthcheck.sh       # check all services and endpoints
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
