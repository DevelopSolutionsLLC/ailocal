# ADR 010 — SearXNG for web search

**Status:** Accepted · **Date:** 2026-07

## Problem

Local models have a training cutoff and no internet. Web lookup must work
without sending queries to a commercial API.

## Decision

SearXNG container on `127.0.0.1:8080`, registered with LiteLLM as the
`searxng-search` tool. LiteLLM rewrites a client's `web_search` tool into a call
against it.

## Engine policy — selection is the lever

`keep_only` is applied to SearXNG's default engine list **before** our `engines:`
block merges onto it, so the whitelist decides what exists at all. This replaced
a `disabled: true` approach that did not work: `use_default_settings: true`
*loads* every default engine, and some fail at **init** time regardless of
`disabled` (wikidata 403s on every boot; ahmia and torch fail to import).

Current set: `google cse`, `duckduckgo`, `github`, `stackoverflow`, `wikipedia`.

Deliberately **not** solved with Redis, a proxy pool, or a FlareSolverr-style
browser: SearXNG is an aggregator, so engine selection is the correct lever and
it keeps the container at ~167 MiB.

## Engine availability drifts — re-measure, never trust the list

Two exclusions were **reversed by measurement** in one session:

- **mojeek removed.** It was the designated google-cse fallback and was measured
  dead from this network: HTTP 403 on 3/3 direct requests, "Suspended: access
  denied" with 0 results via a `!mjk` bang. It 403'd on the first query of every
  fresh container and produced only error noise.
- **duckduckgo added** in its place — previously excluded for CAPTCHA, now
  serving 2 of 3 queries. Strictly better than the 0/3 it replaced, and SearXNG
  degrades cleanly (44 results from the rest when ddg CAPTCHAs).
- **qwant stays excluded.** Its CAPTCHA lines in older logs *predate* the
  `keep_only` list — it is no longer loaded and cannot fail. **Check timestamps
  before acting on log lines.** A "stale" verdict was itself wrong once: mojeek
  was judged stale from a 6-hour window in which it simply was not queried.

## Log noise: fixed by documenting, not by adding machinery

`limiter.toml` exists **solely** to quiet bot-detection startup logging. It does
not enable the limiter (`server.limiter: false` governs that). Use
`botdetection.trusted_proxies` — the deprecated `[real_ip] x_for` loads but emits
two deprecation ERRORs, trading one piece of noise for two.

One line remains and is expected:
`ERROR:searx.botdetection: X-Forwarded-For nor X-Real-IP header is set!` — it
fires **once per container start, never per request** (measured: 8 searches
produced zero more) and blocks nothing, because the limiter is off. Do **not**
inject forwarded headers from LiteLLM to silence it; that turns on machinery a
private, loopback-bound instance does not need.

Net: five error/warning lines on a fresh container down to one.

## Measurements

- 49–70 results per query; reachable from inside LiteLLM as `http://searxng:8080`.
- `/v1/search` through `searxng-search` returns results (validate-deployment.sh).

## Known limitations

- **Claude Code's native `WebSearch` never reaches SearXNG.** It is a
  *client-side* tool. LiteLLM's interception accepts only `litellm_web_search`
  and bare `web_search`, and deliberately refuses a `WebSearch` carrying an
  `input_schema` so it cannot hijack the client's own handler.
- `websearch_interception_params.enabled_providers` matches the **backend**
  provider (`ollama_chat`), not the inbound dialect. It once read `anthropic`
  and silently disabled interception entirely.
- `search_tools` **must be top-level** — nesting it under `litellm_settings`
  silently yields zero registered tools.
- **Interception is verified configured, not verified end to end**: the local
  model narrates instead of emitting a `web_search` tool_use, even under
  `tool_choice` forcing. SearXNG is proven healthy; the unproven link is model
  tool emission.

## Revisit if

- An engine dies or recovers (re-measure; this file is a snapshot).
- A local model becomes reliable at emitting `web_search`.

## Deeper reference

- `deploy/searxng/settings.yml` — the engine list with per-engine measurements.
- `deploy/searxng/limiter.toml` — why it exists and what it does not do.
