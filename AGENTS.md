# AGENTS.md

## Purpose

ailocal runs Claude Code, Codex CLI and VS Code Copilot Chat against local models on Apple Silicon. Ollama serves the models; LiteLLM fronts them on `127.0.0.1:4000` as an OpenAI- and Anthropic-compatible proxy. ailocal owns inference, routing and its bundled integrations, not repository intelligence or agent definitions, and must stay usable on its own.

## Source of truth

| Path | Owns |
|---|---|
| `pyproject.toml` | packaging, dependencies, the `ailocal` console script |
| `src/ailocal/` | implementation |
| `src/ailocal/resources/profiles/<tier>.toml` | capability → model, context and output geometry, sampling |
| `profiles/<tier>.toml` → `model_guidance` | exact-backend prompting advice, limitations, sources and scoped evaluation evidence; shared by client context and MCP callers |
| `src/ailocal/resources/profiles/clients.toml` | which capability each client surface uses |
| `src/ailocal/resources/deploy/ollama/patch.toml` | exact native runtime source, reviewed patches and integrity pins; `version` changes whenever any pinned patch does, because an owned runtime directory is immutable and a revised patch under the same version fails verification instead of rebuilding |
| `src/ailocal/resources/` | shipped assets — `deploy/` and `clients/` are read IN PLACE from the package; only `profiles/` is copied out |
| `src/ailocal/policy.py` | the only reader of any of the above |

## Development

Cadence's shared policy (principal-engineer voice, evidence labels, version safety, the weekly audit) and its `repository-intelligence`, `tool-maintenance`, `security-review` and `work-tracking` skills apply; never copy their rules into shipped client instructions. Trace every change client → protocol → proxy → provider/tool, and keep deterministic checks apart from model-dependent evaluations.

Before changing dependencies, install behaviour, skills or MCP integration, read [AI development maintenance](docs/maintenance.md); weekly maintenance starts there with `cadence doctor`. An old workaround is a hypothesis to revalidate against current upstream releases and issues, not a permanent limitation.

Version safety here: every image is digest-pinned and the gate check `litellm runtime matches the validated version` fails on drift. [REAL] 2026-10-22: the digest pin held LiteLLM on clean `v1.106.0` when `v1.107.0` shipped a credential-stealer to PyPI and the floating Docker tags (BerriAI/litellm#47826). The per-component security audit and what it must record are in [docs/security.md](docs/security.md#supply-chain).

```sh
python3 -m venv .venv && . .venv/bin/activate
pip install -e .
```

The gate imports `ailocal`, so run it from that venv. `pipx install -e .` additionally puts the command on PATH.

## Validate

```sh
ailocal check                  # the whole installation, one report
python3 tests/gate.py          # the regression gate
python3 tests/gate.py --full   # adds the client compatibility matrix; required before a release
```

The gate refuses to run when the installed package or the generated files are stale, because the suites would then test code `ailocal start` never executes. Reinstall and start first.

ShellCheck runs at `warning` severity and is skipped when absent (`brew install shellcheck`). `.zsh` is covered by `zsh -n` instead — ShellCheck does not implement zsh.

These suites are deliberately OUT of the gate because they take minutes, not seconds:

```sh
python3 tests/installed-runtime.py [--stack]   # proves the wheel needs no checkout
python3 tests/measure_geometry.py [--quick]    # re-measures the numbers profiles cite
python3 tests/measure_cache.py [--quick|--tail N]  # is the KV prefix cache being reused?
./tests/artifact-routing.sh [runs]             # does a real model CALL the artifact tool?
```

When each out-of-gate suite must run, and how to read it, is in [docs/maintenance.md](docs/maintenance.md#out-of-gate-suites). Mermaid publishing is validated in-process; see [docs/architecture.md](docs/architecture.md#mermaid-validation).

## Install and runtime ordering

`deploy/` and `clients/` are read straight from the package, so editing them takes effect on the next `ailocal start`. Editing `profiles/` under `src/` changes only the SHIPPED DEFAULT; the live policy is the copy in the config root, which `ailocal install` preserves once you have edited it. `ailocal install --reset-config` takes the shipped defaults anyway, backing your versions up first — which is how you pick up a changed default after editing a profile by hand.

The runtime executes the INSTALLED package, so after changing anything under `src/ailocal/`:

```sh
pipx install --force .   # then
ailocal start
```

Both halves are required, in that order. `ailocal start` regenerates from the installed package, so starting first reverts the change; the gate refuses to run on this condition. A reinstall replaces the `deploy/litellm` directory bind-mounted into the container, which keeps the old inode and serves an empty mount while still reporting healthy, so `ailocal start` must follow to recreate it. `ailocal check` reports both states. Running the generator from the checkout (`PYTHONPATH=src python -m ailocal.generation`) is a check, not a fix.

## Generated state

FIVE roots. Each has ONE owner and ONE lifecycle, and `policy.py` is the only place any is resolved. Two default to the same directory; that does not make them the same root. The optional patched native runtime is installed software, not disposable generated state.

| Root | Override | Owner | Lifecycle |
|---|---|---|---|
| the package's `resources/` | `AILOCAL_DATA` | the distribution | immutable — Compose mounts it `:ro` |
| `~/.config/ailocal` | `AILOCAL_CONFIG` | **the operator** — `profiles/`, `.env.local` | preserved; a shipped default is replaced only while it still matches the digest recorded at install |
| `~/.config/ailocal` | `AILOCAL_CLIENTS` | **ailocal** — generated client config | disposable; rewritten on every `ailocal start` |
| `~/.local/state/ailocal` | `AILOCAL_STATE` | ailocal | disposable |
| `~/.local/share/ailocal/runtimes` | `AILOCAL_RUNTIMES` | ailocal | immutable versioned installs; teardown removes intact owned payloads and preserves modified/foreign files |

An override of the policy root must move policy and nothing else. Authored policy and generated client config share a directory and must never share a resolver.

Generated files are written straight into the home their consumer reads — no staging tree, no deploy-time copy. Each carries a `Generated by ailocal. Do not edit.` header naming its source. Deleting the state and client roots and re-running `ailocal start` must fully recover; deleting `profiles/` or `.env.local` does not, because those are authored.

`ailocal start` is the only thing that regenerates, always before containers start, never against a mounted asset while the stack runs.

## The repository root

Only `src/`, `tests/`, `docs/` and project metadata. No generated artifact is ever tracked. A generated file at the root means a root has lost its owner — fix the resolver, not `.gitignore`.

## Invariants

- Never hand-edit or commit a generated file. Edit the profile.
- Chat backends follow the model-format rule in [docs/maintenance.md](docs/maintenance.md#model-format): originals or calibrated/QAT builds only, GGUF by default, MLX only with recorded same-model evidence.
- Every model selection is a `name:tag` Ollama identity, never `:latest` and never a repository URL; a native import creates the alias from a hash-pinned pull and drops the pull's own manifest. `tests/profiles.py` enforces the form.
- One capability is one `model_list` entry named `ailocal-<capability>`, never a raw model tag.
- Geometry is derived, never restated: `num_ctx = context_input + max_output`, `num_predict = max_output`, `max_input_tokens = context_input`.
- Claude Code's process-wide context and output variables are projected from the launch-default role. They cannot describe the smaller background slots; `settings.json` lists those limits rather than implying parity.
- ailocal stores no conversation history and never summarises or rewrites a client's history. Delegation stores only session identity and permission bindings; native clients own transcripts. See [docs/architecture.md](docs/architecture.md).
- ailocal provisions inference and bundled runtime integrations inside roots it owns, plus its explicitly configured VS Code/Copilot provider and bundled MCP entries. Public documentation MCP registration through native Claude/Codex CLIs is the hosted-root exception; never change hosted model, auth, instruction or plugin settings.
- ailocal installs no language server. It enables the official plugin for a server already on PATH, in its own root only.
- Codex research MCP dispatch is measured on Codex 0.163.0 through the local proxy. Other MCP servers require their own evidence before registration.
- Python is standard library only.
- Ports bind `127.0.0.1`. Never commit secrets.

## Change discipline

Edit the canonical source, never a derived file. Run the smallest relevant check, then `python3 tests/gate.py` before committing. Update `README.md` only when public behaviour changes.

Commits carry one identity — the configured human author — with no assistant attribution trailers or session identifiers. Never push without asking.

Releases follow [RELEASING.md](RELEASING.md): from v0.9.0 the published interface is a contract, and a tag lands only on a commit whose built artifact was tested.

## Markdown formatting

Write Markdown for rendered readers, not 80-column terminals. One paragraph per source line; do not hard-wrap prose. Wrap code, commands, tables and lists only where their syntax requires it.
