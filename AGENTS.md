# AGENTS.md — ailocal

The operating contract for changing this repository. Design lives in
`docs/architecture.md`, operations in `docs/operations.md`, symptoms in
`docs/troubleshooting.md`, secrets in `docs/security.md`.

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

```
./ailocal          the only public command
./install.sh       bootstrap, before ailocal is on PATH

profiles/          hardware policy: capability -> model, geometry, sampling
profiles/clients.yaml  which capability each client surface uses
clients/           authored client templates (never generated output)
deploy/litellm/    proxy hooks/, capability registry, config template, personas
deploy/searxng/    search service definition
benchmarks/        benchmark policy, tasks and suite implementations
lib/               shared implementation and lifecycle; not executable
tests/             domain suites; `ailocal test` is the gate
docs/              architecture, security, troubleshooting
```

## Canonical sources

| Path | Owns |
|---|---|
| `profiles/<tier>.yaml` | capability → model, `context_input`, `max_output`, sampling, reasoning, keep-alive, persona, compaction |
| `profiles/clients.yaml` | which capability each client surface uses — no model tuning |
| `deploy/litellm/registry.yaml` | intrinsic runtime capability: engine, context enforcement, tool support |

`lib/policy.py` is the **one** reader for all of it. It fails closed:
no default tier, unknown fields rejected, duplicate keys and sections rejected.
Nothing else parses YAML, builds a policy path, or resolves geometry.

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

## Testing

`ailocal test` is the gate. Suites live in `tests/` and are
addressable by section, for example `python3 tests/profiles.py resolver`.

Tests assert behaviour, not implementation text. Source inspection is
acceptable only for a security property that cannot be observed through a
public interface — proving an absence, for instance.

## Comments and documentation

Comments explain **why**: constraints, invariants, ownership, surprising
behaviour. Code explains what.

Investigation history belongs in an ADR if durable, `docs/troubleshooting.md`
if operational, `docs/BENCHMARK.md` if about reproducibility, and Git history
otherwise. Keep the invariant, drop the story. No benchmark tables in
configuration. No session narrative in source.

Python follows PEP 8 and PEP 257; shell uses `set -euo pipefail` and the shared
helpers in `lib/output.sh`. Python is standard library only.

## Security and Git

Never commit secrets. Ports bind `127.0.0.1`. Never push without approval. Keep
commits scoped to one responsibility.

`.githooks/commit-msg` rejects assistant attribution trailers, session
identifiers and vendor noreply addresses. Product references are fine — it
matches trailer shape, not words. Commits carry one identity: the configured
human author.

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

## Known external limitations

- **Codex interactive streaming never completes** — BerriAI/litellm#27442.
  Configuration, routing, geometry and tool transport are validated; the
  streamed turn is not.
- **Claude Code shows "0 searches"** even when retrieval worked; the result
  block is dropped during upstream response serialisation.
- **LiteLLM maps `reasoning_effort` unreliably for Ollama** — BerriAI/litellm#15059.
  Per-role defaults are the control that works.
- **The 128 GB profile is unvalidated** and mirrors the 64 GB policy.
