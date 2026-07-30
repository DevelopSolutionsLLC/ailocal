# ADR 007 — SearXNG for web search

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

---

## 2026-07-28 audit — engine reliability is the whole problem

Measured with a ten-query agent-style burst (script since removed; `doctor.sh` covers health).

| engine | run 1 | run 2 | kind |
|---|---|---|---|
| github | 100% | 20%* | API (unauthenticated) |
| stackoverflow | 100% | 100% | API |
| mdn / docker hub / pypi / npm / askubuntu / superuser | — | 100% | API |
| duckduckgo | 30% | **0%** | scraped — `CAPTCHA (wt-wt)` |
| google cse | 10% | **0%** | scraped — `Suspended: too many requests` |

\* github fell to 20% only after sustained measurement bursts exhausted the
unauthenticated API quota. **API-backed is not immunity** — it removes bot
detection, not rate limits. A token would raise the ceiling.

### Why scraped engines cannot be fixed by configuration

Upstream's own docs state that SearXNG "passes through requests from bots and is
thus classified as a bot itself". The CAPTCHA and 429s are upstream's judgement
of our OUTBOUND traffic. `trusted_proxies`, `X-Forwarded-For` and the limiter all
govern our bot detection of INBOUND requests — a different direction entirely.
No amount of header tuning touches this. The documented mitigations are a keyed
API, engine rotation, or more source IPs. **Engine selection is the lever we
actually have.**

### Decision: scraped general-web demoted to opt-in

`google cse` and `duckduckgo` are now `disabled: true`, which in SearXNG means
"not queried by default, still reachable by bang" (`!gcse`, `!ddg`) — the
"fallback only" shape.

Measured effect of the demotion:

| | before | after |
|---|---|---|
| mean results/query | 52.1 | 52.1 |
| zero-result queries | 0 | 0 |
| mean latency | 1.46 s | **0.73 s** |
| engine error lines / 10 queries | ~20 | **0** |

Identical result quality, half the latency, and the error storm gone. With BOTH
scraped engines at 0% the instance still answered every query — they were
contributing noise, not coverage.

Results per run vary (32.7–52.1 across runs) with which engines match; the stable
signal is **zero-result queries**, which is what the health check gates on.

### Rejected: Valkey

Asked for, and it is the wrong tool here. In SearXNG, Valkey backs the
**limiter and bot detection** (`limiter.py`, `botdetection/ip_limit.py`,
`botdetection/link_token.py`) — inbound abuse control this private instance
disables on purpose. The only cache module, `searx/cache.py`
(`ExpireCacheSQLite`), is a generic SQLite token store, **not a results cache**.
So Valkey would add a container, cache zero search results, and re-enable
machinery we deliberately turned off. If result caching is ever wanted it
belongs at the LiteLLM layer, not here — and the motivation (cutting upstream
volume) largely evaporated once engine selection fixed the rate limiting.

### Header/limiter messages: confirmed non-actionable

Upstream docs confirm `X-Forwarded-For`/`X-Real-IP` matter only behind a reverse
proxy. This instance is reached directly, and with `limiter: false` nothing is
rate-limited or blocked. The startup line is cosmetic, fires once per container
start, and must not be "fixed" by injecting headers from LiteLLM.

### Observability

The burst reported per-engine served/failed/rate, failure
reasons, results per query, zero-result count, latency, and the LiteLLM
`/v1/search` leg. Single-shot probes are misleading here — the failures are
volume-triggered, so health can only be judged from a burst.

### Standing rule

**Do not attempt CAPTCHA bypass, and do not make SearXNG impersonate a browser.**
Prefer resilient engine selection and graceful fallback. The CAPTCHAs are
upstream's correct judgement of a scraper; defeating them is both fragile and
the wrong direction. The supported levers are: API-backed engines, a keyed API
where one exists, engine rotation, and querying less.

### Result size: no cap is available (upstream)

A search returns ~62 results / 17,939 B / **~4,484 tokens**. Since prompt eval
dominates latency (ADR 010), that is ~7.5 s of prompt eval per search, and it
persists in context for later turns. Capping would be a genuine win — and it
cannot be done from our side:

- LiteLLM's SearXNG transformer accepts `max_results` and explicitly discards it
  (`transformation.py`: *"we'll ignore this and let SearXNG return its default
  results"*). Measured first: setting `max_results: 10` changed the response by
  zero bytes.
- An `engines:` passthrough also changed nothing.
- SearXNG has no per-query result limit either.

The working lever is the **engine set** in `deploy/searxng/settings.yml` —
result count is a function of how many engines answer. Re-check the LiteLLM
transformer after an upgrade rather than re-adding dead config.

### Latency: search is NOT the bottleneck

Direct SearXNG 0.55–0.98 s mean; LiteLLM → SearXNG adds no measurable penalty.
Against ~27 s of prompt eval for a 16 K prompt, search transport is noise. The
part of search that *does* cost is the **result payload** entering the prompt
(~4,484 tokens ≈ 7.5 s), not the query itself. Optimise bytes returned, not
query speed.

### Validation

```bash
./scripts/doctor.sh                 # includes SearXNG health
```

Single-shot probes are misleading — the failures are volume-triggered. The
regression signal is **zero-result queries**, not any individual engine at 0%.
