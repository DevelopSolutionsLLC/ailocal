# local-artifacts

A local artifact renderer for Claude Code sessions that authenticate with an API key.

Claude Code's hosted Artifact tool requires a claude.ai login. When a session authenticates with `ANTHROPIC_API_KEY` — as [ailocal](https://github.com/DevelopSolutionsLLC/ailocal)'s `claude-local` does, so inference goes to a local model — Claude Code **does not register the Artifact tool at all**, and withholds the `artifact-design` / `artifact-diagramming` / `artifact-capabilities` skills with it. This project puts an artifact capability back, entirely locally.

It is a fork of [xiagaohui/local-artifacts-for-claude-code](https://github.com/xiagaohui/local-artifacts-for-claude-code). See [NOTICE](NOTICE) for what changed and why — the security model in particular is materially different.

## What it is

Two kinds of process. Claude Code spawns one stdio MCP server per session; those
sessions **share a single preview server** that none of them owns.

```
Claude Code ──spawns──> server.py            (one per session, stdio MCP)
                          └── publish ──┐     writes the artifact to the state file
                                        │
Claude Code ──spawns──> server.py       │    (another session)
                          └── publish ──┤
                                        v
                       server.py --serve      ONE per machine, ~25 MiB idle
                       HTTP 127.0.0.1:7891    started on demand by the first
                          ├── /          trusted viewer  (banner + SSE)
                          ├── /content   the artifact, in a sandboxed iframe
                          ├── /events    SSE
                          └── /status    metadata
```

The preview server is deliberately **not** a child of any MCP process. Claude Code
terminates a stdio MCP server when the session ends — the MCP spec has the client
close stdin, then `SIGTERM`, then `SIGKILL` — so a listener living inside it dies
with the session while the `preview_url` stays in the transcript. That was the
measured cause of "127.0.0.1 refused to connect": the artifact was still on disk,
just unreachable. See [ADR 013](../../../../../docs/adr/013-artifact-preview-lifetime.md).

Sessions never each start their own server: whoever loses the bind race exits, and
everyone reuses the winner.

### Memory

[REAL] 25.5 MiB at rest, and it stays there: publishing does not move it at all
(25.4 -> 25.5 MiB over 26 publishes), and serving Markdown does not either
(25.5 -> 25.6 MiB over 20 renders).

Rendering **Mermaid** is different, because `mermaid.min.js` is 3.4 MB and is
inlined into the page, so every `/content` render builds a ~3.5 MB string. RSS
climbs about 10 MiB per render at first, then decelerates and **plateaus at
~361 MiB after roughly 60 renders** — flat from there through 100, and it does
not fall when the artifact is replaced or the client disconnects.

That is the allocator, not a leak. Measured under `tracemalloc`, live Python
allocations are *constant* at 6.8 MiB across 1 to 20 renders (peak 15.3 MiB);
only RSS moves. Freed arenas are not returned to the OS, and are eventually
reused — which is why it plateaus. Caching the vendor read was tried and
rejected: it changed the curve by less than 3% (252 vs 246 MiB at 20 renders)
while permanently retaining 7 MiB, so it did not earn its place.

Nothing here is engineered around. The idle reap already bounds it: the process
exits after 30 minutes of disuse and the next publish starts a fresh one at
25 MiB. Reaching the plateau takes ~60 Mermaid renders inside a single idle
window, and costs less than the containers this deliberately replaces.

It exits by itself after `LOCAL_ARTIFACTS_IDLE_EXIT` seconds (default 30 minutes)
of genuine disuse. Everything that counts as use defers it: any HTTP request
resets the clock, an incoming publish resets it, and an open tab holds an SSE
connection which blocks the reaper outright. Reaping costs almost nothing —
[REAL] a cold start is 0.351s against 0.18s warm — and the next publish starts a
fresh server transparently and returns a working URL.

Publishing crosses the process boundary through the 0600 state file, not over HTTP.
There is no write endpoint: upstream's `POST /publish` accepted unauthenticated
cross-origin writes and was removed in the security audit, and it stays removed.

## Where the routing rules live

Three layers, deliberately not three copies of the same text:

| Layer | Delivered to the model? | Carries |
|---|---|---|
| `mcp__artifact__publish` **tool description** | yes, measured | the routing contract: when to call, and not to return artifact source in a fenced block |
| **skill** (`skill/SKILL.md`) | yes, listed in session init | which `format` to choose |
| **server `instructions`** | **no, on this client** | one line, optional, nothing depends on it |

MCP's `InitializeResult.instructions` is a hint a client **MAY** add to the prompt; the spec
does not require it. [REAL] captured at the Claude Code → LiteLLM boundary on 2.1.231, the
system prompt contains no MCP or artifact text at all, tool search on or off. So correctness
rests on the tool description.

[REAL] stating the missing user vocabulary there — "publish", "flowchart", "diagram" — and
adding "call this tool instead of returning a fenced code block" moved invocation from 6/15
to 13/15 (p = 0.008, negative control unchanged at 0/3) for +443 bytes.

## The presentation boundary

The model owns **meaning**. The renderer owns **presentation**. For `architecture` that
already meant refusing model-authored coordinates; for `mermaid` it now also means dropping
`style`, `classDef`, `class` and `linkStyle`. [REAL] 4 of 18 captured artifacts hard-coded
fills including `#f9f` and `#0f0`, which landed as pastels on the dark canvas under light
text. Graph structure, labels and relationships are untouched.

Separately, `[label]` text containing parentheses is quoted, because Mermaid requires it and
the model does not do it: one unquoted `Reviewer(s)` turned an entire diagram into "Syntax
error in text". That is syntax normalisation, is idempotent, and changes no semantics. A
diagram that is *semantically* wrong — a subgraph containing itself — stays a visible
failure rather than being guessed at.

## Formats

| `format` | The model sends | Who does layout |
|---|---|---|
| `architecture` | JSON: nodes, groups, edges, semantic kinds | **ELK**, then a built-in SVG design system |
| `mermaid` | Mermaid source | **Mermaid**, bundled and inlined locally |
| `html` | one self-contained document | the model |
| `markdown` | Markdown text | `marked`, bundled |

`architecture` exists because local models are poor layout engines. Asked for an architecture diagram, a local model hand-writing SVG produced overlapping boxes and colliding labels. Now it describes *meaning* and never writes a coordinate:

```
architecture JSON -> validate -> size -> elkjs (Node, in the server) -> static SVG
```

The resulting artifact contains **no script at all** — for this format the sandbox has nothing to contain. `check_diagram.py` is the objective gate: node overlap, text escaping its box, text collisions, clipping, arrowheads, expected topology.

## The design system

Colour, typeface and spacing are owned by the renderer, never by the model. Values are
derived from IBM Carbon's DTCG token source at a pinned version rather than hand-picked, and
contrast is gated by `test_design.py` rather than eyeballed.

**[DESIGN-SYSTEM.md](DESIGN-SYSTEM.md) is authoritative** — the token architecture, what is
generated versus hand-authored, the accessibility gates and how to update the tokens.

```sh
python3 tools/update_carbon_tokens.py --check   # is the committed palette current?
```

There is **one** artifact and **one** URL. Publishing again replaces it in place and the open tab refreshes itself — that is how you update.

## Install

Nothing to do — this component ships inside ailocal and is provisioned by:

```sh
ailocal clients claude
```

That creates the runtime venv in the state root, registers `mcpServers.artifact` and installs
the skill. Restart Claude Code; the first call asks for tool permission once, like any MCP
tool. `ailocal check` reports whether both halves are in place.

There is **no standalone installer here** and you should not create one: a second artifact
server registered against the same config would compete for the same tool name and port. See
[ADR 011](../../../../../docs/adr/011-bundled-artifacts.md) for why this is bundled.

Provisioning touches exactly two things inside the config dir — `mcpServers.artifact` in `.claude.json`, and `skills/local-artifact/SKILL.md`. It **never writes `settings.json`**, so a generator that owns `settings.json` (ailocal's, for example) can rewrite it freely without losing this.

Runtime state lives outside the project; `<project>/.artifacts/` holds your documents and is never removed automatically. Upgrading ailocal upgrades this.

**Node.js** is needed only for `architecture` layout. Without it, architecture publishes report the missing dependency rather than failing silently.

**Chrome or Chromium** is needed for `mermaid` diagrams, including Mermaid fenced inside `markdown`. Every diagram is parsed by the real Mermaid grammar before it is published, and that gate FAILS CLOSED: with no usable browser, Mermaid publication is refused rather than publishing unvalidated source. Refusing is deliberate — an unchecked diagram reported as a successful publish is the defect this exists to prevent — and the tool result says so and suggests `html` with inline SVG instead. `html`, `markdown` without diagrams, and `architecture` never invoke it and are unaffected.

### When a publish opens a browser

A successful publish opens the OS default browser when auto-open is enabled **and
nothing is already watching the viewer**. That is decided per publish, not once
per session: the viewer is a shared process that reaps after `IDLE_EXIT`, so a
later publish in the same session can legitimately need a new window.

It is decided by watcher count sampled over a grace window, not instantaneously.
An open tab drops its `EventSource` whenever the viewer restarts and reads as
zero watchers while it heals, so a live tab is given `LOCAL_ARTIFACTS_PRESENT_-
GRACE` seconds to reattach before a window is opened on top of it. A closed tab
never comes back, and the window opens. The wait is only paid when nobody is
watching on the first check.

The presentation browser is the OS default browser and is unrelated to the
isolated headless Chrome that validates Mermaid. The two must not be merged.

```bash
./.venv/bin/python server.py --diagnose    # why a publish would or would not open
```

### Why not a Claude Code plugin?

Plugins were tested, not assumed. A skills-dir plugin at `$CLAUDE_CONFIG_DIR/skills/<name>/` does auto-load, spawn its MCP server, and receive `CLAUDE_PLUGIN_ROOT` / `CLAUDE_PLUGIN_DATA` / `CLAUDE_PROJECT_DIR` — no marketplace or `settings.json` entry required. It was rejected for one measured reason: a plugin-provided MCP server is namespaced

```
mcp__plugin_<plugin>_<server>__<tool>      e.g. mcp__plugin_testplug_artifact__publish_artifact
```

43 characters. The dominant routing failure on a local model is mangling the tool name, so packaging that makes the name longer works against the thing it would be adopted to improve. Direct registration yields `mcp__artifact__publish` — 22.

## Where things live

| What | Where | Owner |
|---|---|---|
| artifact sources | `$CLAUDE_PROJECT_DIR/.artifacts/` | **your project** (durable, editable, not git-ignored by us) |
| runtime state | `${XDG_STATE_HOME:-~/.local/state}/local-artifacts` | this project (disposable) |
| MCP + skill | `$CLAUDE_CONFIG_DIR` | this project's installer |
| inference | ailocal / LiteLLM | ailocal, untouched |

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `LOCAL_ARTIFACTS_PORT` | `7891` | loopback port |
| `LOCAL_ARTIFACTS_ROOT` | server cwd at startup | approved root for `file_path` |
| `LOCAL_ARTIFACTS_AUTO_OPEN` | `1` | `0` disables opening a browser. Takes precedence over the official variable below |
| `CLAUDE_CODE_ARTIFACT_AUTO_OPEN` | unset | Claude Code's official flag, honoured for compatibility; used only when `LOCAL_ARTIFACTS_AUTO_OPEN` is unset |
| `LOCAL_ARTIFACTS_PRESENT_GRACE` | `4.5` | seconds a publish waits for an already-open tab to reattach before deciding nobody is watching. Must stay above the viewer's 3 s self-heal reload |
| `LOCAL_ARTIFACTS_IDLE_EXIT` | `1800` (30 min) | seconds of no request, no publish **and** no open viewer before the shared preview server exits; `0` never |
| `XDG_STATE_HOME` | `~/.local/state` | state lives in `<here>/local-artifacts` |

`CLAUDE_CODE_DISABLE_ARTIFACT` is deliberately **not** honoured — it is Claude Code's switch for the hosted tool, and this exists to work when that tool is gone.

## The security boundary

Generated artifact content is treated as untrusted, because it is model output and the model's context can be influenced by repository files and tool results.

`/` is a trusted viewer this project generates. The artifact is served separately at `/content` and framed with `sandbox="allow-scripts"` and **no** `allow-same-origin`, so it runs in an opaque origin, under a CSP whose `connect-src` is `'none'`. SSE lives in the viewer, so the artifact needs no network permission of its own.

**Generated JavaScript can**: run, manipulate its own DOM, animate, draw to canvas, handle input. Artifacts stay interactive.

Artifacts must be **self-contained**. A remote `<script src>`, stylesheet, font, image or `@import` is refused at publish time with an explanation, rather than published into a page that would render blank. A remote `<a href>` is fine — it is navigation, not a subresource.

**Generated JavaScript cannot**: reach Ollama, Qdrant, LiteLLM or any other loopback port; reach the internet; read the viewer's DOM, cookies or storage; navigate the top frame; submit a form; load a remote image, font or script; read local files.

Verified behaviourally rather than by reading headers: honeypot sockets standing in for those services recorded **0** connections from the artifact, while an identical page served without the CSP lit **all** of them. See `test_browser.py`.

`'unsafe-inline'` for scripts is intentional — it is what makes artifacts interactive. The CSP here is a **network** boundary, not a script boundary. An artifact can still burn CPU or, on user interaction, touch the clipboard.

Markdown gets the *same* boundary rather than sanitization: marked v4.3.0 passes raw HTML through, so a Markdown artifact can contain script — it is contained, not neutered.

`file_path` is confined to an approved root (the server's cwd, which is Claude Code's launch directory), with a suffix allowlist. Paths are resolved before the containment check, so a symlink inside the root pointing outside it is refused.

## Limits

- **The artifact lives only as long as the Claude Code session.** The server is a child process; when the session exits, the URL stops answering. State is persisted, so the next session restores the last artifact.
- **One session at a time.** A second session cannot bind the port; it reports that honestly and refuses to publish rather than pretending to succeed.
- 16 MiB per artifact.
- **Diagram quality is the model's, not this project's.** A local model asked for an architecture diagram tends to reach for a CDN charting library; the subresource guard turns that into a retry, and the retry is usually hand-authored inline SVG whose boxes and labels may overlap.
- Not feature-parity with hosted Artifacts: no versioning, sharing, comments, or runtime capabilities.

## Tests

```sh
./.venv/bin/python test_server.py            # 69: policy, failure modes, headers, persistence
./.venv/bin/python test_architecture.py      # 24: schema validation, layout, geometry gate
./.venv/bin/python test_mermaid_validate.py  # validator states + publish gate, no browser
./.venv/bin/python test_routing_contract.py  # what the tool description says (deterministic)
./.venv/bin/python test_mermaid_grammar.py   # real Mermaid 11.17.2 corpus (needs Chrome)
./.venv/bin/python test_browser.py           # 16: real Chrome + honeypots, no-CSP control
./.venv/bin/python check_diagram.py <artifact.html>   # geometry gate on one artifact
```
