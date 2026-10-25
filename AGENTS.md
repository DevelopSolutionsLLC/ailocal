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
| `src/ailocal/resources/deploy/ollama/patch.toml` | exact native runtime source, reviewed patches and integrity pins |
| `src/ailocal/resources/` | shipped assets — `deploy/` and `clients/` are read IN PLACE from the package; only `profiles/` is copied out |
| `src/ailocal/policy.py` | the only reader of any of the above |

## Development

Apply Cadence's engineering policy and available `repository-intelligence`, `tool-maintenance`, `security-review`, and `work-tracking` skills when relevant; do not copy their rules into shipped client instructions. Work as an AI systems engineer: trace client → protocol → proxy → provider/tool, distinguish deterministic checks from model-dependent evaluations, and report evidence rather than persona claims.

For dependency upgrades, install changes, skills, or MCP integration, read [AI development maintenance](docs/maintenance.md) before changing behavior. Seek current upstream releases, advisories, and relevant open/closed regression issues; an old workaround is a hypothesis to revalidate, not a permanent platform limitation. Promote a candidate only after affected paths pass, then commit and push when authorized. Keep ordinary builds pinned and reproducible.

For weekly application and patch maintenance, follow the [weekly maintenance procedure](docs/maintenance.md#weekly-maintenance), starting with `cadence doctor`. These instructions guide an invoked maintenance run; they do not schedule one or authorize unvalidated upgrades.

Run the latest release *proven safe*, deliberately held about two back (≈N-2): new enough for current fixes, old enough that a poisoned or regressed build has surfaced and been pulled. Validate every candidate before adoption — not yanked, not regressed, not on a security advisory — then pin every image and package by immutable digest and never follow a floating tag (`latest`, `main-latest`, `main-stable`). This file states the policy; it cannot enforce it. The gate already enforces the machine-checkable half — the running image must match the validated pinned digest (`ailocal check`, and the `litellm runtime matches the validated version` gate check) — but confirming a pin is still the latest *safe* version needs a live advisory lookup, which belongs in a maintenance audit command, not prose or the offline gate. [REAL] 2026-10-22: this lag kept the stack on the clean LiteLLM `v1.106.0` digest (`sha256:2e4c0371…`) when `v1.107.0` shipped a credential-stealer and RAT to PyPI and the floating `ghcr.io/berriai/litellm` Docker tags the same day (BerriAI/litellm#47826; compromised digest `sha256:1bbcb144…`). A digest pin makes a moved tag inert; a floating tag would have pulled the malware.

Security auditing is required for every tool ailocal installs, provisions, bundles, or relies on, including SearXNG, LiteLLM, Ollama, Claude Code, Codex, VS Code/Copilot, Docker, Cadence, grepai, language servers, MCP servers, plugins, interpreters, and vendored browser libraries. Before adoption, upgrades, or a release, inventory the actual versions and image digests; check upstream security advisories and affected/fixed ranges; scan available dependency inventories and container images with a current advisory database; review provenance, permissions, network exposure, and affected first-party integration code. Include transitive dependencies and enabled extensions, not just top-level packages. Record the audit date, commands, database freshness, source links, findings, and coverage gaps in the issue or PR. Resolve applicable findings or document their disposition before promotion; unavailable scans and unassessed proprietary or remote services remain explicitly unverified. Passing tests, signatures, or a scan with no matches must never be described as proof of a vulnerability-free codebase. Follow [AI development maintenance](docs/maintenance.md) for validation and preserve operator configuration.

```sh
python3 -m venv .venv && . .venv/bin/activate
pip install -e .
```

The gate imports `ailocal`, so run it from that venv. `pipx install -e .` additionally puts the command on PATH.

## Validate

```sh
ailocal check           # the whole installation, one report
python3 tests/gate.py   # the regression gate
```

ShellCheck runs at `warning` severity and is skipped when absent (`brew install shellcheck`). `.zsh` is covered by `zsh -n` instead — ShellCheck does not implement zsh.

Two suites are deliberately OUT of the gate because they take minutes, not seconds:

```sh
python3 tests/installed-runtime.py [--stack]   # proves the wheel needs no checkout
python3 tests/measure_geometry.py [--quick]    # re-measures the numbers profiles cite
python3 tests/measure_cache.py [--quick|--tail N]  # is the KV prefix cache being reused?
./tests/artifact-routing.sh [runs]             # does a real model CALL the artifact tool?
```

Run `artifact-routing` whenever the artifact tool's description, its
`alwaysLoad`/`searchHint` metadata, or the routing contract in `server.py` changes. It
MEASURES a stochastic model and reports a rate; what the description actually SAYS is
asserted deterministically by `test_routing_contract.py`, which is in the gate and needs no
inference. Keep the two apart when reading a result: [REAL] gemma4:26b-mlx returned fenced
Mermaid for a phrase the description already names verbatim, then quoted the rule back
correctly once corrected. That is a model-dependent routing miss, and adding synonyms does
not fix it. It spends minutes of local inference and asserts an AGGREGATE rate rather than
per-phrase, because sampling variance at n=3 is larger than the effect.

Invalid Mermaid can no longer be published: `publish()` parses every diagram with the real
Mermaid grammar (`mermaid_validate.py`, headless Chrome over `vendor/mermaid.min.js`, ~1.7 s
per diagram) and returns the parser error to the MODEL instead of serving a bomb icon to the
human. The verdict has THREE states and the third is the point -- VALID publishes, INVALID is
refused with the parser message, and UNAVAILABLE (no browser, launch failure, timeout, no
verdict) is ALSO refused, because reporting an unchecked diagram as a successful publish is
the original defect. Only the Mermaid path is gated: `html`, `architecture` and Markdown with
no fence never start a validator and cost nothing. Chrome runs in a throwaway
`--user-data-dir` and is killed once the verdict is read; it never touches the profile a
person is using.

`test_mermaid_validate.py` (CORE) asserts the state machine with the browser replaced through
the `runner` seam, plus one bounded real parse; `test_mermaid_grammar.py` (FULL) is the
authoritative corpus and launches a browser per fixture. Run `installed-runtime` whenever packaging, resources or provisioning change. Run `measure_geometry` after an Ollama or MLX upgrade, after changing a model, or when bringing up an unmeasured tier — it asserts nothing and keeps no history; a human reads the numbers and decides whether the profile still holds. It stops and reloads models, so nothing may run beside it.

Run `measure_cache` to confirm the KV prefix cache is actually reused across turns — the lever that keeps agent prefill off the critical path on Apple Silicon. On a 16 GB host [REAL] the active probe on `qwen3.5:4b` collapsed a warm turn's prefill from ~18.6K tokens to a few dozen (~99.8% reuse, ~115× faster prefill), end to end through the `claude-local` path; a regression that silently turns every turn cold (an engine upgrade, a new hybrid/recurrent model, interleaving background requests evicting the single 16 GB slot) is invisible without it. The default active probe stops and reloads the probed model, so it may not run beside another inference suite; `--tail N` is passive — it summarises the last N real requests from the Ollama log and runs no inference, so use it right after a `claude-local`/`codex-local` session. Run it after an Ollama or MLX upgrade, after changing a model, or when bringing up an unmeasured tier. `--tier 64gb` is the validation tool to run on the MLX (`gemma4:26b-mlx`) path, whose reuse is unmeasured on 16 GB hardware. Like `measure_geometry` it asserts nothing and keeps no history.

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
- One capability is one `model_list` entry named `ailocal-<capability>`, never a raw model tag.
- Geometry is derived, never restated: `num_ctx = context_input + max_output`, `num_predict = max_output`, `max_input_tokens = context_input`.
- Claude Code's process-wide context and output variables are projected from the launch-default role. They cannot describe the smaller background slots; `settings.json` lists those limits rather than implying parity.
- ailocal holds no conversation state. It never summarises or rewrites a client's history; it generates the compaction *threshold* the client applies. See [docs/architecture.md](docs/architecture.md).
- ailocal provisions inference and bundled runtime integrations only inside a root it owns. Public documentation MCP registration through native Claude/Codex CLIs is the explicit hosted-root exception; never change hosted model, auth, instruction or plugin settings.
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
