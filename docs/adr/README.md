# Architecture Decision Records

Why each major piece of this stack is the way it is — written so that six months
from now the reasoning survives, not just the result.

**Reading rule.** Every ADR separates what was **measured** from what was
**assumed**. Where documentation and measured behaviour disagree, both are
recorded and the measurement wins as operational truth. Each one ends with the
conditions that would make us revisit it; if a trigger fires, the ADR is stale
regardless of how confident it sounds.

| # | Decision | Status |
|---|---|---|
| [001](001-litellm-routing.md) | LiteLLM as the single routing layer | Accepted |
| [002](002-capability-names.md) | Capability names, never model tags | Accepted |
| [003](003-persona-injection.md) | Server-side persona injection | Accepted |
| [004](004-tool-gateway.md) | Tool gateway + task classification | Accepted |
| [005](005-model-hierarchy.md) | Five-tier model hierarchy | Accepted |
| [006](006-delegation.md) | Subagent delegation | Accepted |
| [007](007-mcp-architecture.md) | MCP registration fan-out | Accepted |
| [008](008-lsp-bridge.md) | LSP via the mcpls bridge | Accepted |
| [009](009-grepai-qdrant.md) | grepai + Qdrant for repo intelligence | Accepted |
| [010](010-searxng.md) | SearXNG for web search | Accepted |
| [011](011-local-vs-hosted.md) | Local and hosted side by side | Accepted |
| [012](012-client-support.md) | Per-client support levels | Accepted |

## The rule that produced most of these

Presence is not capability. A configured MCP server, an enrolled repo, a written
plist and a listed model all say nothing about whether the thing answers. Nearly
every ADR here exists because something was configured correctly and still did
not work — and the failure was silent, because the layer that broke returned an
empty result rather than an error.

Empty is not absent. That single confusion accounts for more wasted time in this
project's history than any genuine bug.
