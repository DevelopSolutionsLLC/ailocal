# ailocal

Run AI coding tools — Claude Code, Codex, VS Code Copilot Chat — against local models on Apple Silicon. No cloud costs, no data leaving your machine, no changes to the tools.

**How it works:** Ollama runs your models natively for Metal/MLX GPU access. LiteLLM sits in front as an OpenAI/Anthropic-compatible proxy, exposing role names (`coder`, `reasoner`, `supervisor`) instead of raw model names. Your tools point at `localhost:4000` instead of Anthropic or OpenAI — everything else stays the same.

**Why over bare Ollama:** one endpoint for all tools; role names decouple client configs from backend models (swap a model without touching any config); automatic fallback chains; optional per-role cloud fallback; minimal footprint (a single small container — the only heavy memory user is Ollama on the host).

New here? Read [CLAUDE.md](CLAUDE.md) for the architecture and file map.

## Requirements

- macOS 13+ (Apple Silicon M1+)
- 64 GB RAM recommended — 32 GB minimum with smaller models
- ~85 GB free disk for the 64 GB profile's models (13–135 GB by tier)

## Setup

```bash
# Prerequisites (skip any you already have)
brew install git jq
brew install --cask docker ollama
# Open Docker Desktop once to accept its license and finish first-run setup.

ollama serve                  # or open Ollama.app
./scripts/install.sh          # deps, .env, models, the service, health check, client configs
./scripts/smoke-test.sh       # verify a real model request succeeds
```

## Security model

LiteLLM binds to `127.0.0.1:4000` (localhost-only); every client authenticates with `LITELLM_MASTER_KEY`. Designed for single-user local use. **To expose on a LAN** (bind `0.0.0.0`): put an authenticating reverse proxy in front, rotate `LITELLM_MASTER_KEY` to a strong value, and never expose port 4000 directly.

## Services

The stack is a **single container** — Postgres, Redis, a reverse proxy, and a web UI were all removed as unnecessary for local single-user use.

| Container | Port | What it does |
|---|---|---|
| **litellm** | 4000 | The only container. One endpoint for all model requests; speaks OpenAI (`/v1/chat/completions`) and Anthropic (`/v1/messages`); routes to Ollama. No database, no cache. |

**Ollama** runs natively on the host at port 11434 (containerizing it on Apple Silicon loses Metal GPU access). LiteLLM reaches it via `host.docker.internal:11434`. Ollama is the only heavy memory user, sized by your model profile.

You can keep Docker Desktop tiny: **Settings → Resources**, CPUs `2`, Memory `2 GB` — the container uses well under 1 GB; the model memory lives in Ollama on the host.

## Role-based routing

LiteLLM exposes **role names only** — no backend model names are visible to clients. The table shows the **64 GB** profile; backends vary by tier (see [Changing models](#changing-models)).

| Role | Backend model (64 GB) | Purpose |
|---|---|---|
| `router` | qwen3.5:9b-mlx | Fast classification, trivial tasks, autocomplete |
| `reasoner` | deepseek-r1:32b | Planning, decomposition, deep reasoning |
| `coder` | qwen3.6:35b-mlx | Implementation, generation, coding tasks |
| `supervisor` | gemma4:31b-mxfp8 | Review, critique, approval gate |
| `embed` | nomic-embed-text | Semantic retrieval and memory — not for chat |

**Never use backend model names directly in client configs or scripts.** Use role names only.

Only `reasoner` streams chain-of-thought; `router`, `coder`, and `supervisor` are execution roles pinned to `reasoning_effort: "none"` so they answer directly. This keeps them responsive in OpenAI-format clients (VS Code Copilot), which render a long reasoning stream as a hang. Want visible thinking? Select `reasoner`.

### Changing models

Model choices live in **one place**: `config/models.yaml` (the active profile, selected from `config/profiles/{16,32,64,128}gb.yaml` by `install.sh`).

```bash
$EDITOR config/models.yaml         # 1. edit backend / num_ctx / vision flag
./scripts/sync-models.sh           # 2. propagate to every generated file
docker compose restart litellm     # 3. reload the proxy  (or ./scripts/start.sh)
```

`sync-models.sh` regenerates the role blocks in `config/litellm/config.yaml` (backend, `num_ctx`, capability flags), the Codex `model_catalog.json`, and backend names in this README and the client `CLAUDE.md`. **Do not hand-edit those generated files.** Capabilities: tool calling everywhere; parallel tool calls everywhere except `reasoner`; reasoning everywhere except `supervisor`; vision/PDF on backends flagged `vision:` in `models.yaml`.

## Client integration

```bash
./scripts/install-clients.sh              # deploy all three
./scripts/install-clients.sh vscode       # or one at a time: vscode | codex | claude
```

It backs up before touching anything and is safe to re-run. For manual/per-session use, `source config/clients/env.sh` (add to `~/.zprofile` to make it permanent) — this exports the Anthropic and OpenAI base URLs + key.

**Claude Code** — writes `~/.claude/settings.json` and `CLAUDE.md`, defaults to the `coder` role; use `/model` to switch mid-session. To switch back to real Anthropic, remove `env.ANTHROPIC_BASE_URL` from `~/.claude/settings.json`.

**Codex CLI** — merges ailocal provider settings into `~/.codex/config.toml` and copies `model_catalog.json` so the picker shows role names. Use `install-clients.sh` rather than copying the template directly.

**VS Code (Copilot Chat)** — connects through the [LiteLLM Connector for Copilot](https://marketplace.visualstudio.com/items?itemName=Gethnet.litellm-connector-copilot) extension (`Gethnet.litellm-connector-copilot`, VS Code 1.120+). Models and capabilities are auto-discovered from LiteLLM's `/v1/model/info`. `install-clients.sh vscode` installs the extension, applies recommended settings (notably `inactivityTimeout: 300` so a cold model load doesn't trip the idle watchdog, and `chat.byokUtilityModelDefault: mainAgent` — see Troubleshooting), and prints the one-time key entry. The API key must be entered by hand once (it lives in VS Code SecretStorage, which no script can write):

1. Copilot Chat → model-picker → **Manage Models…** → **LiteLLM Connector**
2. Base URL `http://localhost:4000`, API Key = your `LITELLM_MASTER_KEY` (from `.env`)
3. `Cmd+Shift+P` → **LiteLLM: Reload Models**

Any extension supporting a custom OpenAI-compatible endpoint (Cline, Continue) also works — point it at `http://localhost:4000/v1` with your key and use a role name as the model.

**Any SDK** — point the OpenAI or Anthropic SDK at the proxy and use a role name:

```python
from openai import OpenAI
client = OpenAI(base_url="http://localhost:4000/v1", api_key="<LITELLM_MASTER_KEY>")
client.chat.completions.create(model="coder", messages=[{"role": "user", "content": "Hello"}])
```

```python
import anthropic
client = anthropic.Anthropic(base_url="http://localhost:4000", api_key="<LITELLM_MASTER_KEY>")
client.messages.create(model="coder", max_tokens=1024, messages=[{"role": "user", "content": "Hello"}])
```

## Operations

```bash
./scripts/start.sh              # start
./scripts/stop.sh               # stop (preserves volumes; --volumes wipes them)
./scripts/teardown.sh           # full removal (--images also removes pulled images)
./scripts/update.sh             # snapshot .env → pull new image → restart
./scripts/doctor.sh             # preflight + health summary (exit 0 healthy / 2 degraded)
./scripts/smoke-test.sh         # verify a real model request succeeds
```

## Cloud fallback

Disabled by default. `.env` carries `ENABLE_CLOUD`, `ANTHROPIC_API_KEY`, `OPENAI_API_KEY` and docker-compose passes them through, but no cloud-backed role aliases exist. To enable a role, add a second entry with the same `model_name` pointing at a cloud model (LiteLLM load-balances / falls back between the two); see the commented block in `config/litellm/config.yaml`. Add your key to `.env` and `docker compose restart litellm`.

## Troubleshooting

**VS Code: "No utility model is configured for 'copilot-utility-small'"** — a VS Code 1.128+ regression for BYOK providers. Set `"chat.byokUtilityModelDefault": "mainAgent"` in settings.json (`install-clients.sh vscode` adds it) and reload the window. This keeps utility calls (titles, summaries) on your selected local model.

**VS Code: model spins and never answers** — an execution role was emitting a long reasoning stream before any content, which the connector renders as a hang. Fixed by `reasoning_effort: "none"` on `router`/`coder`/`supervisor` in `config/litellm/config.yaml`; if you still see it, select a role other than `reasoner`, or confirm the fix is applied and `./scripts/start.sh` reloaded the proxy. (A persistent 401 with "Ensure Key has Bearer prefix" instead means the connector's API key isn't entered — re-enter it via **Chat: Manage Language Models**.)

**LiteLLM won't start** — `docker logs ailocal_litellm`. Usually a YAML error in `config/litellm/config.yaml` or a missing `LITELLM_MASTER_KEY` in `.env`.

**404 on a role name** — Ollama isn't running (`ollama serve`), the backend model isn't pulled (`ollama list`), or the role isn't in `config/litellm/config.yaml`.

**Models unload too fast** — the Ollama macOS app doesn't read `~/.zshrc`; run `./scripts/setup-ollama-env.sh`, restart Ollama, verify with `ollama ps`.

**Containers restart-looping** — `docker logs <container>`. Most common cause: an empty required `.env` variable.

**Get your API key:** `grep LITELLM_MASTER_KEY .env | cut -d= -f2`
