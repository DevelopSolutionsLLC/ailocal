# AGENTS.md — ailocal

## Purpose

Run AI coding clients (Claude Code, Codex CLI, VS Code Copilot Chat) against
**local** models on Apple Silicon — no cloud, no changes to the tools themselves.

Ollama runs models natively (Metal/MLX). LiteLLM (one Docker container,
`127.0.0.1:4000`) fronts it as an OpenAI + Anthropic-compatible proxy.

## Responsibilities

LiteLLM · Ollama · Docker runtime · model installation and routing · the model
registry · runtime health · isolated local client profiles · client detection ·
launchers · the minimal verified Python LSP baseline for `claude-local`.

## Not our responsibility

Repository intelligence, grepai, Qdrant, MCP registration, agents, skills, and
client instruction files — **those are Cadence's**. ailocal must remain fully
usable with Cadence absent.

Ownership test: *does this exist to make an ailocal-created local profile usable
on its own?* Yes → ailocal. Otherwise → Cadence.

## Architecture

Each capability (`architecture`, `implementation`, `review`, `fast`,
`completion`, `embeddings`) is served as **one** canonical `model_list` entry
named `ailocal-<capability>` — never a raw model tag. The `claude-*` / `gpt-*`
compatibility IDs that clients hard-code are aliased onto those groups via a
`model_group_alias` block generated from `config/clients.yaml`.

Two source files drive everything:

| Source | Answers |
|---|---|
| `config/profiles/<tier>.yaml` | what each capability *is* — backend, context, sampling, `keep_alive` |
| `config/clients.yaml` | which capability each client surface uses |

`config/active-profile` names the live tier, written by `install.sh` from detected RAM.

## Committing

`git config core.hooksPath .githooks` is set repository-locally by
`scripts/install.sh`, enabling `.githooks/commit-msg`. It **rejects** assistant
attribution trailers (`Co-Authored-By:` naming Claude/ChatGPT/Copilot/an
assistant), `Claude-Session:` lines, `claude.ai/code/session` URLs, and
`noreply@anthropic.com`.

It exists because commits carrying those were published to a public repository,
and removing them required rewriting 86 SHAs and force-pushing the default
branch. Session URLs are permanent public metadata and cannot be recalled.

Product references are unaffected and must keep working: `claude-local`,
`Claude Code`, `Anthropic API`, `OpenAI-compatible`. The hook matches trailer
SHAPE and session identifiers, not the words themselves —
`scripts/tests/commit-msg-hook.sh` asserts both directions in a temporary repo.

Nothing in the ailocal runtime depends on the hook. If you have set your own
`core.hooksPath`, install.sh reports it and leaves it alone rather than
clobbering it — the guard is then inactive until you point it at `.githooks`.

## Golden rule

**Use capability names only** in client configs and scripts — never backend tags
(`gemma4:26b-mlx`). The router owns context, sampling, and per-role lifecycle.
Agents request a capability; they never name a model. The example here is
deliberately the CURRENT active model: an illustration written with a retired
tag reads like a stale claim about what is deployed.

**`completion` is FIM/autocomplete only** — a small dedicated model (tier-
specific: check `config/profiles/<tier>.yaml`) at `num_ctx` 4096. Never map a
conversational alias to it and never use it as a fallback target; a real agent
turn routed there hard-400s. Context-window fallbacks must move **up**.
`sync-models.py` hard-fails if any Claude slot resolves to `completion`, so this
is enforced at generation time rather than by this paragraph.

**One capability per Claude slot** — Opus→architecture, Sonnet→implementation,
Haiku→fast, Fable→review.

## Common commands

```sh
./scripts/install.sh [--profile <tier>]   # bootstrap: detect RAM, install models
./scripts/test-all.sh [--full]            # the regression gate (~22s)

ailocal clients | vscode | models-install  # deploy
ailocal start | stop | update | sync       # lifecycle
ailocal status | models | doctor | validate | smoke
ailocal audit | cleanup | teardown         # installation
ailocal trace | metrics | e2e <client>     # diagnostics
ailocal resolve <capability>
```

Before every commit: `./scripts/test-all.sh`, then `ailocal sync`
must produce **zero diff** on a second run.

## Important paths

| Path | What |
|---|---|
| `config/profiles/<tier>.yaml` | **canonical** — capability → backend, context, sampling |
| `config/clients.yaml` | **canonical** — client surface → capability |
| `config/litellm/config.yaml` | generated block + hand-kept fallbacks |
| `config/instructions/` | `_core.md` + per-capability personas, injected server-side |
| `config/clients/` | per-client deployed config, wrappers, `env.sh` |
| `scripts/lib/` | shared helpers |
| `deploy/litellm/` | Compose definition; image pinned by digest |

## Installed layout (XDG)

| Root | Contents |
|---|---|
| `~/.config/ailocal` | client configs, `env`, `repo` (checkout location) |
| `~/.local/bin/ailocal` | generated launcher shim |

Clients are XDG-isolated: everything lands in `~/.config/ailocal/`, so `~/.claude`
and `~/.codex` are never touched and cloud/local sessions coexist.
`CLAUDE_CONFIG_DIR` relocates `.claude.json` itself, so MCP registrations,
history and credentials are genuinely per-root.

**The launcher is a shim, not a symlink.** This repo *is* its runtime — LiteLLM
bind-mounts `config/litellm` out of it — so it cannot live under
`~/.local/share`. The shim reads the checkout location from
`~/.config/ailocal/repo`, making a moved checkout a one-line config fix instead
of a broken command.

## Generated artifacts

`sync-models.py` regenerates, between `BEGIN/END GENERATED` markers or as whole
managed files: the `model_list` and `model_group_alias` in
`config/litellm/config.yaml`, `config/capabilities.generated.json`,
`config/clients/model_catalog.json`, and the `claude/settings.json` ·
`codex/config.toml` · `continue/config.json` · copilot tables.

Never hand-edit a generated region. Edit the two sources, run
`ailocal sync`, then `ailocal clients` to deploy.

## Instruction files

Repository standard is **AGENTS.md**; there is no repository `CLAUDE.md`. Client
config roots keep their tool's filename (`CLAUDE.md` for Claude, `AGENTS.md` for
Codex) and hold **one** instruction file each. Cadence owns their content;
`install-clients.sh` writes none.

## Runtime

**LiteLLM reads its config only at boot, and that config is bind-mounted.** So
editing `config.yaml`, a hook or a persona changes nothing in the running proxy,
and `docker compose up -d` will not restart it — the spec did not change. It
keeps serving the OLD routing with nothing in the logs to say so. `start.sh`
fingerprints those files and restarts on change.

Never invoke `deploy/litellm/docker-compose.yml` directly — it references a
service defined elsewhere. Use `ailocal start`.

**Liveness and readiness prove the proxy is running, not that Ollama is
reachable.** Only `/health` dials the backend, and it must be checked from
*inside* the container.

## Related repositories

**Cadence** — repository intelligence and client enhancement, optional here. Two
seams: ailocal owns the Ollama daemon machine-wide (Cadence's embeddings depend
on it), and Cadence appends a marker block into ailocal's deployed agents that
`install-clients.sh` strips. Install order: **cadence → ailocal → cadence-local**.

## Common workflows

| Task | Do this |
|---|---|
| Repoint a capability | edit `config/profiles/<tier>.yaml`, `ailocal sync`, `ailocal start` |
| Change a client's model | edit `config/clients.yaml`, sync, `ailocal clients` |
| Edit a persona | edit `config/instructions/`, then **restart the proxy** |
| Add a decision | `docs/adr/NNN-name.md`, update `docs/architecture.md` |

## Depth

- `docs/architecture.md` — **start here for cross-system questions**
- `docs/architecture.md` — the five non-obvious mechanisms; read before changing them
- `docs/architecture.md` · `docs/architecture.md` — the tool gateway
- `docs/architecture.md` — decisions
- `cadence/docs/standards/developsolutions-repository-standard.md` — shared conventions

## Conventions

Shell: `set -euo pipefail`; reuse the `info/warn/step/backup` helpers. Python:
stdlib only. Files and directories are kebab-case. Never commit `.env` or
secrets; ports bind `127.0.0.1` only. No Claude commit attribution. Never push
without approval.

## Configuration ownership

`config/profiles/{16,32,64,128}gb.yaml` are the **authoritative** deployment
configuration. Nothing else defines role-to-model assignment or generation
parameters.

`config/active-profile` selects a tier and has **no implicit default**. A
missing, empty or unknown marker is an error. It used to fall through to a
hardcoded `64gb` in eight places across four shell scripts, which silently
installs the 64 GB model set on a smaller machine — the same failure shape the
planner benchmark was built around.

`scripts/lib/profile_config.py` is the **only** profile parser and resolver, and
it parses YAML **only at generation time**, called only by `sync-models.py`.
Runtime consumers read `config/effective-profile.json` with the standard-library
`json` module. Do not add a second parser and do not add a runtime YAML
fallback — a fallback hides a stale or failed generation behind a value that
looks fine.

The constrained parser supports exactly the constructs the four profiles use and
rejects anything else. It is **not** a YAML implementation and is not claimed to
be better than PyYAML. It exists because core ailocal has no managed Python
dependency environment today (no `requirements.txt`, `pyproject.toml`, or venv —
the only venv is the lazily-created benchmark one for lm-eval/RULER), and
introducing one solely to read four small files at generation time was judged
disproportionate. **If core ailocal ever gains other Python dependencies, adopt a
managed venv and PyYAML and delete this parser.**

Shell scripts **do not parse YAML**. They query `scripts/profile-config`
(`active-tier`, `role <r> [--field f]`, `profile-summary`, `validate`), which
prints bare scalars and exits non-zero on any invalid state.

Generated files — `config/litellm/config.yaml`, `config/clients/configure.zsh`,
`config/capabilities.generated.json`, `config/integration-contract.json` — are
**outputs, not sources**. Edit the profile and re-run `sync-models`.

Benchmark consumers use the same resolver; `benchmark.parse_profile()` is a thin
compatibility wrapper. Model capability metadata and profile schema expansion
(provider, seed, min_p, tool/thinking support, quirks) are separate follow-up
work and are deliberately not in the schema yet.

## Before writing a tool, check what is already here

A previous session hit "PyYAML is not importable from `python3`" and wrote a
249-line YAML parser plus two CLIs. That was the wrong move, and the cost was
~500 lines to remove four parsers. What it skipped:

- **`jq` is already a hard dependency** (`install.sh` preflight). Shells parse
  JSON with `jq`. A bespoke JSON extractor was written anyway and then deleted.
- **`/usr/bin/ruby` ships with macOS and has YAML.** A dependency-free
  YAML→JSON conversion existed the whole time.
- **A venv pattern already exists** (`scripts/benchmarks/models setup`).
- **The real question was never asked**: *which callers need YAML at all?* The
  answer was one — `sync-models.py`, at generation time.

So, before adding code:

1. `command -v <tool>` and check `install.sh`'s dependency list — the thing may
   already be installed and required.
2. **Search the internet.** Version-specific behaviour, available flags, and
   whether a maintained library already solves it are all one search away.
   Guessing from first principles and then building around the guess is how a
   200-line workaround gets written for a solved problem.
3. Prefer: existing dependency → standard library → maintained library in a
   managed environment → new code. In that order.
4. If a constraint seems to force a large implementation, **state the constraint
   and verify it** before building on it. "X is unavailable *here*" rarely means
   "X is unavailable".

Generated artifacts are outputs. Parse once, at generation time, and let
everything downstream read JSON.

## Schema and geometry: current measured state

This replaced a "Locked requirements for the next schema migration" section
whose ten items were carried forward as future work. All of them are now
either implemented or decided against, and leaving them as requirements made
future agents plan migrations that had already happened.

**Implemented.** The schema distinguishes `context_input`, `max_output`,
`total_context` (derived as their sum) and `max_input_tokens`. `context` is
retired and rejected: it meant the TOTAL window in production and the INPUT
budget in the benchmark, and that ambiguity was the root of the admission
defect. Every consumer calls `profile_config.geometry()`; nothing recomputes it.

**64 GB output policy is 16K / 8K / 4K by role** — architecture, implementation,
review — with fast at 4K and completion at 128. Role-specific by design; do not
unify them.

**32K output is a supported setting, not a recommendation.** It is accepted by
the backend but has never been validated as *sustained* output; the largest
measured sustained generation is 8,399 tokens on architecture. Requirement 3
("support a true 32K max output") was therefore decided against rather than
implemented. Do not set a role to 32,768 output on the strength of acceptance.

**Runner context enforcement is characterised** — this was the open question
blocking same-model geometry unification, and it is closed:

- **MLX: `dynamic_per_request`.** One resident runner serves different declared
  `num_ctx` values; a 40K request succeeded while `/api/ps` still reported
  24,576, with no truncation and provider token counts confirming the full
  prompt. The earlier "one gemma4 observation contradicted this" note described
  this behaviour before it was understood.
- **llama.cpp: `fixed_runner_window`, front-truncating** (`keep=4`).

Both are recorded in `config/litellm/registry.yaml` under `runtime_engines`
with `revalidate_on_runtime_upgrade: true`. **Do not re-derive this** unless the
Ollama or MLX runtime version changes. The related "duplicate runner keyed by
(model, num_ctx)" theory was measured and **retracted**; different role
geometries on one model do not imply duplicate resident copies.

**Compaction is profile-driven and shares one owner.** Requirement 7 said Claude
and Codex "today do not" derive from the same policy. They now do: both read the
profile's `compaction` block. Claude receives `window x pct`; Codex receives an
absolute limit derived from the same policy and **capped by `context_input`** --
not `total_context`, which produced a trigger above what the backend admits and
would have 400'd before compaction could fire.

**Still true, keep enforcing:** tuned parameters (temperature, top_p, top_k,
repeat_penalty, reasoning, provider, keep-alive, persona, quirks) stay explicit
profile data; no model-specific behaviour is scattered through benchmark,
LiteLLM, Claude or Codex generation code; benchmarks consume production
effective configuration plus explicit overlays.

**Genuinely open:**

- **128 GB is `PENDING_HARDWARE_VALIDATION`.** It mirrors the measured 64 GB
  values and is not a 128 GB design. The 131,072 / 16,384 candidate loads but
  has never been *filled*; a 64 GB machine cannot measure it representatively.
- **16 GB and 32 GB are structurally validated only** — never measured on that
  hardware.
- **Compaction's runtime trigger is unproven at the threshold.** The settings
  are in place and a live session has been observed compacting and preserving
  state, but no session in this repository has been driven to the configured
  trigger to confirm the exact rule.
