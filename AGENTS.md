# AGENTS.md — ailocal

The operating contract for changing this repository. Architecture lives in
`docs/architecture.md`, symptoms in `docs/troubleshooting.md`, secrets in
and `docs/security.md`.

## Mission

Run Claude Code, Codex CLI and VS Code Copilot Chat against local models on
Apple Silicon. Ollama serves the models; LiteLLM fronts them on
`127.0.0.1:4000` as an OpenAI- and Anthropic-compatible proxy.

## Ownership boundary

ailocal owns inference and routing: LiteLLM, Ollama, the Docker runtime, model
installation, tier profiles, context and output geometry, capability aliases,
search, isolated client profiles, tool filtering, and the Python LSP baseline
for `claude-local`.

It does not own repository intelligence, MCP registration, agents or skills.
**ailocal must stay fully usable on its own** — never make another project a
dependency.

## Repository map

Four directories. Everything ailocal ships is inside the package.

```
src/ailocal/           the installed package — the whole product
  cli.py                 the command table; the only public surface
  policy.py              the ONE policy reader, and the only root resolver
  generation.py          the ONE generator; `ailocal sync` is its entry point
  runtime.py             Compose, lifecycle, status, metrics, traces
  install.py             prerequisites, provisioning, agents, models, audit
  clients.py             deploying the isolated client homes
  checks/                validate · smoke · security · doctor · the gate
  resources/             SHIPPED ASSETS — never imported, always provisioned
    deploy/litellm/        proxy hooks, capability registry, template, personas
    deploy/searxng/        search service definition
    clients/               authored client templates
    profiles -> ../../../profiles   symlink: one copy, carried by the wheel

profiles/              hardware policy: capability -> model, geometry, sampling
                       authored, dev-editable, and the shipped default
tests/                 domain suites (`ailocal test` is the gate)
docs/                  runtime flow, security, troubleshooting, ADRs
pyproject.toml         packaging; `[project.scripts]` declares the command
```

**Resources are provisioned, never read in place.** `install.provision()` copies
`resources/` into `$XDG_DATA_HOME/ailocal` and `$XDG_CONFIG_HOME/ailocal`; every
consumer — Compose bind mounts, client deployment, the registry — reads the
managed copy. Nothing reads site-packages at runtime, and `ailocal install`
needs no checkout.

**A package command is a function, not a program.** `cli.py` imports the owning
module and calls `main(argv)`. There is one dispatch mechanism and no second
interpreter.

## Canonical sources

| Path | Owns |
|---|---|
| `profiles/<tier>.toml` | capability → model, `context_input`, `max_output`, sampling, reasoning, keep-alive, persona, compaction |
| `profiles/clients.toml` | which capability each client surface uses — no model tuning |
| `resources/deploy/litellm/registry.yaml` | intrinsic runtime capability: engine, context enforcement, tool support |

`src/ailocal/policy.py` is the **one** reader for all of it. It fails closed:
no default tier, unknown fields rejected, duplicate keys and sections rejected.
Nothing else parses policy, builds a policy path, or resolves geometry —
including admission, which is `policy.geometry()` and nowhere else.

## Authored versus generated

Everything generated lives **outside the checkout**, under
`${AILOCAL_STATE:-~/.local/state/ailocal}`:

```
active-profile                    the selected tier
integration-contract.json         published for external consumers
litellm/   config.yaml · capabilities.json · effective-profile.json
searxng/   settings.yml           carries the Brave key, mode 0600
clients/   claude · codex · continue · copilot · configure.zsh
```

`ailocal sync` is the only generator and the only public entry point. It stages
every artifact, validates, replaces atomically per file, rolls back on a
recoverable failure, and writes its marker last.

**Rules.** Never hand-edit a generated file. Never commit one. A tracked file is
authored source or a template — never a runtime artifact that generation
rewrites. Deleting the state root and re-running `ailocal sync` must fully
recover.

## Change workflow

1. Edit the canonical source, never a derived file.
2. `ailocal sync` — regenerate.
3. `ailocal validate` — deterministic consistency; works with the stack stopped.
4. `ailocal clients` — deploy, if client output changed.
5. `ailocal test` — the gate. Run it twice when timing-sensitive
   suites are involved; a transient first failure is investigated, not waved
   through.
6. Commit only after the gate is green.

## Invariants

- One capability is served as one `model_list` entry named `ailocal-<capability>`,
  never a raw model tag.
- Geometry is derived, never restated: `num_ctx = context_input + max_output`,
  `num_predict = max_output`, `max_input_tokens = context_input`.
- Tool filtering is static per model, never per question — a schema set that
  depends on the prompt breaks caching and reproducibility.
- Codex gets no MCP servers: it cannot dispatch namespaced tools, so an empty
  `[mcp_servers.*]` section is correct.
- Subsystems consume public APIs, never another subsystem's files.
- No fallback paths, no compatibility shims, no second implementation.

## Engineering standard

**Simplicity.** Prefer the smallest complete change. One concept has one owner.
Reuse existing functions and explicit parameters. No compatibility fallback
without an external contract. Add an abstraction only when it removes
measurable duplication, branching, readers, writers or entry points — not to
split cohesive code, and never for a LOC target.

**Ownership.** Authored source and generated state are separate; generated
artifacts live only under `$AILOCAL_STATE`. Subsystems consume public APIs,
never another subsystem's files. Policy has one reader; generation has one
writer and one public entry point.

**Code.** Clear names, cohesive functions, explicit parameters over mutable
globals. Tables only where behaviour differs solely by data. Comments explain
contracts, invariants, security and destructive boundaries, or an active
external compatibility constraint — never investigation history, measurement
diaries or prior layouts. Python is standard library only and follows PEP 8
and PEP 257; the shell that remains uses `set -euo pipefail`, the shared helpers in
`tests/harness.sh`, and fails closed. No generic managers, factories, registries
or utility layers without concrete consumers.

**Tests.** `ailocal test` is the gate; suites live in `tests/` and are
addressable by section, for example `python3 tests/profiles.py resolver`. Test
behaviour at the strongest useful boundary, one owner per invariant, and keep a
regression for every real defect. Source inspection is acceptable only to prove
an absence behaviour cannot — a forbidden MCP block, an attribution trailer, a
secret-shaped value. Default tests and diagnostics must not consume paid
external API quota; anything long, destructive, GUI-bound or metered is opt-in.

**Validation.** Focused checks first, the full gate proportionally when shared
behaviour changes. Never claim success without evidence. Generation stays a
fixed point and leaves the tracked tree clean. Distinguish a product defect
from an environment failure, an upstream limitation and an unmeasured surface.

**Public quality.** Optimise for a cold reader. Documentation describes the
current system; Git history owns investigations and discarded theories. Never
commit secrets, personal paths, generated state, assistant attribution or
session residue. Product installation configures the product — never the
developer's Git, editor or shell policy.

## Instruction ownership

- `AGENTS.md` owns repository-wide policy: this file is authoritative.
- `src/ailocal/resources/clients/<client>/` owns local runtime and client
  compatibility facts only.
- Task prompts, commands and agent definitions own workflow-specific behaviour.
- Tests, schemas and permissions enforce what must not depend on model
  compliance. This repository installs no Git hooks.

Do not duplicate repository policy into a client preload, or copy volatile
profile values into prose when generation can supply them.

Investigation history belongs in an ADR if it explains a durable decision,
`docs/troubleshooting.md` if operational, and Git history otherwise.

## Security and Git

Never commit secrets. Ports bind `127.0.0.1`. Never push without approval. Keep
commits scoped to one responsibility.

Commits carry one identity: the configured human author, with no assistant
attribution trailers or session identifiers.

Ownership is **DevelopSolutions, LLC** (Apache-2.0), maintained by
**Victor T. Chevalier**. The root LICENSE is authoritative; no per-file
copyright banners.

## Do not

- Add a second YAML parser, or parse policy in a shell script.
- Hand-edit a generated file, or edit a client config instead of its source.
- Put model-name conditionals in generation or client code — that belongs in a
  profile or the registry.
- Re-enable MCP for Codex.
- Rewrite LiteLLM behaviour locally to work around an upstream bug.
- Change the LiteLLM pin, model roles, or profile geometry as a side effect of
  another task.

## External compatibility boundary

Codex receives no MCP configuration unless namespaced-tool dispatch is
revalidated and the corresponding architecture decision is explicitly
superseded. Current symptoms, upstream defects, and validation status belong in
`docs/troubleshooting.md`, not in this always-loaded contract.
