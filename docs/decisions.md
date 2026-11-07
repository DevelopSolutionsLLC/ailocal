# Decisions

Each section is one accepted decision, numbered as originally recorded; numbers are never reused, and a gap is a decision recorded elsewhere or never taken. Evidence labels: `[REAL]` measured on the actual path, `[APPROX]` a reduced or simulated probe, `[UNVERIFIED]` not measured.

## 004 Tool gateway and task classification

Accepted 2026-07; partially superseded at LiteLLM 1.98.0.

**Context.** Claude Code declared 61 tools / 104 KB / 24,448 Qwen tokens on every `/v1/messages` request, about 70% orchestration a local model cannot drive, and a persona holding `Read`/`Grep`/`Bash` crawled the repository before answering a plain question.

**Decision.** `tool_gateway.py` is a LiteLLM pre-call hook; `registry.yaml` is the only source of facts about models, clients, routes and tool groups. Removable groups are the intersection of what the client will drop and what the model class denies, then a task classifier narrows further. Guards: an unclassified task keeps every tool (fail-open); only `conversational` may go below the `always` floor, and only on a genuine first turn; `mention_overrides` lets an explicitly named mechanism outrank an inferred class; the hook registers after `websearch_interception`. Mode is `filter` in `.env`; the Compose default is `off`.

**Consequences.** [REAL, LiteLLM 1.93.0] conversational requests went 61 tools → 1; overhead 0.2–0.7 ms per request. At 1.98.0 LiteLLM expands `namespace` tools itself, so removing them is a real saving and the drop set is `[computer_use, image_generation, shell]`; the earlier "Codex gain is 18%, not 71%" figure is withdrawn and not re-measured. `namespace_expansion` ships disabled and has lost its justification: name a requirement 1.98 does not meet, or delete it with its config and suite. Open: the conversational class does not hold across a multi-turn loop (turn 1 kept 1/61, turns 5–9 kept 48/61) because `first_user_text()` changes under subagent context or compaction; the `conversational` task-class check fails by design until classification survives context changes. `registry.yaml` is read once at `gateway_init`.

**Rejected.** Send everything: the measured behaviour above. Fixed per-model allowlist: cannot tell "explain" from "refactor" on one model. Model self-selection: an extra round trip on the component least able to judge.

**Revisit if** a local model is reliably agentic across the full tool surface, or classification strands an agent in practice.

## 008 Local and hosted models side by side

Accepted 2026-07.

**Context.** Local models are private and free but slower and weaker; hosted are the opposite. Both are wanted on one machine, and a setup where `claude` sometimes means local is worse than either.

**Decision.** `claude-local` and `codex-local` are shell functions that inject process-scoped environment only. `CLAUDE_CONFIG_DIR` / `CODEX_HOME` relocate the whole config root to `~/.config/ailocal/`, so MCP registrations, history, credentials and session state are per root; `~/.claude` and `~/.codex` are never touched. The command name is the mode.

**Consequences.** Hosted clients do not route through LiteLLM, so routing, tool gating and classification affect local paths only; every served model name, including the `claude-*`/`gpt-*` compat names, aliases onto a local capability. `CLAUDE_CODE_DISABLE_1M_CONTEXT=1` keeps the client from requesting a window the backend cannot hold. The wrappers exist only in shells that source `.zshrc`; each root accumulates its own state (Codex's plugin cache regrows to ~77 MB per root, disk only).

**Rejected.** Replace hosted entirely: local models cannot yet carry all work. A `.zshrc` mode switch: invisible global state.

**Revisit if** local models close the gap, or a client stops honouring its config-root variable.

## 010 Host policy is TOML, read by `tomllib`

Accepted 2026-08-07.

**Context.** The five policy files (`profiles/{16,32,64,128}gb`, `profiles/clients`) were YAML read by a ~185-line constrained parser in `policy.py`, written because PyYAML is absent from the host interpreter. `tomllib` is standard library from Python 3.11.

**Decision.** The files are TOML, read by `tomllib`, with no YAML fallback. `registry.yaml` and the generated LiteLLM config stay YAML because LiteLLM defines that format.

**Consequences.** Every construct round-trips, and duplicate keys, duplicate tables and malformed files raise `TOMLDecodeError`; regeneration from YAML and TOML sources was byte-identical. `policy.py` lost 223 lines. Hazard: an unquoted dotted key (`gpt-5.5 = …`) silently nests, so `load_client_policy` requires string `compat` values and the suite asserts the bare form is rejected.

**Rejected.** Keep the hand-written parser: a second parser to keep in step with the schema. PyYAML: a dependency for five small files.

## 011 The artifact capability ships inside ailocal

Accepted 2026-08.

**Context.** Claude Code does not register its hosted `Artifact` tool under API-key auth, so a `claude-local` session could not produce a diagram, page or report.

**Decision.** The renderer ships under `resources/integrations/local-artifacts/`. `ailocal clients claude` provisions a venv for it in the state root, installs `mcp` there, and merges one `artifact` key into the local root's `.claude.json`. `mcp` is a subprocess dependency; nothing in `ailocal/` imports it, so `dependencies = []` holds.

**Consequences.** 5.2 MB of vendored elkjs, mermaid and marked enter the wheel, which is what makes the page's `connect-src 'none'` enforceable. Provisioning needs the network once, content-addressed against `requirements.txt`. `architecture` diagrams need `node`; without it only that format fails. [REAL] clean-machine provisioning preserved `github`/`grepai`, a second run wrote nothing, and `claude-local -p` called `mcp__artifact__publish` with the source landing in `.artifacts/`.

**Rejected.** Keep it external: four extra steps to a `claude-local` that silently cannot draw. Add `mcp` to ailocal's dependencies: breaks standard-library-only for code ailocal never imports.

**Revisit if** Claude Code registers `Artifact` for API-key auth (delete the component, its provisioning, its suite and this section together), or the vendored bundle outgrows a wheel.

## 012 Artifact colour comes from a pinned upstream design system

Accepted 2026-08.

**Context.** The renderer's palette was ~20 hand-picked literals: gated for contrast, but untraceable and without an update path.

**Decision.** Colour values derive from `@carbon/themes@11.80.0` (Apache-2.0, DTCG Format Module 2025.10), generated into `themes/carbon.tokens.json` by `tools/update_carbon_tokens.py` (pinned, integrity-verified, byte-reproducible). The semantic mapping, typography and geometry stay authored in `themes/artifact.tokens.json`; `tokens.py` resolves both. Renderers see semantic names only, and a test asserts they hard-code no colour.

**Consequences.** Bumping upstream is `--pin` plus a commit; `--check` fails a stale tree. Accents take Carbon step 60 on light and 40 on dark: [REAL] 4.53–4.57:1 and 6.15–6.48:1 across ten families, which makes the C4 text label the carrier of meaning in greyscale. [REAL] design gates 25 → 47, artifact suite 171 checks, palette 15.8 KB from 255 KB upstream, 26/26 rendered colours traceable to a token. `NOTICE` carries attribution and modification. The authoritative reference is `src/ailocal/resources/integrations/local-artifacts/DESIGN-SYSTEM.md`.

**Rejected.** Runtime dependency on Carbon: needs Node and the network. Hand-copied values: no provenance, silent drift. Vendor the DTCG files whole: 255 KB for ~30 used tokens. Keep the hand palette: no update path.

## 013 The artifact preview server outlives the session

Accepted 2026-08.

**Context.** Artifact URLs intermittently refused connections. [REAL] Cause: the HTTP listener was a daemon thread inside the stdio MCP process, which Claude Code terminates at session end, so every URL died with its session. A second defect: a session that found the port taken never retried. Both were inherited from upstream `xiagaohui/local-artifacts-for-claude-code`.

**Decision.** `server.py --serve` is one shared, on-demand, idle-exiting preview server, started by the first `publish()` with `start_new_session=True` so the session's `SIGTERM` does not reach it; later sessions probe `/status` and reuse it, and a bind-race loser exits. Publishing crosses the process boundary through the existing 0600 state file, written atomically and polled at 4 Hz; no write endpoint is reintroduced. `publish()` returns a URL only after the server confirms it serves that artifact, otherwise reports "Artifact saved, but NOT viewable" with the source path. The server exits after `LOCAL_ARTIFACTS_IDLE_EXIT` (default 30 minutes) without requests, publishes or an open viewer.

**Consequences.** One ~24 MiB process outlives the session, bounded by the reaper; the gate sets the idle exit to 5 s. [REAL] cold start 0.351 s vs 0.18 s warm; a killed publisher or process group leaves the viewer serving; four racing publishers leave one viewer; a foreign server on the port is refused; `test_lifetime.py` 32 passed. Up to 250 ms between publish and tab update. Windows is [UNVERIFIED].

**Rejected.** Ephemeral port: fixes collision, which was not the defect. A container or the LiteLLM container: 23.6 MiB on the host against 121–854 MiB per container. A login-time service: a session that never draws should cost no process.

## 014 Local research owns its evidence boundary and the only Brave path

Accepted 2026-10-18 (#9, #13).

**Context.** Providers may ignore site filters and some pages cannot be read, so a cited answer must not imply every source was fetched. Operators want Brave used automatically when keyless coverage is thin, within a per-period allowance and no overage; SearXNG's `braveapi` engine discards Brave's rate-limit headers, so neither SearXNG nor LiteLLM's interception can bound spending. [REAL] this account's key reports `Policy: 50;w=1, 0;w=2678400`: no monthly cap is visible.

**Decision.** A bundled MCP component (packaged per 011) exposes `search(query, sites?, preset?, use_brave?)` and optional `answer(question, sites?)`. Site scope is enforced on returned URLs and redirects; records distinguish page text from indexed snippets; only public pages are fetched. The server plans providers per preset and bounds fan-out, concurrency, time and session spend; an identical repeated search is served once more with a warning, then refused. `braveapi` is not a SearXNG engine: the research server calls Brave directly behind a cross-process SQLite ledger that reserves each request under `BEGIN IMMEDIATE` and settles it as billed, not billed or unknown, against `AILOCAL_BRAVE_BUDGET` (default 1,000) per provider period; at most two Brave requests per call; a non-`RATE_LIMITED` 429 or 402 stops spending until reset. Every result carries a `brave.summary` separating the local budget from account-wide usage. `answer` uses keyless providers and the local LiteLLM route with no tools, no history and no hosted fallback, and is registered in claude-local only on opt-in because it shares the inference slot.

**Consequences.** ailocal owns fetch safety, cache correctness and citation checks; a valid source ID does not show a source supports a claim. The ledger counts only this machine; Brave's dashboard usage limit is the account bound. [REAL] local models omitted `brave.summary` from 12 of 12 final answers. General web search in the local clients stays keyless.

**Rejected.** Native search plus a Fetch MCP: leaves provenance and scope to each caller. `braveapi` behind a counter: cannot see quota headers. Patching the engine: forks upstream. A per-process counter: cannot bound concurrent sessions.

**Revisit if** a supported component meets the evidence contract, SearXNG exposes provider headers, Brave publishes a usage API, or scheduling makes the `answer` restriction unnecessary.

## 015 Delegation binds session authority and takes explicit images

Accepted 2026-10-28 (#35–#37).

**Context.** The delegation adapter took only a string prompt, and a resume UUID was not bound to the agent, mode, directory or model that created it; omitted options reused fresh-call defaults, and native clients allow overrides on resume.

**Decision.** Native CLIs keep inference and transcripts. ailocal stores only a private, versioned binding under the state root: UUID, CLI, worker root, mode, requested and effective working directories, alias and execution-policy fingerprint; no prompt, reply, transcript or image bytes. Only bound IDs resume; ID-only resumes inherit, conflicts and policy changes fail before launch, and the operator ceiling is rechecked. Images are an optional list of explicit local paths inside `cwd`: four PNG/JPEG, 5 MiB, 8192 px per side, 16 Mpx each, validated with macOS `sips`, staged as private copies, delivered as Claude structured input or repeated Codex image flags. A native completion without a final reply is `DELEGATE_EMPTY` carrying `resume=UUID`; no automatic retry, mode change or reasoning-to-answer promotion.

**Consequences.** Resume cannot silently switch model or reuse a stronger session under a weaker mode. Deleting binding state makes native sessions unmanaged, not deleted. Remote URLs, parent-conversation copying, video and PDF are outside the interface.

**Rejected.** UUID plus prompt advice: scope unenforced. Reading native transcripts: a second history reader coupled to two clients. Signed resume tokens: a new key lifecycle. Mode change on resume: an untested transition. Filenames in prose or parent-generated base64: no guaranteed binary input, inflated arguments.

## 016 Qwen tool-loop history keeps the thinking-mode signature

Accepted 2026-10-29 (#38).

**Context.** With thinking on, Qwen3.5 4B/9B could finish a correct tool loop with its answer inside an open think block and no visible content. [REAL production inference; APPROX one captured Codex conversation, 2026-10-29] earlier assistant turns reached the model as empty `<think>\n\n</think>` blocks, the non-thinking signature: the stock renderer ended empty in 9 of 12 replays, restored or omitted blocks in none. Two causes: Ollama's `Qwen35Renderer` writes a think block for every post-query assistant turn, and LiteLLM 1.106.2's non-streaming Responses bridge emits reasoning without `summary`, which Codex 0.165 drops.

**Decision.** A reviewed Ollama patch omits the think block of a completed assistant turn in the current tool loop when thinking is on and the turn has no reasoning; reference rendering is unchanged elsewhere, and `prepare()` must execute the three history regressions. `tool_repair` gives each de-streamed reasoning item the `summary_text` form, so Codex replays real reasoning. No client message is dropped, reordered or invented; `DELEGATE_EMPTY` stays an error.

**Consequences.** The patch is reapplied and its regressions rerun on every Ollama base; it retires when upstream renders the case or every client replays reasoning, and the `tool_repair` normalisation retires when LiteLLM's bridge emits `summary`. No shipped profile currently selects a role that needs the patch.

**Rejected.** A decoder grammar forbidding end-of-generation in thinking: treats the symptom and changes sampling. Disabling thinking, retrying or promoting reasoning: hides the defect. Replay only: depends on every client and two third-party hops.

## 017 Local client roots publish an instruction budget; delegated denials are reported

Accepted 2026-10-29 (#39, #47, DevelopSolutionsLLC/cadence#64).

**Context.** ailocal and Cadence each run without the other; their only seam is the versioned `integration-contract.json`. Cadence composes `CLAUDE.md`/`AGENTS.md` into ailocal's roots. [REAL claude-local 2.1.308, 32gb Qwen3.5 9B thinking; APPROX three fixture tasks, 2026-10-29] with the 17 KB hosted policy, auto memory on failed 3/6 and off passed 4/6 (one run re-read `CLAUDE.md` ~90 times); with the file excluded, 9/9 passed in ~36 s instead of 55 s. A delegated child retried one denied `Read` 228 times, and headless Claude reports denied runs as `subtype: success`.

**Decision.** ailocal publishes `instructions.<client>.max_bytes` (contract schema 3) from `instruction_max_bytes` in `clients.toml`, default 4096, replaced per tier by `instruction_max_bytes_by_tier` where measured; `ailocal check` warns when always-on instructions plus the output of every hook ailocal registers exceed it, and never rewrites the file. Cadence composes a compact policy for a published budget and refuses to write over it. `claude-local` defaults `autoMemoryEnabled: false`. Delegated Claude runs with `--permission-prompts none` and the result reports `permission_denials`.

**Consequences.** [REAL 2026-11-04] #60 cut the guide hook to 1,368–1,742 bytes for a named model; #62 excludes the hosted `~/.claude/CLAUDE.md` and `~/.claude/rules/` (13,335 bytes loaded as ancestor memory); #66 denies eight unused grepai tools (7.2 KB). [REAL claude-local, 128gb gemma4:26b-qat-mtp, stock Ollama 0.40.14, 2026-11-07; APPROX three fixture tasks] 4.6 KB and 8.2 KB always-on both passed 3/3 fix and scope tasks; both failed 5/5 on a denied-write task, so denial reporting remains the guard; `claude.instruction_max_bytes_by_tier = { "128gb" = 8192 }`. Codex and the other tiers keep 4,096, unmeasured. A Cadence without schema 3 fails closed; Codex denials stay invisible where its JSON stream omits them.

**Rejected.** ailocal excludes the file itself: silently overrides a co-installed tool. ailocal writes its own instruction file: two writers for one path. Fail a delegation on any denial: some denials are harmless.

## 018 The chat roles are `primary` and an optional `fast`

Accepted 2026-11-05 (#89).

**Context.** Read from the 0.15.1 profiles: every tier served `architecture`, `implementation` and `review` from one backend with one thinking mode, differing only in output reserve and prose; `fast` was that backend with thinking off.

**Decision.** The roles are `primary`, `fast`, `cyber` and `completion` (`policy.ROLES`). `primary` thinks on 64/128 GB and not on 16/32 GB. `fast` (the primary with thinking off) is declared only where the primary thinks; a slot, compat id or default naming it elsewhere resolves to `primary` through `policy.resolve_capability()`. Claude opus, sonnet and fable map to `primary`, haiku to `fast`. The retired names are served for one release as deprecated LiteLLM aliases (`policy.RETIRED_ROLES`) and removed in 0.17. A profile still declaring a retired section fails closed; `ailocal install` migrates it (`install.migrate_profile_roles`): `[primary]` takes `[architecture]` verbatim, a conflicting edit is reported and nothing is written, originals are backed up.

**Consequences.** The picker, Codex catalog, VS Code group and LiteLLM `model_list` each list one chat model plus `fast` where it exists. The alias override is per role (`AILOCAL_PRIMARY_ALIAS_OVERRIDE`, `AILOCAL_FAST_ALIAS_OVERRIDE`). [REAL 2026-11-05] ailocal-owned always-on Claude context is 2,100 bytes on 64/128 GB and 1,549 on 16/32 GB. No backend, geometry, sampling or thinking mode changed for any request.

**Rejected.** Keep the names as roles on one backend: four entries for one thing. `fast` on every tier: a second name with a smaller reserve. Resolve absent `fast` in each consumer: per-tier cases that can disagree. Precedence-based conflict migration: discards an operator value silently.
