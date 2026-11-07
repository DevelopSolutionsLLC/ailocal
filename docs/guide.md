# User guide

Everything past the install in the [README](../README.md): what each client gets, VS Code, profiles, every `claude-local` variable, Homebrew pinning, removal, and troubleshooting. How it works inside is in [architecture.md](architecture.md).

## Clients

| Client | Launch | Status |
|---|---|---|
| Claude Code | `claude-local` | Full: tools, web search, research MCP, language servers, local artifacts |
| Codex CLI | `codex-local` | Interactive and `exec` sessions, tools, file edits, web search, research MCP (measured on 0.163.0) |
| VS Code Copilot Chat | the `ailocal` models in the picker | Chat and inline completion |
| Any OpenAI- or Anthropic-compatible app | `http://127.0.0.1:4000` | with the key in `~/.local/state/ailocal/env` |

The wrappers set process-scoped environment and a separate config root (`CLAUDE_CONFIG_DIR`, `CODEX_HOME`) under `~/.config/ailocal/`; hosted `claude` and `codex` and their `~/.claude`/`~/.codex` are never read or written. In either client `/model` lists the enabled `ailocal-*` capabilities, each with its own context window and reasoning default. A client installed later is configured by `ailocal clients`.

`codex-local` runs in Codex's `workspace-write` sandbox without approval prompts: writes stay in the workspace and commands get no network. `codex-local --sandbox danger-full-access` lifts that for one session.

Local models are capable everyday assistants, not frontier models: expect strong routine work, not hosted Opus or GPT results on the hardest problems.

### claude-local compared with hosted claude

| | hosted `claude` | `claude-local` |
|---|---|---|
| Inference | Anthropic | LiteLLM → Ollama on `127.0.0.1` |
| Context | the model's full window | the launch default's `context_input`; 1M-context beta disabled |
| Tool discovery | eager | MCP tool search on |
| Auto memory | on | off unless set explicitly |
| Commit and PR attribution | trailer, footer, session link | off unless set explicitly |
| Native Workflow | on | off, with an override |
| Artifacts | hosted `Artifact` → claude.ai | bundled local artifact MCP → `127.0.0.1` |
| Web search | Anthropic | ailocal's own SearXNG |

### What a local session gets

- **Web search.** `claude-local` searches the live web through ailocal's SearXNG and answers with sources. Claude Code's "0 searches" counter tracks Anthropic-hosted search, which never runs here.
- **Research MCP.** Requests for documentation, discussion, projects or papers route to the bundled `search` tool, in claude-local, codex-local and VS Code. Results carry provider attribution and are indexed snippets, not page reads. A Brave key is optional and used only here, within a budget: set `BRAVE_API` in `~/.config/ailocal/.env.local`, then `ailocal clients claude`. Each result's `brave.summary` names any refusal (`BRAVE_BUDGET_EXHAUSTED`, `BRAVE_QUOTA_LIMITED`, `BRAVE_QUOTA_UNKNOWN`). Scraped general-web engines are disabled because they fail with CAPTCHAs under sustained use.
- **Artifacts.** Ask for a diagram, page or report; it opens from `127.0.0.1` in a sandboxed page that cannot reach the network, and its source is written to `.artifacts/` in the project. `html`, `markdown` and `mermaid` need nothing; `architecture` needs `node` on `PATH`. Invalid Mermaid is refused with the parser error rather than published.
- **Language servers.** ailocal enables the official Claude Code LSP plugin for each server already on `PATH`, in the local root only; install one and re-run `ailocal clients claude`.

  | Language | Server | Install |
  |---|---|---|
  | Python | `pyright-langserver` | `npm i -g pyright` |
  | TypeScript | `typescript-language-server` | `npm i -g typescript-language-server typescript` |
  | Go | `gopls` | `brew install gopls` |
  | C/C++ | `clangd` | `xcode-select --install` |

- **Model guidance.** Each profile carries a guide per backend; Codex receives it with the selected model, Claude Code on session start and model switch (a hook that needs Claude Code 2.1.251+). The full guide is `ailocal profile guide <alias>` or the agent MCP's `model_guide`.
- **Delegation.** A hosted Claude or Codex session can hand a bounded task to a local worker through the bundled `agent` MCP when Cadence declares it, in `text`, `research`, `edit` or `full` mode under the ceiling `delegate_max_mode` in `~/.config/ailocal/profiles/clients.toml` (default `edit`). The contract is in [architecture.md](architecture.md#delegation).
- **Sleep.** `codex-local` holds Codex's idle-sleep assertion during each running turn, as hosted Codex does; Claude Code holds its own while it works. The display can still sleep, and closing the lid still sleeps the Mac.
- **Cadence is optional.** [Cadence](https://github.com/DevelopSolutionsLLC/cadence) composes instructions, skills and agents into the local roots when installed; ailocal neither installs nor needs it.

### Tool search and Workflow

`ENABLE_TOOL_SEARCH=true` is the default and the supported value: tools that declare themselves deferrable are fetched by name (`ToolSearch("select:LSP,mcp__github__issue_read")`, batched in one call); built-in file and shell tools, `ToolSearch` and the artifact and research tools stay eager. [REAL] on this stack: 70 tools / 144,893 schema bytes → 24 / 53,044, latency median/p90 41/729 s → 24/40 s; deferred GitHub discovery 5/5. It needs LiteLLM ≥ 1.98, which keeps the mid-array `role: "system"` messages the deferred listing travels in.

Claude Code's native `Workflow` tool is off because its 21,822-byte schema cannot be deferred ([anthropics/claude-code#54716](https://github.com/anthropics/claude-code/issues/54716), closed not planned). [REAL] it was invoked 0 times in 148 claude-local sessions; disabling it cut the schema 53,044 → 31,221 bytes and the fixed prompt 21,789 → 16,695 input tokens, with 10/10 capability checks unaffected. `AILOCAL_NATIVE_WORKFLOWS=1 claude-local` restores it for one session.

### Subagent limits

A Claude Code subagent receives the tools its definition declares minus `Grep` and `Glob`, which the parent lacks too and `ToolSearch` cannot acquire. [REAL, Claude Code 2.1.231, measured before the research integration was added] an agent declaring `Read, Grep, Glob, Bash` got `Read, Bash`; MCP tools survived delegation; disabling tool search changed nothing. Write agent definitions that search with `grep`/`rg`/`find` through `Bash`, and declare `ToolSearch` if the agent needs a deferred tool.

## VS Code

VS Code keeps model API keys in its own encrypted storage, which no other program can write, so one step is manual: run `grep LITELLM_MASTER_KEY ~/.local/state/ailocal/env`, then in Copilot Chat open the model picker → **Manage Language Models** → **ailocal** and paste the key. Until then `ailocal check` reports `API key not initialized`.

ailocal uses VS Code's built-in [Custom Endpoint provider](https://code.visualstudio.com/docs/agent-customization/language-models) and installs no extension; Copilot Chat needs no subscription for this. `ailocal clients vscode` writes the `ailocal` provider group (aliases, limits, thinking and sampling from the active profile), registers the `research` and `agent` MCP servers in VS Code's `mcp.json` and [Copilot's portable scope](https://code.visualstudio.com/docs/agents/reference/mcp-configuration), and writes `~/.copilot/instructions/ailocal.instructions.md`; foreign entries are preserved. Open VS Code once before this so its settings folder exists. `ailocal start` refreshes an existing group after a profile change.

## Profiles

ailocal measures memory and selects a tier; every profile also serves `ailocal-cyber`, a separate unrestricted model that releases memory sooner when idle, and `ailocal-completion` for inline completion.

| Memory | Primary | Fast | Cyber | Completion |
|---|---|---|---|---|
| 16 GB | `qwen3.5:4b-mtp`, 96K | — | `qwen3.5:4b-unrestricted-mtp` | `qwen2.5-coder:1.5b-base` |
| 32 GB | `qwen3.5:9b-calibrated-mtp`, 160K | — | `qwen3.5:9b-unrestricted-mtp` | `qwen2.5-coder:1.5b-base` |
| 64 GB | `gemma4:26b-qat-mtp`, thinking, 192K | same model, thinking off | `gemma4:26b-unrestricted-qat-mtp` | `qwen2.5-coder:3b-base` |
| 128 GB | `gemma4:26b-qat-mtp`, thinking, 256K | same model, thinking off | `gemma4:26b-unrestricted-qat-mtp` | `qwen2.5-coder:3b-base` |

Windows are totals including the output reserve (K = 1,024 tokens). The 0.15 role names (`ailocal-architecture`, `ailocal-implementation`, `ailocal-review`, and `ailocal-fast` on 16/32 GB) route to `ailocal-primary` in 0.16 and are removed in 0.17 ([018](decisions.md#018-the-chat-roles-are-primary-and-an-optional-fast)). What was measured for each model is in [maintenance.md](maintenance.md#model-audit).

| To | Run |
|---|---|
| override the detected tier | `ailocal profile use 32gb`, then `ailocal start` |
| change a model or its sampling | edit `~/.config/ailocal/profiles/<tier>.toml`, then `ailocal start`; edits survive upgrades |
| adopt newly shipped models, keeping your budgets | `ailocal install --upgrade-models` (originals backed up) |
| take every shipped default | `ailocal install --reset-config` (your edits backed up) |
| allow two parallel Ollama requests where measured | `AILOCAL_OLLAMA_NUM_PARALLEL=2` in `.env.local`, then `ailocal install --yes` |
| return to stock Ollama from the patched runtime | `ailocal install --yes --ollama-patch none` with a profile that does not require the patch |

The patched Ollama runtime is built only for `--ollama-patch` or a role that declares it, and needs Go and Xcode's command-line tools; no shipped profile requires it.

## Defaults and claude-local variables

| | Default | Change with |
|---|---|---|
| Inference | Ollama via LiteLLM on `127.0.0.1:4000` | — |
| Proxy binding | this machine only | `ailocal bind lan` / `local`, applied on the next `ailocal start` |
| LiteLLM and SearXNG | pinned by digest; `ailocal check` reports versions | a deliberate upgrade |
| Web search | ailocal's SearXNG, keyless | `BRAVE_API` adds budgeted Brave to research |
| Hosted Claude/Codex settings | never written | native client configuration; documentation MCPs come from Cadence |

`ailocal bind lan` opens only the proxy, with the master key as its only protection; `ailocal check` warns until `ailocal bind local`. Ollama and SearXNG never leave loopback.

Every variable below is read by the `claude-local` wrapper for that process only; set it inline (`VAR=1 claude-local`) or export it. Nothing is written to disk and hosted `claude` is unaffected.

| Variable | Default | Accepted | Effect |
|---|---|---|---|
| `AILOCAL_TOOL_SEARCH` | `true` | `true`, `auto`, `auto:N`, `false` | Sets `ENABLE_TOOL_SEARCH`; `true` is the measured configuration. `force` is not accepted by the client. |
| `AILOCAL_NATIVE_WORKFLOWS` | unset | `1` | Restores Claude Code's native Workflow tool. |
| `AILOCAL_API_TIMEOUT_MS` | profile | integer ms | Client request timeout; must not be below the proxy's. |
| `AILOCAL_LITELLM_PORT` | `4000` | port | Port the wrapper points the client at. |
| `AILOCAL_PROXY` | `http://127.0.0.1:4000` | URL | Full base URL override. |
| `AILOCAL_STATE` | `~/.local/state/ailocal` | path | State root (env file, runtimes). |
| `AILOCAL_PRIMARY_ALIAS_OVERRIDE`, `AILOCAL_FAST_ALIAS_OVERRIDE` | unset | a served LiteLLM alias | Points every slot routed to that role at the alias; the primary override also passes `--model`. Fails closed if LiteLLM does not serve it. |

## Homebrew pinning

ailocal is validated against specific dependency versions, and Homebrew has no minimum-age setting, so hold versions with pins; `brew upgrade` skips pinned packages.

```bash
brew pin --cask codex claude-code@latest ollama-binary
brew pin pipx
```

To move one: unpin, upgrade, `ailocal check`, pin again. Docker Desktop and VS Code ignore cask pins and update themselves; turn off automatic updates in each. `brew upgrade --cask ollama-binary` replaces the files under the running server; `com.ailocal.ollama-watch` restarts it on the new version within five minutes. Service images are pinned by digest, so Docker Desktop updates never change LiteLLM or SearXNG.

`ollama-binary` is Ollama's command-line release with no menu-bar app; ailocal runs its server under launchd and refuses to install while `/Applications/Ollama.app` exists. To switch from the app: `brew uninstall --cask ollama-app && brew install --cask ollama-binary`, then `ailocal install`; models stay in the shared store. Installs before #74 disabled the app's `com.ollama.ollama` login agent with a receipt at `~/Library/LaunchAgents/com.ailocal.ollama.restore.json`; install and teardown now re-enable it and delete the receipt (a one-release shim).

To upgrade ailocal: `pipx install --force` the new release tag, then `ailocal start`. That regenerates the runtime; it does not change Ollama, models or pinned images.

## Removing ailocal

`ailocal teardown` removes the containers and volumes, the three login agents and their published `OLLAMA_*` environment, `~/Library/Logs/ailocal`, intact owned patched runtimes, generated presets, Docker Desktop settings it changed (restored), and every client entry it can prove it wrote outside its roots: the two `~/.zshrc` lines, the Copilot instruction file, the `research` and `agent` MCP entries, the VS Code provider group and recommended settings still at their shipped value. `ailocal clients --remove` reverses the client entries alone. Teardown keeps `~/.config/ailocal`, the client root and `/Users/Shared/ollama/models`; delete those by hand.

## Troubleshooting

Start with `ailocal check`: it names a fix for anything wrong and exits `0` healthy, `1` when it cannot resolve the active profile, `2` degraded. Generated state under `~/.local/state/ailocal` can be deleted and recovered with `ailocal start`.

### Services

- **LiteLLM never becomes ready.** The container is crash-looping on a bad generated config or an unimportable callback (`docker logs ailocal-litellm --tail=40` shows `ImportError`). Run `ailocal start`; if it persists, it is a defect.
- **Generated or client configuration is stale.** A profile changed without regeneration, a generated file was hand-edited, or deployment did not follow generation; `ailocal check` reports drift. Run `ailocal start`, then `ailocal clients` (or `ailocal clients claude codex vscode`).
- **Ollama is unavailable.** The daemon is down or on another address. `launchctl kickstart -k gui/$(id -u)/com.ailocal.ollama`; if Ollama listens elsewhere, export `OLLAMA_HOST`, the one variable every subsystem reads.
- **New model loads fail with `llama-server binary not found`.** The file launchd started from is gone (the app was replaced by the cask, or a cask upgrade has not yet been picked up by `ollama-watch`); resident models keep answering. `ailocal check` names both programs; `ailocal install` rewrites the agent for the `PATH` ollama and restarts it.
- **A required model is missing.** The tier changed or a pull failed; `ailocal check` names it. Run `ailocal install`.
- **Search returns nothing.** SearXNG is not running; `ailocal check` reports `searxng` (reachable), `searxng-query` (one free-engine search) and `brave-key` (configuration only, no Brave query). Run `ailocal start`. No Brave key is a supported configuration.
- **State-root permissions are wrong.** Expect `0700` on `~/.local/state/ailocal` and `0600` on `searxng/settings.yml` and the Brave ledger; `chmod` them before using search.
- **A SearXNG bot-detection line at startup** is expected on a single-user instance.

### Requests

- **A request is rejected for exceeding the context window.** The prompt exceeds the alias's `max_input_tokens`; rejecting is correct, silent truncation is not. Use the larger-admission capability (`ailocal-fast` on 64/128 GB) or raise `context_input` in the profile within what memory holds, then `ailocal start`.
- **A long-context request seems hung.** Cold prefill can take minutes on large windows; flat memory and swap with the model resident mean it is working. Client and proxy share a 900 s timeout. If every turn is slow, not just the first, the KV prefix cache is not being reused: `python3 tests/measure_cache.py --tail 20` (passive) or `--quick` (active) from a checkout. [REAL] a healthy warm turn re-prefills a few dozen tokens (~99.8% reuse, `qwen3.5:4b`, 16 GB). On 16 GB, background requests can evict the single slot; that is a memory limit.
- **Claude Code reports an API error after a long request.** The client gave up first (`grep -a "client closing the connection" ~/.ollama/logs/server.log`). `ailocal start && ailocal clients` redeploys the wrapper, which matches `API_TIMEOUT_MS` to the proxy.
- **Codex produces content but the turn never completes.** Check the backend first: `ailocal check` must show Ollama responding and every capability serving, then `codex-local exec --skip-git-repo-check --ephemeral --json "Reply with exactly PONG."` must reach `turn.completed`. [REAL 2026-10-18, LiteLLM 1.106.0, Codex 0.163.0] `exec` and interactive turns with search, tools and file writes completed. On LiteLLM 1.103+ streamed web search depends on `tool_repair` ([BerriAI/litellm#42770](https://github.com/BerriAI/litellm/issues/42770)); check upstream before patching locally.
- **A thinking-mode turn ends with no reply.** A Qwen3.5 thinking session finishes a tool loop with reasoning and no answer, or a delegation returns `DELEGATE_EMPTY`. Cause and fix: [016](decisions.md#016-qwen-tool-loop-history-keeps-the-thinking-mode-signature); [REAL] stock 13/16 runs ended empty, patched 16/16 replied; a live Qwen3.5 loop on the 0.40.14 base is [UNVERIFIED]. The shipped 16/32 GB primaries run thinking off. Request a follow-up (`resume=<id>`) or use a non-thinking role; ailocal never switches model or mode itself.
- **The context is unexpectedly large.** `AILOCAL_TOOL_SEARCH=false` or `AILOCAL_NATIVE_WORKFLOWS=1` exported in your shell: together ~114K schema bytes against 31K with the defaults.

### Clients

- **Codex asks "Hooks need review" on every launch.** Before 0.13.0 regeneration dropped Codex's trust record; trust the scratchpad hook once ("Trust all and continue" or `/hooks`). A changed hook definition needs review again by Codex's design.
- **Claude Code shows "0 searches" although search worked.** Cosmetic: the counter tracks Anthropic-hosted search. `docker logs ailocal-litellm 2>&1 | grep -c searxng-search` confirms retrieval.
- **VS Code is configured but not answering.** The key was never pasted, the group is stale, or VS Code predates the Custom Endpoint provider. `ailocal clients vscode`, reload the window; `ailocal check` reports a stale group and a missing key separately.
- **The VS Code reply area is cramped.** VS Code 1.132's agent-sessions panel, not ailocal: set `"chat.viewSessions.enabled": false`.
- **The artifact tool is missing.** `ailocal check | grep artifact`; `ailocal clients claude` provisions and registers it, preserving other servers. If the check passes, the model did not route to it: ask explicitly ("publish an artifact showing …").
- **Only the `architecture` artifact format fails.** It needs Node: `brew install node`.
- **The artifact preview will not open.** The preview server exits 30 minutes after last use; republish from the source in `.artifacts/`. A blocked request in the page's console is the `connect-src 'none'` boundary working.
- **ToolSearch cannot find a GitHub or LSP tool.** They are deferred: request them by exact name. If a name does not resolve, check the server is registered and LiteLLM is ≥ 1.98. `Grep`/`Glob` cannot be acquired; use `rg`/`find` through `Bash`.
- **`/workflows` does nothing.** Native Workflow is off by default; `AILOCAL_NATIVE_WORKFLOWS=1 claude-local`.
- **An editable install raises `ModuleNotFoundError: ailocal`.** Either the `.pth` file is flagged `hidden` (`ls -lO .venv/lib/*/site-packages/*.pth`), which CPython skips: run with `PYTHONPATH=<repo>/src`; or `pyvenv.cfg` names an upgraded-away Python: recreate the venv.

The 128 GB profile mirrors 64 GB policy and is only partly measured on 128 GB hardware; treat its numbers as provisional.
