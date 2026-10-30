# ailocal

**Run Claude Code, Codex CLI, and VS Code Copilot Chat with local models on your Mac.**

[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE) [![Platform](https://img.shields.io/badge/platform-macOS%20Apple%20Silicon-lightgrey.svg)]() [![Memory](https://img.shields.io/badge/memory-16%20GB%20minimum-lightgrey.svg)]()

| Your tool | What you run |
|---|---|
| Claude Code | `claude-local` |
| Codex CLI | `codex-local` |
| VS Code | local models in Copilot Chat |

Local models. Local data. One configuration.

---

## What is ailocal?

ailocal runs the coding tools you already use against models on your own machine. Local inference has no per-token charge. Documentation MCP lookups contact the vendors' public documentation services when the agent uses them.

**What it configures for you:** the models, the local API they are served on, and every supported client you have installed. You do not edit a config file by hand.

Every `ailocal install`, including `--yes`, registers the official Claude Code, OpenAI and Microsoft Learn documentation MCP servers for installed Claude Code and Codex CLIs, in both their regular and local client roots. No API keys or extra processes are required. Existing settings and foreign servers are preserved; a conflicting same-name server is reported rather than replaced. Run `ailocal clients claude codex` to add them later. Copilot documentation registration is deferred. These public documentation registrations remain in hosted client settings after ailocal teardown.

**What it does not do:** install software. You install the prerequisites and whichever clients you want; ailocal detects what is present and configures it. Every client is optional.

**Requirements:** macOS on Apple Silicon, 16 GB unified memory minimum.

---

## 1. Install prerequisites

If you do not have Homebrew, install it first from [brew.sh](https://brew.sh).

**Docker Desktop and Ollama are required.** They run the models and the local API; ailocal cannot work without them.

```bash
brew install --cask docker-desktop ollama-app
```

**Now open Docker Desktop and Ollama once, from your Applications folder.** Both need one manual launch before anything can use them.

**pipx** is how you install ailocal, not something ailocal needs at runtime:

```bash
brew install pipx
```

`pipx` brings its own Python — you do not need to install Python or set up a virtual environment.

**Language servers** are optional and only matter for Claude Code. Install the ones for
languages you work in; ailocal enables the matching plugin for each server it finds. See
[Language servers](#language-servers).

```bash
npm i -g pyright                                    # Python
npm i -g typescript-language-server typescript      # TypeScript/JavaScript
brew install gopls                                  # Go
xcode-select --install                              # C/C++ (clangd)
```

## 2. Install clients (optional)

Install whichever you want to use, or none. You can add one later at any time.

```bash
brew install --cask claude-code           # Claude Code
brew install --cask codex                 # Codex CLI
brew install --cask visual-studio-code    # VS Code Copilot Chat
```

**If you install VS Code, open it once** — it creates its settings folder on first launch, and ailocal cannot configure it before that.

ailocal works with no client at all: it still serves a local OpenAI- and Anthropic-compatible API at `http://127.0.0.1:4000` for any app you point at it.

## 3. Install ailocal

```bash
pipx install git+https://github.com/DevelopSolutionsLLC/ailocal.git
ailocal install
```

`ailocal install` measures your Mac, picks models that fit its memory, downloads them, starts the services, and configures the clients it finds. It will ask you before touching any client configuration.

Installation checks that every selected model can load through Ollama, including models already on disk, then ends with `ailocal check`. It exits nonzero if validation fails, with findings and recovery commands. The load check uses a small context and generates no tokens; it does not establish full-window memory headroom or coding quality. Preview client setup without changing anything with `ailocal clients --dry-run`.

The shipped profiles use native GGUF backends and require no compatibility patch. Operator profiles retaining MLX imports can still declare the reviewed compatibility bundle; `ailocal install --yes` prepares it automatically. It fixes Gemma expert loading and mixed-precision routers, and enforces configured MLX stop strings. Builds require Go and Xcode's compiler tools. The installer verifies pinned source and patches, builds and tests an isolated native runtime, then selects it only after the daemon reports the expected version. The signed app stays intact. Existing runtime selections are preserved; `--ollama-patch none` returns to stock Ollama once the selected profile no longer requires the bundle. See [the patch's scope and validation](docs/maintenance.md#patched-ollama).

Allow roughly 10–55 GB of model storage, depending on your profile. Run it once; it is safe to re-run.

## 4. Verify

```bash
ailocal check
```

**Success is `CHECK: OK` on the last line.** The report is grouped by area; the **Clients** group shows what ailocal did with each supported client:

```
Clients
  ✓ Claude Code    configured
  — Codex CLI      not installed (optional)
      → brew install --cask codex
  ✓ VS Code        configured
```

- `✓` — configured and working.
- `—` — not installed. Not an error: ailocal left it alone. Install it and run `ailocal clients` if you want it.
- `⚠` — advisory. The line below it is the exact command that fixes it.

## Use it

Open a **new** terminal, then:

```bash
claude-local        # Claude Code, against your local models
codex-local         # Codex CLI, against your local models
```

Installed a client after ailocal? Run `ailocal clients` and it picks it up.

### VS Code: paste the key once

VS Code keeps model API keys in its own encrypted storage, which no other program can write to, so this one step is yours. Everything else on the VS Code side is already configured.

```bash
grep LITELLM_MASTER_KEY ~/.local/state/ailocal/env
```

Then in VS Code: Copilot Chat → model picker → **Manage Language Models** → **ailocal** → paste the key. The `ailocal-*` chat models appear in the picker right after; autocomplete (`ailocal-completion`) is FIM-only and is not listed.

Until you do, `ailocal check` reports `VS Code   provider configured; API key not initialized` and repeats these two steps.

Copilot Chat ships inside VS Code and needs no Copilot subscription for the local Custom Endpoint workflow. `ailocal clients vscode` (and `ailocal install`, when you accept client configuration) uses VS Code's built-in [Custom Endpoint provider](https://code.visualstudio.com/docs/agent-customization/language-models) and installs no extension: each `ailocal-*` alias is listed with the active profile's input/output limits, thinking support and sampling. Image input is enabled only by the selected backend's qualified model guide. The `research` and `agent` MCP servers are registered in both the Local harness's user-profile `mcp.json` and [Copilot's portable global scope](https://code.visualstudio.com/docs/agents/reference/mcp-configuration), `$COPILOT_HOME/mcp-config.json` (default `~/.copilot/mcp-config.json`). Foreign entries and operator options are preserved; collisions are reported. A copy of the active profile's model guidance is written into the Copilot instruction file, so a local model gets its prompting advice without MCP. `ailocal start` refreshes models and guidance after a profile change. VS Code must have been opened once first, and `ailocal install --yes` leaves client configuration to `ailocal clients`.

## Everyday commands

| Command | What it does |
|---|---|
| `ailocal install` | set everything up (run once) |
| `ailocal install --reset-config` | re-take the shipped policy defaults, backing up your edits first |
| `ailocal install --upgrade-models` | adopt the shipped models, sampling and guidance in every existing profile, keeping your context/output budgets; originals are backed up |
| `ailocal start` | bring the models and proxy up |
| `ailocal stop` | bring them down |
| `ailocal status` | what is loaded right now |
| `ailocal check` | is everything configured and working? |
| `ailocal clients` | configure the supported clients you have installed |

`ailocal check` answers the whole question end to end — configuration, running services, every model, and one real response — and prints the fixing command next to anything that is wrong.

To upgrade: `pipx upgrade ailocal && ailocal start`.

This upgrades the Python package from its configured installation source and regenerates the runtime; it does not automatically upgrade Ollama, models, or pinned service images. `ailocal update --json` reports available image candidates and explicitly incomplete discovery. `ailocal audit` checks whether the pinned versions are still safe: it queries live advisory sources and exits non-zero when a pin is affected or yanked. Review and validation before promoting dependencies are described in [AI development maintenance](docs/maintenance.md).

### Pinning Homebrew packages (macOS)

ailocal holds its dependencies at validated versions, roughly two releases behind the newest. Homebrew has no minimum-release-age setting (Homebrew/brew#21421 and #22659, both closed as not planned), so hold a version with a **pin**: `brew upgrade`, including a scheduled `brew autoupdate --upgrade`, skips pinned packages.

```bash
brew pin --cask codex claude-code@latest   # hold the clients ailocal is measured against
brew pin pipx
brew list --pinned
```

To upgrade a pinned package, unpin it, upgrade, validate, and pin it again:

```bash
brew unpin --cask codex && brew upgrade --cask codex
ailocal check && python3 tests/gate.py     # the gate needs a checkout
brew pin --cask codex
```

To install a specific older formula version, use `brew extract` into a local tap; Cadence's README gives the full procedure, including the checksum check.

Docker Desktop, the Ollama app and VS Code update themselves and ignore a cask pin. Turn off automatic updates in each app's own settings (in VS Code: `"update.mode": "manual"`). ailocal pins its service images by digest itself, so Docker Desktop updates never change LiteLLM or SearXNG.

## Supported tools

| Tool | Status |
|---|---|
| Claude Code | Fully supported — tools, web search, language servers, and local artifacts |
| VS Code Copilot | Supported for chat and code completion |
| Codex CLI | Supported — interactive and `exec` sessions, tools, file edits, web search and the bundled research MCP (dispatch measured on 0.163.0) |

Any OpenAI- or Anthropic-compatible app also works directly: point it at `http://127.0.0.1:4000` with the key in `~/.local/state/ailocal/env`.

Local models are capable everyday assistants, not frontier models. Expect strong routine work, not hosted Opus or GPT on the hardest problems.

## Language servers

ailocal enables the official Claude Code LSP plugin for each language **whose server binary
you already have**. It installs no language ecosystem, and reports the fixing command for a
language whose server is absent.

| Language | Server binary | You install it with | Plugin ailocal enables |
|---|---|---|---|
| Python | `pyright-langserver` | `npm i -g pyright` | `pyright-lsp` |
| TypeScript | `typescript-language-server` | `npm i -g typescript-language-server typescript` | `typescript-lsp` |
| Go | `gopls` | `brew install gopls` | `gopls-lsp` |
| C/C++ | `clangd` | `xcode-select --install` | `clangd-lsp` |

Install a server, re-run `ailocal clients claude`, and the plugin follows — in **ailocal's
own root only**. Plugin state is per config root, so this root is self-sufficient and
depends on nothing your hosted client has.

Your hosted `claude` is **yours**; ailocal never adds plugins to it. To get the same
languages there, run `/plugin` in a hosted session once.

There is no bash plugin in the official marketplace, so shell is covered by ShellCheck
instead — static analysis, not LSP.

## Web search

`claude-local` searches the live web: the local proxy intercepts the request, runs it against ailocal's own SearXNG instance, and returns results the model answers with sources.

Claude Code may display **0 searches** on a clearly sourced answer. That counter tracks Anthropic-hosted search, which never runs here; retrieval still happened.

**Brave Search is optional** and is used only by the research MCP, within a budget. Without a key, research uses the keyless engines:

```bash
$EDITOR ~/.config/ailocal/.env.local    # set BRAVE_API=your-key; optional AILOCAL_BRAVE_BUDGET=1000
ailocal clients claude                  # or: ailocal clients codex
```

The key stays in that file on your machine. It is never committed or printed, and it is sent only to Brave's API.

General searches use Yahoo and Yep. Developer registries use the `it` category, research sources use `science`, and individual engines remain reachable by bang, such as `!gh`, `!hn` or `!oa`. Wiby is bang-only (`!wib`).

GitHub code search (`!ghc`) is optional: add `GITHUB_SEARCH_TOKEN=` to `~/.config/ailocal/.env.local`, then run `ailocal start`. Use a fine-grained PAT restricted to public repositories, read-only, with an expiry. Never reuse the `gh` CLI token, which has `repo` scope. Without this token, the engine stays inactive and startup succeeds.

### Site-scoped research

`ailocal clients claude`, `ailocal clients codex` and `ailocal clients vscode` provision the bundled research MCP for their local clients. Requests that need external information (documentation, community discussion, projects or papers) route to its `search(query, sites?, preset?, use_brave?)` tool without the user naming it. The server plans providers itself: specialist engines plus keyless Yahoo and Yep, site narrowing across Reddit, Hacker News, Stack Exchange and lobste.rs for `forums`, and a keyword retry for AND-only APIs. Results are deduplicated, keep provider attribution, and report coverage, attempts, fallbacks and failures. Every source is an indexed snippet, not a page read; Reddit sources are marked `login_walled`.

When keyless coverage is thin and a Brave key is configured, the server adds at most two Brave requests per call, within `AILOCAL_BRAVE_BUDGET` (default 1,000 per Brave billing period). Each request is reserved in a ledger under the state root before it is sent, so concurrent sessions cannot overspend. Every response carries a `brave.summary` line: requests used, the remaining budget as counted by ailocal on this machine, and the period reset. Account-wide remaining allowance is reported as unknown unless Brave reports a monthly cap or refuses a request for quota or balance, which is reported as account-wide exhaustion until the reset; usage by other consumers of the key is not visible. Brave publishes no usage or balance API, and prepaid keys report no request quota. To bound spending across every consumer of the key, set a usage limit in the Brave dashboard and leave auto-reload off: Brave enforces it on its side and refuses requests once it is reached. When the budget is exhausted or the quota is unknown, research continues keyless and says why. See [ADR 014](docs/adr/014-local-research.md).

The stable stdio launcher is `ailocal mcp research`. Experimental `answer(question, sites?)` additionally reads bounded public pages and synthesizes a cited answer through the local proxy. It is disabled by default because claude-local shares its inference slot; a separately configured caller can explicitly launch `ailocal mcp research --enable-answer`. VS Code's compatibility user-profile registration and the portable Copilot registration use the absolute launcher path, preserve other servers and never replace foreign same-name entries. Hosted Claude/Codex research registration belongs to Cadence; ailocal adds only the public documentation MCPs to those hosted roots.

Returned sources distinguish full-page text from search snippets and report failed page reads. Source text is untrusted evidence, not instructions; check substantive claims against the cited URLs. Cached search results expire after 15 minutes and page text after 30 minutes. The bounded cache lives under ailocal's disposable state root, contains queries and public evidence, and stores no conversations or synthesized answers. `ailocal check` checks provisioning without issuing a search. See [the architecture](docs/architecture.md#local-research) for limits and ownership.

## Artifacts

Anthropic's hosted `Artifact` tool publishes to claude.ai and is unavailable to a
locally-routed session. `claude-local` gets a local equivalent instead: ailocal ships the
artifact renderer inside the package and `ailocal clients claude` provisions it, so there is
nothing extra to clone, install, or keep in sync.

Ask for a diagram, a page, or a report and the model calls `mcp__artifact__publish`. The
result opens in your browser from `127.0.0.1` and the canonical source is written to
`.artifacts/` in your project, so it survives the session and can be committed.

| Format | Rendered by | Needs |
|---|---|---|
| `html`, `markdown` | bundled `marked` | — |
| `mermaid` | bundled `mermaid` | — |
| `architecture` | bundled `elkjs`, laid out in Node | `node` on PATH |

The model supplies only semantics — components, edges, groupings. Layout is computed by ELK
and presentation comes from a design token file, so the same input renders the same way
every time rather than depending on the model's sense of geometry.

**The page cannot reach the network.** The artifact renders in a sandboxed iframe with an
opaque origin under `connect-src 'none'`, served separately from the viewer that frames it.
Every asset is vendored; nothing is fetched from a CDN.

Your hosted `claude` keeps its real `Artifact` tool.

## Your Mac's profile

ailocal measures your Mac's memory and chooses a profile automatically. You do not need to think about this to install or use it.

| Memory | Main model | Context window | Models on disk |
|---|---|---|---|
| 16 GB | `qwen3.5:4b-mtp-q4_K_M` | 104K | ~10 GB |
| 32 GB | `qwen3.5:9b-calibrated-mtp-q4_K_M` | 136K | ~21 GB |
| 64 GB | `gemma4:26b-qat-mtp` | 192K | ~55 GB |
| 128 GB | `gemma4:26b-qat-mtp` | 256K | ~55 GB |

These are total context windows, including each role's output reserve (K = 1,024 tokens). Architecture retains its original operator budget, and Cyber uses the same total in each tier. Aliases sharing architecture's backend share that window; fast roles on separate models and FIM keep their own budgets.

Backends follow one [model-format rule](docs/maintenance.md#model-format): prefer official or author-published builds, require documented optimization for community builds, and select MLX only where a same-model comparison on real hardware shows it is at least as fast and as faithful. The 16/32 GB profiles use native Qwen GGUF MTP builds; 64/128 GB use Gemma QAT GGUF with a pinned MTP head and Qwen 9B MTP for fast tasks. Native pull/create recipes verify target/head hashes and select Ollama's built-in renderers and parsers. No tensors are converted and no client history is rewritten. MTP accelerates inference; calibration and QAT do not establish additional training-data knowledge. The [model audit](docs/model-audit.md) records exact artifacts, speed and quality results, including calibrated MLX comparisons and their limits. The table lists shipped defaults; edited operator profiles retain their own budgets. Physical 16/32/128 GB full-window fit remains unverified. To override the detected tier: `ailocal profile use 32gb`, then `ailocal start`.

The 32 GB shared primary and 64/128 GB fast backend use Unsloth's hash-pinned, imatrix-calibrated Qwen3.5 9B Q4_K_M with embedded MTP and image input. The 32 GB Cyber model uses calibrated unrestricted Qwen3.5 9B Q4_K_M; both select draft depth 3. The 16 GB pair is unchanged. Qwen3.8-distilled candidates were evaluated rather than promoted on their names alone; see the model audit for speed, coding and compatibility tradeoffs.

Inline autocomplete (`ailocal-completion`) serves the official GGUF base tags `qwen2.5-coder:1.5b-base` (16/32 GB, ~1 GB) and `qwen2.5-coder:3b-base` (64/128 GB, ~2 GB; Qwen Research License, non-commercial). Ollama's MLX runner has no Qwen2 loader, and only the base tags complete a fill-in-the-middle prompt sent through the proxy. Send the prompt pre-formatted as `<|fim_prefix|>…<|fim_suffix|>…<|fim_middle|>`; LiteLLM drops a separate `suffix`. Evidence is in [maintenance](docs/maintenance.md#inline-completion).

A long conversation compacts automatically before it reaches that window. Your client does the compacting at its own default point; ailocal only tells it the window the backend admits.

To change which model a profile uses, edit the file in `~/.config/ailocal/profiles/` and run `ailocal start`. Your edits are preserved across upgrades.

MTP selections are shipped profile defaults, so fresh installs provision them automatically. A normal `ailocal install` updates profiles that still match the previously installed defaults and preserves edited profiles. `ailocal install --reset-config` adopts all shipped policy defaults after backing up edited files; it is a full policy reset, rather than a models-only migration. Restarting ailocal preserves the selected models and draft settings.

Profiles also control sampling, including `temperature`, `top_p`, `top_k`, `min_p`, `presence_penalty` and Ollama's `repeat_penalty`. Shipped settings follow model-specific guidance with documented role exceptions; see [model evaluation](docs/maintenance.md#local-model-evaluation).

Every profile exposes **Cyber** as `ailocal-cyber`, a peer of the other capabilities with its own model guide. Shipped 16/32 GB defaults use hash-pinned localslm unrestricted Qwen 4B/9B imatrix GGUFs with embedded MTP, thinking off and Qwen's non-thinking sampling. Shipped 64/128 GB defaults use HauhauCS's balanced unrestricted Gemma 26B-A4B QAT GGUF and its published MTP head, thinking on and Google's sampling. Cyber is a separate model and releases its residency sooner when idle. These replace the earlier Opus-distilled Qwen and unrestricted MLX weights; their training and accuracy evidence do not transfer. Small native trials establish neither universal non-refusal nor repository-scale reliability. In `codex-local`, choose `ailocal-cyber` with `/model`; backend tags are implementation details.

In `claude-local`, `/model` lists exactly the enabled `ailocal-*` capabilities, in one order, with the same labels and descriptions `codex-local` shows; the built-in model lineup is replaced. Run `codex-local`, then use `/model` to select `ailocal-cyber` or another capability. Each selection automatically uses its own context window and reasoning default from the generated catalog. Capabilities configured with thinking off default to `none`; reasoning capabilities default to `medium`. An explicit client effort choice overrides that default. No extra launch preset is required. The API and gateway catalog use the same model IDs.

Each memory profile also owns a guide for every selected backend: how to frame a task, known limitations, sources and the scope of measured results. Codex includes that guide with the selected model; Claude Code receives it on session start and model switches through native hooks (model-switch hooks require Claude Code 2.1.251 or later). `claude-local` and `codex-local` can read the same current knowledge through the bundled `agent` MCP's read-only `model_guide(model?)` tool. Their launchers mark local callers, and the server refuses `delegate` before starting a child; use the current local session directly. Delegated children retain a separate recursion guard. Hosted clients can delegate through `local-agent` when Cadence declares it, because ailocal does not write hosted Claude/Codex roots beyond documentation MCPs. `delegate` accepts only an enabled chat alias, gives the child that alias's limits, and kills the child's process group on timeout, cancellation or a host stop; a hosted Codex parent needs `tool_timeout_sec` of at least 3660 for long runs. Below `full`, neither delegated worker can read ailocal's key-bearing env files. `delegate` runs in `text` mode by default, in an empty ailocal-owned directory. `research` (Claude only) reads inside `cwd` and searches the web. `edit` changes files only under `cwd`. `full` (no sandbox or permission prompts) runs only where `delegate_max_mode` in your `clients.toml` allows it for that worker. Below `full`, delegated Claude does not load the target project's own Claude settings, hooks, `.mcp.json` or `CLAUDE.md`. Each result's `capabilities` field states what the child was given. `ailocal profile guide ailocal-cyber --tier 16gb --json` reads exact current settings without loading a model. Guidance separates current routing facts from historical trial settings; this reduces ambiguity but does not guarantee a model reads them correctly. Coding still needs real syntax checks, tests and bounded repair attempts.

Pass a returned `session_id` as `resume` to continue a delegated session. Omitted options inherit its original agent, mode, model and directory. Conflicting options, a changed model policy or worker root, and unknown or pre-upgrade unbound sessions are refused; start a fresh delegation to change scope. The current operator ceiling is checked again. Only session identity and permission bindings are stored by ailocal; native clients retain transcripts.

For visual tasks, pass `image_paths=["/absolute/project/diagram.png"]` with `cwd="/absolute/project"`. Up to four PNG/JPEG files inside `cwd` are accepted, each at most 5 MiB, 8192 pixels per side and 16 million pixels. Images are explicit inputs and enable no additional tools. Text mode still runs in the owned empty directory; its `cwd` scopes only the supplied attachments. Both workers support fresh and resumed image input when the selected backend's guide declares images. Parent conversation images are not automatically forwarded. Codex and VS Code derive image capability from the same profiles; FIM and unqualified replacement models remain text-only.

For coding tasks, name the files and acceptance checks, and request an actual editing-tool call: `Edit` in Claude or `apply_patch` in Codex. Patch text alone does not change files. Claude's native guidance hook supplies the current task directory on startup and model changes; delegated tasks also carry their working directory. Treat a denied operation or incomplete reply as a failed task and validate the resulting files independently. A verified native completion without a final reply remains `DELEGATE_EMPTY`, but its error includes a bound `resume=UUID` for an explicit follow-up; no retry, fallback or task-success claim is automatic.

Standard Qwen 4B/9B thinking-mode tool conversations can stop with reasoning and no final text, matching an [open upstream report](https://github.com/ollama/ollama/issues/18916). Codex's existing `implementation` default uses non-thinking inference. Choosing `architecture` enables its thinking default; an empty final reply is still a failure, even when a file changed. Request an explicit follow-up or choose a non-thinking role deliberately; ailocal does not silently switch the model or mode.

`ailocal start` preserves other MCP servers registered in Codex's configuration, including their nested settings. Cadence owns repository-intelligence integrations; ailocal supplies its bundled research server.

`ailocal teardown` stops ailocal's server before restoring stock Ollama's recorded startup preference; open the stock app to start its server afterward. Existing installations with no original-state receipt report a recovery command instead of guessing. Models and operator configuration are retained. See [startup restoration](docs/maintenance.md#restoring-ollama-startup-on-teardown) for the exact scope.

## Defaults

What a fresh install gives you, without configuring anything.

| | Default | Change it with |
|---|---|---|
| Inference | Ollama, via LiteLLM on `127.0.0.1:4000` | — |
| Profile | chosen from your Mac's memory | edit `~/.config/ailocal/profiles/`, then `ailocal start` |
| LiteLLM | pinned by digest, version reported by `ailocal check` | a deliberate upgrade, then the gate |
| Tool search (`claude-local`) | on (`ENABLE_TOOL_SEARCH=true`) | `AILOCAL_TOOL_SEARCH=…` |
| Native Workflow (`claude-local`) | off | `AILOCAL_NATIVE_WORKFLOWS=1` |
| Local artifacts | on, bundled | — |
| Web search | ailocal's own SearXNG, keyless | `BRAVE_API` adds budgeted Brave to research |
| Context | the profile's `num_ctx`; 1M-context beta off | profile |
| Hosted Claude/Codex settings | public documentation MCPs only | native client configuration, retained after teardown |

Every `claude-local` variable is **process-scoped** — set it inline for one session. The full
table, with accepted values and effects, is in [docs/claude-local.md](docs/claude-local.md#configuration).

## Optional enhancements

None of these are required, and ailocal does not install them.

- **[Cadence](https://github.com/DevelopSolutionsLLC/cadence)** — composes engineering
  instructions, skills and agents. Separately owned; ailocal does not depend on it, and
  `claude-local` works normally without it.
- **Native Workflow** — `AILOCAL_NATIVE_WORKFLOWS=1 claude-local` restores Claude Code's
  built-in Workflow tool for one session. It is off by default because its schema cannot be
  deferred and it was never invoked in 148 measured sessions.
- **Language servers** — install a server binary and re-run `ailocal clients claude`; the
  plugin follows, in ailocal's root only.
- **Brave Search** — budgeted coverage for thin research results.
- **Node.js** — only the `architecture` artifact format needs it.

## Documentation

| Document | Purpose |
|---|---|
| [docs/claude-local.md](docs/claude-local.md) | what claude-local is, tool search, Workflow, every variable |
| [docs/architecture.md](docs/architecture.md) | how the pieces fit together |
| [docs/troubleshooting.md](docs/troubleshooting.md) | symptoms and fixes |
| [docs/security.md](docs/security.md) | secrets, permissions, exposure |
| [docs/adr/011-bundled-artifacts.md](docs/adr/011-bundled-artifacts.md) | why artifacts ship inside the package |
| [AGENTS.md](AGENTS.md) | developing and validating ailocal |

## License

Apache-2.0 — see [LICENSE](LICENSE).

Developed and maintained by Victor T. Chevalier for DevelopSolutions, LLC.
