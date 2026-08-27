# claude-local

`claude-local` is Claude Code, unchanged, pointed at your machine. It is a shell function
that sets `CLAUDE_CONFIG_DIR` and a handful of environment variables for **that process
only**, then runs the same `claude` binary.

Your hosted `claude` is untouched. `~/.claude` is never read for this and never written by
ailocal; everything below lives in ailocal's own config root.

## How it differs from hosted `claude`

| | hosted `claude` | `claude-local` |
|---|---|---|
| Inference | Anthropic | LiteLLM → Ollama on `127.0.0.1` |
| Context | model's full window | the profile's `num_ctx` (1M-context beta disabled) |
| Tool discovery | eager | **MCP tool search on** |
| Native Workflow | on | **off**, with an escape hatch |
| Artifacts | hosted `Artifact` → claude.ai | **bundled local artifact MCP** → `127.0.0.1` |
| Web search | Anthropic | ailocal's own SearXNG |

## Tool search

`ENABLE_TOOL_SEARCH=true` is the supported configuration and the default. `true` defers
tools that declare themselves deferrable and keeps the rest eager.

[REAL] measured on the wire through this stack: **70 tools / 144,893 schema bytes → 24 /
53,044** (−63%, roughly 21K fewer tokens per request); latency median/p90/max **41/729/941 s
→ 24/40/62 s**.

**Eager** — always present: the built-in file and shell tools, `ToolSearch` itself, the
bundled artifact tool (it declares `alwaysLoad`), and anything the client refuses to defer.

**Deferred** — acquired on demand by name: GitHub MCP, LSP, and other MCP tools. Ask for
them with `ToolSearch("select:LSP")`, batching several names into one call.

[REAL] deferred GitHub discovery succeeded 5/5. grepai, Cadence skills, Cadence agents and
multi-turn reminders were unaffected — 16/16 tasks completed with no capability lost.

**Requires LiteLLM ≥ 1.98.** The deferred-tool listing travels in mid-array `role: "system"`
messages; the previous pin, LiteLLM 1.93, discarded those, which made deferred tools
undiscoverable. This is
why the pin matters, and why `ailocal check` reports the running version.

**Limitation.** `Grep` and `Glob` are frequently not present *and* not offered as deferred
tools, so `ToolSearch` cannot acquire them. Use `grep`/`rg` and `find` through `Bash`.

Do **not** conclude that tool search causes reduced tool surfaces inside subagents — see
*Known subagent limitations*.

## Native Workflow is off by default

Claude Code's built-in `Workflow` tool ships a 21,822-byte schema that **cannot be
deferred**: `non_deferrable_builtins` only ever grows, and the request to opt built-ins into
deferral ([anthropics/claude-code#54716](https://github.com/anthropics/claude-code/issues/54716))
is closed as not planned. So the choice is on or off, not lazy.

[REAL] on this stack it is pure overhead: **0 invocations across 148 real claude-local
sessions**. Disabling it took the model-visible tool schema from **53,044 → 31,221 bytes**
and the fixed prompt from **21,789 → 16,695 input tokens**, with capability preserved 10/10
(ordinary coding, Cadence skills, Cadence agents, GitHub and LSP discovery, grepai, artifacts
and a negative control all unaffected).

Restore it for one command:

```sh
AILOCAL_NATIVE_WORKFLOWS=1 claude-local
```

The override is **process-scoped**. It affects that invocation only; nothing is written to
disk, and hosted `claude` keeps Anthropic's native Workflow either way.

## Artifacts

`claude-local` publishes artifacts locally through a bundled MCP server — see
[the README's Artifacts section](../README.md#artifacts) and
[ADR 011](adr/011-bundled-artifacts.md). Nothing to clone or install separately.

## Repository intelligence

- **grepai** — semantic search over an index; unaffected by tool search.
- **LSP** — deferred; acquire by name.
- **GitHub MCP** — deferred; acquire by name.

## Cadence is optional

[Cadence](https://github.com/DevelopSolutionsLLC/cadence) composes engineering instructions,
skills and agents. It is **separately owned and entirely optional**; ailocal does not depend
on it and does not install it.

- **Without Cadence:** `claude-local` works normally. No skills, no Cadence agents, no
  composed policy file — just Claude Code on local models.
- **With Cadence installed later:** it writes into its own instruction and skill roots, which
  `claude-local` already reads. Nothing in ailocal needs to change.

Cadence has a `/workflow` capability under design. It does **not** exist today, and it is
not what `AILOCAL_NATIVE_WORKFLOWS` controls — that flag is only about Anthropic's built-in
tool.

## Known subagent limitations

A subagent does not necessarily receive the tools its definition declares. [REAL] an agent
declaring `Read, Grep, Glob, Bash` was granted `Read, Bash`; the same run with tool search
**disabled** granted the same two. This is the client's own subagent behaviour, not an
ailocal or tool-search effect, and disabling tool search does not fix it.

Write agent definitions so they still work when `Grep` and `Glob` do not arrive.

## Configuration

Every variable below is read by the `claude-local` wrapper and applies to **that process
only** unless stated otherwise. Set it inline (`VAR=1 claude-local`) or export it in your
shell. None of them are written to disk, and none affect hosted `claude`.

| Variable | Default | Accepted | Effect |
|---|---|---|---|
| `AILOCAL_TOOL_SEARCH` | `true` | `true`, `auto`, `auto:N`, `false` | Sets `ENABLE_TOOL_SEARCH`. `true` is the supported, measured configuration. |
| `AILOCAL_NATIVE_WORKFLOWS` | unset | `1` | `1` restores Claude Code's native Workflow tool. Any other value, or unset, disables it. |
| `AILOCAL_API_TIMEOUT_MS` | profile | integer ms | Client-side request timeout. Must not be below the proxy timeout. |
| `AILOCAL_LITELLM_PORT` | `4000` | port | Port the wrapper points the client at. |
| `AILOCAL_PROXY` | `http://127.0.0.1:4000` | URL | Full base URL override. |
| `AILOCAL_STATE` | `~/.local/state/ailocal` | path | State root (env file, runtimes). |
| `AILOCAL_<ROLE>_ALIAS_OVERRIDE` | unset | a LiteLLM alias | Points one role (`ARCHITECTURE`, `IMPLEMENTATION`, `FAST`, `REVIEW`) at an existing alias. **Fails closed** if LiteLLM does not serve it. |

```sh
AILOCAL_TOOL_SEARCH=false claude-local          # one session, everything eager
AILOCAL_NATIVE_WORKFLOWS=1 claude-local         # one session, Workflow back
AILOCAL_ARCHITECTURE_ALIAS_OVERRIDE=bench-gemma4-26b-mlx-off-32k claude-local
```

`ENABLE_TOOL_SEARCH=force` is **not** a supported value. It was tried during investigation
and is not accepted by the client; use `true`.

Changes take effect on the **next invocation** — these are per-process environment
variables, so no restart of anything is required. Changing a *profile* (which model a role
uses) is different: edit the file in `~/.config/ailocal/profiles/` and run `ailocal start`.

## See also

- [architecture.md](architecture.md) — how the pieces fit together
- [troubleshooting.md](troubleshooting.md) — symptoms and fixes
- [adr/011-bundled-artifacts.md](adr/011-bundled-artifacts.md) — why artifacts ship inside ailocal
- [adr/004-tool-gateway.md](adr/004-tool-gateway.md) — tool filtering, and what changed at LiteLLM 1.98
