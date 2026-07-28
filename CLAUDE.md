# CLAUDE.md — ailocal repo primer

Agent primer. Keep it under ~70 lines; merge or delete before adding a doc file.
User-facing setup/troubleshooting lives in README.md — don't duplicate it here.

## What this repo is

Tooling to run AI coding clients (Claude Code, Codex CLI, VS Code Copilot Chat)
against **local** models on Apple Silicon — no cloud, no code changes to the tools.

**Ollama** runs models natively (Metal/MLX). **LiteLLM** (one Docker container,
`127.0.0.1:4000`) fronts it as an OpenAI+Anthropic-compatible proxy. Each capability
(`architecture`, `implementation`, `review`, `completion`, `embeddings`) is served as ONE canonical
model_list entry named `ailocal-<capability>` — never a raw model tag. The `claude-*`/`gpt-*`
compatibility IDs (external client adapters that Claude Code / OpenAI SDK hard-code) are aliased onto
those `ailocal-*` groups via a `model_group_alias` block **generated from `config/clients.yaml`** —
one backend entry, many client-facing names. There is no `local/*` namespace. The old ailocal role names (`coder-main`/`deep-think*`/`supervisor`/…) are
gone. The map is the two source files: `config/profiles/<tier>.yaml` (what each capability is —
backend, context, sampling, keep_alive) and `config/clients.yaml` (which capability each client uses).

## Golden rule

**Use capability names only** in client configs and scripts — never backend tags
(`qwen3-coder:30b-a3b-q4_K_M`). Capabilities decouple configs from the models behind them, and the router owns
context, sampling, and per-role lifecycle (`keep_alive`). Agents request a capability; they never
name a model.

**`completion` is FIM/autocomplete only** — the 3B tier at `num_ctx` 4096. Never map a
conversational alias to it, and never use it as a `fallbacks` or `context_window_fallbacks`
target: any real agent turn routed there hard-400s with "No models have context window large
enough". Only autocomplete surfaces (`continue.autocomplete`) may point at it. Context-window
fallbacks must move **up** to a larger window, never down — falling from 16K to 4K cannot
succeed by construction. Measured regression: `claude-haiku-4-5` (and the `gpt-*` compat names)
mapped to `completion`, so Claude Code's background calls failed at `Got=12023`.
`sync-models.py` now **hard-fails** if any Claude slot resolves to `completion`, so this
invariant is enforced at generation time instead of living only in this paragraph.

**One capability per Claude slot.** Opus→architecture, Sonnet→implementation, Haiku→fast,
Fable→review. Two slots sharing a capability is legal but lists it twice in `/model` and
wastes a tier (sync warns). Haiku→`fast` also keeps the 14B free for real edits rather than
background summarisation. The generated block lives in `config/clients/configure.zsh`
between `BEGIN/END GENERATED claude slots` markers — it was hand-maintained until it drifted
into the `completion` bug above, so it is now spliced from `config/clients.yaml` like every
other generated region. The `/model` picker's ORDER is the key order of
`config/profiles/<tier>.yaml`.

**LiteLLM only reads its config at boot, and the config is bind-mounted.** So editing
`config.yaml`, a hook, or a persona changes nothing in the running proxy, and
`docker compose up -d` will not restart it — the spec did not change. It keeps serving the
OLD routing with nothing in the logs to say so. `start.sh` now fingerprints those files and
restarts the container when they change. Measured: after repointing `claude-haiku-4-5` at
`ailocal-fast`, the proxy still routed it to `ailocal-implementation` until an explicit
restart.

**Claude Code web search never reaches SearXNG.** Its native `WebSearch` is a *client-side*
tool. LiteLLM's interception accepts only `litellm_web_search` and bare `web_search`, and
deliberately refuses a `WebSearch` carrying an `input_schema`
(`integrations/websearch_interception/tools.py`) so it doesn't hijack the client's own handler.
Also: `websearch_interception_params.enabled_providers` matches the **backend** provider
(`ollama_chat`), not the inbound API dialect — it read `anthropic` and silently disabled
interception entirely. SearXNG itself is healthy; it is simply unreachable from that tool.

## The five non-obvious mechanisms

Most of this repo's complexity is in these; change them carefully.

1. **Generation.** Two source files drive everything: `config/profiles/<tier>.yaml` (WHAT each
   capability is — backend `active`, context, sampling, keep_alive, metadata) and
   `config/clients.yaml` (WHICH capability each client surface uses + the `claude-*`/`gpt-*`
   compat map). `sync-models.py` regenerates, between GENERATED markers or as whole managed
   files: the `model_list` **and** `model_group_alias` in `config/litellm/config.yaml`,
   `config/capabilities.generated.json`, `config/clients/model_catalog.json`, and the
   `claude/settings.json` · `codex/config.toml` (+ profiles) · `continue/config.json` · copilot
   tables. Never hand-edit a generated region — edit the two sources and run
   `./scripts/sync-models.sh`, then `./scripts/install-clients.sh` to deploy. Which tier is active
   is the one-line `config/active-profile` marker, written by `install.sh` from detected RAM
   (`config/profiles/{16,32,64,128}gb.yaml`); `--profile <tier>` overrides.
   `sync-models.py --resolve <capability>` prints the active backend (used by the shell scripts).
2. **Persona injection.** `config/litellm/persona_injector.py` is a LiteLLM pre-call
   hook merging `config/instructions/_core.md` + `<capability>.md` into whatever system
   prompt the client sent — server-side, so every alias inherits it. It handles **both**
   request shapes: OpenAI (`call_type` completion/acompletion — system lives in
   `messages[]`) and Anthropic `/v1/messages` (`call_type anthropic_messages` — system is
   the **top-level `system`** field), which is the route Claude Code uses. Reasoners get
   **no** persona (DeepSeek's guidance), temp 0.6 / top-p 0.95. LiteLLM issue #27518 (hook
   bypassed on `/v1/messages`) was filed against **v1.83.10**; on the **1.93.0** we run,
   the hook fires AND its mutation reaches the backend on both routes — measured, not
   assumed (persona marker + the propagation probe). Re-verify only after a LiteLLM version change — the image is now PINNED BY DIGEST
   (`deploy/litellm/docker-compose.yml`) and `scripts/check-litellm-version.sh` fails the
   regression gate on drift, because `main-stable` is a floating tag that already moved us
   from 1.92.0 to 1.93.0 with the docs left claiming the old version. Coupling: injection depends on model names resolving back to a
   capability key. The hook resolves the requested model through `model_group_alias` and
   uses that capability key to load `config/instructions/<capability>.md`. Any future change
   to canonical model names, aliases, or routing layers must preserve this mapping or
   personas silently stop applying.
   Completion and embeddings intentionally have no persona.
3. **Reasoning vs. non-reasoning.** No installed model currently thinks — the `deep-think*`
   reasoners (deepseek-r1) were removed, and `qwen3-coder` is Qwen's non-thinking variant. Every
   capability carries `additional_drop_params: ["thinking", "reasoning_effort"]` (so Claude Code
   sending `thinking` to a non-thinking backend doesn't 400) **plus** `think: false` (suppresses a
   backend's default reasoning, which otherwise hangs VS Code Copilot). Both are required — dropping
   either reintroduces a real, previously-hit bug. The reasoning path (`reasoning`/`merge` flags →
   `merge_reasoning_content_in_choices`, no drop, no `think:false`) is still in `sync-models.py`; a
   commented `reasoning` slot in `config/profiles/<tier>.yaml` restores the tier in one repoint.
4. **Client deployment is XDG-isolated.** Everything lands in `~/.config/ailocal/`;
   `~/.claude` and `~/.codex` are never touched, so cloud and local sessions coexist.
   `configure.zsh` defines the `claude-local` / `codex-local` / `ailocal-code` wrappers
   and is sourced from `.zshrc` between installer markers (`finalize.zsh` runs last).
   `CLAUDE_CONFIG_DIR` relocates `.claude.json` itself, so MCP registrations, history, and
   credentials are genuinely per-root — nothing leaks between local and cloud.

5. **Tool gateway.** `config/litellm/tool_gateway.py` is a pre-call hook that measures (and
   optionally trims) the tool payload clients declare. Measured: Claude Code sends **61 tools /
   104KB / 24,448 real Qwen tokens** on every `/v1/messages` request; **70.8%** of it is
   orchestration/scheduling/worktree machinery a local 30B cannot drive. Three modes via
   `AILOCAL_TOOL_GATEWAY` — `off` (compose default), `report`, `filter`. **`.env` sets `filter`,
   so filtering is live**; `off` is only the fallback when `.env` says nothing.

   **Tool activation is what shapes behaviour most, and it is task-classified.** The registry's
   `task_classes` decide which groups survive. A `conversational` class carries
   `override_always: true` so it drops BELOW the `always` floor to *no tools* — without it, "show
   me hello world in C++" arrived holding Read/Glob/Grep/Bash and the coding persona dutifully
   crawled the repo before answering (measured 61 tools → 1 after). Two guards, because losing
   tools mid-task strands an agent while spare schemas only cost tokens: an unmatched task keeps
   everything (fail-open), and the conversational override applies only to a genuine first turn —
   classification reads the FIRST user message, which never changes as a session grows, so without
   that guard a session opening with a chat question would stay tool-less forever.
   `mention_overrides` re-adds a group the user names explicitly; classification matches on TOPIC
   and is blind to instructions about HOW to work, so "delegate this to the reviewer subagent"
   matched `review` on the word "security" and lost the very delegation tool it asked for.

   **The subagent tool is `Agent`, and it lives in `delegation`, NOT `orchestration`.** Claude Code
   renamed it from `Task` in v2.1.63; the live `Task*` names are BACKGROUND-TASK management
   (`TaskCreate`/`TaskGet`/…), not delegation. `Agent` sat in `orchestration`, which every local
   class denies, so the gateway dropped it on every request — misread twice, first as "the model
   won't delegate" and then as "headless mode has no subagents". Both were the gateway. Verified
   working once `Agent` reached the model: it called `Agent`, and the reviewer subagent ran on
   `review` (`claude-fable-5 → review` in request_trace) while the parent stayed on `architecture`.
   The token argument never applied: Workflow alone is 21,525 B, `Agent` is ~1 KB.
   **MEASURED 2026-07-28 — Codex cannot use MCP tools at all, by either route.** Per-client
   gateway metrics show Claude Code receiving `mcp__lsp__get_hover` (flat function tools, which
   survive the `search`/`lsp` groups) while Codex's payload contains only
   `exec_command / multi_agent_v1 / apply_patch / <web_search>` — **no `mcp__*` entry**, with 104 B
   pre-filtered by LiteLLM before the gateway saw it. Codex declares MCP servers as `namespace`
   BUNDLES, which LiteLLM discards before the backend; enabling `namespace_expansion` instead makes
   the model emit flattened names that Codex's own dispatcher then refuses
   (`unsupported call: mcp__lsp__workspace_symbol_search`, openai/codex#20652). Both paths dead-end.
   So MCP-delivered capability — grepai, the LSP bridge, and Cadence's intelligence server — is
   reachable from Claude Code and VS Code but NOT from Codex, regardless of registration. Do not
   "fix" this at the proxy; it is a client limitation with an upstream issue.

   ALL facts about models/clients/routes/tools live in `config/litellm/registry.yaml` (the
   capability registry); the negotiator contains no such literal and a test enforces that by
   grepping its code. `tool-policy.yaml` was superseded by the registry and removed. Frontier
   models are `passthrough` — measured, forwarded untouched, and feature flags cannot override it.
   Two traps the code encodes: it is registered
   **last** so `websearch_interception` still sees the client's `web_search` tool, and it never
   drops an entry it cannot name (Codex's bare `{"type":"web_search"}` normalises to
   `<web_search>`; dropping it kills SearXNG silently). It also refuses to book Codex's
   `namespace` tools as savings — LiteLLM already discards those before the backend, so Codex's
   real gain is 18%, not 71%. Full detail, including the token calibration against Ollama's
   `prompt_eval_count`, in `docs/local-agent-gateway.md` (full architecture: registry,
   negotiation, verification, metrics, client profiles, flags, recovery). Phase-2 history
   and the measurement discipline behind the numbers is in `docs/tool-gateway.md`.

## Two shared boundaries with Cadence

ailocal and Cadence are independent *installations*, not independent *content*. Two seams matter:

1. **ailocal owns the Ollama daemon machine-wide** (`com.ailocal.ollama-env.plist`:
   `OLLAMA_MODELS=/Users/Shared/ollama/models`, keep-alive, parallelism), and Cadence's semantic
   index depends on it for `nomic-embed-text`. Tearing down ailocal breaks Cadence's indexing
   silently. Cadence's own `setup-ollama-env.sh` sets values only when unset, so it defers to this
   plist rather than fighting it — but the dependency direction is real and one-way.
2. **Cadence appends into this repo's deployed agents.** `cadence install claude --root
   ~/.config/ailocal/claude` writes a marker block (`<!-- cadence:start -->` …
   `<!-- cadence:end -->`) into `implementer/planner/reviewer/search/tester` in the *deployed* root,
   and links `repository-health` alongside. Cadence's `Explore/Plan/verify` are deliberately **not**
   linked — `search/planner/tester` already cover them, and adding both makes a nine-agent picker.
   **`install-clients.sh` rewrites those files and strips the block**, so the install order is
   cadence → ailocal → cadence-local. Re-run the Cadence installer afterwards;
   `cadence doctor --root ~/.config/ailocal/claude` reports `NO-OVERLAY` when it is missing.
   The same rewrite used to erase Codex's `[mcp_servers.*]` blocks; `install-clients.sh` now
   re-invokes `cadence mcp sync` itself, so that ordering is enforced in code rather than memory.

## Where things live

- `docs/environment-cheatsheet.md` — **start here for cross-system questions.** One page
  covering the ailocal/Cadence split, the request path, which retrieval system answers
  which question, per-language LSP status and its routing limits, required vs optional
  dependencies, and per-client differences. Written for a cold session.

- `config/profiles/<tier>.yaml` — role → backend + num_ctx + sampling (the source of truth).
- `config/litellm/` — `config.yaml` (generated block + hand-kept aliases/fallbacks)
  and `persona_injector.py`.
- `config/instructions/` — `_core.md` + per-capability enhancers (`_`-prefixed = not a capability).
  Per-capability instruction/behavior profiles; injected server-side by `persona_injector.py`.
- `config/clients/` — `configure.zsh`/`finalize.zsh`, `env.sh`, `model_catalog.json`,
  `scratchpad-hook.sh` (shared SessionStart hook → per-session
  `/tmp/scratchpad/<tool>-<session_id>/`), and per-client dirs `claude/` (settings,
  `agents/`, `commands/`), `codex/`, `copilot/`, `continue/`. Detail in its own
  `config/clients/CLAUDE.md`.
- `scripts/` — `install.sh`, `install-clients.sh`, `sync-models.sh` → `sync-models.py`,
  `start/stop/update/teardown`, `setup-ollama-env.sh`, `setup-startup.sh` (login
  LaunchAgents), `preload-model.sh`, `doctor.sh`, `smoke-test.sh`.

## Verify

`./scripts/test-all.sh` is the regression gate — ten checks (six suites plus
sync idempotence, shell/python syntax, and "every registered hook imports inside
the proxy image"). Run it before every commit; `--full` adds the end-to-end client
benchmark. A stopped/unhealthy proxy fails the gate rather than silently reducing
it. Then `./scripts/doctor.sh` (0=healthy, 2=degraded), `./scripts/smoke-test.sh`,
`bash -n` any edited script, and `./scripts/sync-models.sh` must produce **zero diff**
on a second run. After editing a persona `.md`, restart the proxy — the hook reads
them at load.

## Conventions

Shell: `set -euo pipefail`, reuse the `info/warn/step/backup` helpers. Python: stdlib
only. Never commit `.env` or secrets; ports bind `127.0.0.1` only. No Claude commit
attribution — Victor's identity only. Never `git push` without explicit approval.
