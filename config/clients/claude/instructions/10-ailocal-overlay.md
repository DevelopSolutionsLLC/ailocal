# ailocal local profile

This session runs in the ailocal Claude Code configuration root
(`CLAUDE_CONFIG_DIR=~/.config/ailocal/claude`). It is a separate root from the hosted default
`~/.claude` and inherits nothing from it — every rule this profile follows is in this file.

## Model routing

Model inference is routed through the local LiteLLM proxy at `http://localhost:4000`, which serves
Ollama models running on this machine. This describes inference only: it is not a claim that the
session makes no network calls, since tools, MCP servers, and web search reach the network
independently.

Capabilities are the canonical, authoritative names:

<!-- ailocal:capabilities -->

Claude-style model names are compatibility aliases mapped onto those capabilities:

<!-- ailocal:claude-slots -->

Do not infer Anthropic cloud-model behavior, context window, or reasoning ability from an alias
name — the alias says nothing about the backend. When the actual backend matters, read it from the
generated configuration or from runtime evidence rather than assuming.

## Tools

Native LSP is a Claude Code client-side tool. It is not an MCP server, and the LSP protocol itself
does not pass through LiteLLM — only the tool schema does. MCP-delivered language-server tools are
named `mcp__lsp__*`, and grepai's tools use their own MCP names; these are distinct systems from
native LSP and from `Grep`/`Glob`.

A tool being visible in the session is not evidence that invoking it succeeds. Current status:

- Hosted Claude Code, native LSP — working.
- This ailocal profile, native LSP — tool is present, invocation currently fails. `[UNVERIFIED]` as a working path; do not rely on it without probing first.
- `codex-local` MCP LSP bridge — configured but blocked by namespace dispatch (upstream client limitation).

If a preferred discovery tool fails, say so before falling back, per the no-silent-fallback rule.

## Delegation on local models

Local backends are slower than hosted ones, so a subagent is a real cost — delegate exploration and
verbose tooling, not one-line lookups. The deployed agents under `agents/` (`search`, `tester`,
`planner`, `implementer`, `reviewer`) each bind a capability. Any capability may drive the main
session; none of them is restricted to subagent use.

Context limits differ per capability and change with the active profile. Read the limit from the
capability registry or the running proxy instead of assuming a number.

## Runtime

- Health check: `curl -s http://localhost:4000/health/liveliness`
- Restart after a config change: `./scripts/start.sh` from the ailocal repo root. There is no root compose file — the stack is assembled from `deploy/` by `scripts/lib/compose.sh`, so a bare `docker compose` in the root finds nothing.
- List served models (the endpoint requires auth; the wrapper exports the key into this session): `curl -s -H "Authorization: Bearer $ANTHROPIC_API_KEY" http://localhost:4000/v1/models | jq '.data[].id'`

The proxy reads its config only at boot and the config is bind-mounted, so editing it changes
nothing until the container restarts.

## Git identity

Commits in this repository use Victor T. Chevalier's identity, set as repository-local config. If
`git config user.email` is already correct, leave it alone — do not reconfigure it on every commit.
