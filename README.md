# ailocal

Run AI coding tools — Claude Code, Codex, VS Code Copilot Chat — against local models on Apple Silicon. No cloud costs, no data leaving your machine, no changes to the tools.

**How it works:** Ollama runs your models natively for Metal/MLX GPU access. LiteLLM sits in front as an OpenAI/Anthropic-compatible proxy, exposing role names (`coder-main`, `deep-think-more`, `supervisor`) instead of raw model names. Your tools point at `localhost:4000` instead of Anthropic or OpenAI — everything else stays the same.

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

`install.sh` offers **production autostart**: answer `y` and it runs
`scripts/setup-startup.sh`, which installs launchd LaunchAgents so at every login
`ollama serve` starts (env baked in — `OLLAMA_MODELS=/Users/Shared/ollama/models`,
`MAX_LOADED=3`, `NUM_PARALLEL=2`, `KEEP_ALIVE=-1`, flash-attn, q8 KV cache) and the
coder-main model preloads once Ollama is healthy. Disable Ollama.app's "launch at login"
(menubar → Settings) so two servers don't fight over port 11434. Re-run any time:
`./scripts/setup-startup.sh --model coder-main` (add `--with-litellm` to also run LiteLLM
natively; `--uninstall` to remove). Answer `n` to keep using Ollama.app and only set
runtime env vars (`scripts/setup-ollama-env.sh`).

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
| `coder-main` | qwen3-coder:30b | Primary repository coding, logic, syntax — the daily driver |
| `coder-agent` | qwen3.6:35b-mlx | Multi-step planning / agentic orchestration (Cline) |
| `coder-fast` | qwen2.5-coder:3b | Fast small tasks; IDE autocomplete (FIM) |
| `deep-think` | deepseek-r1:14b-qwen-distill-q8_0 | Lighter reasoning, thinking merged into the answer text |
| `deep-think-more` | deepseek-r1:32b | Deep reasoning / decomposition, thinking merged |
| `supervisor` | gemma4:31b-mxfp8 | Review, critique, approval gate |
| `embed` | nomic-embed-text | Semantic retrieval and memory — not for chat |

**Never use backend model names directly in client configs or scripts.** Use role names only.

**Personas & sampling.** Each role gets an "Opus-like" grounded engineering persona injected server-side by the `persona_injector` LiteLLM hook (from `config/personas/<role>.md`) — merged into the client's system message, so it survives even when the client sends its own. Reasoners are the exception: per DeepSeek's official guidance they get **no** persona and run at temperature 0.6 / top-p 0.95. Coders use Qwen's recommended sampling (0.7 / 0.8 / top-k 20), supervisor uses Gemma's (1.0 / 0.95 / top-k 64). All sampling lives in `config/models.yaml`.

**Reasoning behavior by role.** `coder-*` and `supervisor` are execution roles pinned to `reasoning_effort: "none"` so they answer directly — a long invisible reasoning stream reads as a hang in OpenAI-format clients (VS Code Copilot). The `deep-think*` roles are the thinking tiers; their reasoning stream is merged into the answer text (`merge_reasoning_content_in_choices`) so it renders as visible `<think>…</think>` content instead of a silent "Considering…" spinner.

### Changing models

Model choices live in **one place**: `config/models.yaml` (the active profile, selected from `config/profiles/{16,32,64,128}gb.yaml` by `install.sh`).

```bash
$EDITOR config/models.yaml         # 1. edit backend / num_ctx / vision flag
./scripts/sync-models.sh           # 2. propagate to every generated file
docker compose restart litellm     # 3. reload the proxy  (or ./scripts/start.sh)
```

`sync-models.sh` regenerates the `model_list` block in `config/litellm/config.yaml` (backend, `num_ctx`, sampling, capability flags — between the GENERATED markers) and the Codex `model_catalog.json`. **Do not hand-edit those generated regions.** Capabilities: tool calling everywhere; parallel tool calls everywhere except the `deep-think*` reasoners; reasoning (streamed `<think>`) on the `deep-think*` roles only; vision/PDF on backends flagged `vision:` in `models.yaml`. Backend model tags are served directly (no persona overlays); the persona is injected by the `persona_injector` hook.

## Client integration

```bash
./scripts/install-clients.sh              # deploy all three
./scripts/install-clients.sh vscode       # or one at a time: vscode | codex | claude
```

The installer is safe to re-run and backs up before touching anything. Client state lives in `~/.config/ailocal/` (XDG-style) — cloud clients (`~/.claude`, `~/.codex`) are never touched, so cloud and local sessions coexist safely.

**Claude Code** — run `claude-local` to start a Claude Code session pointed at local models (the wrapper sets `CLAUDE_CONFIG_DIR=~/.config/ailocal/claude` + per-invocation env vars). Defaults to the `coder-main` role; use `/model` to switch mid-session. Plain `claude` still connects to Anthropic cloud.

**Codex CLI** — run `codex-local` for local models (sets `CODEX_HOME=~/.config/ailocal/codex` + env vars). The model picker shows role names. Plain `codex` still connects to OpenAI cloud.

**VS Code (Copilot Chat)** — run the `ailocal` profile launcher (`ailocal-code` or VS Code **Remote: Open Local Folder in Codespace** → select "ailocal"). Opens a new VS Code window with ailocal environment vars pre-set. Models and capabilities are auto-discovered from LiteLLM's `/v1/model/info`. 

To configure Copilot Chat manually or in an existing window, connect the [LiteLLM Connector for Copilot](https://marketplace.visualstudio.com/items?itemName=Gethnet.litellm-connector-copilot) extension:

1. Copilot Chat → model-picker → **Manage Models…** → **LiteLLM Connector**
2. Base URL `http://localhost:4000`, API Key = your `LITELLM_MASTER_KEY` (from `.env`)
3. `Cmd+Shift+P` → **LiteLLM: Reload Models**

The installer handles extension install, recommended settings (`inactivityTimeout: 300`, `chat.byokUtilityModelDefault: mainAgent`), and prints the one-time key entry instructions. Any extension supporting a custom OpenAI-compatible endpoint (Cline, Continue) also works — point it at `http://localhost:4000/v1` with your key and use a role name as the model.

**For full-shell environment (optional)** — `source ~/.config/ailocal/env` redirects both SDKs (Claude Code, Codex, and any Python/JS SDK) to local models for that shell session only. The wrappers above are the recommended path.

**Uninstall** — `./scripts/teardown.sh --clients` removes the installer's `.zshrc` markers and `~/.config/ailocal` (backs up the API key first).

**Any SDK** — point the OpenAI or Anthropic SDK at the proxy and use a role name:

```python
from openai import OpenAI
client = OpenAI(base_url="http://localhost:4000/v1", api_key="<LITELLM_MASTER_KEY>")
client.chat.completions.create(model="coder-main", messages=[{"role": "user", "content": "Hello"}])
```

```python
import anthropic
client = anthropic.Anthropic(base_url="http://localhost:4000", api_key="<LITELLM_MASTER_KEY>")
client.messages.create(model="coder-main", max_tokens=1024, messages=[{"role": "user", "content": "Hello"}])
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

**VS Code: model spins on "Considering…" and never answers** — you selected a thinking model whose reasoning streams invisibly. Use `coder-main` (or `coder-fast`) for direct answers, or `deep-think` / `deep-think-more` for visible thinking (their reasoning is merged into the answer text). If you hit "Message exceeds token limit," pick a role with a larger window (`coder-main`/`coder-agent` are 64K, `deep-think*` 64K, `supervisor` 32K, `coder-fast` 16K). (A persistent 401 with "Ensure Key has Bearer prefix" instead means the connector's API key isn't entered — re-enter it via **Chat: Manage Language Models**.)

**LiteLLM won't start** — `docker logs ailocal_litellm`. Usually a YAML error in `config/litellm/config.yaml` or a missing `LITELLM_MASTER_KEY` in `.env`.

**404 on a role name** — Ollama isn't running (`ollama serve`), the backend model isn't pulled (`ollama list`), or the role isn't in `config/litellm/config.yaml`.

**Claude Code `/model` only shows Opus/Sonnet/Haiku** — gateway discovery isn't on (needs Claude Code v2.1.129+). The `claude-local` wrapper sets `CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY=1` so `/model` lists every LiteLLM role (`coder-main`, `coder-agent`, `coder-fast`, `deep-think`, `deep-think-more`, `supervisor`) under "From gateway", and remaps the built-in slots — Opus→`deep-think-more`, Sonnet→`coder-main`, Haiku→`coder-fast` — so background calls stay local. If you don't see them, reload your shell (`source ~/.zshrc`) and relaunch `claude-local`.

**Models unload too fast** — the Ollama macOS app doesn't read `~/.zshrc`; run `./scripts/setup-ollama-env.sh`, restart Ollama, verify with `ollama ps`.

**Containers restart-looping** — `docker logs <container>`. Most common cause: an empty required `.env` variable.

**Get your API key:** `grep LITELLM_MASTER_KEY .env | cut -d= -f2`
