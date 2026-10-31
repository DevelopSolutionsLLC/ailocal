# Changelog

Release policy and the meaning of each bump: [RELEASING.md](RELEASING.md).

## v0.15.1 (2026-10-30) — documentation

### Changed

- One install path. The README and each release's notes carry the same command, `pipx install git+https://github.com/DevelopSolutionsLLC/ailocal.git@vX.Y.Z`, pinned to the release; upgrading installs the next tag with `--force`. The wheel stays attached to the release for download and verification.
- Documentation states each rule once and keeps procedure apart from evidence. `docs/maintenance.md` holds procedure only; its dated measurements from 2026-10-22 to 2026-10-26 moved verbatim to `docs/model-audit.md`. `docs/architecture.md` carries one flow diagram and one on-disk layout. README, AGENTS.md and RELEASING.md are shortened without a change in meaning.

## v0.15.0 (2026-10-30) — local models and bound delegation

### Breaking

- Delegation resumes require a session binding created by this implementation. Unknown or legacy session IDs are refused; start a fresh delegation to migrate. A resumed call inherits its original worker, model, mode and directory, and refuses conflicting choices or changed execution policy.
- The integration contract moves to schema 3 to publish local instruction-file budgets. Upgrade Cadence to a build with schema-3 support before regenerating its local client instructions; older builds refuse those targets. The instruction-file budget does not change model context or output limits. See [ADR 017](docs/adr/017-instruction-budget.md).

### Fixed

- Runtime preparation runs the upstream renderer suite from its package directory, as `go test` does. Run from the payload directory, two upstream fixture tests failed to open `testdata/` and every build of `0.40.8-ailocal.18631.4` from the shipped pins was refused [REAL 2026-10-30]. The install warning now carries the failing step's last lines instead of the exception class alone.
- Native imports drop a repository pull's own `hf.co/...` manifest once the alias verifies, so `ollama list` shows only the names policy chose; shared layers are kept and an official library source tag is never removed. [REAL 2026-10-30] A Cyber import had left `hf.co/localslm/Qwen3.5-4B-MTP-Uncensored:model_q4_k_m.gguf` beside `qwen3.5:4b-unrestricted-mtp-q4_K_M`.
- Every profile selection must be a `name:tag` identity and never `:latest`; `tests/profiles.py` fails otherwise. `ollama create` without a tag publishes `name:latest`, which names nothing, and an earlier naming scheme left such entries in `ollama list`.
- Isolate native runtime regression tests from the operator's model store and Ollama listener. Runtime preparation also runs the upstream renderer suite; the three required Qwen history regressions must execute even when optional upstream parity tests skip.
- Validate typed SDK reasoning summaries before copying them, preserving replay metadata without serialization warnings that can include reasoning text.
- Fresh-install validation distinguishes client model routes from delegation permission ceilings. `delegate_max_mode = "edit"` no longer fails as an unknown capability; invalid routes and ceilings still fail.
- Native client guidance names the current task directory, distinguishes Codex tool calls from printed patches and preserves patch indentation. Claude's hook derives the directory from each event rather than retaining an earlier session's location.
- A native turn that completes successfully without a visible reply remains `DELEGATE_EMPTY`. Its error now provides a bound session ID for an explicit follow-up under the same execution policy. Failed, cancelled and timed-out fresh runs receive no resume binding; ailocal never retries automatically or treats file changes as task success.
- **Qwen thinking-mode empty replies (#38).** With thinking on, Qwen3.5 4B/9B could finish a correct tool loop with the answer inside the open think block and no visible reply. Earlier loop turns reached the model as empty think blocks, the non-thinking signature, because Ollama's Qwen renderer writes one for every turn and Codex never replayed reasoning. The patched runtime (`0.40.8-ailocal.18631.4`) omits that empty block in thinking-mode tool loops only, and `tool_repair` now emits de-streamed Codex reasoning with the `summary` Codex requires, so it is replayed as Ollama `thinking`; Claude Code already replayed it. [REAL native inference; APPROX one captured conversation with fixture tool results] Stock: 13/16 runs ended empty across the four Qwen backends; patched: 16/16 genuine final replies. Through actual `codex-local`, `claude-local` and MCP delegation on Qwen 9B, no run ended empty; remaining failures are client-configuration path confusion (#39). ADR 016. **Migration:** run `ailocal install` to build the runtime.

- **Local models no longer misuse long instruction files or claim success past denied calls (#39, ADR 017).** `claude-local` defaults `autoMemoryEnabled: false` in its own settings (an explicit setting wins), because the auto-memory path in the system prompt was taken for the working directory. The integration contract moves to **schema 3** and publishes `instructions.<client>.max_bytes` from `instruction_max_bytes` in `clients.toml` (default 4096); `ailocal check` warns when a `CLAUDE.md`/`AGENTS.md` in ailocal's roots exceeds it, naming its owner. Delegated Claude runs with `--permission-prompts none` and results list `permission_denials`. [REAL claude-local, Qwen 9B; APPROX three fixture tasks] Full 17 KB policy loaded: 4/6; excluded: 9/9. **Compatibility:** a Cadence that predates schema 3 blocks its ailocal targets until upgraded (DevelopSolutionsLLC/cadence#64); ailocal is unaffected.

### Security

- Pin the wheel build backend to setuptools 84.1.0, beyond the assessed manifest-normalization and legacy-wheel path advisory ranges. This does not resolve the separately recorded machine and inference-runtime audit findings.

### Changed

- **Bound delegation resume.** A returned session ID is bound to its worker root, CLI, mode, model, working directory and execution policy. ID-only resumes inherit those choices; conflicts, changed policy, unknown/legacy sessions and concurrent resumes fail before launch. The current operator ceiling is checked again. Only identity/permission metadata is stored; native clients retain conversation history.
- **Delegated image input (#37).** `delegate(image_paths=[…], cwd=…)` attaches up to four PNG/JPEG files inside `cwd`, at most 5 MiB and 16 million pixels each, with an 8192-pixel side limit. Images use Claude's structured input and Codex's native image flags on fresh and resumed calls. Private copies are removed after the call; images enable no additional tools. Parent attachments are not inherited automatically. Codex's catalog now projects the same exact-backend image declarations as VS Code; FIM and unqualified replacements remain text-only.
- **Behaviour change.** VS Code uses its built-in Custom Endpoint provider instead of the LiteLLM Connector extension, which ailocal no longer installs. The `ailocal` provider group lists every enabled chat alias with the active profile's input/output limits, thinking and temperature/top-p; FIM is excluded from the picker. The stored API-key reference of a former connector group aimed at this proxy is carried over; other provider groups are untouched. `ailocal start` refreshes an already configured group and a generated model-guidance block in the Copilot instruction file. The `agent` MCP is registered beside `research` for VS Code's Local harness. **Migration:** run `ailocal clients vscode`; uninstall the connector extension yourself if nothing else uses it.
- `ailocal install --upgrade-models` explicitly adopts the shipped model identities, sources, sampling, MTP depth, enabled flags, role/purpose text, `disk_gb` and model guides in every existing profile, keeping authored output reserves and keep-alive; aliases sharing one backend are aligned to their largest authored window. All candidates are validated before any write, originals are backed up under `backups/model-defaults-<ns>/`, and a failed write restores every replaced profile. A normal install still preserves edited profiles.
- Model guides carry each alias's actual sampling preset and MTP depth into `ailocal profile guide`, the Codex catalog instructions, Claude's hooks and the Copilot instruction block, so direct local sessions receive advice without MCP.
- **Behaviour change (local-agent `delegate`).** The default `tools=false` mode now gives local Claude no built-in tools and no MCP servers; local Codex has no tool-free mode and runs in its read-only sandbox, which can read files. `tools=true` requires an existing absolute `cwd` and is now actually confined to it: delegated Claude ran with `--dangerously-skip-permissions`, which lifts the working-directory boundary, and a model that misread its working directory rewrote an unrelated file under `$HOME` [REAL, 2026-10-28]. It now runs in `acceptEdits` mode with Claude Code's OS Bash sandbox required (no unsandboxed fallback) and auto-memory off; Codex keeps its `workspace-write` sandbox. A delegated agent cannot delegate again. Failed runs (Claude `is_error` or a nonzero exit, Codex nonzero exit) are reported as errors instead of returned as answers. Inherited cloud-provider and hosted-auth variables are dropped from the child environment, and delegated Claude gets the same slot and timeout environment as `claude-local`.
- **local-agent `delegate` modes (#36).** `delegate` takes `mode`: `text` (the default), `research` (Claude only), `edit` (sandboxed and confined to `cwd`, as `tools=true` was) or `full`. Research gives local Claude only `Read`, `Grep` and `Glob`, confined to `cwd` (symlinks included) or an empty directory when no `cwd` is given, plus the `search` tool of ailocal's own research server, with its local `answer` tool off. It has no write, Bash or Agent tool, and its answers attribute claims to search snippets, not pages read. Codex refuses `research` with `UNSUPPORTED_MODE`, because its read-only sandbox still runs code. Every result carries a `capabilities` block (built-in tools, MCP servers, hooks, settings sources, permission mode, sandbox, reads, writes, denied reads, working directory, environment policy). It is built from the same per-mode table that builds the argv, so it cannot drift from what ran. Full runs without the CLI's sandbox or permission prompts and with the parent environment, including the SSH agent, tokens and readable key files. It is for tasks a sandbox blocks, such as docker, git over SSH or package installs. Claude runs it with `bypassPermissions` and the sandbox off; Codex runs it with `danger-full-access`. Routing and hosted-session variables are still dropped, and a full-mode child still cannot delegate. Each worker's ceiling is the operator's `delegate_max_mode` under `[claude]` and `[codex]` in `clients.toml`. It defaults to `edit` when absent and is read on every call, so lowering it needs no restart. A call above the ceiling fails with `MODE_NOT_PERMITTED`, and `mode` contradicting `tools` fails with `CONFLICTING_MODE`; neither falls back to another mode. `tools` keeps its meaning, and results report the effective `mode`. **To allow full mode:** set `delegate_max_mode = "full"` in `~/.config/ailocal/profiles/clients.toml`. An edited copy is preserved, so an existing file has no key and stays at `edit`.
- **Behaviour change (local-agent `delegate`, #35/#36).** `text` mode, and `research` without a `cwd`, now run in an empty ailocal-owned directory (`~/.local/state/ailocal/local-agent/empty`) instead of the hosted session's working directory, so local Codex no longer reads that repository by default and neither child picks up its configuration. One fixed directory keeps resume working: Codex filters resume by working directory and Claude stores sessions per directory. Below `full`, delegated Claude runs with `--setting-sources user`: the worker root's own hooks and model guide still load, but the target project's `.claude/settings.json`, hooks, `.mcp.json` servers and `CLAUDE.md` do not. A headless run had loaded a repository's `.mcp.json` servers without asking, and its settings could add hooks or widen the sandbox, all outside the edit confinement. Put what the task needs in the prompt. `full` still loads them. Edit mode no longer passes `--permission-mode acceptEdits`. With the credential scrub on, Claude Code 2.1.308 forces the mode to `default`, so headless edits would be denied. Edits are now granted by an allow rule for files under `cwd`.
- **Behaviour change (local-agent `delegate`, #35).** `model` must be an enabled alias from `model_guide`; `ailocal-completion` and unknown names are refused before launch (`MODEL_NOT_DELEGABLE`, `UNKNOWN_MODEL`). Delegated Claude now receives the selected alias's output and context limits instead of the launch default's. `resume` takes only a returned session id (`INVALID_SESSION`), and a concurrent resume of one session is refused (`SESSION_BUSY`). Codex resume works: it previously failed on every call because `exec resume` rejects `--sandbox`. An empty reply is an error (`DELEGATE_EMPTY`), and so is a Codex run without `turn.completed`. Output past the bound ends the run with `DELEGATE_OUTPUT_LIMIT`. The server no longer blocks while a child runs. Timeout, cancellation, the host closing the connection, and SIGINT/SIGTERM to the server now kill the child's whole process group; a SIGKILLed server cannot, and a descendant that starts its own session escapes. The proxy key no longer reaches delegated processes under `LOCAL_AGENT_KEY`, and Claude scrubs credentials from its subprocesses. Delegated Claude is denied reads of the key-bearing env files, both in the Bash sandbox and through the Read tool; before this, `tools=true` Claude could read them. Delegated Codex is denied them too: below `full` it runs under a per-run permission profile (`ailocal-delegate`, extending `:read-only` or `:workspace`) with a `deny` entry for each key file, selected with `-c` and never `--sandbox`. Interactive `codex-local` keeps its config's `sandbox_mode` unchanged. Hosted-session variables (`CLAUDE_*`, including the messaging token and socket, and `SSH_AUTH_SOCK`) are no longer passed to the child. A hosted Codex parent needs `tool_timeout_sec` of at least 3660 on its `local-agent` entry (Cadence declares it); Codex's 60 s default cut long delegations short. After a normal exit, the child's remaining process group gets a 1 s grace after TERM before KILL; previously KILL followed at once. A Codex event line larger than the output bound is skipped rather than failing the run, because the reply is read from Codex's last-message file.

- **Model default upgrade (#33).** Use a hash-pinned Unsloth imatrix/MTP Qwen3.5 9B GGUF for the 32 GB shared primary and 64/128 GB fast backend. Keep calibrated unrestricted 9B GGUF for Cyber; both select measured draft depth 3. Preserve image input and the 16 GB model pair. Record the Qwen3.8-derived MLX load failure and GGUF speed/quality tradeoffs rather than treating the version label as proof of an upgrade.
- Verify native model pins only against Ollama's leading asset directives, so template/license text or a projector cannot impersonate the target or MTP head. Recheck native asset bindings for existing receipts rather than grandfathering earlier imports. Document the remaining gap between target/head pins and complete source-manifest locking.
- **Behaviour change.** Chat backends follow the model-format rule in `docs/maintenance.md`: native originals or documented optimized builds, GGUF by default, MLX only with measured speed/fidelity evidence. Shipped 16/32 GB defaults select Qwen 4B/9B MTP GGUF; Cyber uses pinned localslm imatrix/MTP GGUFs. The 64/128 GB shared defaults select official Gemma 26B QAT GGUF plus a pinned MTP head, HauhauCS balanced unrestricted QAT/MTP for Cyber, and Qwen 9B MTP for fast tasks. Native pull/create recipes verify target/head identities and built-in rendering; no tensor conversion or additional Ollama patch is introduced. Draft depth is selected per backend from native trials. Exact scores, failed cases and hardware limits are in `docs/model-audit.md`; MTP does not establish improved accuracy or additional training knowledge. **Migration:** edited profiles are preserved; `ailocal install --reset-config` adopts the shipped defaults after backing up operator edits. Old model tags remain on disk.
- **Behaviour change:** ailocal no longer writes the delegation MCP into hosted roots (#28). It registered `local-agent` in `~/.claude.json` and `~/.codex/config.toml` with no owner marker, outside the documentation-only hosted exception, so deleting ailocal's roots left hosted entries pointing at a removed command. The server is now registered as `agent` in `claude-local` and `codex-local` only, the same split as `research` and `local-research`; hosted delegation is Cadence's marked `local-agent` declaration. A former `local-agent` entry in a local root is removed when it has ailocal's exact shape. **Migration:** a hosted `local-agent` that ailocal wrote earlier is left in place; Cadence adopts it on `cadence mcp sync`, and without Cadence remove it with `claude mcp remove local-agent --scope user` and `codex mcp remove local-agent`. Local tool names change from `mcp__local-agent__*` to `mcp__agent__*`.
- **Behaviour change:** `ailocal clients codex` writes no `AGENTS.md` into the codex root, matching the claude root (#28). Cadence composes that path, so each product's install overwrote the other's. A file byte-identical to one of the nine renderings ailocal ever wrote is retired; any other file, including Cadence's, is left alone. The research routing it carried is the `local-research` skill's, and the template is deleted.
- **Behaviour change.** `ailocal-cyber` is a peer of the other capabilities on every surface (DevelopSolutionsLLC/ailocal#27). Claude Code's `/model` is a generated `modelPicker` with `replaceBuiltInOptions: true` listing every enabled capability once, in `policy.ROLES` order; gateway discovery is no longer enabled for `claude-local`, so Cyber no longer appears twice below the built-in lineup. The model-guide hook resolves the documented `requested_model` and decodes Claude Code's `claude-router-<hex>` spelling as a last resort, so selecting Cyber no longer fails with `MODEL_GUIDANCE_UNKNOWN_MODEL`. One description, `<role> — <model name>, <N>K ctx`, rendered by `policy.capability_description()` from the role and the model guide's new optional `name`, is shared by the Claude picker, the Codex catalog and the LiteLLM config; Codex catalog priority follows the same order.
- Preserve the original live architecture windows in shipped defaults: 104K / 136K / 192K / 256K for 16/32/64/128 GB. Cyber matches each architecture total; output reserves remain role-specific. Aliases sharing the primary backend share its window, while separate fast models and FIM retain their budgets. Native model capacity remains a ceiling, not an automatic expansion of operator policy.
- Local launchers mark the caller so the agent MCP refuses redundant local delegation before starting a child; read-only model guidance and hosted-to-local delegation remain available. Distinguish current MTP depth from historical trial values in client guidance. FIM no longer advertises function/parallel calls, Cyber no longer advertises parallel calls, and effort descriptions do not promise unmeasured graded thinking.
- Register bundled MCP servers in Copilot's portable global configuration as well as VS Code's compatibility Local harness scope, preserving foreign servers, options and stored model-key references. Exact-backend guides declare qualified image input; unknown replacements default to text-only. Six chat backends passed bounded proxy/provider image probes on a 64 GB host; this is not general vision or target-hardware qualification.
- **Behaviour change.** ailocal no longer generates compaction triggers; each client compacts at its own default against the window it is declared. Removed: the profile `[compaction]` section (an installed one is ignored), `CLAUDE_CODE_AUTO_COMPACT_WINDOW` and `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE` (removed from an existing `settings.json` on `ailocal start`), and the Codex catalog's `auto_compact_token_limit` and `effective_context_window_percent`. `CLAUDE_CODE_MAX_CONTEXT_TOKENS` is now the launch default's `context_input`, and each Codex entry's `context_window` its `context_input`, so every client's default trigger lies below LiteLLM admission. To pick up the new geometry after editing a profile by hand, run `ailocal install --reset-config`.

- Record stock Ollama's startup preference before taking over its service, preserve it across reinstalls, and restore it after ailocal's agents stop during teardown. Failed restoration retains its receipt and runtime; older installs without an original-state record report an explicit recovery command.

- Point the existing `ailocal-cyber` capability at pinned unrestricted Gemma 4 26B MLX on 64/128 GB profiles. The profile's native runtime requirement drives installer preparation automatically; no additional client alias is introduced.
- Add an opt-in, source-pinned Ollama 0.40.8 Gemma MLX loader backport with a separately pinned security dependency patch. Native regression tests, payload integrity checks and version-confirmed service activation precede selection; rollback and teardown preserve the vendor app and operator configuration.
- Wait for launchd to finish removing a service before registering its replacement. Remove the legacy load fallback that could return success with no registered job; bound service commands and report readiness failures before restoring the previous configuration.
- Acquire the exact digest-pinned, signed native base for a first build when the vendor app has updated. Verify its version and approved CLI hash in temporary build space, without downgrading the installed app or mixing native ABIs.
- Login model warmup resolves the profile's current context and keep-alive instead of inheriting Ollama's automatic context. Register it after short-lived load checks, keep its command in the native agent, and let the native load request handle resident models.
- Measure cache continuations without chat retemplating or hidden thinking, and salt synthetic prefill prompts once instead of multiplying their token count through repeated identifiers. Geometry and cache probes forward the selected profile's draft depth, including explicit zero, so a measurement does not silently exercise a different MTP setting. Quick geometry checks use short prefixes and skip full output-ceiling generation; they do not claim deep-cache or output-limit validation.
- Add bounded, non-thinking Cyber candidates to the 16/32 GB profiles using pinned Qwen3.5 4B/9B MLX snapshots. Verify downloaded assets and import through Ollama's native CLI into the serving daemon's model store. Model installation failures now stop install instead of being silently skipped.
- Coding/decode measurements exercise the chat protocol; short decode pilots use 128 tokens to avoid spending minutes on throughput-only output.

- Fresh installs, including unattended `--yes`, register public Claude Code, OpenAI and Microsoft Learn documentation MCPs through the native Claude and Codex CLIs, in hosted and local roots. Existing settings survive; registration failures and name collisions return a nonzero status. Copilot documentation provisioning remains deferred.
- `ailocal audit` audits what is running beside what is declared: the daemon's `/api/version` base is audited separately when it is not the declared patch base, a daemon that does not answer is an unverified pin, and each service reports whether its running container serves the declared digest. Every run is recorded at `~/.local/state/ailocal/audit/local.json`, each advisory carries its `modified` time, and the integration contract moves to `schema_version` 2 with an `audit` block naming that record and `ailocal audit`, so Cadence merges these findings into its weekly machine-wide audit (DevelopSolutionsLLC/ailocal#22). A Cadence older than the reader for schema 2 refuses the contract; update Cadence first.
- `ailocal update` reports the patched Ollama base beside the images: its declared base, the running daemon's version, how many newer upstream releases exist (its N-position), the ≈N-2 candidate, and the hold recorded in `deploy/ollama/patch.toml` that explains why 0.40.8 is kept. Nothing is upgraded (DevelopSolutionsLLC/ailocal#22).
- Add `ailocal audit`, an online advisory audit of every pin ailocal declares: both service images (through their publishers' GitHub advisories, plus the LiteLLM PyPI release), the reviewed Ollama patch base and its security dependencies, and the vendored browser libraries (#18). Each pin reports clean, affected, yanked or unverified, with the sources it queried and the time; a failed lookup is unverified, never clean, and a registry 404 is reported as yanked. Exits 1 on affected or yanked, 2 when only unverified. It needs a network, so it is kept out of the regression gate; the gate runs its offline test instead.

### Research without naming the tool

`ailocal clients claude`, `codex` and `vscode` provision the bundled research MCP, and each client's instructions route requests for external information to its `search` tool. VS Code registers it in the user `mcp.json` through `code --add-mcp`; other servers and a foreign `research` entry are preserved, and hosted Copilot models see it too because VS Code has no separate local root.

### One budgeted Brave path

**Behaviour change.** `braveapi` is no longer a SearXNG engine, because its engine discards the quota headers. With `BRAVE_API` set, Brave is used only by the research MCP, at most two requests per call when keyless coverage is thin, within `AILOCAL_BRAVE_BUDGET` (default 1,000 per Brave billing period), counted in a ledger under the state root. Every result carries a `brave.summary` line. Brave publishes no usage or balance API, so account-wide usage is `unknown` until Brave refuses a request for quota or balance; set a usage limit in the Brave dashboard for a server-enforced bound.

No migration step: the existing key keeps working, and keyless search is unchanged.

### Fixes

- `ailocal install --yes` registers the `agent` MCP in `claude-local` and `codex-local` (#31). It was excluded while registration also wrote the hosted roots; since #28 it writes only ailocal's own roots, which `--yes` already provisions.
- Retiring a directory ailocal no longer ships is silent when there was nothing to retire (#31). Every `ailocal clients codex` reported "retired ailocal content in …/prompts (other files kept)" for a directory that did not exist.
- The delegate MCP no longer sets `CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY` for its Claude child (#31). Discovery only fills the `/model` picker; [REAL] 2026-10-26 `claude -p --model ailocal-cyber` through the proxy completed with discovery off, and emitted the same `unrecognized_model` diagnostic with it on.
- Remove the unused `generation._staged_text()` and the unvalidated `orcarouter/Qwen3.8-27B-Uncensored` entry from `tests/measure_agentic.py` (#31).
- The imported Cyber models are tagged `qwen3.5:4b-uncensored-mlx` (16 GB), `qwen3.5:9b-uncensored-mlx` (32 GB) and `gemma4:26b-uncensored-mlx` (64/128 GB), replacing `ailocal-…:mlx-<rev>` and dropping `coder`. A real tag, not a bare name, keeps Ollama from listing them as `:latest`; the Qwen bases are general reasoning distills, not coder fine-tunes (#31). The revision leaves the tag, so `ailocal install` now records which pinned revision and Ollama manifest each import produced and re-imports a tag that no longer matches its profile's pin, instead of trusting it by name. **Migration:** the next `ailocal install` imports the new tag once; remove the old one with `ollama rm`.
- Retiring a Claude Code `env` key (`ENABLE_LSP_TOOL`, and now the compaction variables) is reported on the console instead of as a `//` note in `settings.json`. The note appeared only on the generation that found the key, so the next generation differed and `ailocal check` reported drift right after `ailocal start`.
- The gate refuses to run when the local-agent runtime is absent, naming `ailocal clients claude codex`; its own idempotency suite provisioned it mid-run, so the model-guidance suite failed on a fresh state root and passed on rerun. The registry suite no longer restates one tier's cyber window; it asserts the generated window's provenance.
- Select official Ollama Qwen3.5 4B/9B MLX tags for the small-tier primary and large-tier fast roles, and use MLX for every chat alternative. Generation now honors disabled roles, and the admission check chooses an enabled capability.
- Inline autocomplete is enabled on every tier, on the official GGUF base tags `qwen2.5-coder:1.5b-base` (16/32 GB) and `qwen2.5-coder:3b-base` (64/128 GB) (#31). Ollama's MLX runner has no Qwen2 loader, and the base template is what lets a FIM prompt through LiteLLM, which drops `suffix` (BerriAI/litellm#6900) and delivers the prompt as a chat message: [REAL] on that path the instruct tag scored 0/10 executable infills and the 1.5B base 10/10. A reviewed MLX loader patch and mxfp8 import were built and measured, then dropped: slower, twice the footprint, no fidelity gain. The 3B is under the non-commercial Qwen Research License. Evidence: `docs/maintenance.md#inline-completion`.
- Classify the pinned unrestricted Qwen 4B/9B MLX backends in the conservative local gateway class instead of forwarding them as unknown models. Classification alone did not cure the earlier editing failures; coding evidence is recorded per exact backend.
- Give local Codex models a complete native patch example, and select pinned reasoning-distilled 4B/9B MLX checkpoints for 16/32 GB Cyber after native Codex/Claude edit checks. Keep the existing Ollama/LiteLLM serving path and record the limited scope of those checks; no new runner patch or tool adapter is introduced.
- Enforce request stop strings in native MLX decoding, including markers split across response chunks, and stop generation before invented later turns. Require the validated compatibility bundle for small-tier Cyber profiles, gate builds on its native regression, and reuse only intact owned native files for the same source base when the vendor app has updated.
- Let Codex's selected model catalog entry choose its reasoning default from the profile; remove the global medium-effort override that enabled thinking on non-thinking capabilities.
- Give the 16 GB Cyber profile 24K input headroom: its previous 12K limit rejected native Codex's startup tool prompt. Keep its 4K output ceiling and record bounded client-evaluation limitations in the shared model guides.
- Avoid stopping a healthy Ollama daemon when legacy model-store metadata is already present in the shared store; allow launchd's startup delay during verified activation.
- Prevent concurrent install/start/stop/teardown commands from racing over the same services; failed Docker rollouts report an incomplete install without an internal traceback.
- Verify every selected model through Ollama's native loader during installation, including existing models. A successful import or metadata lookup no longer hides an unsupported weight layout; failed source imports retain verified assets for recovery without selecting a substitute model.
- Preserve externally registered Codex MCP servers and their nested settings across `ailocal start`; the template remains authoritative for ailocal's own research server.
- Add the `cyber` capability on 64/128 GB tiers, serving unrestricted Qwen MLX as `ailocal-cyber`; installation includes the backend.
- Codex model selection now takes its context window from each generated catalog entry. Remove global limits and redundant plan/review/cyber launch presets. Install/start and teardown retire only ailocal-generated legacy presets, preserving authored profiles and symlinks.
- The 64/128 GB profiles name the explicit MLX 4-bit unrestricted Qwen alternative; its publisher's `latest` tag selects GGUF. Model installation accepts only the selected tag (or its omitted `:latest` equivalent), never a similarly named variant.
- Profiles support native `min_p` and `presence_penalty`, including LiteLLM's required allowlist. The 16/32 GB Qwen coding roles use current task-specific vendor presets; measured fast-agent and autocomplete exceptions remain explicit.
- Preserve `xhigh` reasoning requests through LiteLLM's Ollama adapter for both Chat Completions and Codex's Responses protocol instead of silently disabling thinking. Model presence and residency checks distinguish exact variants rather than accepting any tag in the same family.
- Opt-in model measurements read the actual Ollama runner and distinguish model variants. The agentic driver uses the active architecture context and rejects unknown candidates. A separate benign over-refusal measurement records answers for manual review without executing generated text.
- Add `tests/measure_cache.py`, an opt-in diagnostic for KV prefix-cache reuse across turns — the lever that keeps agent prefill off the critical path on Apple Silicon. An active probe runs a cold then a warm turn and reports the warm turn's `prompt_eval_count` collapse with a plain ACTIVE/INACTIVE verdict; `--tail N` passively summarises reuse of the last N real requests from the Ollama log with no inference. Out of the gate, keeps no history, asserts nothing. The client and LiteLLM do not surface cached-token counts, so the Ollama log was the only window onto per-request reuse.
- The research MCP launcher honors `AILOCAL_SEARXNG_PORT` in operator configuration, with process environment taking precedence as it does for Compose.
- Teardown reports Docker removal failures without claiming completion or removing login agents. Install validates after optional client setup and exits nonzero when checks fail.
- `ailocal clients --dry-run` previews targets without changing files or installing integrations.
- Supply-chain checks use Compose ownership, so unrelated projects and isolated test installs cannot affect this installation's verdict. An unconfigured optional Claude artifact integration no longer fails a fresh install.
- `ailocal <command> --help` prints usage for every command; `clients --help` used to configure every client and `start --help` to start the stack.
- `ailocal-code` opens the VS Code profile ailocal configures. `code --profile ailocal` created an empty profile with no connector, provider or research server.
- Saved logs keep step order; redirected stdout is line-buffered.
- `ailocal install --yes` passes its own check on a fresh machine. It now provisions the claude-local and codex-local roots, including their artifact and research runtimes, for detected clients. It still leaves `~/.zshrc`, VS Code and the agent MCP to `ailocal clients`.
- `ailocal check` no longer reports `codex/config.toml` as drifted after a native `codex mcp add`. TOML is now compared as a parsed document, the same way co-owned JSON already is.
- VS Code counts as installed only when its app bundle or `code` CLI is present. A settings directory left behind after removing VS Code no longer counts.

## v0.13.0 — local artifacts, and a smaller `claude-local`

Three things change what a `claude-local` session actually carries, and one corrects a fact this repository had wrong.

### Artifacts work locally

Claude Code does not register its hosted `Artifact` tool when a session authenticates with `ANTHROPIC_API_KEY`, so `claude-local` had no way to publish a diagram, a page or a report. It does now, through a renderer that ships inside the package.

```sh
ailocal clients claude        # provisions the runtime and registers the MCP server
ailocal check | grep artifact # confirms both halves
```

Nothing to clone, no second virtualenv, no separate installer. `mcp` is a subprocess dependency with its own interpreter in the state root, exactly as LiteLLM is a container, so ailocal is still standard-library only. Registration **merges** one key into `.claude.json`; anything else you have registered there survives.

Artifacts render from `127.0.0.1` and the canonical source is written to `.artifacts/` in your project, so it outlives the session. The page cannot reach the network: it runs in a sandboxed iframe with an opaque origin under `connect-src 'none'`, and every asset is vendored.

**`node` is optional.** Without it the `architecture` format is unavailable and every other format works; provisioning says so rather than failing quietly.

### Tool search is on

`ENABLE_TOOL_SEARCH=true` for `claude-local`. [REAL] measured on the wire: **70 tools / 144,893 schema bytes to 24 / 53,044**, and latency median/p90/max **41/729/941 s to 24/40/62 s**. Deferred GitHub discovery succeeded 5/5; grepai, Cadence skills, Cadence agents and multi-turn reminders were unaffected across 16/16 tasks.

Set `AILOCAL_TOOL_SEARCH=false` to opt out for one session.

### Native Workflow is off

Claude Code's built-in `Workflow` tool ships a 21,822-byte schema that cannot be deferred, and [REAL] it was invoked **0 times across 148 real `claude-local` sessions**. Disabling it took the model-visible schema from **53,044 to 31,221 bytes** and the fixed prompt from **21,789 to 16,695 input tokens**, with capability preserved 10/10.

```sh
AILOCAL_NATIVE_WORKFLOWS=1 claude-local   # restores it, for that process only
```

Hosted `claude` is untouched either way. This flag controls Anthropic's built-in tool and has nothing to do with any Cadence workflow capability, which does not exist.

### LiteLLM 1.98.0

Pinned by digest. The `system_transport` hook is **removed**, not disabled: it existed because LiteLLM discarded mid-array `role: "system"` messages, which upstream fixed in BerriAI/litellm#30443, first shipping in a stable release with 1.98.0. That channel is what carries the deferred-tool listing, which is why tool search requires this version.

**Correction.** The registry declared that `/v1/responses` drops `namespace` tools. That was read off the 1.93.0 image and is no longer true — 1.98.0 expands them, so they reach the backend and removing them is a real saving. The gateway was under-counting, and two suites asserted the old upstream behaviour as though it were a property of the gateway. ADR 004 is marked partially superseded and its Codex figure is **withdrawn rather than replaced**, because no replacement was measured.

### Upgrading

```sh
pipx install --force . && ailocal start && ailocal clients claude
```

`ailocal start` pulls the new image; `ailocal clients claude` provisions artifacts and re-writes the wrapper. Existing profiles and configuration are untouched.

**Nothing breaks.** CLI commands, configuration layout and profile behaviour are unchanged. The two behavioural changes are `claude-local` defaults, both per-process and both reversible with an environment variable, documented in [docs/claude-local.md](docs/claude-local.md).

## v0.12.0 — `ailocal install --reset-config`

v0.11.0 changed profile defaults and then told you, in its own migration note, that an edited profile would not receive them. That note described a gap with no tool to close it: the only ways to take a new default were to hand-copy values or delete your file. This adds the third way.

```sh
ailocal install --reset-config
```

Takes the shipped policy defaults for every config file, including ones you have edited. Your versions are copied to a timestamped `~/.config/ailocal-backup-<stamp>/` directory **first**, containing only the files it was about to replace, and the path is printed. A deleted default is written back too — the flag means "give me the shipped tree", and a hole is not part of it.

Without the flag nothing changes: an edited file is still preserved and still never updated, which remains the right default. The installer cannot know whether a divergence was deliberate, so it assumes it was. `--reset-config` is how you say it was not.

## v0.11.0 — sampling, output ceilings and models that actually unload

Three defects that all failed the same way: silently. Nothing here needs user action, but if you have edited your own `~/.config/ailocal/profiles/`, see the migration note at the end.

### Gemma now runs at Google's published sampling

The gemma4 roles shipped `temperature 0.1 / top_k 20 / top_p 0.9`, chosen for run-to-run reproducibility. They now ship Google's documented `temperature 1.0 / top_k 64 / top_p 0.95`.

This was measured, not adopted on faith. A 50-generation A/B on coding tasks scored by *executing* the model's code against hidden assertions:

| | old (t0.1/k20/p0.9) | vendor (t1.0/k64/p0.95) |
|---|---|---|
| tasks passed | 18/25 (72%) | **24/25 (96%)** |
| decode | 82.9 tok/s | 81.0 tok/s |

The mechanism is not "better code". All 7 failures at the old setting were **empty responses**: constrained sampling sent the model into 13,000–23,000 characters of reasoning until it exhausted its output budget and returned nothing. The one failure at vendor sampling was genuinely wrong code. Sampling costs nothing in throughput — draft acceptance was 0.79 against 0.77.

`tests/measure_sampling.py` is that benchmark, kept out of the gate like the other measurement scripts.

### Models unload

`implementation` and `review` declared `keep_alive = -1`. All three gemma roles share **one** resident model, and keep_alive is last-writer-wins on it, so the highest-frequency role re-pinned 16 GiB permanently while `architecture`'s `6h` sat in the profile looking like the policy. Measured: a request carrying `-1` set `expires_at` to the year **2318**.

All three now declare `6h`. Roles sharing a model must declare the same keep_alive or the shortest one is fiction. `fast` (20m) and `completion` (2h) are unchanged.

`OLLAMA_KEEP_ALIVE` also drops from `-1` to `6h`. It is the default for anything loaded *without* an explicit value — the preload agent, a bare `ollama run`, and models other tools load on the same daemon. `-1` there meant none of them ever released memory. A caller that wants a model pinned can still pass `-1` explicitly.

### Output ceilings raised to what the window allows

64 GB: `architecture`, `implementation` and `review` go to **32768** (implementation was 8192). 128 GB: `implementation` to 16384 — its `architecture` and `review` were already at the model's native 262144 ceiling and could not grow without cutting input.

Free on MLX, where KV is allocated lazily: `num_ctx` 196608 was verified to load at 16.15 GiB resident, identical to a small window. `CLAUDE_CODE_MAX_OUTPUT_TOKENS` now derives to 32768, above Claude Code's own 32000 default request, so the truncate-and-retry behaviour v0.10.0 addressed is gone rather than narrowed.

To be clear about what this does *not* fix: with no ceiling at all, gemma4 finishes these tasks in 2,337–5,175 tokens, so 8192 was never truncating normal work. The empty answers came from looping, and the sampling change is what fixes those. This is headroom.

### `OLLAMA_MAX_LOADED_MODELS` 5 → 4

The tier needs three distinct models, not five: three roles share one gemma. The fourth slot is `nomic-embed-text`, which is in no profile but is served by the same daemon. Three would fit only while `completion` stays standby — and `keep_alive -1` stops the idle timer, not a capacity eviction, so admitting a 1.9 GB FIM model could evict the 16 GiB gemma.

### Fixes

- `ailocal check` reported **"Ollama not responding"** against a healthy daemon whenever `OLLAMA_HOST` was set — which `ailocal install` guarantees via `launchctl setenv`. Ollama's variable is documented as bare `host:port`; read as a URL, `127.0.0.1` parses as the scheme and every request raises. New `policy.ollama_url()` normalises it for both readers.
- `ailocal check` now detects a **deleted log destination**. If `~/Library/Logs/ailocal/` is removed while the agent runs, launchd neither recreates it nor reopens the descriptors: the process writes to an unlinked inode, `launchctl print` still says running, and every diagnostic is lost with no symptom. Remediation is a mkdir *and* a kickstart; either alone leaves the process writing to nowhere.
- `OLLAMA_FLASH_ATTENTION` and `OLLAMA_KV_CACHE_TYPE` are documented as reaching the **llama_cpp runner only**. The MLX runner takes neither, so every MLX-served role pays unquantized fp16 KV regardless. Measured 46.2 KB/token at 64K depth, which matches the 45.4 the profiles claim.
- `registry.yaml` engine behaviour re-measured on Ollama 0.32.14 and re-stamped, not version-bumped. MLX: loaded at 24576, processed 57,787 tokens without truncation. llama_cpp: loaded at 20480, processed 10,243 — *half*, because `OLLAMA_NUM_PARALLEL` splits the window into slots.

### New measurement scripts

`tests/measure_agentic.py` compares candidate models for the agentic default — decode, speculation acceptance, cold and warm prefill, resident memory, tool-loop correctness. `tests/measure_sampling.py` A/Bs sampling on executed code. Both are out of the gate, keep no history and assert nothing, like `measure_geometry.py`.

For the record, on this hardware `gemma4:26b-mlx` remains the fastest by a wide margin: 2.9x the decode and 3.8–6x the prefill of `qwen3.8:27b-mlx`. Ollama's MLX build of gemma4 has **no vision**; the GGUF `gemma4:26b` does, and beats qwen3.8 on every axis while keeping it.

### Migration

`ailocal install` preserves a profile you have edited — it compares against the shipped manifest digest and keeps anything that differs. So an untouched profile picks these values up on upgrade; **an edited one does not**. If you have customised `~/.config/ailocal/profiles/`, copy the new `temperature`, `top_k`, `top_p`, `max_output` and `keep_alive` values across by hand, or delete your copy to take the shipped one.

## v0.10.0 — language servers, client alignment, and a runtime that cannot drift silently

`claude-local` now bootstraps a complete, self-sufficient config root, Claude Code's context and output settings match what the backend actually serves, and the generated runtime can no longer disagree with the repository without saying so.

### Language servers in `claude-local`

`ailocal clients claude` enables the official Claude Code LSP plugin for each language whose server binary is already on your machine — Python (`pyright-lsp`), TypeScript/JavaScript (`typescript-lsp`), Go (`gopls-lsp`) and C/C++ (`clangd-lsp`).

ailocal installs no language server and no language ecosystem; a missing server is reported with the command that installs it. Plugins are enabled **only in ailocal's own config root** — hosted `~/.claude` is never modified. Plugin state is per config root, so a fresh root starts empty regardless of what your hosted client has.

### Claude Code now asks for what the backend serves

Claude Code defaults to a 32,000-token output request, while the backend serves the profile's `max_output`. Long answers were truncated and retried rather than ending cleanly. `settings.json` now carries `CLAUDE_CODE_MAX_OUTPUT_TOKENS` and `CLAUDE_CODE_MAX_CONTEXT_TOKENS` derived from the active profile — the same numbers LiteLLM receives.

Those variables are process-wide while the model slots differ, so the generated file also lists the background slots whose smaller limits they cannot express.

### 64 GB tier: more usable context

`architecture` input rises 131,072 → 163,840 (180,224 total), compacting at 139,264. Re-measured cold prefill on the MLX runner justified the increase; the full 262,144 native window was measured and deliberately left unexposed, because a cache miss at that size costs over nine minutes.

### More reliable tool calls on Gemma

Repaired the one `Agent` argument `gemma4` omits, and kept `Skill` and `SendMessage` available — delegation and skill invocation now work instead of failing quietly.

### Configuration ownership

`~/.config/ailocal/.env` held generated secrets and your own provider keys in one file, which is why upgrading it meant offering to destroy your keys. It is now split: generated state is ailocal's, `.env.local` is yours and wins. Existing files migrate automatically, byte-for-byte, without rotating a key.

The persona mechanism is removed. `ENABLE_LSP_TOOL` is no longer written and is retired from existing installs — the LSP tool is built into current Claude Code.

### A runtime that reports its own drift

- `ailocal check` detects a container whose config mount a reinstall detached; previously the proxy answered 200 while serving nothing.
- The gate refuses to run on generated-config drift instead of regenerating over it mid-run.
- The gate refuses to run when the installed package is older than the checkout. `ailocal start` runs the *installed* generator, so a tested change could otherwise be missing from the live runtime with a green suite either side of it. Remediation: `pipx install --force . && ailocal start`.
- Codex keeps its own trust records across regeneration.

### Upgrading

```sh
pipx upgrade ailocal && ailocal start
```

No commands or configuration layout change. Run `ailocal clients claude` to pick up LSP plugins for servers you already have.

## v0.9.1 — context windows sized from measured runner behaviour

Every profile carried context limits inherited from a model backend ailocal no longer runs. This release re-measures the real constraints and re-sizes all four tiers against them. No commands, configuration layout or client files change shape — only the numbers inside the profiles.

### What changed

Context windows and output ceilings rise on every tier:

| Tier | `architecture` input | Auto-compaction (was → now) |
|---|---|---|
| 16 GB | 57,344 → 90,112 | 45,875 → 67,891 |
| 32 GB | 57,344 → 122,880 | 45,875 → 92,262 |
| 64 GB | 81,920 → 131,072 | 49,152 → 111,411 |
| 128 GB | 81,920 → 245,760 | 49,152 → 139,264 |

`implementation` and `review` now get the same input window as `architecture` rather than roughly half of it. Context capacity and role identity are separate concerns; a review that cannot see a whole change is not a cheaper review.

Every tier now compacts at a uniform **85%** of its window, so no tier runs a different safety margin. The window stays per-tier, because it encodes the one thing that genuinely differs: how many tokens that tier's runner can cold-prefill inside its latency budget.

Output ceilings are now set by role rather than per tier, identically everywhere:

| Role | Output | Why |
|---|---|---|
| `architecture` | 16,384 | long design documents |
| `implementation` | 8,192 | large code generation without spending input budget |
| `review` | 16,384 | long review reports |
| `fast` | 4,096 → 8,192 | more than a word, still not a generation tier |
| `completion` (FIM) | 128 → 512 | multi-line suggestions; 128 truncated mid-block |
| `embeddings` | n/a | no generation route |

### Why the old numbers were wrong

They described a llama.cpp/GGUF backend the 64 and 128 GB tiers stopped using when they moved to the MLX runner. The profiles still justified a 49,152 compaction point with "cold prefill ~5 min at ~58K, 13 min at ~88K". Measured cold on 64 GB hardware, `gemma4:26b-mlx` evaluates 137,233 tokens in 229.5s — 88K costs 147s, not 13 minutes.

No model was ever the limit. Every chat model in every tier reports 262,144 native context.

### The 32 GB tier

16 GB and 32 GB previously shared identical geometry and an identical compaction point despite different memory and different models. That was a real defect, but not the obvious one: the four roles on those tiers share one llama.cpp runner, where `num_ctx` is fixed at first load and an over-long prompt is truncated **from the front with HTTP 200 and no error**. They must declare one identical total, so `context_input` and `max_output` offset each other. Raising a single role would have silently clipped the other three. The tier now moves as a unit, and a test enforces the equality.

### Auto-compaction

Ceilings are sized by memory; compaction triggers are sized by latency. These were previously conflated, which is how a tier ended up compacting at less than half the context it could carry.

The trigger is also no longer set by cold-prefill cost alone. An interactive session grows incrementally and hits the prefix cache every turn, so the full cold cost is paid on session *resume*, not per turn — sizing for resume penalised every ordinary turn to protect the rare one.

The full model window remains available for deliberate one-shot work. Compaction is a client-side threshold, not a model limit; ailocal still never summarises conversations itself.

### Deliberately unchanged

- **Embeddings** stay at 2,048. That is `nomic-embed-text`'s real maximum — it clips silently above it and still returns a valid vector. The model's own `num_ctx 8192` default parameter is misleading and should not be followed.
- **Inline completion (FIM)** keeps its 3,968-token input. The model supports 32,768, but there are no recorded requests and no installed consumer, so a larger input window would buy nothing. Its output ceiling does rise to 512, because 128 truncated multi-line completions mid-block.

### Upgrading

Run `ailocal start` after upgrading to regenerate client configuration and restart the proxy.

**If you have edited a file in `~/.config/ailocal/profiles/`, ailocal will not overwrite it** — that is the standing promise about authored policy, and it means a customised profile keeps its old context values. To adopt the new sizing, compare your file against the shipped one and merge, or delete yours and re-run `ailocal install`.

### Notes

The 64 GB tier is measured. The 16, 32 and 128 GB tiers are sized from model limits and from per-token costs measured on 64 GB hardware; no machine of those sizes has run them, and each profile records which of its numbers are measured and which are not. The 128 GB profile is no longer a copy of the 64 GB one.

## v0.9.0 — first public release

ailocal provides a local AI development environment for Apple Silicon Macs, integrating local models with Claude Code, Codex CLI, and VS Code Copilot through a single local gateway.

Everything runs on your own hardware using Ollama and Docker. Supported clients are configured automatically when present, while remaining optional.

### Highlights

**Simple installation**

```sh
brew install --cask docker-desktop ollama-app
brew install pipx
pipx install git+https://github.com/DevelopSolutionsLLC/ailocal.git
ailocal install
ailocal check
```

**Supported clients**

- Claude Code (`claude-local`)
- Codex CLI (`codex-local`)
- VS Code Copilot (`ailocal-*` models)

Clients are optional. ailocal configures only the clients installed on your machine.

**Hardware-aware configuration**

Automatically selects appropriate model profiles for Apple Silicon systems with:

- 16 GB
- 32 GB
- 64 GB
- 128 GB (experimental)

No manual profile editing is required for typical installations.

**Local API**

Provides both an OpenAI-compatible API and an Anthropic-compatible API, allowing local models to work with existing tooling.

**Validation**

`ailocal check` performs an end-to-end validation of prerequisites, runtime, local gateway, models, client configuration, and live inference — with actionable remediation when something is missing.

### What's new

This release includes extensive stabilization work across the project:

- simplified repository structure
- cleaner packaging
- improved installation experience
- automatic client detection
- improved VS Code integration
- improved Claude Code integration
- improved Codex CLI integration
- cleaner configuration ownership
- removal of obsolete generated artifacts
- stronger validation gates
- improved documentation
- comprehensive installation verification

### Known limitations

- Docker Desktop and Ollama must be installed by the user.
- VS Code requires one manual API key paste into its encrypted SecretStorage. This is a limitation of the VS Code extension model rather than ailocal.
- 128 GB hardware profiles remain lightly validated compared to the other configurations.

### Stability

This release establishes the initial public interface for ailocal. Future releases will aim to preserve compatibility for CLI commands, configuration layout, generated client configuration, and profile behaviour. Breaking changes will be documented in release notes.

### Thank you

Thanks to everyone who tested early versions, reported issues, and helped improve the project through repeated installation, packaging, and validation testing.
