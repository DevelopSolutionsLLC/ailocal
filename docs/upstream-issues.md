# Upstream limitations

Defects and constraints in third-party components — LiteLLM, SearXNG, upstream
search engines — that shape how search behaves here. **None of these are ailocal
defects, and none should be "fixed" by adding code to this repository.** They are
recorded so a future session recognises a known constraint instead of
rediscovering it and building a workaround around it.

The rule that produced this file: prefer upstream configuration over custom code,
and carry a documented upstream limitation rather than a fork.

---

## Search: why general-knowledge queries are weak

Three independent causes, isolated by measurement on 2026-08-03. They compound,
which is why fixing any one alone would not have made general search good.

### 1. The engine set is deliberately coding-first

`deploy/searxng/settings.yml` disables the scraped general-web engines
(`google cse`, `duckduckgo`) by default. This is an intentional, measured
decision, not an oversight — see the rationale in that file. The result is that
the API-backed coding engines supply nearly all ranking, including on questions
they know nothing about.

Measured: `latest stable Python release version` returned 44 results, top five
being an MDN Python glossary entry, a yt-dlp Superuser thread, an Ask Ubuntu
Jupyter question, a CentOS nginx question, and Firefox 153 release notes. Zero
addressed the question.

### 2. LiteLLM sends the wrong text as the search query — [#31902][31902]

The `websearch_interception` short-circuit uses **the last user message** as the
search query rather than the model's `web_search` tool-call query. A
conversational question is therefore submitted verbatim to the search engine.

Measured here: asking *"What is the latest stable Python release version?"*
returned MDN's *"What is a web server"*, *"What is a URL"*, *"What is a
progressive web app"* — the engines matched on **"What is"**. This degrades
results for *any* engine, so it would also degrade a future Brave or Kagi
integration.

Status: open upstream. **Do not work around this locally.**

### 3. LiteLLM discards `infoboxes[]` — issue not yet filed

Wikipedia answers through SearXNG's `infoboxes[]` array, not `results[]`.
LiteLLM's `transform_search_response()` iterates only
`response_json.get("results", [])`, so Wikipedia content never becomes
model-visible.

```
!wikipedia Albert Einstein  ->  results: 0    infoboxes: 1
    content: "Albert Einstein was a German-born theoretical physicist…"
    url:     https://en.wikipedia.org/wiki/Albert_Einstein
```

Same on `France` and `photosynthesis`, so this is not a technical-query effect.
Wikipedia is kept enabled anyway: it costs one API-backed request and starts
working the moment upstream reads infoboxes.

**Consequence: the one enabled engine that reliably answers general-knowledge
questions cannot reach the model at all.** This is why adding more general
engines is not a fix on its own.

Draft issue awaiting review: [`docs/upstream/litellm-infoboxes.md`](upstream/litellm-infoboxes.md)

---

## "0 searches" is a counter artifact, not a failed search

Claude Code may report **0 searches** — `usage.server_tool_use.web_search_requests
= 0` — while the model has in fact received a full result set. **A zero counter
here is expected and is not evidence of zero retrieved results.**

That field counts **Anthropic-hosted, server-side** web search only. Under
ailocal the search is executed by LiteLLM's `websearch_interception` and comes
back as an ordinary `tool_result` block, so Anthropic's servers never run a
search and the counter is structurally always zero.

Traced end-to-end on 2026-08-03 with the query `python asyncio semaphore`:

| Boundary | Observed |
|---|---|
| Claude Code tool call | `{"name":"WebSearch","input":{"query":"python asyncio semaphore"}}` |
| SearXNG JSON | **75 results** — crossref 20, github 15, stackoverflow 10, mdn 10, docker hub 10, arxiv 10 |
| Claude Code `tool_result` | **22,033 chars, 50 `Title:`, 50 `URL:`** |
| `usage.server_tool_use.web_search_requests` | **0** |

The string `0 results` / `no results` appears nowhere in the response stream —
the only zero anywhere in the trace is that counter.

**Authoritative success evidence is the `WebSearch` tool call plus a non-empty
`tool_result`, never the counter.** Do not attempt to alter Claude Code's
counter, and do not treat it as a search failure.

Related upstream: [#31902][31902], whose title records the same symptom
("Claude Code returns 'Did 0 searches'").

### Root cause — a dropped content block, NOT the usage counter

An earlier revision of this document blamed `usage.server_tool_use.web_search_requests`.
**That was wrong and is corrected here.** Claude Code counts **`server_tool_use`
content blocks**, as upstream states in [#31687][31687]:

> "Anthropic-native clients require both blocks to render citation cards.
> Without the `server_tool_use` block, clients report 'Did 0 searches' even
> though the search ran and results were returned."

**Claude Code sends two request shapes** (captured on the wire, 2026-08-03):

| Request | Tools | WebSearch declaration |
|---|---|---|
| main conversation | 41 | `name="WebSearch"`, no `type` — plain function tool |
| standalone sub-request | 1 | **`name="web_search"`, `type="web_search_20250305"`** — Anthropic-native |

So the native contract *is* used, and LiteLLM's detector agrees — tested
directly in-container, `is_anthropic_native_web_search_tool(...)` returns
**True** for that exact tool. `handler.py` then correctly builds both blocks on
the short-circuit path.

**They are dropped at response serialization.** In `litellm/types/llms/anthropic.py`
the `AnthropicResponse` content union admits only:

```
AnthropicResponseContentBlockText | ...ToolUse | ...Thinking | ...RedactedThinking
```

`grep -c server_tool_use` on that file returns **0**. The blocks are built, fail
to match the union, and never reach the client.

**First failing boundary: Anthropic response model validation. Owner: LiteLLM.**
Not Claude Code, not the Anthropic API, not the interception logic.

### Upstream status — NOT fixed as of v1.95.0

Verified by isolated container test on 2026-08-03 (v1.95.0, digest
`sha256:af806882b7a6…`), replaying the captured native request against both
versions:

| | 1.93.0 (prod) | 1.95.0 (isolated) |
|---|---|---|
| content types | `['text']` | `['text']` |
| `server_tool_use` | 0 | **0** |
| `web_search_tool_result` | 0 | **0** |

**Identical. Upgrading does not fix this.** The relevant upstream PRs are all
still open — [#31209][31209] (the exact missing block types), [#31672][31672],
[#32196][32196], [#31182][31182] — and [#31687][31687] was closed unmerged.

Production therefore stays on 1.93.0. Re-test when #31209 lands; do not upgrade
for this reason alone, and validate Codex-local streaming on any upgrade (1.94.1
was reverted here for breaking it).

[31182]: https://github.com/BerriAI/litellm/pull/31182
[31209]: https://github.com/BerriAI/litellm/pull/31209
[31672]: https://github.com/BerriAI/litellm/pull/31672
[31687]: https://github.com/BerriAI/litellm/pull/31687
[32196]: https://github.com/BerriAI/litellm/pull/32196

---

## Search profiles are not achievable in configuration

`websearch_interception` binds **one** search tool at initialisation:

```python
# litellm/integrations/websearch_interception/handler.py
def _select_search_tool_from_list(self, search_tools, source):
    if self.search_tool_name:
        matching_tools = [t for t in search_tools
                          if t.get("search_tool_name") == self.search_tool_name]
        if matching_tools:
            return matching_tools[0]     # always the same tool
    if search_tools:
        return search_tools[0]           # or always the first
```

There is no per-request, per-model, or model-driven selection.

**Therefore:**

- Additional `search_tools:` entries in `config/litellm/config.yaml` are **dead
  config** — only the named (or first) one is ever used.
- Developer / general / research *profiles* cannot be expressed here.
- A **typo** in `websearch_interception_params.search_tool_name` silently falls
  back to `search_tools[0]` rather than erroring. Check that key by hand.

What *does* work: the SearXNG transformation forwards unrecognised params
verbatim (`transformation.py`), so `engines:` or `categories:` on the single
configured tool sets **one global profile**. Bangs (`!arx`) also survive to
SearXNG, but depend on the model emitting them.

Revisit weighting only if upstream gains dynamic search-tool selection.

---

## Other LiteLLM search issues affecting this stack

| Issue | Summary | Relevance |
|---|---|---|
| [#31902][31902] | Short-circuit uses last user message as the search query | **Active cause of poor results here** |
| [#26163][26163] | Anthropic follow-up passes duplicate Claude Code params (`context_management`, `output_config`) | Claude Code + web search |
| [#30822][30822] | `tool_choice` not converted, so forced `web_search` fails on Bedrock | Not hit here (ollama_chat), watch on provider change |

[31902]: https://github.com/BerriAI/litellm/issues/31902
[26163]: https://github.com/BerriAI/litellm/issues/26163
[30822]: https://github.com/BerriAI/litellm/issues/30822

---

## Engine reliability is runtime data, not a constant

Engine availability drifts, and the comments in `settings.yml` are dated
observations rather than standing truth. Two measured reversals:

| Engine | 2026-07-28 | 2026-08-03 |
|---|---|---|
| `google cse` | 0% — "Suspended: too many requests" | **3/3 healthy, 20 results** (low volume) |
| `crossref` | not enabled | healthy, then **"Suspended: too many requests"** after repeated probing |

Both are true. The variable is **request volume**, not time — which is also why
`disabled: true` works as a demand-reduction mechanism: a bang-only engine
accumulates almost no volume and recovers.

Re-measure with:

```
scripts/diag-search-engines.py            # all engines
scripts/diag-search-engines.py --repeat 3 # sample CAPTCHA/rate-limit behaviour
```

That tool is **diagnostic only** — it never enables or disables anything. It also
consumes rate limit: it rate-limited crossref during its own first run. Record
its output and the date next to any enable/disable decision in `settings.yml`.

---

## What is deliberately not done

- **No CAPTCHA/Cloudflare bypass.** FlareSolverr targets Cloudflare; the actual
  failures here are Google volume suspension and DuckDuckGo CAPTCHA, neither of
  which is Cloudflare. It would add a brittle service without addressing them.
- **No residential proxies or CAPTCHA-solving services.** Recurring cost,
  operational complexity, and upstream ToS problems.
- **No browser automation.**
- **No LiteLLM fork or patch.** Carry the documented limitation instead.
- **No custom search routing or middleware.** If upstream cannot select tools
  per request, neither will this repository.
