# Troubleshooting

Symptom lookup. Each entry states what you see, how to confirm the cause, and what to do. System design is in [architecture.md](architecture.md); normal operation is in the README and `ailocal help`.

Start with `ailocal check` — it reports health with a fix for anything wrong, and exits `0` healthy, `1` when it cannot resolve the active profile and refuses to guess, `2` degraded.

---

## LiteLLM never becomes ready

**Likely cause** — the container is crash-looping, usually a bad generated config or a callback that cannot be imported.

**Verify**

```sh
docker ps --filter name=ailocal-litellm --format '{{.Status}}'
docker logs ailocal-litellm --tail=40
```

Look for `ImportError` or `Could not find module file`.

**Fix**

```sh
ailocal start     # regenerates the configuration, then restarts the stack
```

**Status** — local defect if it persists after a clean start.

---

## Generated configuration is stale or invalid

**Likely cause** — a profile or client policy changed without regenerating, or a generated file was hand-edited.

**Verify**

```sh
ailocal check     # the configuration section reports drift
```

**Fix**

```sh
ailocal start     # regenerates and reloads the proxy
ailocal clients   # redeploy client configuration
```

Generated files live under `${AILOCAL_STATE:-~/.local/state/ailocal}`. Deleting that directory and re-running `ailocal start` is a supported recovery.

**Status** — configuration issue.

---

## Ollama is unavailable

**Likely cause** — the daemon is not running, or is on a non-default address.

**Verify**

```sh
curl -fsS "${OLLAMA_HOST:-http://127.0.0.1:11434}/api/tags" >/dev/null && echo up
```

**Fix**

```sh
ollama serve      # or open Ollama.app
ailocal check
```

If Ollama listens elsewhere, export `OLLAMA_HOST` — it is the only variable that redirects it, for every subsystem.

**Status** — configuration issue.

---

## A required model is missing

**Likely cause** — the profile changed tier, or a pull failed.

**Verify**

```sh
ailocal check     # names every missing model
```

**Fix**

```sh
ailocal install
```

**Status** — configuration issue.

---

## A request is rejected for exceeding the context window

**Likely cause** — the prompt is larger than the capability admits. Each alias advertises `max_input_tokens = context_input`; the proxy rejects rather than silently truncating.

**Verify**

```sh
ailocal check     # advertised geometry matches the profile, and an
                  # oversized prompt is rejected rather than truncated
```

**Fix** — use a larger capability (`ailocal-architecture`), or raise `context_input` in the profile and `ailocal start`. Do not raise it past what the machine can hold.

**Status** — expected behaviour. A rejection is correct; silent truncation would not be.

---

## An architecture request appears hung

**Likely cause** — cold prefill, not a hang. Long-context evaluation on the large tier can take minutes while everything remains healthy, and it degrades super-linearly with prompt size: a session that was fast can stall once a turn misses the KV cache.

**Verify**

```sh
ailocal check     # reports whether the model is resident and whether a
                  # generation is still running from a disconnected client
```

Memory and swap staying flat while the model is resident means it is working, not stuck.

**Fix** — wait; client and proxy share a 900 s timeout and give up together. To avoid the stall, keep sessions shorter or use a smaller capability for routine work.

**Status** — hardware limitation.

---

## Claude Code reports an API error after a long request

**Likely cause** — the client gave up before the backend finished, leaving Ollama generating into a closed socket.

**Verify**

```sh
grep -a "client closing the connection" ~/.ollama/logs/server.log | tail -3
```

**Fix** — ensure the deployed client config is current; `configure.zsh` pins `API_TIMEOUT_MS` to match the proxy.

```sh
ailocal start && ailocal clients
```

**Status** — fixed by configuration; re-deploy if it reappears.

---

## Codex produces content but the turn never completes

**Likely cause** — a streamed Responses turn never emits its terminal event.

**Verify** — open a session and watch `docker logs -f ailocal-litellm`.

**Fix** — none available locally. Configuration, routing, geometry and tool transport are validated; only interactive streaming is affected.

**Status** — upstream, [BerriAI/litellm#27442](https://github.com/BerriAI/litellm/issues/27442). Do not work around it by rewriting LiteLLM behaviour locally.

---

## Claude Code shows "0 searches" although search worked

**Likely cause** — the search result block is dropped during upstream response serialisation. Retrieval succeeded; only the count is wrong.

**Verify**

```sh
docker logs ailocal-litellm 2>&1 | grep -c searxng-search
```

A non-zero count with search content in the answer confirms retrieval ran.

**Fix** — none needed; the answer is correct.

**Status** — upstream, cosmetic.

---

## Search returns nothing, or Brave fails

**Likely cause** — SearXNG is not running. Brave is optional: with no key, ailocal renders the `braveapi` engine inactive and search falls back to the keyless engines, which is a supported configuration, not a fault.

**Verify**

```sh
ailocal check     # searxng: JSON API reachable from LiteLLM (no query issued)
                  # searxng-query: one free-engine search really returned results
                  # brave-key: whether braveapi is configured, and whether it is ACTIVE
```

`brave-key` reads the rendered settings only — no default check ever spends a Brave query.

**Fix**

```sh
ailocal start     # renders settings and starts the service
```

To add or change the Brave key, edit your `.env.local` and restart:

```sh
$EDITOR ~/.config/ailocal/.env.local    # set BRAVE_API=your-key
ailocal start                     # re-renders the settings with the key
```

`.env.local` is yours and survives every upgrade; see [security.md](security.md) for where each secret lives.

Search is coding-first: scraped general-web engines are disabled because they fail with CAPTCHAs under sustained use. Brave's API-backed engine restores general-web coverage.

**Status** — configuration issue.

---

## A client is using stale configuration

**Likely cause** — generation ran but deployment did not.

**Verify**

```sh
ailocal check       # compares generated against deployed
```

**Fix**

```sh
ailocal clients             # every client detected on this machine
ailocal clients claude codex vscode   # or name them
```

**Status** — configuration issue.

---

## State-root permissions are wrong

**Likely cause** — the runtime directory was created by another process, or permissions were changed by hand.

**Verify**

```sh
ls -ld "${AILOCAL_STATE:-$HOME/.local/state/ailocal}"
ls -l  "${AILOCAL_STATE:-$HOME/.local/state/ailocal}/searxng/settings.yml"
```

Expect `0700` on the root and `0600` on the rendered settings, which carry the Brave key.

**Fix**

```sh
chmod 700 "${AILOCAL_STATE:-$HOME/.local/state/ailocal}"
chmod 600 "${AILOCAL_STATE:-$HOME/.local/state/ailocal}/searxng/settings.yml"
```

**Status** — security issue; fix before using search.

---

## VS Code is configured but not answering

**Likely cause** — the connector is installed but the model list was not refreshed, or the extension is absent.

**Verify** — open a Copilot chat and confirm the request appears in `docker logs ailocal-litellm`.

**Fix**

```sh
ailocal clients vscode
```

Then reload the VS Code window.

**Status** — configuration issue. GUI submission cannot be automated, so the final step is manual.

---

## The VS Code chat reply area is cramped

**Likely cause** — not ailocal. VS Code 1.132 added an agent-sessions panel inside the chat view, which takes space from the conversation. Its settings are `chat.viewSessions.enabled` and `chat.viewSessions.orientation` (`stacked` puts the panel above the chat input; `sideBySide` puts it beside the chat when the view is wide enough). ailocal writes neither.

**Fix** — in VS Code settings:

```json
"chat.viewSessions.enabled": false
```

**Status** — upstream behaviour, not a defect. Responses through the proxy are unaffected: reasoning arrives in `reasoning_content` and the answer in `content`, which is what the connector expects.

---

## The artifact tool is missing in claude-local

The model says it has no way to publish, or `mcp__artifact__publish` never appears.

```sh
ailocal check | grep artifact
```

`ailocal clients claude` provisions the runtime and registers the MCP server; run it if the
check reports either half missing. It is idempotent and preserves the other MCP servers in
`.claude.json`.

If the check passes but the tool still does not appear, the registration is correct and the
model simply did not route to it. Ask for the artifact explicitly ("publish an artifact
showing…").

**Status** — fixed by re-running client configuration.

---

## An artifact renders but the `architecture` format fails

Every other format works; only `architecture` errors.

That format computes its layout with the bundled elkjs, in Node:

```sh
node --version || brew install node
```

`ailocal clients claude` warns about this at provisioning time. No other format needs Node.

**Status** — install Node.

---

## The artifact preview will not open

The page is served from `127.0.0.1` by the artifact server, which runs as a child of the
Claude Code session. If the session has ended, the preview is gone — but the canonical source
was written to `.artifacts/` in the project you launched from, and republishing it re-renders
the page.

The page itself cannot reach the network by design: it runs in a sandboxed iframe under
`connect-src 'none'`. A blocked request in the browser console is the boundary working, not a
fault.

**Status** — expected; re-publish from the saved source.

---

## ToolSearch cannot find a GitHub or LSP tool

Those tools are deferred, not absent. Acquire them by name, batching into one call:

```
ToolSearch("select:LSP,mcp__github__issue_read")
```

If a name genuinely does not resolve, check the server is registered
(`ailocal check`) and that LiteLLM is ≥ 1.98 — the deferred-tool listing travels in mid-array
`role: "system"` messages, which the previous pin discarded.

`Grep` and `Glob` are a known exception: they are frequently neither present nor offered as
deferred tools, so `ToolSearch` cannot acquire them. Use `grep`/`rg` and `find` through
`Bash`.

**Status** — expected behaviour; see [claude-local.md](claude-local.md#tool-search).

---

## A subagent has fewer tools than its definition declares

An agent declaring `Read, Grep, Glob, Bash` is granted `Read, Bash`.

This is Claude Code's own subagent behaviour. [REAL] the same run with tool search
**disabled** granted the same two tools, so it is not caused by ailocal's configuration and
turning tool search off does not fix it. Write agent definitions that still work over `Bash`.

**Status** — upstream; no ailocal-side fix.

---

## Native Workflow is missing (`/workflows` does nothing)

`claude-local` disables Claude Code's built-in Workflow tool by default — its 21,822-byte
schema cannot be deferred and it was invoked 0 times across 148 measured sessions. Restore it
for one session:

```sh
AILOCAL_NATIVE_WORKFLOWS=1 claude-local
```

The override is process-scoped, and hosted `claude` is unaffected either way.

**Status** — intentional; see [claude-local.md](claude-local.md#native-workflow-is-off-by-default).

---

## The context is unexpectedly large

Check what is actually being sent before tuning anything:

```sh
ailocal check
```

The usual causes are tool search disabled (`AILOCAL_TOOL_SEARCH=false` in your shell) or
native Workflow re-enabled (`AILOCAL_NATIVE_WORKFLOWS=1`). Both are per-process, so an
`export` in your shell profile is easy to forget. Together they account for roughly 114K
schema bytes versus 31K with the defaults.

**Status** — check the two variables first.

---

## An editable install stops importing (`ModuleNotFoundError: ailocal`)

Two different causes look identical.

```sh
ls -lO .venv/lib/*/site-packages/*.pth   # is the .pth flagged `hidden`?
grep executable .venv/pyvenv.cfg          # does that interpreter still exist?
```

- **`.pth` marked `hidden`** — CPython deliberately skips hidden `.pth` files, so the path
  entry is never added. Recreating the venv does not help. Run with
  `PYTHONPATH=<repo>/src` instead; subprocesses inherit it.
- **`pyvenv.cfg` names a Python that was upgraded away** (e.g. 3.14.6 → 3.14.7) — recreate
  the venv.

**Status** — developer environment; does not affect an installed `ailocal`.

---

## A cosmetic warning appears at startup

SearXNG logs a bot-detection line during boot. It is expected on a single-user local instance reached only from LiteLLM, and can be ignored.

**Status** — cosmetic.

---

## Notes on diagnosis

**Do not infer a fixed startup-context baseline from a single provider-token reading.** Measure the actual request: conversation accumulation is not startup, and a figure taken mid-session will mislead any compaction analysis.

**The 128 GB profile is unvalidated.** It mirrors the 64 GB policy. Treat its numbers as provisional until measured on 128 GB hardware.
