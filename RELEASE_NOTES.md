# Release notes

## v0.9.0 — 2026-08-04

Local-first AI stack: LiteLLM + Ollama/MLX behind capability aliases, with
Claude Code, Codex and VS Code as clients. This release completes the profile
consolidation, adds authenticated general web search, and corrects two
configuration defects that could have produced hard failures in long sessions.

### New

- **Brave Search API**, integrated through generated SearXNG settings. The key
  lives only in the untracked `.env`; the tracked profile carries a placeholder
  and `scripts/lib/compose.sh` renders a runtime copy into `$AILOCAL_STATE`
  (mode 0600, outside the checkout, so it cannot be committed). Fails closed
  with `BRAVE_KEY_MISSING` / `BRAVE_PLACEHOLDER_MISSING` /
  `BRAVE_SETTINGS_SECRET_LEAK`; disabling Brave never breaks deployment.
- **Corrected 64 GB profile geometry**, measured under filled context rather
  than assumed — see *Fixed*.
- **Codex compaction derived from admissible input**, closing a latent defect.
- **Conditional Continue generation** — configuration is written only when the
  extension is present, `AILOCAL_CONTINUE=1` is set, or a managed config already
  exists.
- **Bounded Codex validation harness** — reports
  `BLOCKED_UPSTREAM_LITELLM_27442` within a timeout instead of hanging.
- **Search engine health diagnostic** (`scripts/diagnostics/search-engines.py`),
  deliberately not in the regression gate: a green gate must not depend on a
  third party's CAPTCHA.
- **Research search tier** — arXiv and Crossref, API-backed and keyless.

### Fixed

- **Implementation and review context sizing (64 GB).** Both run
  `gemma4:26b-mlx` but still carried geometry sized for the smaller models they
  used to run, leaving implementation at 16,384 input on a model architecture
  already drives at 81,920. Now 65,536 input with role-specific output ceilings.
  Verified with prompts reaching ~107% of the intended budget: no truncation,
  all head/middle/tail sentinels recalled, zero swap growth.
- **Codex compaction calculation.** The trigger was derived from
  `total_context` (input + output) rather than `context_input`, producing 18,432
  on a role admitting 16,384 — a long session would have taken HTTP 400
  `ContextWindowExceeded` **before** it could compact. Now capped at admissible
  input, with the invariant asserted in the gate.
- **Stale model references.** Retired models removed from `preferred` lists and
  from client documentation that still presented them as current with the wrong
  context sizes. Measurement evidence retained.
- **Continue lifecycle.** A keyed config was written for an absent extension on
  every install and repair, accumulating backups.
- **Search diagnostics no longer manufacture noise.** The health check probed
  bang-only engines, producing the CAPTCHA tracebacks it then reported; an
  earlier revision also drove Crossref from healthy to rate-limited during a
  single run.
- **Semantic Scholar removed** — its SearXNG engine targets an internal endpoint
  now returning `HTTP 202 text/html`, which the engine parses as JSON.
- **Profile documentation.** Rationale describing models no longer in use, and
  claims resting on a retracted duplicate-runner theory, corrected.

### Changed

- **Gemma 4 26B MLX is the primary engineering model** — architecture,
  implementation and review on the 64 GB tier.
- **Qwen 3.5 2B remains the fast role**, with `reasoning: false` mandatory: it
  moved the model from 6/10 to 9/10 on the fast-role suite.
- **GPT-OSS 20B is benchmark-only** — not the reviewer, no measured advantage
  over Gemma on review.
- **Qwen3-Coder 30B is inactive** — retained in benchmark history only.
- **GitHub MCP and grepai ownership clarified**: both are Cadence-owned. ailocal
  installs and registers neither, preserves Cadence's registrations, and remains
  fully installable when Cadence is absent.

### Known upstream issues

Both are documented, bounded, and outside the supported working surface.

- **[LiteLLM #27442](https://github.com/BerriAI/litellm/issues/27442)** —
  `/v1/responses` streams content in bare `data:` frames with no `event:` line,
  so Codex renders the text but never marks the turn complete. Production stays
  pinned to LiteLLM 1.93.0; the validation harness classifies this rather than
  hanging. Non-streaming paths and tool calls are unaffected.
- **[LiteLLM #31209](https://github.com/BerriAI/litellm/issues/31209)** — the
  Anthropic response model omits `server_tool_use` and `web_search_tool_result`
  block types, so Claude Code displays "0 searches" while the model receives the
  full result set (traced: 50 URLs, ~22 KB). Cosmetic. Verified unfixed in
  v1.95.0 by isolated container test.

### Tier status

| Tier | Status |
|---|---|
| 16 GB, 32 GB | structurally validated; not measured on that hardware |
| **64 GB** | **measured on this hardware** |
| 128 GB | `PENDING_HARDWARE_VALIDATION` — mirrors 64 GB, not a 128 GB design |

### Client support

| Client | Status |
|---|---|
| Claude Code (`claude-local`) | supported — routing, tools, search, streaming, resume |
| Codex (`codex-local`) | configuration, routing, geometry and tool-call transport validated; **interactive turns remain blocked by LiteLLM #27442** — the Responses stream does not terminate in the form Codex requires |
| VS Code (LiteLLM Connector) | supported; endpoint and key require a one-time entry in VS Code SecretStorage, which no script can seed |
| VS Code (Continue) | optional; configuration generated only when installed or opted in |
