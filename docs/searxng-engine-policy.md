# SearXNG engine policy measurement

[REAL] Measured on 2026-10-18 through the live `127.0.0.1:8080/search?format=json` endpoint, before and after installing the issue #8 change and restarting ailocal. The image remained pinned at `2026.10.17-1c10abe2d`. This is a six-query smoke measurement, with relevance judged manually from the top three titles, not a broad search-quality benchmark. Results and provider availability can change.

LiteLLM sends no category, so SearXNG selects `general`. Developer registries previously participated in that category and displaced web results. Registries now use `it`, literature sources use `science`, and default web search uses Yahoo and Yep. Brave was removed from SearXNG by [ADR 015](adr/015-research-routing-and-brave-budget.md); the tables below record the measurement as taken. Wikipedia and Wiby remain available by bang. GitHub code search is inactive when its optional token is absent.

## Before

| Query | Results | Contributing engines (attributed result counts) | Unresponsive engines | Top three titles |
|---|---:|---|---|---|
| best budget mechanical keyboard 2026 | 69 | Brave 18, Docker Hub 10, GitHub 1, MDN 10, arXiv 10, Crossref 20 | askubuntu, stackoverflow, superuser: HTTP error | The 4 Best Budget Mechanical Keyboards of 2026 - RTINGS.com; r/keyboards on Reddit: Best Gaming Keyboard For The Money In 2026? (Price, Build Quality); Best Gaming Keyboards 2026: We've Tested Full-size, TKL, Mini, and more \| Tom's Hardware |
| how long to boil an egg soft | 70 | Brave 20, MDN 10, Docker Hub 10, arXiv 10, Crossref 20 | askubuntu, stackoverflow, superuser: suspended HTTP error | How to; How to; How to |
| ollama vs lm studio reddit opinions | 66 | Brave 20, MDN 10, Docker Hub 10, arXiv 10, Crossref 16 | askubuntu, stackoverflow, superuser: suspended HTTP error | LM Studio; What is LM Studio?; Forensic Implications of Localized AI: Artifact Analysis of Ollama, LM Studio, and llama.cpp |
| python asyncio TaskGroup cancel on first exception | 70 | Brave 20, MDN 10, Docker Hub 10, arXiv 10, Crossref 20 | askubuntu, stackoverflow, superuser: suspended HTTP error | Python; Exception; TaskController |
| litellm cancel_on_disconnect | 50 | Brave 20, MDN 10, Docker Hub 10, arXiv 10 | askubuntu, stackoverflow, superuser: suspended HTTP error; Crossref: too many requests | USB: disconnect event; downloads.cancel(); HID: disconnect event |
| rust borrow checker cannot move out of borrowed content | 50 | Brave 20, MDN 10, Docker Hub 10, arXiv 10 | askubuntu, stackoverflow, superuser: suspended HTTP error; Crossref: suspended too many requests | In flow and out of flow; Out-of-order patching; Compiling from Rust to WebAssembly |

One of six queries had relevant top results in this fresh baseline. The earlier handoff's throwaway-container comparison reported zero of six for its baseline; these are different observations.

## After

All six queries had relevant top results and no unresponsive engines. Each count below comes from results whose `engines` field names the provider. A merged result may name multiple engines, so provider counts need not sum to the unique result count.

| Query | Results | Brave / Yep / Yahoo attributed counts | Top three titles |
|---|---:|---|---|
| best budget mechanical keyboard 2026 | 45 | 18 / 20 / 7 | The 4 Best Budget Mechanical Keyboards of 2026 - RTINGS.com; r/keyboards on Reddit: Best Gaming Keyboard For The Money In 2026? (Price, Build Quality); Best Gaming Keyboards 2026: We've Tested Full-size, TKL, Mini, and more \| Tom's Hardware |
| how long to boil an egg soft | 44 | 20 / 20 / 7 | How to Soft Boil an Egg (Perfectly Runny or Jammy Every Time); How To Make Perfect Soft-Boiled Eggs \| Food Network; How to Soft Boil an Egg |
| ollama vs lm studio reddit opinions | 46 | 20 / 20 / 7 | LM Studio vs Ollama 2026: 3 Shifts That Change Everything; Why ollama faster than LMStudio? : r/LocalLLaMA - Reddit; LM Studio vs Ollama: Performance, Features & Which to Choose (2026) \| Blog |
| python asyncio TaskGroup cancel on first exception | 43 | 20 / 20 / 7 | python - How to cancel all tasks in a TaskGroup - Stack Overflow; Python Concurrency: Making sense of asyncio; Python Asyncio Task Cancellation - Stack Overflow |
| litellm cancel_on_disconnect | 46 | 20 / 20 / 7 | /responses \| liteLLM; [Feature]: Cancel LLM stream response on client disconnect; [Feature]: cancel backend llm api calls, when client-side ... |
| rust borrow checker cannot move out of borrowed content | 46 | 20 / 20 / 7 | Cannot move out of borrowed content / cannot move out of behind a shared reference; How to Fix 'Cannot move out of borrowed content' Errors; The borrow checker - Rust Compiler Development Guide |

[REAL] `!cb searxng` returned 10 results attributed to Codeberg, with no unresponsive engines, after increasing its timeout to 8 seconds. Its top three titles were `searxng/searxng`, `chestso/nevermore`, and `vojkovic/searxng`. Before this change, Codeberg was not configured; its bang returned zero Codeberg-attributed results even though SearXNG fell back to other engines.

[UNVERIFIED] Authenticated GitHub code search remains unmeasured because no dedicated `GITHUB_SEARCH_TOKEN` is configured. Isolated render tests use synthetic credentials; they do not prove GitHub accepts a token.

[REAL] `!gh tokio` returned 30 GitHub-attributed results and no unresponsive engines. Its top three titles were `tokio-rs/tokio`, `SeaQL/sea-orm`, and `telemt/telemt`, confirming that coding engines remain reachable explicitly.

## Proxy and local model

[REAL] An Anthropic `/v1/messages` request to `127.0.0.1:4000`, using `ailocal-fast` and `web_search_20250305`, returned general-web search snippets including Serious Eats, RecipeTin Eats and The Kitchn. Interception returned snippets with zero model tokens. A second conversation turn supplied those returned snippets to the same endpoint without another search tool, and the local model produced an answer citing RecipeTin Eats and The Kitchn by name (4,425 input tokens, 113 output tokens). It omitted the requested URL links. This proves retrieval followed by local synthesis in two API calls; it does not measure an interactive Claude Code session.
