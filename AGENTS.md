# AGENTS.md — ailocal

What an agent needs before changing this repository. Design lives in
`docs/architecture.md`, symptoms in `docs/troubleshooting.md`, secrets and
supply chain in `docs/security.md`.

## Purpose

Run AI coding clients (Claude Code, Codex CLI, VS Code Copilot Chat) against
**local** models on Apple Silicon — no cloud, no changes to the tools themselves.
Ollama runs models natively (Metal/MLX); LiteLLM (one container,
`127.0.0.1:4000`) fronts it as an OpenAI + Anthropic-compatible proxy.

## Ownership boundary

**ailocal owns:** LiteLLM · Ollama · Docker runtime · model installation and
routing · tier profiles · context/output geometry · role aliases · search
(Brave/SearXNG) · isolated client profiles and launchers · base tool filtering ·
the Python LSP baseline for `claude-local`.

**Cadence owns:** repository intelligence · grepai · GitHub MCP · MCP
registration · agents · skills · client instruction *content*.

Ownership test: *does this exist to make an ailocal-created local profile usable
on its own?* Yes → ailocal. Otherwise → Cadence. **ailocal must stay fully usable
with Cadence absent** — never make it a dependency.

## Canonical configuration

| Path | Owns |
|---|---|
| `config/profiles/<tier>.yaml` | **canonical** — capability → model, context_input, max_output, sampling, reasoning, keep-alive, persona, compaction |
| `config/clients.yaml` | **canonical** — which capability each client surface uses. No model tuning. |
| `config/litellm/registry.yaml` | intrinsic runtime capability (engine, context enforcement, tool support) |
| `config/active-profile` | the selected tier, and nothing else |
| `config/effective-profile.json` | **generated** — the resolved artifact every consumer reads |

`scripts/lib/profile_config.py` is the **one** parser and resolver. It fails
closed: no default tier, unknown role fields rejected, duplicate keys and
sections rejected. Shell entry points ask it — they never parse YAML.

## Generated artifacts

`sync-models.py` regenerates, between `BEGIN/END GENERATED` markers or as whole
managed files: the `model_list` and `model_group_alias` in
`config/litellm/config.yaml`, `capabilities.generated.json`,
`clients/model_catalog.json`, `integration-contract.json`, and the
`claude/settings.json` · `codex/config.toml` · `continue/config.json` · copilot
tables.

**Never hand-edit a generated region.** Edit the source, run `ailocal sync`, then
`ailocal clients` to deploy. Generation is per-file atomic **with rollback on
partial failure**; `effective-profile.json` is replaced last as the commit marker.

## Architecture in five lines

Client → LiteLLM (`:4000`) → capability alias (`ailocal-<role>`) → Ollama → model.
Every capability is served as one canonical `model_list` entry named
`ailocal-<capability>`, never a raw model tag. Aliases carry geometry from the
profile: `num_ctx = context_input + max_output`, `num_predict = max_output`,
`max_input_tokens = context_input`. The tool gateway filters each request's tool
schemas before the backend sees them; personas are injected server-side; search
goes LiteLLM → SearXNG.

## Installed layout

Clients are XDG-isolated under `~/.config/ailocal/`, so `~/.claude` and
`~/.codex` are never touched and cloud/local sessions coexist.
`CLAUDE_CONFIG_DIR` relocates `.claude.json` itself, so MCP registrations,
history and credentials are genuinely per-root.

`~/.local/bin/ailocal` is a **shim, not a symlink** — this repo *is* its runtime
(LiteLLM bind-mounts `config/litellm` out of it), so it reads the checkout
location from `~/.config/ailocal/repo`.

## Commands

```
ailocal doctor | status | validate | smoke      inspect
ailocal sync | clients | vscode                 regenerate and deploy
ailocal start | stop | update                   lifecycle
./scripts/test-all.sh                           the gate (23 checks)
python3 scripts/tests/<suite>.py                one suite
```

`scripts/` root holds entry points only. Implementation lives in
`scripts/lib/`, `scripts/tests/`, `scripts/diagnostics/`, `scripts/benchmarks/`.

## Committing

Run the focused tests, confirm they pass, **then** commit. Run the full gate
twice if a timing-sensitive suite is involved — a transient first failure is
investigated, not waved through.

`.githooks/commit-msg` (enabled by `install.sh` via repo-local
`core.hooksPath`) rejects assistant attribution trailers, `Claude-Session:`
lines, session URLs and vendor noreply co-author addresses. Product references
(`claude-local`, `Claude Code`, `Anthropic API`) are fine — it matches trailer
shape, not words. Commits carry one identity: the configured human author.

Never commit `.env` or secrets. Ports bind `127.0.0.1`. Never push without
approval. Keep commits scoped to one responsibility.

## Conventions

Shell: `set -euo pipefail`; reuse the `info/warn/step/backup` helpers. Python:
stdlib only. Files and directories kebab-case.

## Do not

- Add a second YAML parser, or parse profile fields in a shell script.
- Hand-edit generated files, or edit a client config instead of its source.
- Put model-name conditionals in generation or client code — that belongs in a
  profile or the registry.
- Re-enable MCP for Codex: it cannot dispatch namespaced tools, so an empty
  `[mcp_servers.*]` section is correct.
- Run a global `cadence mcp sync` from ailocal, or own Cadence's registrations.
- Rewrite LiteLLM behaviour locally to work around an upstream bug.
- Change the LiteLLM pin, model roles, or profile geometry as a side effect of
  another task.

## Current state, briefly

**Geometry is explicit.** `context_input` + `max_output`; `total_context` is
derived. `context` is retired and rejected. The 64 GB output policy is 16K/8K/4K
by role. 32K output is a supported setting but was **decided against** as a
target — the largest measured sustained generation is 8,399 tokens.

**Runner context enforcement is settled** and recorded in `registry.yaml` with
`revalidate_on_runtime_upgrade`: MLX is `dynamic_per_request`; llama.cpp uses a
fixed window and front-truncates. The "duplicate runner keyed by
`(model, num_ctx)`" theory was measured and **retracted** — do not re-derive it.

**Compaction is profile-driven with one owner.** Claude gets `window × pct`;
Codex gets an absolute limit capped by `context_input` — not `total_context`,
which produced a trigger above what the backend admits.

**Parser.** Core ailocal has no managed Python environment; the constrained
parser is generation-time only. Revisit if a core venv is introduced or the
supported schema grows.

**Open:** 128 GB is `PENDING_HARDWARE_VALIDATION`; 16/32 GB are structurally
validated only; the compaction runtime trigger is unproven at the threshold. Two
upstream watches (LiteLLM #27442, #31209) are in `docs/troubleshooting.md`.

## Depth

- `docs/architecture.md` — system design, data flow, and the ADRs
- `docs/troubleshooting.md` — symptoms, diagnostics, known limitations
- `docs/security.md` — secrets, ports, images, supply chain
- `docs/BENCHMARK.md` — reproducing model and planner decisions
