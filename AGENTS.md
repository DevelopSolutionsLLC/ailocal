# AGENTS.md

## Purpose

ailocal runs Claude Code, Codex CLI and VS Code Copilot Chat against local models on Apple Silicon. Ollama serves the models; LiteLLM fronts them on `127.0.0.1:4000` as an OpenAI- and Anthropic-compatible proxy. ailocal owns inference and routing only — not repository intelligence, MCP registration, or agent definitions — and must stay usable on its own.

## Source of truth

| Path | Owns |
|---|---|
| `pyproject.toml` | packaging, dependencies, the `ailocal` console script |
| `src/ailocal/` | implementation |
| `src/ailocal/resources/profiles/<tier>.toml` | capability → model, context and output geometry, sampling |
| `src/ailocal/resources/profiles/clients.toml` | which capability each client surface uses |
| `src/ailocal/resources/` | shipped assets — deploy/ and clients/ are read IN PLACE from the package; only `profiles/` is copied out |
| `src/ailocal/policy.py` | the only reader of any of the above |

## Development

```sh
python3 -m venv .venv && . .venv/bin/activate
pip install -e .
```

`pipx install -e .` also works, and gives you the `ailocal` command on PATH.

## Validate

```sh
ailocal check           # the whole installation, one report
python3 tests/gate.py   # the regression gate
```

The gate runs ShellCheck over the shell this repository ships and tests with, and
**skips that check when ShellCheck is absent** so the gate stays runnable without
it. Install it to get the check instead of the skip:

```sh
brew install shellcheck
```

It runs at `warning` severity — the note level here is almost entirely style and
would bury a real finding. `.zsh` is excluded because ShellCheck does not
implement zsh; those files stay covered by `zsh -n`.

The gate imports `ailocal`, so it runs under the interpreter the package is installed into — the activated `.venv` above. A pipx installation puts the command on PATH but not the module on the host interpreter's import path, so run the gate from the venv.

```sh
python3 tests/installed-runtime.py           # no Docker: path proof only
python3 tests/installed-runtime.py --stack   # also start/status/stop
```

DELIBERATELY OUT OF THE GATE. It builds a venv, installs the package and (with `--stack`) starts containers, which is minutes rather than the seconds the gate is held to. Run it whenever packaging, resources or provisioning change — it is the only check that proves the wheel needs no checkout, and it rotted unnoticed once precisely because nothing named it.

```sh
python3 tests/measure_geometry.py           # active tier
python3 tests/measure_geometry.py --quick   # skip the slow cold-prefill probe
```

ALSO OUT OF THE GATE, AND NOT A BENCHMARK. The profiles justify their context and output geometry with measured numbers — KV bytes per context token, cold-prefill rate, resident size — and `resources/deploy/litellm/registry.yaml` says to revalidate after any Ollama or MLX upgrade. This is how that is carried out. It asserts nothing, keeps no history and has no thresholds: it prints what it measured, and a human decides whether the profile still holds. It stops and reloads models, so nothing may run beside it. Run it after an engine upgrade, after changing a model, or when bringing up a tier no one has measured.

`src/ailocal/resources/deploy` and `.../clients` are read straight from the package, so editing them takes effect on the next `ailocal start` — there is no copy to refresh. Editing `profiles/` under `src/ailocal/resources` changes only the SHIPPED DEFAULT; the live policy is the copy in the config root, which `ailocal install` installs and thereafter preserves once you have edited it.

Which also means: **reinstalling the package while the stack is running detaches the running container from it.** `deploy/litellm` is bind-mounted into `ailocal-litellm` as `/app/config`, and `pipx install --force` (or an upgrade, or anything else that recreates the venv) replaces that directory. The container keeps the old inode, so `/app/config` goes empty while `docker ps` still reports healthy and the proxy still answers 200 — it is serving the hooks Python imported at boot. End every reinstall with `ailocal start`, which recreates the containers and re-resolves the mount; [REAL] it does so even when the reinstall changed nothing, so it is always the safe way to finish. `ailocal check` reports this state if you forget. A bare `docker restart` repairs the mount too, but only that — `ailocal start` also picks up compose and generated-config changes, and it is what README tells users to run after `pipx upgrade`.

And the same ordering in reverse: **`ailocal start` runs the INSTALLED generator, not your checkout.** Change anything under `generation.py` and the sequence is `pipx install --force .` *then* `ailocal start` — the other way round, `start` regenerates from the stale package and silently reverts the change you just made. [REAL] this reverted a committed `settings.json` line; the repository was correct, the runtime was not, and the only symptom was `generation --check` reporting drift. Running the generator out of the checkout (`PYTHONPATH=src python -m ailocal.generation`) writes the right thing but leaves the installed package stale for the next `start`, so it is a check, not a fix.

## Generated state

FOUR roots. Each has ONE owner and ONE lifecycle, and `policy.py` is the only place any of them is resolved. Two of them default to the same directory; that does not make them the same root.

| Root | Override | Owner | Lifecycle |
|---|---|---|---|
| the package's `resources/` | `AILOCAL_DATA` | the distribution | immutable — ailocal never writes here; Compose mounts it `:ro` |
| `~/.config/ailocal` | `AILOCAL_CONFIG` | **the operator** — `profiles/`, `.env.local` | preserved; a shipped default is replaced only while it still matches the digest recorded at install |
| `~/.config/ailocal` | `AILOCAL_CLIENTS` | **ailocal** — generated client config | disposable; rewritten on every `ailocal start` |
| `~/.local/state/ailocal` | `AILOCAL_STATE` | ailocal | disposable |

Authored policy and generated client config share a directory and must never share a resolver. While one function answered both, pointing `AILOCAL_CONFIG` at the checkout — which the test harness does, to read the shipped profiles — redirected every generated client file into the repository, and they were committed. **An override of the policy root must move policy and nothing else.**

Generated files are written STRAIGHT INTO the home their consumer reads. There is no staging tree and no deploy-time copy, so a generated file cannot be stale relative to another copy of itself. Every one carries a `Generated by ailocal. Do not edit.` header naming its source. Deleting the state and client roots and re-running `ailocal start` must fully recover; deleting `profiles/` or `.env.local` does not, because those are authored.

`ailocal start` is the only thing that regenerates. Generation happens BEFORE containers start and nothing mutates a mounted asset while the stack runs.

## The repository root

Only project-level concepts belong there: `src/`, `tests/`, `docs/`, and project metadata. No generated artifact is ever tracked. A generated file appearing at the root is not a tidiness problem — it is evidence that a root has lost its owner, and the fix is the resolver, not `.gitignore` (the ignore entries are a backstop, not the boundary).

## Invariants

- Never hand-edit or commit a generated file. Edit the profile.
- One capability is one `model_list` entry named `ailocal-<capability>`, never a raw model tag.
- Geometry is derived, never restated: `num_ctx = context_input + max_output`, `num_predict = max_output`, `max_input_tokens = context_input`.
- ailocal holds no conversation state. It never summarises or rewrites a client's history; it generates the compaction *threshold* the client applies. See [docs/architecture.md](docs/architecture.md) for which component owns which half.
- Codex receives no MCP configuration: it cannot dispatch namespaced tools, so an empty `[mcp_servers.*]` section is correct.
- Python is standard library only.
- Ports bind `127.0.0.1`. Never commit secrets.

## Change discipline

Edit the canonical source, never a derived file. Run the smallest relevant check, then `python3 tests/gate.py` before committing. Update `README.md` only when public behaviour changes.

Commits carry one identity — the configured human author — with no assistant attribution trailers or session identifiers. Never push without asking.

Releases follow [RELEASING.md](RELEASING.md): from v0.9.0 the published interface is a contract, and a tag lands only on a commit whose built artifact was tested.

## Markdown formatting

Write Markdown for rendered readers (GitHub, documentation sites, mobile apps), not 80-column terminals. Keep one paragraph per source line. Do not hard-wrap prose. Wrap code, shell commands, tables, lists, and other structured content only where their syntax requires it.
