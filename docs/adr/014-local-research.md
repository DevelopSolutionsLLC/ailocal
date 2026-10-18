# ADR 014: Local research has an explicit evidence and inference boundary

**Status:** Accepted · **Date:** 2026-10-18 · **Issue:** [#9](https://github.com/DevelopSolutionsLLC/ailocal/issues/9)

## Context

Search providers may ignore site filters, and some source pages cannot be read. Returning a cited answer must not imply that every source was fetched. Local synthesis also competes for the same Ollama slot when its caller is claude-local.

## Decision

Expose `search(query, sites?, preset?)` and optional `answer(question, sites?)` through a bundled MCP component, using [ADR 011's packaging and ownership model](011-bundled-artifacts.md). Enforce site scope on returned URLs and redirects, retain requested-engine attribution, and distinguish page text from indexed snippets in source records. Only public pages may be fetched.

Default search and all delegated answers use keyless engines. Brave use requires an explicit preset that discloses metered usage. Synthesis uses the configured local LiteLLM route, receives evidence rather than client history, and has no tools. There is no hosted fallback. claude-local exposes search by default; delegated answers require an explicit opt-in because of the shared inference slot.

ailocal registers only its own claude-local integration. Hosted client registration remains separately owned by Cadence.

## Alternatives and consequences

Native search plus a Fetch MCP leaves evidence provenance, enforced site scope and local synthesis to each caller. Existing research MCPs were considered: the closest reviewed server documents best-effort site constraints and direct Ollama synthesis ([tool reference](https://github.com/TadMSTR/searxng-mcp/blob/main/docs/tools.md)). The unsupported requirements that justify this adapter are enforced site scope, explicit provider-credit policy and synthesis through the local proxy.

This boundary makes evidence limitations visible and keeps hosted inference out of the component. It also means ailocal owns fetch safety, cache correctness and citation-quality validation. A valid source ID alone does not establish that a source supports an answer.

Reopen this decision if a supported platform or existing component satisfies the contract without these custom mechanisms, or if local inference scheduling makes the default answer restriction unnecessary.
