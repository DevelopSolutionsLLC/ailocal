# ADR 001 — LiteLLM as the single routing layer

**Status:** Accepted · **Date:** 2026-07 · **Supersedes:** direct client→Ollama config

## Problem

Three AI clients (Claude Code, Codex, VS Code) each speak a different API dialect
— Anthropic `/v1/messages`, OpenAI Responses `/v1/responses`, Chat Completions
`/v1/chat/completions` — and each hard-codes model IDs it will not stop sending
(`claude-haiku-4-5`, `gpt-4o`). Ollama serves none of those dialects or names.

## Constraints

- No forks or patches of the clients. They must work unmodified.
- Cloud sessions must keep working on the same machine, unaffected.
- One place to change a model, not four client configs.

## Alternatives considered

1. **Point each client at Ollama's OpenAI-compatible endpoint.** Rejected:
   Claude Code speaks Anthropic, not OpenAI, and there is nowhere to inject
   personas or filter tools.
2. **A hand-written proxy.** Rejected: re-implements dialect translation,
   streaming, retries and context checks that LiteLLM already does.
3. **LiteLLM.** Chosen.

## Decision

One LiteLLM container on `127.0.0.1:4000` fronts Ollama and serves all three
dialects. Client-facing names resolve through `model_group_alias` onto one
canonical `ailocal-<capability>` entry per capability.

## Why

It is the only option that gives a single interception point for personas, tool
filtering and context enforcement, while letting every client stay stock.

## Tradeoffs

- A hop that must be running; `doctor.sh` exists because of it.
- **LiteLLM parses config once at boot and the config is bind-mounted**, so a
  config edit changes nothing until the process restarts — and
  `docker compose up -d` will not restart it, because the compose spec did not
  change. Measured: after repointing `claude-haiku-4-5` at `ailocal-fast`, the
  proxy kept routing to `ailocal-implementation` with nothing in the logs saying
  so. `start.sh` now fingerprints `config.yaml`, `registry.yaml`, the hooks and
  the personas, and restarts when they change.

## Measurements

- 6 models register at boot; startup log is otherwise clean (no auth failures,
  no deprecations).
- Context-window enforcement is real: an oversized prompt is rejected by
  `_pre_call_checks`, measured `Max Input Tokens=4096, Got=10813`.
- `/v1/models` requires auth; an unauthenticated 401 is correct, not a fault.

## Documentation vs measurement (validated 2026-07-28)

Two upstream claims were checked against this deployment. Where they disagree,
**the measurement is operational truth** and the discrepancy is recorded rather
than resolved in the docs' favour.

**Agrees.** "LiteLLM Proxy drops `image_generation`, `namespace`, custom tools
and forwards only function/mcp/web_search tools" — matches our
`routes./v1/responses.drops_tool_types`, which was read from LiteLLM's
transformation source rather than inferred. This is the mechanism behind the
Codex MCP blocker: LiteLLM discards Codex's namespace-typed tools.

**Disagrees.** BerriAI/litellm [#5524](https://github.com/BerriAI/litellm/issues/5524)
and [#19217](https://github.com/BerriAI/litellm/issues/19217) report that
`model_group_alias` names are **not** returned by `/v1/models`. On our 1.93.0
they **are** — all 11 compat aliases (`claude-sonnet-4-6`, `gpt-4o`, …) appear
alongside the 6 canonical `ailocal-*` entries. Most likely fixed after those
issues were filed.

This matters: Claude Code's gateway model discovery reads `/v1/models`, so if a
future LiteLLM release regressed to the documented behaviour, the compat names
would silently vanish from the `/model` picker. Check this after any upgrade —
`validate-deployment.sh` asserts two aliases are present for exactly this reason.

## Known limitations

- Cost accounting is meaningless for local models; zeros are emitted so the cost
  layer stops logging "not in built-in cost map" for every model at boot.
- Non-streaming requests produce no `request_trace` line. Absence of a trace is
  not absence of a request — send `"stream": true` when you need to confirm
  routing.

## Revisit if

- LiteLLM gains hot config reload (the restart fingerprint becomes unnecessary).
- A client ships native Ollama support good enough to bypass the proxy.
- A LiteLLM upgrade changes `drops_tool_types` for `/v1/responses` — that is
  read from its source, not inferred, and pinned to the 1.93.0 image.
