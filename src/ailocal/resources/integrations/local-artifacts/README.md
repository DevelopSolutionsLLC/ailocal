# local-artifacts

A local artifact renderer for Claude Code sessions that authenticate with an API key.

Claude Code's hosted Artifact tool requires a claude.ai login. When a session authenticates with `ANTHROPIC_API_KEY` — as [ailocal](https://github.com/DevelopSolutionsLLC/ailocal)'s `claude-local` does, so inference goes to a local model — Claude Code **does not register the Artifact tool at all**, and withholds the `artifact-design` / `artifact-diagramming` / `artifact-capabilities` skills with it. This project puts an artifact capability back, entirely locally.

It is a fork of [xiagaohui/local-artifacts-for-claude-code](https://github.com/xiagaohui/local-artifacts-for-claude-code). See [NOTICE](NOTICE) for what changed and why — the security model in particular is materially different.

## What it is

One Python process, spawned by Claude Code as a stdio MCP server, with a loopback HTTP thread inside it.

```
Claude Code ──spawns──> server.py
                          ├── MCP: publish_artifact
                          └── HTTP 127.0.0.1:7891
                                ├── /          trusted viewer  (banner + SSE)
                                ├── /content   the artifact, in a sandboxed iframe
                                ├── /events    SSE
                                └── /status    metadata
```

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

**Node.js** is needed only for `architecture` layout. Without it, every other format works and architecture publishes report the missing dependency rather than failing silently.

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
| `LOCAL_ARTIFACTS_AUTO_OPEN` | `1` | `0` disables opening a browser |
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
./.venv/bin/python test_server.py        # 69: policy, failure modes, headers, persistence
./.venv/bin/python test_architecture.py  # 24: schema validation, layout, geometry gate
./.venv/bin/python test_browser.py       # 16: real Chrome + honeypots, no-CSP control
./.venv/bin/python check_diagram.py <artifact.html>   # geometry gate on one artifact
```
