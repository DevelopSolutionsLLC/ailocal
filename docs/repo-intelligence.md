# Repository intelligence: which tool for which question

Four retrieval mechanisms are available. They are not interchangeable, and the
difference is *what you already know* when you ask.

## The boundary, with measured examples

| You know | Use | Returns | Evidence |
|---|---|---|---|
| the exact symbol name | **LSP** | precise location, refs, diagnostics | `ToolGateway` → `tool_gateway.py:383` |
| the concept, not the name | **grepai** | ranked chunks by meaning | "tool gateway capability negotiation" → `tool_gateway.py:432`, `capability_registry.py:237` (0.62–0.65) |
| an exact string | **grep/Glob** | literal matches | fastest when the string is known |
| a natural-language question about docs | **discover.py** | budgeted doc sections | see `scripts/discover.py` |

**LSP answers "where is this defined."** It needs the symbol name and gives an
exact answer with no ranking — a fact, not a guess.

**grepai answers "what code is about this."** It needs no name and returns
similarity-ranked chunks. It found the negotiation logic from a description that
appears nowhere verbatim in the code.

They do not overlap. Neither replaces the other, and adding one on top of the
other buys nothing.

## grepai: use the MCP, not the CLI

**Measured, and this is the trap:**

```
grepai search "tool gateway negotiation"        -> No results found
grepai_search (MCP, workspace=cadence)          -> 4 correct hits, 0.62-0.65
```

Same query, same repo, opposite outcomes. The CLI reads the deprecated local
`index.gob`; the MCP reads the shared Qdrant workspace index, which is the
authoritative store. **A CLI "no results" is not evidence that code does not
exist.**

## Index health [REAL]

```
workspace  cadence          provider ollama / nomic-embed-text
  cadence         symbols_ready: true    17 symbols
  ailocal         symbols_ready: true   170 symbols
  pawsome-pals    symbols_ready: true    33 symbols
```

`symbols_ready: true` with a non-zero count is what makes call-graph tracing
meaningful. A prose-heavy repo legitimately reports 0 — that is correct, not a
fault.

## Client availability

| | LSP | grepai |
|---|---|---|
| **Claude Code** | YES (20 tools) | YES |
| **Codex** | NO — [codex#20652](https://github.com/openai/codex/issues/20652) | NO — same cause |
| **VS Code** | editor's own | not wired |

Both depend on MCP reaching the model, so Codex loses both for the same reason:
LiteLLM drops `namespace` bundles, and flattened names are rejected by Codex's
router.

## Deliberately not added

**No separate vector RAG layer.** grepai already *is* the vector search
(nomic-embed-text into Qdrant). A second embedding store over the same corpus
would add a cache to invalidate and a ranking to reconcile, for no capability
the current path lacks.
