# ADR 014: Local research owns its evidence boundary and the only Brave path

**Status:** Accepted · **Date:** 2026-10-18 · **Issues:** [#9](https://github.com/DevelopSolutionsLLC/ailocal/issues/9), [#13](https://github.com/DevelopSolutionsLLC/ailocal/issues/13)

## Context

Search providers may ignore site filters, and some source pages cannot be read, so a cited answer must not imply that every source was fetched. Local synthesis competes for the same Ollama slot as its caller when that caller is claude-local.

Operators want Brave Search used automatically when keyless coverage is thin, bounded by an allowance they set per billing period, with no per-search approval and no overage. An allowance can only be enforced where requests are counted. SearXNG's `braveapi` engine discards Brave's response headers, so neither SearXNG nor LiteLLM's WebSearch interception (which queries SearXNG) can observe or bound spending.

Brave's [rate-limiting guide](https://api-dashboard.search.brave.com/documentation/guides/rate-limiting) (read 2026-10-18) states that `X-RateLimit-Limit`, `-Policy`, `-Remaining` and `-Reset` carry one comma-separated value per window, that a monthly quota of 0 means unlimited, and that only successful requests are billed. It documents a 429 for exceeding a limit and no account-usage API. [REAL] This account's key returned `Limit: 50, 0` and `Policy: 50;w=1, 0;w=2678400`: no monthly cap is reported, so the account-wide remaining allowance cannot be observed.

## Decision

A bundled MCP component, packaged per [ADR 011](011-bundled-artifacts.md), exposes `search(query, sites?, preset?, use_brave?)` and an optional `answer(question, sites?)`.

- **Evidence.** Site scope is enforced on returned URLs and redirects, provider attribution is kept, and source records distinguish page text from indexed snippets. Only public pages are fetched.
- **Planning.** The server chooses providers per preset, narrows by site across forum communities, retries AND-only APIs with one keyword reformulation, and bounds fan-out, concurrency, elapsed time and session spend. An identical search repeated within a session is served once more with a warning, then refused.
- **One Brave path.** `braveapi` is not a SearXNG engine. The research server calls Brave directly behind a cross-process SQLite ledger in the state root. Each request is reserved under `BEGIN IMMEDIATE` before it is sent and settled as billed, not billed, or unknown (counted). The operator's budget (`AILOCAL_BRAVE_BUDGET`, default 1,000) is counted per provider period taken from the headers; an unknown period is counted over the longest window and established by one request at a time. A 429 or 402 other than `RATE_LIMITED` stops spending until the observed reset. Brave is added, at most two requests per call, when keyless coverage is thin; a caller may request it from the start or forbid it.
- **Reporting.** Every result carries a `brave.summary` line that separates the operator's budget, as counted on this machine, from account-wide usage, which is `unknown` unless Brave reports a monthly cap. Relaying the line into a final answer is model-dependent: [REAL] local models omitted it in 12 of 12 measured answers, including when it led the result text.
- **Routing.** The tool description, server instructions and the client skill route requests for outside information to `search` without the user naming it.
- **Synthesis.** `answer` uses keyless providers only and the configured local LiteLLM route, receives evidence rather than client history, has no tools and no hosted fallback. claude-local registers it only on explicit opt-in because of the shared inference slot.

ailocal registers the component in the client roots it owns and in VS Code's user `mcp.json`, beside the provider configuration it already manages there, without replacing a foreign entry. Hosted Claude and Codex registration belongs to Cadence.

## Alternatives and consequences

Native search plus a Fetch MCP leaves provenance, site scope and synthesis to each caller. The closest reviewed research MCP documents best-effort site constraints and direct Ollama synthesis ([tool reference](https://github.com/TadMSTR/searxng-mcp/blob/main/docs/tools.md)). Keeping `braveapi` in SearXNG behind a counter cannot see quota headers and leaves LiteLLM's interception uncounted; patching the engine forks upstream; a per-process counter cannot bound concurrent sessions.

ailocal therefore owns fetch safety, cache correctness and citation-quality checks; a valid source ID does not show that a source supports a claim. The ledger is local: other machines or tools sharing the key are not counted, and Brave's dashboard remains the authoritative account view. General web search in claude-local and codex-local is keyless.

Reopen this decision if a supported component satisfies the evidence contract, if SearXNG exposes provider response headers, if Brave publishes a usage API, or if local inference scheduling makes the `answer` restriction unnecessary.
