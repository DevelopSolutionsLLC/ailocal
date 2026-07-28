# ailocal

Run AI coding tools — Claude Code, Codex, VS Code Copilot Chat — against local models on Apple Silicon. No cloud costs, no data leaving your machine, no changes to the tools: Ollama runs the models natively (Metal/MLX GPU) and LiteLLM fronts them as an OpenAI/Anthropic-compatible proxy on `localhost:4000` that exposes **capability names** (`architecture`, `implementation`, `review`, `completion`, `embeddings`) instead of raw model tags. Point a tool at the proxy instead of Anthropic/OpenAI — everything else stays the same.

Architecture and file map: [CLAUDE.md](CLAUDE.md).

## Requirements

- macOS 13+ (Apple Silicon, M1 or newer)
- 64 GB RAM — the supported/tested profile
- ~85 GB free disk for the models

## Setup

```bash
brew install git jq
brew install --cask docker ollama     # open Docker Desktop once to finish first-run setup
ollama serve                          # or open Ollama.app
./scripts/install.sh                  # deps, .env, models, proxy, health check, client configs
./scripts/smoke-test.sh               # verify a real model request succeeds
```

`install.sh` can also install login LaunchAgents (autostart `ollama serve` + preload the resident model) — answer `y` when prompted, or manage it later with `./scripts/setup-startup.sh`.

## Test it

Everything routes through the proxy on `localhost:4000`:

```bash
KEY=$(grep LITELLM_MASTER_KEY .env | cut -d= -f2)
curl -s http://localhost:4000/v1/chat/completions \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"model":"implementation","messages":[{"role":"user","content":"say ok"}]}' \
  | jq -r '.choices[0].message.content'
```

Any client that speaks OpenAI (`/v1/chat/completions`) or Anthropic (`/v1/messages`) works the same way: base URL `http://localhost:4000`, key `LITELLM_MASTER_KEY`, model = a capability name.

## Services

- **litellm** — one Docker container on `127.0.0.1:4000` (localhost only, authed with `LITELLM_MASTER_KEY`). No database, no cache.
- **Ollama** — runs natively on the host (`:11434`) for Metal GPU access; the only heavy memory user.

## Capabilities

LiteLLM exposes capability names only — the router owns the backend, context, sampling, and residency, so a model swap never touches a client config. Only the **64 GB** profile is active (`config/profiles/64gb.yaml`); it's the tested configuration.

| Capability | Backend | ctx / keep_alive |
|---|---|---|
| `architecture` | qwen3-coder:30b-a3b-q4_K_M | 64K / resident — shared big-context hub: design, deep reasoning, large agent prompts |
| `implementation` | qwen2.5-coder:14b | 16K / 20m — everyday coding, features, tests |
| `review` | gpt-oss:20b | 16K / 20m — code review, bug & security |
| `fast` | qwen3.5:2b | 32K / 20m — classification, summarisation, cheap tool-driven lookups |
| `completion` | qwen2.5-coder:3b | 4K / 20m — inline autocomplete (FIM) **only**; never a chat tier |
| `embeddings` | nomic-embed-text | 8K / resident — retrieval infrastructure |

Order in the `/model` picker follows the key order of `config/profiles/64gb.yaml`.

**Which model for which task.** `architecture` is the only tier measured able to
sustain a multi-step tool loop, so it is the launch default and the one that can
act as a parent agent; `implementation` is a strong single-shot coder but does
not drive loops; `review` is the only reasoning tier; `fast` handles background
summarisation; `completion` is FIM autocomplete **only** and hard-400s on any
chat turn.

**Tool activation is automatic.** The gateway classifies each request: a general
question gets no tools at all (measured 61 → 1), a small edit gets read/edit/run,
and only architecture/debug/review-class work gets search, LSP and delegation.
Nothing to switch on.

**grepai vs LSP.** grepai (semantic) answers "where is X handled" and concept
questions; LSP answers exact ones — where a symbol is defined, what references
it. Prefer LSP's document-scoped tools: `workspace_symbol_search` only answers
for one language at a time and returns empty for the rest, which looks exactly
like "not found".

**Common mistakes:** editing a generated region instead of the two source files;
expecting `docker compose up -d` to pick up a config change (it will not — the
config is bind-mounted and parsed once at boot, so use `./scripts/start.sh`);
reading `content[0].text` on a `review` response and seeing empty (it returns a
`thinking` block first); and treating an empty LSP or grepai result as proof of
absence.

Deeper detail, per-client differences and debugging commands:
[`docs/environment-cheatsheet.md`](docs/environment-cheatsheet.md).

Change a backend in `config/profiles/64gb.yaml`, run `./scripts/sync-models.sh`, and every generated client config regenerates. `architecture`/`implementation`/`review` get a server-side engineering persona (`config/instructions/<capability>.md`), injected on both the OpenAI and Anthropic routes. Use capability names only — never raw model tags.

## Clients

```bash
./scripts/install-clients.sh          # all three (or one: vscode | codex | claude)
```

Client state lives in `~/.config/ailocal/`; your cloud `~/.claude` / `~/.codex` are never touched.

- **Claude Code** — `claude-local` (launches on `implementation`; `/model` to switch to `architecture` or delegate to subagents). Plain `claude` stays on cloud.
- **Codex** — `codex-local`. Plain `codex` stays on cloud.
- **VS Code Copilot Chat** — `ailocal-code [path]` opens an isolated VS Code profile with the [LiteLLM Connector](https://marketplace.visualstudio.com/items?itemName=Gethnet.litellm-connector-copilot) wired up; models auto-discover from `/model/info`. To wire it manually: Copilot model-picker → **Manage Models → LiteLLM Connector**, base URL `http://localhost:4000`, key = `LITELLM_MASTER_KEY`, then ⌘⇧P → **LiteLLM: Reload Models**.

## Operations

```bash
./scripts/start.sh                # start
./scripts/stop.sh                 # stop
./scripts/update.sh               # pull latest image + restart
./scripts/doctor.sh               # health summary (exit 0 healthy / 2 degraded)
./scripts/teardown.sh --clients   # full removal + client uninstall
```

## Troubleshooting

**"Message exceeds token limit" (VS Code)** — select `architecture` (64K, ~49K usable after VS Code's 0.75 reserve); the smaller-window capabilities can't hold a large workspace prompt.

**VS Code utility-model error** (`copilot-utility-small`) — set `"chat.byokUtilityModelDefault": "mainAgent"` and reload the window (the installer adds it).

**LiteLLM won't start** — `docker logs ailocal-litellm`; usually a YAML error in `config/litellm/config.yaml` or a missing `LITELLM_MASTER_KEY`.

**404 on a capability** — Ollama isn't running, the backend model isn't pulled (`ollama list`), or it's missing from `config/litellm/config.yaml`.

**Claude Code `/model` shows only Opus/Sonnet/Haiku** — reload your shell and relaunch `claude-local` (the wrapper enables gateway discovery and remaps the built-in slots to local capabilities).

**Models unload too fast** — the Ollama app ignores `~/.zshrc`; run `./scripts/setup-ollama-env.sh` and restart Ollama.
