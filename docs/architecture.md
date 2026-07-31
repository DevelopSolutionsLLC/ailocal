# ailocal architecture

How the local runtime is put together and why. `README.md` covers installation and
use; `AGENTS.md` is the AI bootstrap; `docs/troubleshooting.md` covers symptoms.

## Request path

```
client → LiteLLM (127.0.0.1:4000) → Ollama → model
         ├─ persona injection      (pre-call hook)
         ├─ tool gateway           (pre-call hook)
         └─ session observation    (post-call hook)
```

LiteLLM speaks both the OpenAI and Anthropic dialects, so Claude Code, Codex and
Copilot Chat reach the same backends unmodified. No client is patched; every
adaptation happens in the proxy.

## Capabilities, not models

Six capabilities are served, each as one canonical `model_list` entry named
`ailocal-<capability>`:

| Capability | Purpose |
|---|---|
| `architecture` | design, large refactors, repository-wide planning |
| `implementation` | daily coding |
| `review` | code review |
| `fast` | background summarisation and cheap turns |
| `completion` | FIM autocomplete only |
| `embeddings` | retrieval |

Clients never name a model. They request a capability, and the router owns
context, sampling and per-role lifecycle (`keep_alive`). Swapping a backend is a
one-line profile edit no client sees.

The `claude-*` and `gpt-*` IDs clients hard-code are aliased onto these groups
through a generated `model_group_alias` block: one backend entry, many
client-facing names.

**`completion` is autocomplete only** — a 3B model at `num_ctx` 4096. A
conversational turn routed there fails with "No models have context window large
enough", and a context-window fallback must always move *up*: falling from 16K to
4K cannot succeed by construction. `sync-models.py` refuses to generate a
configuration where a Claude slot resolves to `completion`, so the rule is
enforced rather than merely written down.

## Generation

Two files are canonical:

| Source | Defines |
|---|---|
| `config/profiles/<tier>.yaml` | what each capability *is* — backend, context, sampling, `keep_alive` |
| `config/clients.yaml` | which capability each client surface uses |

`config/active-profile` names the live tier, chosen by `install.sh` from detected
RAM. `sync-models.py` regenerates everything downstream: the `model_list` and
`model_group_alias` in `config/litellm/config.yaml`,
`config/capabilities.generated.json`, `config/clients/model_catalog.json`, and
each client's configuration file.

Generated regions sit between `BEGIN/END GENERATED` markers. Regeneration is a
fixed point — running it twice produces no diff — and the gate asserts that.

### Generated output lives beside its source — deliberately, for now

Every file `sync-models.py` writes is git-ignored, and each has a tracked
`<name>.template.<ext>` beside it. Source and output therefore share a directory:

    config/litellm/          11 tracked source  +  2 generated
    config/clients/          25 tracked source  +  5 generated

This is the same three-stage pipeline Cadence runs as `src/` → `build/` → `dist/`,
without the directory separation. **Separating them is the preferred end state**; the current layout is what shipped in the first release candidate
because it is fully validated.

**Why it is not separated yet.** LiteLLM resolves callback modules relative to the
config file's directory:

    --config /app/config/config.yaml        # config/litellm → /app/config
    callbacks:
      - tool_repair.proxy_handler_instance  # bare module name

So `config.yaml` must sit with the hook modules, and that directory is the bind
mount. Moving output to `dist/` therefore means moving the mount and copying the
hooks into it — the shape Cadence's build already has.

**The planned change.** Mount `dist/litellm` instead of `config/litellm`, and have
`sync` copy the hook modules alongside the generated `config.yaml`. Result:
`config/` becomes entirely source, `dist/` entirely output, and both repositories
follow one rule.

**Trigger:** after clean-install validation and the first baseline release. It is
deferred rather than rejected because it changes the bind mount — if it is wrong
the proxy does not boot — and that is not a risk worth carrying into a release
candidate alongside a first clean-machine install.

## Persona injection

`config/litellm/persona_injector.py` is a pre-call hook that merges
`config/instructions/_core.md` with `<capability>.md` into whatever system prompt
the client sent. Running server-side means every alias inherits the persona
without client configuration.

It handles both request shapes, which are not alike: the OpenAI dialect carries
the system prompt inside `messages[]`, while Anthropic's `/v1/messages` — the
route Claude Code uses — carries it in a top-level `system` field. A hook that
understands only one shape does nothing on the other, silently.

Reasoning models receive no persona. Completion and embeddings have none by
design.

**Coupling worth knowing:** injection depends on the requested model resolving
back to a capability key, which the hook looks up through `model_group_alias`.
Any change to model naming or routing must preserve that mapping or personas stop
applying without an error.

## Reasoning and non-reasoning backends

No installed model emits a reasoning stream. Every capability therefore carries
both `additional_drop_params: ["thinking", "reasoning_effort"]` — so a client
sending `thinking` to a non-thinking backend does not 400 — and `think: false`,
which suppresses a backend's own default reasoning. Both are required: dropping
the first breaks Claude Code, dropping the second hangs Copilot Chat.

The reasoning path remains in `sync-models.py`; a commented `reasoning` slot in a
profile restores it in one edit.

## Tool gateway

Clients declare their whole tool surface on every request. Measured on Claude
Code: **61 tools, 104 KB, 24,448 Qwen tokens**, roughly **71%** of it
orchestration and scheduling machinery a local 30B cannot drive.

`config/litellm/tool_gateway.py` measures and optionally trims that payload.
Three modes via `AILOCAL_TOOL_GATEWAY`: `off`, `report`, `filter`. `.env` sets
`filter`, so trimming is live.

### Task classification

Activation matters more than raw size. The registry's `task_classes` decide which
tool groups survive. A `conversational` class carries `override_always: true`,
dropping *below* the normal floor to no tools at all — without it, "show me hello
world in C++" arrived holding Read/Glob/Grep/Bash and the coding persona crawled
the repository before answering. Measured: 61 tools reduced to 1.

Two guards exist, because losing a tool mid-task strands an agent while a spare
schema only costs tokens:

- An unmatched task keeps everything. Classification fails open.
- The conversational override applies only to a genuine first turn.
  Classification reads the first user message, which never changes as a session
  grows, so without this guard a session opening with a chat question would stay
  tool-less forever.

`mention_overrides` re-adds a group the user names explicitly. Classification
matches on *topic* and is blind to instructions about *how* to work, so "delegate
this to the reviewer subagent" once matched `review` on the word "security" and
lost the delegation tool it was asking for.

### Delegation

The subagent tool is `Agent`, in the `delegation` group. Claude Code renamed it
from `Task` in v2.1.63; the live `Task*` names are background-task management, not
delegation. While `Agent` sat in `orchestration` — a group every local class
denies — the gateway dropped it from every request, and the symptom was misread
twice as a model limitation. The token argument never applied: `Workflow` alone is
21 KB, `Agent` about 1 KB.

### Honest accounting

Two ways the numbers could mislead, both closed:

- **Credit for work LiteLLM already does.** LiteLLM discards Codex's `namespace`
  tool bundles before the backend, so the gateway refuses to book them as savings.
  Codex's real gain is 18%, not 71%.
- **Token honesty.** Byte counts convert through a ratio calibrated against
  Ollama's `prompt_eval_count`, not an assumed characters-per-token.

Every fact about models, clients, routes and tools lives in
`config/litellm/registry.yaml`. The negotiator holds no such literal, and a test
enforces that by grepping its source. Frontier models are `passthrough` —
forwarded untouched, and no feature flag overrides it.

Two traps the code encodes deliberately: the gateway registers **last**, so
`websearch_interception` still sees the client's `web_search` tool; and it never
drops an entry it cannot name, because Codex's bare `{"type":"web_search"}`
normalises to `<web_search>` and dropping it would kill search silently.

## Client isolation

Every client artefact lands under `~/.config/ailocal/`. `~/.claude` and `~/.codex`
are never touched, so hosted and local sessions coexist. `CLAUDE_CONFIG_DIR`
relocates `.claude.json` itself, so MCP registrations, history and credentials are
genuinely per-root rather than shared.

`configure.zsh` defines the `claude-local`, `codex-local` and `ailocal-code`
wrappers and is sourced from `.zshrc` between installer markers.

The `ailocal` launcher in `~/.local/bin` is a generated shim, not a symlink. This
repository *is* its runtime — LiteLLM bind-mounts `config/litellm` out of it — so
it cannot be copied under `~/.local/share` without duplicating the source of
truth. The shim reads the checkout location from `~/.config/ailocal/repo`, making
a moved checkout a one-line configuration fix instead of a broken command.

## Client capabilities

| Client | Inference | Native LSP | MCP tools |
|---|---|---|---|
| Claude Code (hosted) | vendor | yes | yes |
| `claude-local` | local | yes | yes (reduced by the gateway) |
| VS Code Copilot | local | editor-managed | yes |
| Codex (hosted) | vendor | none | yes |
| `codex-local` | local | none | **no** |

**codex-local cannot use MCP tools by either route.** Codex declares MCP servers
as `namespace` bundles, which LiteLLM discards before the backend; enabling
namespace expansion instead makes the model emit flattened names that Codex's own
dispatcher refuses. Gateway flattening clears four of seven boundaries — bundles
expand, the model emits well-formed calls — and then Codex's router rejects both
the bare and `mcp__`-prefixed forms. The blocker is the client's dispatcher, not
the proxy: openai/codex#20652.

Treat this as measured and closed. Do not re-probe without a Codex release
claiming a fix.

## LSP

`claude-local` has a verified Python baseline: `ENABLE_LSP_TOOL` plus the official
`pyright-lsp` plugin driving `pyright-langserver` over stdio. ailocal owns this
much so a local profile is useful on its own; Cadence provisions additional
languages when a repository needs them.

Shell has no official Claude LSP plugin, and a settings-level `lspServers` block
is ignored — only plugin manifests declare servers. Shell is validated with
`bash -n`, `zsh -n` and `shellcheck` instead.

## Runtime constraints

**LiteLLM reads its configuration only at boot, and that configuration is
bind-mounted.** Editing `config.yaml`, a hook or a persona changes nothing in the
running proxy, and `docker compose up -d` will not restart it because the
container spec did not change. It keeps serving the old routing with nothing in
the logs to say so. `start.sh` fingerprints those files and restarts on change.

Never invoke `deploy/litellm/docker-compose.yml` directly — it references a
service defined elsewhere and fails as an invalid project. Use `ailocal start`.

**Liveness and readiness do not prove Ollama is reachable.** Both describe
LiteLLM's own process and return healthy during a total backend outage. Only
`/health` dials the backend, and it must be checked from *inside* the container:
the host reaches Ollama directly while LiteLLM reaches it as
`host.docker.internal`, and that path fails independently.

**Claude Code's web search never reaches SearXNG.** `WebSearch` is a client-side
tool. LiteLLM's interception accepts `litellm_web_search` and bare `web_search`,
and deliberately refuses a `WebSearch` carrying an `input_schema` so it does not
hijack the client's own handler.

## Verification

`./scripts/test-all.sh` is the gate; `--full` adds the client-compatibility matrix
and the benchmark. A stopped or unhealthy proxy fails the gate rather than
silently reducing it.

To inspect a real client tool payload, set `AILOCAL_TOOL_GATEWAY_CAPTURE` in
`.env`, restart, exercise the client, then read `data/tool-captures/`. Captures
record request text — disable it and delete them when the investigation ends.
