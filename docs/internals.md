# ailocal internals — the five non-obvious mechanisms

Most of this repo's complexity lives in these. Change them carefully.
`AGENTS.md` carries the bootstrap and links here.

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
   `ailocal sync`, then `ailocal clients` to deploy. Which tier is active
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
   **The local root inherits nothing from `~/.claude`, including its `AGENTS.md`.** So
   `config/clients/AGENTS.md` carries the shared engineering policy itself rather than
   pointing at one, and it is **composed by `sync-models.py`** from
   `config/clients/claude/instructions/{00-engineering-policy,10-ailocal-overlay}.md` with the
   capability and compat-alias tables substituted from the same sources as every other
   generated file. Edit the sources, not the composed file. It was hand-maintained until it
   drifted (a stale 16-32K context claim, a backend table four rows wrong, and a
   filesystem-first search rule contradicting the repository-intelligence ladder);
   `scripts/test-claude-instructions.py` now asserts those removals by string.

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
   **RE-VERIFIED 2026-07-29 on Codex 0.146.0** (the latest STABLE; `0.147.0-alpha.1` is
   schema-identical on every relevant field, so upgrading fixes nothing). Namespace wrapping is
   UNCONDITIONAL — it is NOT Code Mode, and no setting turns it off. These were each measured inert;
   do not re-propose them: `namespace_tools` does not exist in the binary (`ModelProviderInfo` has
   exactly 18 fields, none namespace-related); `[features.code_mode] direct_only_tool_namespaces`
   does nothing under any of the four plausible name forms; `[features] code_mode = false` does
   nothing; model-catalog `tool_mode = "direct"` does nothing — and the enum is
   `direct|code_mode|code_mode_only`, so `direct_only` is NOT a `tool_mode` value at all, it belongs
   only to `direct_only_tool_namespaces`. Gateway flattening clears **four of seven** boundaries:
   bundles expand (49 tools, zero namespaces left, zero killed by translation) and the model emits
   structured calls against them — then Codex's router refuses to dispatch BOTH
   `grepai_list_projects` and `mcp__grepai__grepai_list_projects`, with
   `[features.non_prefixed_mcp_tool_names]` enabled. The blocker is Codex's dispatcher, not the name
   shape and not the proxy, so `namespace_expansion` stays `enabled: false`.
   Schema claims about Codex must come from the NATIVE binary: the `codex` on
   `PATH` is a JS shim with none of the Rust config schema in it. Resolve it
   dynamically rather than hardcoding a version- or arch-specific path:
   `ls "$(npm root -g)"/@openai/codex/node_modules/@openai/codex-*/vendor/*/bin/codex`
   then `strings -a` that file (e.g. `struct ModelProviderInfo with N elements`
   enumerates every accepted provider key). If that glob resolves to zero or to
   more than one binary, STOP and report the ambiguity — do not pick one. Zero
   means the layout moved and the guidance is stale; several means multiple
   installs or architectures are present, and reading the wrong one yields a
   schema that looks authoritative while describing a binary you are not running.
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

