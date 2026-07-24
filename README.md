# ailocal

Run AI coding tools — Claude Code, Codex, VS Code Copilot Chat — against local models on Apple Silicon. No cloud costs, no data leaving your machine, no changes to the tools.

**How it works:** Ollama runs your models natively for Metal/MLX GPU access. LiteLLM sits in front as an OpenAI/Anthropic-compatible proxy, exposing capability names (`architect`, `coder`, `reviewer`, `autocomplete`) instead of raw model names. Your tools point at `localhost:4000` instead of Anthropic or OpenAI — everything else stays the same.

**Why over bare Ollama:** one endpoint for all tools; capability names decouple client configs from backend models (swap a model without touching any config); automatic fallback chains; optional per-role cloud fallback; minimal footprint (a single small container — the only heavy memory user is Ollama on the host).

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
`MAX_LOADED=5`, `NUM_PARALLEL=2`, `KEEP_ALIVE=-1`, flash-attn, q8 KV cache) and the
architecture model preloads once Ollama is healthy. Disable Ollama.app's "launch at login"
(menubar → Settings) so two servers don't fight over port 11434. Re-run any time:
`./scripts/setup-startup.sh --model architecture` (add `--with-litellm` to also run LiteLLM
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

LiteLLM exposes **capability names only** — no backend model names are visible to clients. Agents
request a capability; the router owns the backend, context, sampling, and lifecycle. The table shows
the **64 GB** profile (see [Changing models](#changing-models)). Source of truth: two files —
`config/profiles/<tier>.yaml` (what each capability is) and `config/clients.yaml` (which capability each client uses).

| Capability | Backend model (64 GB) | ctx / keep_alive | Purpose |
|---|---|---|---|
| `architecture` | qwen3-coder:30b | 64K / -1 (resident) | Design, deep reasoning, large interactive/agent prompts — the shared big-context hub for every client |
| `implementation` | qwen2.5-coder:14b-instruct-q4_K_M | 16K / 20m | Implementation, features, tests, everyday refactoring |
| `review` | deepseek-coder-v2:16b-lite-instruct-q4_K_M | 16K / 20m | Code review, bug & security detection, alternatives |
| `completion` | qwen2.5-coder:3b-instruct-q4_K_M | 4K / -1 (resident) | Inline completion (FIM), quick fixes, small transforms |
| `embeddings` | nomic-embed-text | 8K / -1 (resident) | Semantic retrieval and memory — infrastructure, not chat |

Also accepted (aliased onto the above): the `claude-*`/`gpt-*` compatibility IDs Claude Code and the
OpenAI SDK hard-code (external adapters). The old ailocal role names
(`coder-main`/`deep-think*`/`supervisor`/…) and the `local/*` namespace have been removed.
**Never use backend model names directly in client configs or scripts.** Use capability names only.

**Personas & sampling.** `architecture`, `implementation`, and `review` get a grounded engineering
persona injected server-side by the `persona_injector` hook (from `config/instructions/<capability>.md`)
— merged into the client's system prompt (the `messages[]` system entry for OpenAI clients, or the
top-level `system` field for Claude Code's Anthropic `/v1/messages` route), so it survives even when the
client sends its own. `completion` and `embeddings` are persona-free by design (lean/infra). Sampling
lives in `config/profiles/<tier>.yaml` (architecture/implementation temp 0.2, review 0.1, completion 0).

**No reasoning tier right now.** None of the installed models emit `<think>` — the deepseek-r1
reasoners were removed, and `qwen3-coder` is Qwen's non-thinking variant. Every capability carries
`additional_drop_params: ["thinking", "reasoning_effort"]` + `think: false` so a client sending
`thinking` doesn't 400 and a backend's default reasoning can't hang VS Code Copilot. The reasoning
path (merged `<think>` via `merge_reasoning_content_in_choices`) still exists in `sync-models.py`; a
commented `reasoning` slot in `config/profiles/<tier>.yaml` restores the tier in one repoint.

### Changing models

Model choices live in **one place**: `config/profiles/<tier>.yaml` (the active profile, selected from `config/profiles/{16,32,64,128}gb.yaml` by `install.sh`).

```bash
$EDITOR config/profiles/<tier>.yaml         # 1. edit backend / num_ctx / vision flag
./scripts/sync-models.sh           # 2. propagate to every generated file
docker compose restart litellm     # 3. reload the proxy  (or ./scripts/start.sh)
```

`sync-models.sh` regenerates the `model_list` block in `config/litellm/config.yaml` (backend, `num_ctx`, sampling, capability flags — between the GENERATED markers) and the Codex `model_catalog.json`. **Do not hand-edit those generated regions.** Capabilities: tool calling everywhere; parallel tool calls everywhere (no reasoner is installed); reasoning (streamed `<think>`) only if a `reasoner` slot is enabled; vision/PDF on backends flagged `vision:` in the active profile. Backend model tags are served directly (no persona overlays); the persona is injected by the `persona_injector` hook.

## Client integration

```bash
./scripts/install-clients.sh              # deploy all three
./scripts/install-clients.sh vscode       # or one at a time: vscode | codex | claude
```

The installer is safe to re-run and backs up before touching anything. Client state lives in `~/.config/ailocal/` (XDG-style) — cloud clients (`~/.claude`, `~/.codex`) are never touched, so cloud and local sessions coexist safely.

**Claude Code** — run `claude-local` to start a Claude Code session pointed at local models (the wrapper sets `CLAUDE_CONFIG_DIR=~/.config/ailocal/claude` + per-invocation env vars). Launches on `architect` — the heavy tier — and delegates via the `/model` picker or subagents; use `/model` to switch mid-session. Plain `claude` still connects to Anthropic cloud.

**Codex CLI** — run `codex-local` for local models (sets `CODEX_HOME=~/.config/ailocal/codex` + env vars). The model picker shows role names. Plain `codex` still connects to OpenAI cloud.

**VS Code (Copilot Chat)** — run `ailocal-code [path]` to open the isolated `ailocal` VS Code profile (`code --profile ailocal`), which keeps these extensions and settings out of your normal window. Models and capabilities are auto-discovered from LiteLLM's `/v1/model/info`.

To configure Copilot Chat manually or in an existing window, connect the [LiteLLM Connector for Copilot](https://marketplace.visualstudio.com/items?itemName=Gethnet.litellm-connector-copilot) extension:

1. Copilot Chat → model-picker → **Manage Models…** → **LiteLLM Connector**
2. Base URL `http://localhost:4000`, API Key = your `LITELLM_MASTER_KEY` (from `.env`)
3. `Cmd+Shift+P` → **LiteLLM: Reload Models**

The installer handles extension install, recommended settings (`inactivityTimeout: 300`, `chat.byokUtilityModelDefault: mainAgent`), and prints the one-time key entry instructions. Any extension supporting a custom OpenAI-compatible endpoint (Cline, Continue) also works — point it at `http://localhost:4000/v1` with your key and use a role name as the model.

### What the installer deploys beyond endpoints

- **Subagents and commands** (Claude Code): `planner`, `implementer`, `reviewer`, `search`, `tester`, plus `/local-build` and `/analyze-repo`. Each subagent is pinned to the role that suits it, so heavy search doesn't occupy the orchestrator's context. Codex gets the equivalent prompts.
- **Per-session scratchpad**: a shared `SessionStart` hook gives every session its own `/tmp/scratchpad/<tool>-<session_id>/`, so concurrent Claude/Codex sessions never collide over temp files. Wired identically for Claude Code (`settings.json`) and Codex (`config.toml`).

### Layering Cadence on top (optional)

[Cadence](https://github.com/DevelopSolutionsLLC/cadence) can deploy its rules, hooks, and
repository-intelligence agents into these same local roots, so `claude-local` gets the same
workflow tooling as a cloud session:

```bash
cadence install claude --root ~/.config/ailocal/claude
cadence install codex  --root ~/.config/ailocal/codex
CLAUDE_CONFIG_DIR=~/.config/ailocal/claude \
  claude mcp add grepai -s user -- grepai mcp-serve --workspace DevelopSolutions
```

MCP servers are registered per-root, so this does not touch your cloud `~/.claude` setup. Cadence
keeps ailocal's five subagents and **appends** its operating rules into them between
`<!-- cadence:start -->` / `<!-- cadence:end -->` markers, adding only `repository-health` as a new
agent — it does not install competing `Explore`/`Plan`/`verify` agents, because `search`/`planner`/
`tester` already cover those jobs.

**Order matters:** `install-clients.sh` rewrites the subagent files and strips that block. Install
cadence → ailocal → cadence-local, and re-run the Cadence installer after any `install-clients.sh`
run. `cadence doctor --root ~/.config/ailocal/claude` reports `NO-OVERLAY` when the block is gone.

**For full-shell environment (optional)** — `source ~/.config/ailocal/env` redirects both SDKs (Claude Code, Codex, and any Python/JS SDK) to local models for that shell session only. The wrappers above are the recommended path.

**Uninstall** — `./scripts/teardown.sh --clients` removes the installer's `.zshrc` markers and `~/.config/ailocal` (backs up the API key first).

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

**VS Code: model spins on "Considering…" and never answers** — a backend emitted reasoning that streams invisibly. All installed models answer directly (none stream `<think>`). If you hit "Message exceeds token limit," pick a capability with a larger window (`architect` 32K, `coder`/`reviewer` 16K, `autocomplete` 4K). (A persistent 401 with "Ensure Key has Bearer prefix" instead means the connector's API key isn't entered — re-enter it via **Chat: Manage Language Models**.)

**LiteLLM won't start** — `docker logs ailocal-litellm`. Usually a YAML error in `config/litellm/config.yaml` or a missing `LITELLM_MASTER_KEY` in `.env`.

**404 on a role name** — Ollama isn't running (`ollama serve`), the backend model isn't pulled (`ollama list`), or the capability isn't in `config/litellm/config.yaml`.

**Claude Code `/model` only shows Opus/Sonnet/Haiku** — gateway discovery isn't on (needs Claude Code v2.1.129+). The `claude-local` wrapper sets `CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY=1` so `/model` lists every LiteLLM capability (`architect`, `coder`, `reviewer`, `autocomplete`, `embed`) under "From gateway", and remaps the built-in slots — Opus→`architect`, Sonnet→`coder`, Haiku→`autocomplete`, Fable→`reviewer` — so background calls stay local. If you don't see them, reload your shell (`source ~/.zshrc`) and relaunch `claude-local`.

**Models unload too fast** — the Ollama macOS app doesn't read `~/.zshrc`; run `./scripts/setup-ollama-env.sh`, restart Ollama, verify with `ollama ps`.

**Containers restart-looping** — `docker logs <container>`. Most common cause: an empty required `.env` variable.

**Get your API key:** `grep LITELLM_MASTER_KEY .env | cut -d= -f2`
