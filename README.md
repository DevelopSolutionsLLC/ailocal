# ailocal

[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)
[![Platform](https://img.shields.io/badge/platform-macOS%20Apple%20Silicon-lightgrey.svg)]()
[![Status](https://img.shields.io/badge/status-release%20candidate-orange.svg)]()

Run AI coding tools — Claude Code, Codex, VS Code Copilot Chat — against local models on Apple Silicon. No cloud costs, no data leaving your machine, no changes to the tools: Ollama runs the models natively (Metal/MLX GPU) and LiteLLM fronts them as an OpenAI/Anthropic-compatible proxy on `localhost:4000` that exposes **capability names** (`architecture`, `implementation`, `review`, `fast`, `completion`, `embeddings`) instead of raw model tags. Point a tool at the proxy instead of Anthropic/OpenAI — everything else stays the same.

## What this is, and what it is not

**ailocal is infrastructure: local inference and routing.** It runs Ollama, fronts
it with LiteLLM, and owns everything about *which model answers and how* —
capability routing, personas, the tool gateway, token optimisation, SearXNG
integration. That is the whole job.

It does **not** own developer tooling. MCP registration, LSP provisioning,
repository intelligence (grepai/Qdrant), client installation and validation
belong to **[Cadence](https://github.com/DevelopSolutionsLLC/cadence)**, which
enhances any AI client whether or not ailocal is installed.

The two compose but do not depend on each other in both directions:

- **Cadence without ailocal** — fully supported. MCP, LSP and grepai work
  against hosted Claude or hosted Codex with no LiteLLM anywhere.
- **ailocal without Cadence** — works. You get local models through the proxy,
  without repository intelligence.
- **Both** — Cadence detects ailocal and enables the LiteLLM-backed extras
  (local search routing, capability-aware tooling).

If you do not use LiteLLM, you do not need this repo.

## Start here

| Question | Answer |
|---|---|
| **Supported clients?** | `claude-local`, `codex-local`, VS Code, plus hosted Claude/Codex untouched alongside. Per-client state: [compatibility matrix](docs/architecture.md) |
| **Which model?** | `architecture` for anything agentic (default), `implementation` for edits, `review` for critique, `fast` for background work. `completion` is FIM autocomplete **only** — it hard-400s on a chat turn |
| **Which tools do I get?** | The gateway removes what a local model cannot drive: Claude Code declares 54 tools, 41 are kept, 13 orchestration tools are dropped (~14,500 tokens, 56% of tool schema). Filtering is per-model, not per-question — a trivial question receives the same 41. |
| **LSP?** | Native Claude Code LSP. ailocal installs the **Python** baseline (`pyright-lsp`) into the isolated `claude-local` root; Cadence adds TypeScript/Go/C and repository intelligence. Shell has no native plugin — use `bash -n`, `zsh -n`, `shellcheck` |
| **grepai or LSP?** | grepai for *concepts* ("where is retry handled"), LSP for *exact* ("where is this defined, what calls it"). Prefer LSP's document-scoped tools |
| **Something's wrong** | `ailocal doctor` → `./scripts/validate-deployment.sh` → `./scripts/test-all.sh`. Run them **idle**; contention causes phantom failures |
| **Why is it built this way?** | [ADRs](docs/adr/) — one per decision, with the measurements behind it |
| **What's not done?** | [future work](docs/architecture.md) |

**Common mistakes:** editing a generated region instead of the two source files
(`config/profiles/<tier>.yaml`, `config/clients.yaml`); expecting
`docker compose up -d` to pick up a config change (it will not — use
`ailocal start`); reading `content[0].text` on a `review` response and
seeing empty (it returns a `thinking` block first); treating an empty LSP or
grepai result as proof of absence (it usually means still-indexing, or the wrong
language server answered).

Full operational detail: [environment cheat sheet](docs/architecture.md).
Architecture and file map: [AGENTS.md](AGENTS.md).

## Requirements

**A bare Mac is fine.** `install.sh` reports everything missing up front, asks
once, then requests administrator rights once. Docker's licence terms are
recorded in Docker's own settings file before first launch, so there is no
manual accept-and-re-run step. `--yes` runs unattended.

| Prerequisite | Administrator rights | Why |
|---|---|---|
| git (Command Line Tools) | **yes** | `softwareupdate -i` runs as root; installed headlessly, no dialog |
| Homebrew | **yes** | creates `/opt/homebrew` |
| jq | no | Homebrew formula, user-owned prefix |
| Docker Desktop | **yes** | root-owned privileged helpers (cask `docker-desktop`, not the CLI-only formula) |
| Ollama | no | cask `ollama-app`; `/Applications` is admin-group writable |

You must be an **administrator**. A standard account cannot install these, and
the installer says so up front rather than failing partway through.

### Prerequisites

- macOS 13+ (Apple Silicon, M1 or newer)
- **16 GB unified memory minimum.** The installer reads `sysctl hw.memsize` and
  picks the highest tier that does not exceed it, never rounding up: 16-31 GB
  selects `16gb`, 32-63 `32gb`, 64-127 `64gb`, 128+ `128gb`. Below 16 GB it stops
  rather than offering a profile that would swap.
- Disk depends on the tier and on what is already installed. The installer prints
  the unique model set, what you already have, and the additional space needed
  before pulling anything.

## Install

```bash
brew install git jq
brew install --cask docker ollama     # open Docker Desktop once to finish first-run setup
ollama serve                          # or open Ollama.app
./scripts/install.sh                  # deps, .env, models, proxy, health check, client configs
ailocal smoke               # verify a real model request succeeds
```

`install.sh` can also install login LaunchAgents (autostart `ollama serve` + preload the resident model) — answer `y` when prompted, or manage it later with `./scripts/setup-startup.sh`.

## Verify

Everything routes through the proxy on `localhost:4000`:

```bash
KEY=$(grep LITELLM_MASTER_KEY .env | cut -d= -f2)
curl -s http://localhost:4000/v1/chat/completions \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"model":"ailocal-implementation","messages":[{"role":"user","content":"say ok"}]}' \
  | jq -r '.choices[0].message.content'
```

Any client that speaks OpenAI (`/v1/chat/completions`) or Anthropic (`/v1/messages`) works the same way: base URL `http://localhost:4000`, key `LITELLM_MASTER_KEY`, model = `ailocal-<capability>` (e.g. `ailocal-architecture`, `ailocal-implementation`, `ailocal-review`, `ailocal-fast`, `ailocal-completion`, `ailocal-embeddings`). Configured clients (`claude-local`, `codex-local`, the VS Code connector) remap their own model slots to these automatically — you only type a bare capability name inside those tools' `/model` pickers, not on the raw HTTP API.

## Architecture

Clients speak to LiteLLM on `127.0.0.1:4000`, which routes each request to an
Ollama-hosted model by **capability** — never by model name. Server-side hooks
inject the per-capability persona and trim the client's tool payload before the
request reaches the backend.

Full design in [`docs/architecture.md`](docs/architecture.md).

### Services

- **litellm** — one Docker container on `127.0.0.1:4000` (localhost only, authed with `LITELLM_MASTER_KEY`). No database, no cache.
- **Ollama** — runs natively on the host (`:11434`) for Metal GPU access; the only heavy memory user.

### Capabilities

LiteLLM exposes capability names only — the router owns the backend, context, sampling, and residency, so a model swap never touches a client config. The active profile is chosen from detected memory and recorded in
`config/active-profile`. Four exist:

| Tier | architecture / implementation / review / fast | completion | embeddings | Unique models | Level |
|---|---|---|---|---|---|
| `16gb` | `qwen3.5:4b` — one shared backend | `qwen2.5-coder:1.5b` | `nomic-embed-text` | 3 | configuration-verified |
| `32gb` | `qwen3.5:9b` — one shared backend | `qwen2.5-coder:1.5b` | `nomic-embed-text` | 3 | configuration-verified |
| `64gb` | specialised per capability | `qwen2.5-coder:3b` | `nomic-embed-text` | 6 | **measured** |
| `128gb` | exact copy of `64gb` | same | same | 6 | configuration-verified |

The smaller tiers deliberately point four capabilities at **one** resident model.
The aliases still differ in persona, sampling and reasoning — they share a backend
so the tier holds one model instead of rotating several. Configured context is a
maximum, not a guarantee that two parallel requests can each consume all of it.

`128gb` mirrors `64gb` exactly for this release. It is not claimed to be faster or
more capable, because nothing has been measured on 128 GB hardware.

| Capability | Purpose |
|---|---|
| `architecture` | Shared big-context hub: design, deep reasoning, large agent prompts |
| `implementation` | Everyday coding, features, tests |
| `review` | Code review, bug & security |
| `fast` | Classification, summarisation, cheap tool-driven lookups |
| `completion` | Inline autocomplete (FIM) **only**; never a chat tier |
| `embeddings` | Retrieval infrastructure |

Which model backs each capability, its context window, and its `keep_alive` are all tier-specific — see the table above and `config/profiles/<tier>.yaml` for what your detected profile actually runs (`config/active-profile` names it; `ailocal status` shows it live). Nothing generation-side stays resident forever: `architecture` holds for a bounded number of hours to survive a working session without reload cost, then frees on genuine idle; the rotating capabilities (`implementation`/`review`/`fast`/`completion`) self-unload sooner. Only `embeddings` is pinned permanently — it's small (~370 MB) and other tools (grepai) depend on it never reloading. Exact durations live in the profile YAML, not here, so this doc can't drift from what's actually configured.

Order in the `/model` picker follows the key order of the active profile.

Change a backend in `config/profiles/<tier>.yaml`, run `ailocal sync`, and every generated client config regenerates. `architecture`/`implementation`/`review` get a server-side engineering persona (`config/instructions/<capability>.md`), injected on both the OpenAI and Anthropic routes. Use capability names only — never raw model tags.

### Clients

```bash
ailocal clients          # all three (or one: vscode | codex | claude)
```

Client state (model routing, base URL, keys) lives in `~/.config/ailocal/`; your cloud `~/.claude` / `~/.codex` sessions keep pointing at Anthropic/OpenAI's cloud, not the local proxy — ailocal never changes which backend they talk to. The one exception is the Python LSP baseline (`pyright-lsp`): it's installed and enabled into both `claude-local` and plain `claude`, since it's a Claude Code plugin wiring up a binary you already have, independent of routing.

- **Claude Code** — `claude-local` (launches on `architecture`, the only tier measured able to sustain a tool loop; `/model` to switch). Plain `claude` stays on cloud for model routing, but gets the same LSP baseline.
- **Codex** — `codex-local`. Plain `codex` stays on cloud.
- **VS Code Copilot Chat** — `ailocal-code [path]` opens an isolated VS Code profile with the [LiteLLM Connector](https://marketplace.visualstudio.com/items?itemName=Gethnet.litellm-connector-copilot) wired up; models auto-discover from `/model/info`. To wire it manually: Copilot model-picker → **Manage Models → LiteLLM Connector**, base URL `http://localhost:4000`, key = `LITELLM_MASTER_KEY`, then ⌘⇧P → **LiteLLM: Reload Models**.

## Update

```bash
ailocal start                # start
ailocal stop                 # stop
ailocal update               # pull latest image + restart
ailocal doctor               # health summary (0 healthy / 1 profile unresolved / 2 degraded)
ailocal teardown --clients   # full removal + client uninstall
```

## Uninstall

```sh
ailocal teardown          # stop services, remove containers and client configs
```

Removes only what ailocal installed. Models, `~/.claude`, `~/.codex` and Cadence
content are left alone. Runtime state under `~/.local/state/ailocal` is kept —
delete it yourself if you want it gone.

## Troubleshooting

**"Message exceeds token limit" (VS Code)** — select `architecture` (96K, ~73K usable after VS Code's 0.75 reserve); the smaller-window capabilities can't hold a large workspace prompt.

**VS Code utility-model error** (`copilot-utility-small`) — set `"chat.byokUtilityModelDefault": "mainAgent"` and reload the window (the installer adds it).

**LiteLLM won't start** — `docker logs ailocal-litellm`; usually a YAML error in `config/litellm/config.yaml` or a missing `LITELLM_MASTER_KEY`.

**404 on a capability** — Ollama isn't running, the backend model isn't pulled (`ollama list`), or it's missing from `config/litellm/config.yaml`.

**Claude Code `/model` shows only Opus/Sonnet/Haiku** — reload your shell and relaunch `claude-local` (the wrapper enables gateway discovery and remaps the built-in slots to local capabilities).

**Models unload too fast** — the Ollama app ignores `~/.zshrc`; run `bash scripts/setup-ollama-env.sh` and restart Ollama.

## Links

- [`docs/architecture.md`](docs/architecture.md) — system design, ownership, data flow
- [`docs/troubleshooting.md`](docs/troubleshooting.md) — runtime symptoms
- [`docs/adr/`](docs/adr/) — durable decisions
- [`AGENTS.md`](AGENTS.md) — AI operating instructions
- Cadence — optional repository-intelligence layer; see its README

## License

[Apache License 2.0](LICENSE) — Copyright © 2026 DevelopSolutions, LLC.

Developed and maintained by Victor T. Chevalier for DevelopSolutions, LLC.
