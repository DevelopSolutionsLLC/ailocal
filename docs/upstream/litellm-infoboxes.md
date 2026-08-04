# DRAFT — not yet filed

Proposed LiteLLM issue. **Review before publishing.** Post with:

```
gh issue create --repo BerriAI/litellm \
  --title "[Bug]: SearXNG search provider drops infoboxes[] and answers[], so Wikipedia returns zero results" \
  --body-file docs/upstream/litellm-infoboxes.md
```

(Strip this header block before posting.)

---

**Title:** `[Bug]: SearXNG search provider drops infoboxes[] and answers[], so Wikipedia returns zero results`

## What happened

`SearXNGSearchConfig.transform_search_response()` reads only
`response_json.get("results", [])`. SearXNG returns several engines' content in
other top-level arrays — most importantly `infoboxes[]` (Wikipedia, Wikidata)
and `answers[]` (calculators, DNS/IP lookups, unit conversion).

For those engines LiteLLM returns an **empty** `SearchResponse` even though
SearXNG answered correctly. There is no error and no warning: the search
succeeds and the model receives nothing.

The practical effect is that **Wikipedia contributes no content to web search
through LiteLLM**, which disproportionately affects general-knowledge queries —
exactly the queries a self-hosted SearXNG instance depends on Wikipedia for.

## Minimal reproduction

SearXNG (any recent version), queried directly:

```bash
curl -s 'http://searxng:8080/search?q=!wikipedia+Albert+Einstein&format=json' \
  | jq '{results: (.results|length), infoboxes: (.infoboxes|length)}'
```

```json
{
  "results": 0,
  "infoboxes": 1
}
```

The infobox contains exactly the content a caller wants:

```json
{
  "engine": "wikipedia",
  "content": "Albert Einstein was a German-born theoretical physicist best known for developing the theory of relativity...",
  "urls": [{"url": "https://en.wikipedia.org/wiki/Albert_Einstein"}]
}
```

Now the same query through LiteLLM:

```yaml
search_tools:
  - search_tool_name: searxng-search
    litellm_params:
      search_provider: searxng
      api_base: http://searxng:8080
```

```python
import litellm
r = await litellm.asearch(query="!wikipedia Albert Einstein",
                          search_provider="searxng")
print(len(r.results))   # 0
```

Reproduced on non-technical queries too — `France`, `photosynthesis` — so this
is not a query-topic effect. Every one returns `results: 0, infoboxes: 1`.

## Where

`litellm/llms/searxng/search/transformation.py`, in
`transform_search_response()`:

```python
results = []
for result in response_json.get("results", []):
    ...
    results.append(search_result)
return SearchResponse(results=results, ...)
```

`infoboxes` and `answers` are never read.

## Expected behaviour

Include infobox and answer content in `SearchResponse.results`, mapping:

| SearXNG infobox field | `SearchResult` field |
|---|---|
| `infobox` (title) | `title` |
| `content` | `snippet` |
| `urls[0].url` (or `id`) | `url` |

`answers[]` entries carry `answer` and sometimes `url`, and could map similarly.

Appending them after `results[]` would preserve existing ordering and be
backwards-compatible for callers that already get results today. Sorting is not
required to fix the bug.

## Environment

- LiteLLM `1.93.0` (proxy, Docker `ghcr.io/berriai/litellm`)
- SearXNG (Docker `searxng/searxng`), JSON format enabled
- Search provider: `searxng` via `search_tools`
- Also reached through `websearch_interception` with a local `ollama_chat`
  backend, though the bug is in the SearXNG provider and is independent of that
  integration.

## Notes

Happy to open a PR if the proposed mapping looks right — mainly want to confirm
whether infobox/answer content is intended to appear in `SearchResponse.results`
or whether `SearchResponse` should grow a separate field for it.
