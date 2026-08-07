# AGENTS.md

## Purpose

ailocal runs Claude Code, Codex CLI and VS Code Copilot Chat against local
models on Apple Silicon. Ollama serves the models; LiteLLM fronts them on
`127.0.0.1:4000` as an OpenAI- and Anthropic-compatible proxy. ailocal owns
inference and routing only — not repository intelligence, MCP registration, or
agent definitions — and must stay usable on its own.

## Source of truth

| Path | Owns |
|---|---|
| `pyproject.toml` | packaging, dependencies, the `ailocal` console script |
| `src/ailocal/` | implementation |
| `profiles/<tier>.toml` | capability → model, context and output geometry, sampling |
| `profiles/clients.toml` | which capability each client surface uses |
| `src/ailocal/resources/` | shipped assets, provisioned into the managed roots — never read in place |
| `src/ailocal/policy.py` | the only reader of any of the above |

## Development

```sh
python3 -m venv .venv && . .venv/bin/activate
pip install -e .
```

`pipx install -e .` also works and is what a developer running the gate uses.

## Validate

```sh
ailocal check           # the whole installation, one report
python3 tests/gate.py   # the regression gate
```

After editing anything under `profiles/` or `src/ailocal/resources/`, provision
before validating — the managed copies are what runs, and `ailocal start`
regenerates from them:

```sh
ailocal install     # provisions resources into the managed roots
ailocal start       # regenerates, then remounts
```

## Invariants

- Generated state lives only under `$AILOCAL_STATE` (default
  `~/.local/state/ailocal`). Never hand-edit it, never commit it. Deleting the
  state root and re-running `ailocal start` must fully recover.
- One capability is one `model_list` entry named `ailocal-<capability>`, never a
  raw model tag.
- Geometry is derived, never restated: `num_ctx = context_input + max_output`,
  `num_predict = max_output`, `max_input_tokens = context_input`.
- Codex receives no MCP configuration: it cannot dispatch namespaced tools, so
  an empty `[mcp_servers.*]` section is correct.
- Python is standard library only.
- Ports bind `127.0.0.1`. Never commit secrets.

## Change discipline

Edit the canonical source, never a derived file. Run the smallest relevant
check, then `python3 tests/gate.py` before committing. Update `README.md` only when
public behaviour changes.

Commits carry one identity — the configured human author — with no assistant
attribution trailers or session identifiers. Never push without asking.
