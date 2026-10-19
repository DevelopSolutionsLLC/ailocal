# ADR 015: Research routes itself, and Brave is spent through one budgeted path

**Status:** Accepted · **Date:** 2026-10-18 · **Issue:** [#13](https://github.com/DevelopSolutionsLLC/ailocal/issues/13) · **Revises:** [ADR 014](014-local-research.md)'s Brave clause

## Context

ADR 014 spent Brave only through an explicit `forums` or `brave` preset. A request that never named a preset therefore received no Brave coverage even when keyless results were thin, and Brave use depended on the user knowing the preset vocabulary. Operators need Brave to be available automatically, but bounded by an allowance they set per billing period, with no per-search approval and no overage.

An allowance can only be enforced where requests are counted. SearXNG's `braveapi` engine discards Brave's response headers, so neither the SearXNG path nor LiteLLM's WebSearch interception (which queries SearXNG's `general` category) could observe or bound spending. Two uncounted paths reached the same key.

Brave's [rate-limiting guide](https://api-dashboard.search.brave.com/documentation/guides/rate-limiting) (read 2026-10-18) states that `X-RateLimit-Limit`, `-Policy`, `-Remaining` and `-Reset` carry one comma-separated value per window, that a monthly quota of 0 means unlimited, and that only successful requests are counted and billed. It documents a 429 for exceeding a limit and no account-usage API. [REAL] A probe of this account's key returned `Limit: 50, 0`, `Policy: 50;w=1, 0;w=2678400`: no monthly cap is reported, so account-wide remaining allowance is not observable from responses.

## Decision

Brave is spent only by the research MCP, directly, behind a cross-process SQLite ledger in the state root. `braveapi` is removed from SearXNG, which closes the uncounted path and takes the key out of the rendered settings.

- Every Brave request is reserved under `BEGIN IMMEDIATE` before it is sent and settled from its outcome: billed (2xx), not billed (error status), or unknown (response lost, counted conservatively). Retries are not attempted.
- The operator's budget (`AILOCAL_BRAVE_BUDGET`, default 1,000) is counted per provider period taken from the headers. Without a current observation the period is unknown, counted over the longest possible window, and only one request may establish it at a time. A billed response without headers stops further spending until a period is known.
- A 429 or 402 other than `RATE_LIMITED` stops spending until the observed reset. Distinguishing the two relies on the error body's code; the guide documents only the status.
- Reports always separate the operator's budget as counted by ailocal on this machine from the account-wide allowance, which is a number only when Brave reports a monthly cap and otherwise `unknown`. Each report carries its observation time. Other consumers of the key are invisible here.
- The server plans its own providers: specialist engines plus keyless Yahoo and Yep, site narrowing across the forum communities, one keyword reformulation for AND-only APIs, and a Brave fill (at most two requests per call) when keyless coverage is thin. A caller may request Brave from the start or forbid it. Fan-out, concurrency, elapsed time and session spend are bounded. An identical search repeated within a session is served once more with a warning and then refused, so a looping caller receives a definite signal rather than the same cached result indefinitely.
- Routing is carried by the tool description, server instructions and the skill, so a natural request reaches `search` without naming it. `answer` stays keyless.

## Alternatives and consequences

Keeping `braveapi` in SearXNG with a counter around it cannot see quota headers and would still leave LiteLLM's interception uncounted. Patching the SearXNG engine forks upstream code. A per-process counter cannot bound concurrent sessions.

The ledger is local: usage by other machines or tools sharing the key is not counted, and the account-wide figure is only as fresh as its observation. Brave's dashboard remains the authoritative account view. General web search in claude-local and codex-local is now keyless only.

Reopen this decision if SearXNG exposes provider response headers, or Brave publishes a usage API.
