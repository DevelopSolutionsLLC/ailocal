# Architecture

How a request moves through the running system, what an install writes, and what is protected. The repository map, root ownership and engineering invariants are in [AGENTS.md](../AGENTS.md); the decisions behind this design are in [decisions.md](decisions.md).

## Overview

Ollama serves the models on the host GPU. LiteLLM fronts them on `127.0.0.1:4000` as an OpenAI- and Anthropic-compatible proxy exposing **capabilities** (`ailocal-primary`, `ailocal-fast`, `ailocal-cyber`, `ailocal-completion`), never model tags. `fast` is the primary with thinking off, declared only where the primary thinks; a client surface naming it on another tier resolves to `primary` through `policy.resolve_capability()` ([018](decisions.md#018-the-chat-roles-are-primary-and-an-optional-fast)). A hardware profile decides which model answers each capability, and every client configuration is generated from that one decision.

```
authored profiles + client policy + templates
                    ↓
              policy.py                one reader, fails closed
                    ↓
             generation                the only generator, one staged transaction:
                    ↓                  stage → validate → back up → replace → roll back on failure
             $AILOCAL_STATE            outside the checkout
                    ↓
      Compose mounts        client deployment
                    ↓                     ↓
      LiteLLM · SearXNG      Claude · Codex · VS Code
```

Inference always goes through LiteLLM; there is no direct-Ollama path for an agent or subagent. LiteLLM maps each alias to its backend and filters tool schemas before the backend sees them ([004](decisions.md#004-tool-gateway-and-task-classification)). Hosted clients never route through it ([008](decisions.md#008-local-and-hosted-models-side-by-side)).

## Environment

```
~/.local/state/ailocal/env    ailocal's, generated    LITELLM_MASTER_KEY, SEARXNG_SECRET
        +
~/.config/ailocal/.env.local  yours                   provider keys, overrides — WINS
```

Every consumer loads them in that order. No key is rotated. `AILOCAL_BIND` in `.env.local` is the proxy's host address (`ailocal bind lan|local|<ipv4>`; `127.0.0.1` when unset).

## What an install writes

Nothing outside this list is created. Shipped `deploy/` and `clients/` trees are read in place from the installed package.

| Path | Writer | Contract |
|---|---|---|
| `~/.config/ailocal/profiles/*.toml` | `install` | The only copied resource. Replaced only while it still matches the digest recorded at install; edits are preserved, deletions stay deleted, unshipped files are retired. |
| `~/.config/ailocal/.env.local` | `install` | Created empty if absent; afterwards only the `AILOCAL_OLLAMA_PATCH` key is written, after a successful activation. Mode 0600. |
| `$AILOCAL_STATE/env` | `install` | Generated secrets, 0600; regenerated only when absent, because the key is embedded in every client config. |
| `$AILOCAL_STATE/install-manifest.json` | `install` | Digests of what was shipped, which makes edit-preserving upgrades possible. |
| `$AILOCAL_STATE/active-profile` | `install` | The selected tier; sole writer `select_tier()`. |
| `$AILOCAL_STATE/litellm/`, `litellm-config.sha` | `generation`, `start` | Proxy config, bind-mounted read-only; restart fingerprint. |
| `$AILOCAL_STATE/searxng/settings.yml` | `start` | Rendered from the shipped template; may carry the GitHub search token; 0600. |
| `$AILOCAL_STATE/model-sources/imports.json` | `install` | Receipts binding each `name:tag` to its pinned source and the manifest Ollama created; a changed pin or manifest means re-import. |
| `$AILOCAL_STATE/local-agent/` | agent MCP | Empty working directory for `text`/`research` delegations and session bindings (no transcripts), 0700. |
| `$AILOCAL_STATE/local-research/` | research MCP | Bounded SQLite caches and the Brave ledger. |
| `~/.config/ailocal/` client files (`$AILOCAL_CLIENTS`) | `start` | Disposable, rewritten every start: `integration-contract.json`, `configure.zsh`, hooks, `claude/` (settings, seeded `.claude.json`, managed `agents/`, `commands/`, `references/`), `codex/` (`config.toml`, `model_catalog.json`, `skills/`). `claude/plugins/` and `claude/backups/` are written by the `claude` binary, not ailocal. Neither root carries `CLAUDE.md`/`AGENTS.md`: Cadence composes those. |
| `~/Library/LaunchAgents/com.ailocal.{ollama,ollama-env,ollama-watch}.plist` | `install` | The server runs the `ollama` on `PATH` (the cask symlink), so an upgrade relinks it; `ollama-watch` restarts the server when the executable is no longer the link target (on change and every five minutes). Logs in `~/Library/Logs/ailocal/`. |
| `/Users/Shared/ollama/models` | `install` | Shared model store, group-writable + setgid; an existing `~/.ollama/models` is moved here before `OLLAMA_MODELS` is repointed. |
| `~/.zshrc` | `clients` | Two marker-commented lines (`# ailocal-configure`, `# ailocal-finalize`) after a timestamped backup (newest 3 kept). |
| `~/.copilot/instructions/ailocal.instructions.md`, `~/.copilot/mcp-config.json`, VS Code `User/mcp.json`, `chatLanguageModels.json`, `settings.json` | `clients vscode`, `start` | The Copilot instruction file; `research` and `agent` MCP entries (absolute launcher path, foreign entries preserved); the native `customendpoint` group `ailocal`, refreshed by `start` only if it exists. The API key is not written: it lives in VS Code SecretStorage. |

Codex's `config.toml` is co-owned: regeneration preserves trust records, `[hooks.state.*]`, `[projects.*]` and foreign MCP tables. ailocal writes no hosted Claude or Codex root; public documentation MCPs and hosted delegation (`local-agent`) are Cadence's. `lsp_baseline()` enables the official LSP plugin for each server already on `PATH`, in the local Claude root only.

## Containers

| Container | Mount | Access |
|---|---|---|
| LiteLLM | `<package>/resources/deploy/litellm → /app/config` | read-only: hooks, registry, config template |
| LiteLLM | `$AILOCAL_STATE/litellm → /app/generated` | read-only: the config it loads (`--config /app/generated/config.yaml`) |
| LiteLLM | `$AILOCAL_STATE/captures → /app/captures` | the only writable path |
| SearXNG | rendered `settings.yml → /etc/searxng/settings.yml` | read-only |
| SearXNG | `<package>/resources/deploy/searxng/limiter.toml` | read-only |

Generation completes before `docker compose up`; nothing regenerates a mounted asset while the stack runs. Callback modules are named by absolute path because LiteLLM resolves them relative to the config file. Profiles, client templates and the repository are not mounted. Ollama runs on the host at `OLLAMA_HOST`.

## Geometry

**Admission is bounded by memory, up to the backend's native window; compaction is the client's latency budget, never a geometry input.**

- The native window caps everything: the 64/128 GB Gemma 4 26B and 16/32 GB Qwen3.5 4B/9B backends report 262144 tokens. No role totals more.
- Primary totals 98304 / 163840 / 196608 / 262144 tokens for 16/32/64/128 GB. The 16/32 GB totals are memory bounds estimated from mapped weights plus KV on the 128 GB host (#93). Cyber uses the same total per tier with its own output reserve.
- Roles sharing a backend share its window and differ only in output reserve: `context_input = total − max_output`. Primary reserves 16384 on 16/32 GB and 32768 on 64/128 GB; `fast` reserves 8192.
- `num_ctx = context_input + max_output`, `num_predict = max_output`, `max_input_tokens = context_input`, all derived by `policy.geometry()`.
- Shipped chat defaults use GGUF MTP, eager context allocation and q8_0 KV. Cyber is a separate model, so residency must hold both backends; Ollama can evict an idle model, and Cyber's shorter `keep_alive` releases it sooner. Physical 16/32/128 GB fit and simultaneous filled-window residency are [UNVERIFIED].
- Cold prefill grows with depth; the declared window is an admission budget, not a latency guarantee.

The engines enforce context differently: the MLX runner per request, llama.cpp at load, truncating an over-long prompt from the front with HTTP 200. LiteLLM's `max_input_tokens` is the only layer that fails loudly. Engine behaviour is recorded in `resources/deploy/litellm/registry.yaml`.

## Model names

Every selection is `<family>:<size>[-unrestricted][-calibrated][-it][-base][-qat][-mtp]`: lowercase family, never a repository URL, never `:latest`, variant tokens in that order and at most once. A quantization token never appears; the `source` hash pins the quantization. A library tag whose name carries one (`qwen3.5:4b-mtp-q4_K_M`) is imported natively under the structured name (`qwen3.5:4b-mtp`) and the pull's own manifest is dropped, so `ollama list` shows one name per selection and `model_guidance` is keyed by the same name. `policy.selection_name_error` owns the rule; `load_profile` fails closed with `PROFILE_SCHEMA_INVALID`; `ailocal install` drops a trailing quantization token from a 0.15 operator profile first (`install.migrate_selection_names`, #101).

## Context and compaction

ailocal owns the numbers, not the conversation: it stores no history, never summarises and never rewrites a client's history. Each client compacts at its own default point below admission:

| Client | ailocal declares | Client default | Source |
|---|---|---|---|
| Claude Code | `CLAUDE_CODE_MAX_CONTEXT_TOKENS = context_input` and `CLAUDE_CODE_MAX_OUTPUT_TOKENS = max_output` of the launch-default role | compacts at window − min(max output, 20000) − 13000 | [REAL] Claude Code 2.1.310 binary |
| Codex | `context_window = max_context_window = context_input` per catalog entry | 90% of `context_window` when `auto_compact_token_limit` is unset | [REAL] Codex 0.163.0 source |
| VS Code Copilot Chat | `maxInputTokens = context_input`, `maxOutputTokens = max_output` | summarises at a fraction of `maxInputTokens` | [REAL] Copilot Chat 0.71.0 bundle |

Claude Code gets `context_input`, not the total, because it reserves at most 20000 output tokens: the total on a 262144-token tier with 32768 output would put its trigger 232 tokens under admission. Its variables are process-wide, so `settings.json` lists the smaller background slots they cannot describe. `PreCompact`/`PostCompact` hooks only log to `$AILOCAL_STATE/compaction.jsonl`. A `[compaction]` profile section is retired and ignored. Revalidate Codex's rule on each Codex upgrade.

Claude Code's `/model` is the generated `modelPicker` with `replaceBuiltInOptions: true`: each enabled capability once, in `policy.ROLES` order. `policy.capability_description()` renders `<role> — <model name>, <N>K ctx` for the picker, the Codex catalog, LiteLLM and VS Code.

## Delegation

The bundled `agent` MCP (`ailocal mcp agent`) lets a hosted Claude or Codex session run one bounded task on `claude-local` or `codex-local`. Contract ([015](decisions.md#015-delegation-binds-session-authority-and-takes-explicit-images)):

- **Modes.** `text` (default: no tools, no MCP), `research` (Claude only: `Read`/`Grep`/`Glob` inside `cwd` plus `search`), `edit` (sandboxed to `cwd`), `full` (no sandbox, parent environment). The ceiling is `delegate_max_mode` under `[claude]`/`[codex]` in `clients.toml`, default `edit`, read per call; a call above it fails with `MODE_NOT_PERMITTED`, with no fallback. Codex refuses `research`.
- **Working directory.** `text`, and `research` without `cwd`, run in `$AILOCAL_STATE/local-agent/empty`. `edit`/`full` need an existing absolute `cwd`. Below `full`, Claude runs with `--setting-sources user`, so project settings, hooks, `.mcp.json` and `CLAUDE.md` do not load.
- **Confinement.** Hosted-session variables, cloud keys and `SSH_AUTH_SOCK` are dropped; the proxy key never reaches the child; key-bearing env files are denied below `full`. A child cannot delegate, and `delegate` from a local launcher is refused.
- **Model.** `model` must be an enabled chat alias; the child gets that alias's limits.
- **Resume.** A `session_id` is bound to worker, mode, model, directory and execution policy; conflicts, changed policy, unknown sessions and concurrent resumes are refused.
- **Images.** `image_paths`: up to four PNG/JPEG inside `cwd` (5 MiB, 8192 px per side, 16 Mpx), when the backend's guide declares images.
- **Outcomes.** Each result carries a `capabilities` block built from the table that builds the argv. Failed, cancelled, timed-out and empty runs are errors; `DELEGATE_EMPTY` carries a `resume` ID. ailocal never retries. Timeout, cancellation, disconnect and SIGINT/SIGTERM kill the child's process group. A hosted Codex parent needs `tool_timeout_sec` ≥ 3660 on its `local-agent` entry. Delegated Claude reports `permission_denials` ([017](decisions.md#017-local-client-roots-publish-an-instruction-budget-delegated-denials-are-reported)).

## Local research

`resources/integrations/local-research/` runs the official MCP SDK in its own state-root venv ([011](decisions.md#011-the-artifact-capability-ships-inside-ailocal), [014](decisions.md#014-local-research-owns-its-evidence-boundary-and-the-only-brave-path)) and imports no ailocal code. It is registered in claude-local (via `claude mcp add-json` in the local root), codex-local and VS Code's user `mcp.json`. [REAL] Codex 0.163.0 completed `research.search` through the local proxy on 2026-10-18.

- **Search** plans bounded SearXNG requests per preset, filters by engine attribution and DNS site scope, merges duplicates, and reports coverage, attempts, fallbacks and provider failures. Brave is called only from here, behind `brave-ledger.sqlite3`.
- **Fetching** checks every resolved address and redirect, pins connections to validated public addresses, verifies TLS hostnames, refuses URL credentials and ignores ambient proxies; HTML/plain text only.
- **Limits.** 10 results, 4 pages, 1 MiB per response, 8,000 characters per page, 24,000 evidence characters, 800 output tokens, 120 s per call.
- **Cache.** Searches 15 minutes, pages 30 minutes, 64 MiB cap; credentials, conversations and answers are never stored.
- **Synthesis** (`answer`) uses `ailocal-fast` with no tools, history or hosted fallback, and is off in the default claude-local registration.

`tests/research.py` covers contracts with staged backends; `tests/measure_research.py` is an opt-in live measurement.

## Mermaid validation

`publish()` parses every Mermaid diagram with the real grammar (`mermaid_validate.py`: headless Chrome over the vendored `mermaid.min.js`, ~1.7 s per diagram, a throwaway `--user-data-dir`). VALID publishes; INVALID is refused with the parser message returned to the model; UNAVAILABLE (no browser, launch failure, timeout) is also refused, because reporting an unchecked diagram as published is the original defect. `html`, `architecture` and fence-free Markdown never start a validator. `test_mermaid_validate.py` (core) asserts the state machine through the `runner` seam; `test_mermaid_grammar.py` (full) is the grammar corpus.

## Extension points

| Add | Do |
|---|---|
| a model | edit the role's `active` in `profiles/<tier>.toml`, then `ailocal install` |
| a profile | create `profiles/<tier>.toml` and add it to `policy.TIERS`; thresholds are in `install.py` |
| a capability | add the role to the profiles and `policy.ROLES`, map client surfaces in `clients.toml` |
| a client template | author under `resources/clients/<client>/`, render from the generator, deploy from `clients.py`; never write output beside the template |
| a bundled integration | `resources/integrations/<name>/` with provenance and licences, provisioned from `clients.py` into the state root, checked in `src/ailocal/checks/`; it runs with its own interpreter, so `dependencies = []` holds |

## Security

The threat model is a single developer workstation: it protects against accidental exposure and disclosure, not against a local attacker who already has the user's account.

### Trust boundaries

| Boundary | Protection |
|---|---|
| Host ↔ network | Every port binds `127.0.0.1`. Only `ailocal bind lan` (or an address) opens the proxy, the one service that authenticates; `ailocal check` warns while it is open. Ollama and SearXNG never leave loopback. |
| Client ↔ proxy | One shared key, `LITELLM_MASTER_KEY`; unauthenticated requests are rejected. |
| Proxy ↔ SearXNG | Internal Compose network only. |
| Proxy ↔ Ollama | Host loopback; Ollama has no authentication and must not be exposed. |
| Repository ↔ runtime | Generated state lives outside the checkout, so a secret cannot be committed. |
| Artifact page ↔ network | Sandboxed iframe under `connect-src 'none'`; the preview server has no write endpoint. |

### Secrets and permissions

| Secret or path | Lives in | Protection |
|---|---|---|
| `LITELLM_MASTER_KEY`, `SEARXNG_SECRET` | `$AILOCAL_STATE/env` | generated, 0600 |
| `BRAVE_API` | `.env.local` (optional) | passed only to the research process, which removes it from its environment and sends it only to Brave; never rendered, logged or stored in the ledger |
| `GITHUB_SEARCH_TOKEN` | `.env.local` (optional) | fine-grained, read-only, public-repository PAT with an expiry; never the `gh` token |
| rendered SearXNG settings | `$AILOCAL_STATE/searxng/settings.yml` | 0600; the tracked template carries only the `__GITHUB_SEARCH_TOKEN__` placeholder, because SearXNG supports no `${VAR}` substitution |
| `$AILOCAL_STATE`, `active-profile` | state root | 0700, 0600 |
| `/tmp/ailocal-scratchpad-<uid>/` | scratchpad hook | 0700; a symlinked or foreign-owned base is refused for a fresh `mktemp` directory |

No secret appears in the generation manifest, generated artifacts, logs or captured evidence, which redacts key-shaped content. `ailocal check` reports a secret file readable by other users. A tracked file is authored source or a template, never a runtime artifact, so generation cannot dirty the tree.

### Supply chain

- **Images** are pinned by digest in the [LiteLLM](../src/ailocal/resources/deploy/litellm/compose.yaml) and [SearXNG](../src/ailocal/resources/deploy/searxng/compose.yaml) Compose files (the only place the digests live). `ailocal check` verifies the running image is the declared one, loopback binding, and the publisher signature where one exists (needs `cosign`). Upgrading an image means a new digest, a rescan, the full gate and a served-alias geometry check; a tag bump without a digest change is not an upgrade.
- **`ailocal audit`** checks every pin, and the Ollama base actually running, against live advisories. It needs a network, so it is outside `ailocal check` and the gate. Images map to their publisher's GitHub repository explicitly; an unmapped image is unverified. Each run is recorded under the state root and published through the integration contract for `cadence audit --local`.
- **Go reachability.** Run govulncheck in source mode on the pinned tree (`GOENV=off go run golang.org/x/vuln/cmd/govulncheck@<version> ./...`). Binary mode over-reports on Ollama's stripped binary: on 2026-11-05 it listed twelve advisories against stock 0.40.14, while source mode found GO-2026-6860, GO-2026-6866 (`x/net/html`) and GO-2026-5932 (`x/crypto/openpgp`) required but not imported (#83). `ailocal audit` matches by module version and still reports GO-2026-5932; there is no disposition mechanism, so record the source-mode result in the issue and do not raise a pin to quiet an unreachable finding.
- **Patched Ollama builds** drop every Go checksum-database exemption (`GONOSUMDB`, `GOPRIVATE`, `GOINSECURE`, `GOFLAGS`, …) and set `GOENV=off`; every Mach-O file copied into the payload must pass `codesign --verify --strict` against Ollama's team `3MU9H2V9Y9`.
- **Auditing a component** (every tool ailocal installs, bundles or relies on, transitive dependencies included): inventory actual versions and digests; check advisories and fixed ranges; scan inventories and images against a current database; review provenance, permissions and network exposure; record date, commands, database freshness and gaps in the issue; resolve or disposition every finding before promotion. An unavailable scan stays unverified; a clean scan is not proof of absence.

### Version policy

Every path that chooses an upstream version (the Ollama base, the LiteLLM image, the Go toolchain) applies one rule, implemented once in [`version_policy.py`](../src/ailocal/version_policy.py):

- Adopt the newest stable release whose publisher record (GitHub `published_at`) is at least seven full days old. "N releases behind" is not the rule: on 2026-11-02 it proposed go1.27.1 over go1.27.3, with 14 standard-library advisories open.
- A younger release qualifies only when it fixes an advisory affecting the declared version; if advisories cannot be read, none qualifies.
- An unsupported line is never adopted or kept. For Go, supported means the newest point release of one of the two lines go.dev lists; the build refuses any other toolchain and refuses to guess when go.dev is unreachable.

The age is the defence against a compromised release, usually withdrawn within days (LiteLLM 1.107.0, 2026-10-22). `ailocal update` reports the candidate per component and flags a declared version newer than it. SearXNG publishes no versioned releases and stays on a reviewed digest.

### Model artifacts

The [source importer](../src/ailocal/model_sources.py) uses native Ollama pull/create; it never executes downloaded repository Python, install scripts or pickle checkpoints. Native recipes bind the target weight SHA-256 and any declared draft and projector SHA-256, require the built-in renderer/parser, and record the resulting manifest digest; a declared projector is pulled as its own tag, checked, and attached by digest, because a publisher's pull can attach a different one (#87). Verification reads only Ollama's leading asset header and requires the first `FROM` to be the target (fixed 2026-10-27: the former verifier searched the whole Modelfile, so a publisher-controlled template could impersonate a reviewed asset; [APPROX] reproduced with a simulated daemon response, no malicious model demonstrated).

Remaining limits: library tags are mutable, and recipes pin the main weights and declared heads, not every metadata layer, so an upstream change to an unpinned layer is detected only after import by the receipt. Unrestricted cyber weights are third-party derivatives (`localslm` on 16/32 GB); publisher model cards and refusal counts are claims, not certification. Hashes establish byte identity, not absence of backdoors; GGUF still enters native parsers, which were not fuzzed. A successful empty-message load is a compatibility check, not a malware scan.

### Reporting

Report a vulnerability privately to the maintainer, not in a public issue. Commit attribution is a convention stated in [AGENTS.md](../AGENTS.md#change-discipline); the product installs no Git hook.
