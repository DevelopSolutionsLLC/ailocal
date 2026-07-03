# ailocal

Run AI coding tools — Claude Code, Codex, VS Code Copilot Chat — against local models on Apple Silicon. No cloud API costs, no data leaving your machine, no code changes to your tools.

**How it works:** Ollama runs your models natively for full Metal/MLX GPU access. LiteLLM sits in front as an OpenAI/Anthropic-compatible proxy, exposing role names (`coder`, `reasoner`, `supervisor`) instead of raw model names. Your tools point at `localhost:4000` instead of Anthropic or OpenAI — everything else stays the same.

**Why this over bare Ollama:**
- Single endpoint for all tools — configure once, works everywhere
- Role names decouple your client configs from backend models — swap `gemma4:31b-mxfp8` for something better without touching a single config file
- Automatic fallback chains — degrade a role to a lighter one when it errors or overflows its context window
- Optional cloud fallback per role — route `reasoner` to Claude Opus when the local model isn't enough
- Minimal footprint — one small container (no database, no cache); the only heavy memory user is Ollama on the host

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

LiteLLM binds to `127.0.0.1:4000` (localhost-only) and every client authenticates with the `LITELLM_MASTER_KEY`. The stack is designed for single-user local use.

**If you expose this on a LAN** (changing the bind to `0.0.0.0`): put an authenticating reverse proxy in front of LiteLLM, rotate `LITELLM_MASTER_KEY` to a strong unique value, and never expose port 4000 directly.

## Services

The stack is a **single container** — everything else (Postgres, Redis, a reverse proxy, a web UI) was removed as unnecessary for local single-user use. See the notes in `docker-compose.yml`.

| Container | Port | What it does |
|---|---|---|
| **litellm** | 4000 | The only container. Single endpoint for all model requests; exposes role-based model names, speaks both OpenAI (`/v1/chat/completions`) and Anthropic (`/v1/messages`) formats, and routes to Ollama. Runs with **no database and no cache**. |

**Ollama** runs natively on the host at port 11434 — not in Docker (containerizing it on Apple Silicon loses Metal GPU access). LiteLLM reaches it via `host.docker.internal:11434`. Ollama is the only heavy memory user, sized by your model profile.

## Docker Desktop resource tuning

With a single small container you can keep Docker's footprint tiny:

1. Open **Docker Desktop → Settings → Resources**
2. Set **CPUs** to `2` — LiteLLM is a single async worker; the real compute is Ollama on the host
3. Set **Memory** to `2 GB` — the LiteLLM container uses well under 1 GB
4. Click **Apply & Restart**

The model memory (tens of GB) lives in Ollama on the host, not in Docker.

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

`sync-models.sh` regenerates, from `models.yaml`, the canonical role blocks in `config/litellm/config.yaml` (backend, `num_ctx`, and the `model_info` capability flags — tool calling, vision/PDF, reasoning, token budgets), the Codex `model_catalog.json`, and the backend names in this README and the client `CLAUDE.md` template. **Do not hand-edit those generated files** — capability flags in `config.yaml` and the role table above are produced by the generator. Capabilities per role: tool calling everywhere; parallel tool calls everywhere except `reasoner` (DeepSeek-R1); reasoning everywhere except `supervisor` (Gemma is not a thinking model); vision/PDF on multimodal backends (`coder` except 16 GB, `supervisor` on all tiers), driven by the `vision:` flag.

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
./scripts/update.sh            # snapshot .env → pull new image → restart
./scripts/doctor.sh            # one-command preflight + health summary (exit 0/2)
./scripts/smoke-test.sh        # verify a real model request succeeds
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

**LiteLLM won't start** — check `docker logs ailocal_litellm`. Most often a YAML error in `config/litellm/config.yaml` or a missing `LITELLM_MASTER_KEY` in `.env`.

**404 on role name** — either Ollama isn't running (`ollama serve`), the backend model for that role isn't pulled (`ollama list`), or the role isn't defined in `config/litellm/config.yaml`.

**Models unload too fast** — the Ollama macOS app doesn't read `~/.zshrc`; set keep-alive where it can see it with `./scripts/setup-ollama-env.sh`, then restart Ollama. Verify with `ollama ps` (the UNTIL column).

**Containers restart-looping** — `docker logs <container>` is fastest. Most common cause: a required `.env` variable is empty.

**Getting your API key:**
```bash
grep LITELLM_MASTER_KEY .env | cut -d= -f2
```
