# Architecture

## Purpose

ailocal runs AI coding clients against local models on Apple Silicon. Ollama
serves the models on the host GPU; LiteLLM fronts them on `127.0.0.1:4000` as an
OpenAI- and Anthropic-compatible proxy that exposes **capabilities**
(`architecture`, `implementation`, `review`, `fast`, `completion`, `embeddings`)
rather than model tags. A hardware profile decides which model answers each
capability, and every client configuration is generated from that one decision.

---

## Repository layout

Every directory owns one thing. Generated output never appears in any of them.

| Directory | Owns | Never contains |
|---|---|---|
| `profiles/` | hardware-tier policy: capability → model, geometry, sampling, keep-alive, compaction | generated state |
| `profiles/clients.toml` | which capability each client surface uses | model tuning |
| `clients/` | client templates and deployment assets | rendered client configuration |
| `deploy/litellm/` | authored proxy assets: hooks, capability registry, config template, compose definition | the generated `config.yaml`, rendered secrets |
| `deploy/litellm/instructions/` | per-capability personas, mounted into the proxy | anything client-specific |
| `deploy/searxng/` | the search service definition and authored settings | the rendered `settings.yml` carrying the Brave key |
| `benchmarks/` | benchmark policy, tasks, and suite implementations | production model policy |
| `src/ailocal/` | the installed package; `cli.py` owns the command surface | implementation of any command |
| `./ailocal` | development shim for running from a checkout | a second command list |
| `lib/` | shared implementation and lifecycle: `policy.py`, `checks/`, `diagnostics/`, shell helpers | duplicate owners, public entry points |
| `tests/` | domain suites; `ailocal test` is the gate | production code |
| `docs/` | this document set | history |

---

## Runtime flow

```
authored profiles + client policy + templates
                    ↓
              policy.py                one reader, fails closed
                    ↓
             ailocal sync              the only generator
                    ↓
      one staged generation transaction
        stage → validate → back up → replace → roll back on failure
                    ↓
             $AILOCAL_STATE            outside the checkout
                    ↓
      Compose mounts        client deployment
                    ↓                     ↓
      LiteLLM · SearXNG      Claude · Codex · VS Code
```

---

## Runtime state

Everything generated lives under `${AILOCAL_STATE:-~/.local/state/ailocal}`,
mode `0700`. Nothing here is authored; deleting the directory and running
`ailocal sync` is a supported recovery.

```
$AILOCAL_STATE/
├── active-profile              selected tier, mode 0600
├── integration-contract.json   published facts for external consumers
├── litellm/
│   ├── config.yaml             proxy configuration
│   ├── capabilities.json       capability → backend, context
│   └── effective-profile.json  the resolved profile every consumer reads
├── searxng/
│   └── settings.yml            rendered; carries the Brave key, mode 0600
├── clients/
│   ├── claude/ codex/ continue/ copilot/
│   ├── configure.zsh           shell integration
│   └── model_catalog.json
├── captures/                   request traces, writable by the proxy
└── backups/                    pre-replacement copies
```

---

## Ownership rules

- One concept has one owner; if two files own it, one is deleted.
- Policy is read by `policy.py` and nothing else.
- Generation is one staged transaction with rollback; nothing writes to a final
  location directly.
- Generated artifacts never live in the repository, and are never tracked.
- A tracked file is authored source or a template — never a runtime artifact
  that generation rewrites.
- Subsystems talk through public APIs; no subsystem reads another's files.
- Service access — endpoint, credential, health, timeouts — has one owner.
- There are no fallback paths, compatibility shims, or second implementations.
- Capabilities are served as `ailocal-<capability>`, never a raw model tag.
- Geometry is derived, never restated: `num_ctx = context_input + max_output`,
  `num_predict = max_output`, `max_input_tokens = context_input`.
- Codex compaction is capped by admissible input (`context_input`), not total
  context; a trigger above the admitted input would take an HTTP 400 before
  compaction could run.
- Tool filtering is static per model, never per question.

---

## Public commands

`ailocal` is the only supported entry point. Modules under `lib/` implement
these commands and are not a public interface. `ailocal help` owns the command
list; it is not restated here.

Exit codes: `validate` and `smoke` return `0` or `1`. `doctor` returns `0`
healthy, `1` when it cannot resolve a trustworthy profile and refuses to
diagnose, `2` degraded.

---

## Containers

**LiteLLM**

| Mount | Kind | Why |
|---|---|---|
| `./deploy/litellm → /app/config:ro` | authored | hooks, capability registry, personas, template |
| `$AILOCAL_STATE/litellm → /app/generated:ro` | generated | the config the proxy actually loads |
| `$AILOCAL_STATE/captures → /app/captures` | writable | the only path the proxy may write |

The proxy starts with `--config /app/generated/config.yaml`. Callback modules
are named by absolute path because LiteLLM resolves them relative to the config
file, which lives on a different mount from the hooks.

Profiles, benchmark files and client templates are not visible to the container.

**SearXNG**

| Mount | Kind | Why |
|---|---|---|
| `$AILOCAL_SEARXNG_SETTINGS → /etc/searxng/settings.yml:ro` | generated | carries the Brave key; rendered outside the checkout so committing it is impossible |
| `./deploy/searxng/limiter.toml → /etc/searxng/limiter.toml:ro` | authored | bot-detection policy |

Ollama runs on the host, not in a container, and is reached at `OLLAMA_HOST`.

---

## Data flow

```
profiles/<tier>.toml     capability → model, geometry
profiles/clients.toml             client surface → capability
                    ↓
policy.py           resolve_role · geometry · load_client_policy · required_models
                    ↓
generation          model_list · aliases · capabilities · client configs · contract
                    ↓
$AILOCAL_STATE      one root, one writer
                    ↓
LiteLLM             capability alias → Ollama model, persona injected server-side,
                    tool schemas filtered before the backend sees them
                    ↓
deployment          ~/.config/ailocal/<client>/ — clients read the deployed copy,
                    never the checkout or the generated staging area
```

External consumers read the deployed `integration-contract.json` from the client
config root — never the repository.

---

## Extension points

**Add a model** — edit the capability's `active` in `profiles/<tier>.toml`,
then `ailocal sync && ailocal models-install`.

**Add a profile** — create `profiles/<tier>.toml` and add the tier to
`policy.TIERS`. Selection thresholds live in `src/ailocal/install.py`.

**Add a capability** — add the role to the profiles and to `policy.ROLES`, then
map client surfaces to it in `profiles/clients.toml`.

**Add a client template** — place the authored template under
`clients/<client>/`, emit its rendered output to
`$AILOCAL_STATE/clients/<client>/` from the generator, and deploy it from
`src/ailocal/clients.py`. Never write generated output beside the template.

**Add a benchmark suite** — implement it under `benchmarks/` and add a
case to the `benchmark` dispatcher in `ailocal`.

---

## Decisions

Durable decisions and their consequences are in [adr/](adr/). This document
describes the system as it is; history belongs to Git.

---

## Instruction ownership

Instruction files are configuration and have one owner.

| Layer | Owner | Contains |
|---|---|---|
| Repository contract | `AGENTS.md` | project-specific architecture boundaries, canonical sources, change workflow, validation, and prohibitions |
| Runtime/client preload | `clients/<client>/` | local endpoint, capability routing, context constraints, tool transport, and client-specific compatibility facts |
| Task workflow | prompts, commands, skills, or agent definitions | repeatable task-specific procedure and output contracts |
| Enforcement | tests, schemas, hooks, CI, and permissions | requirements that must not depend on model compliance |
| Generated client state | `$AILOCAL_STATE/clients/` | rendered configuration; never edited or committed |

Repository policy is not copied into runtime preloads. Runtime facts are not
copied into a repository's `AGENTS.md`. Volatile capability names, model
assignments, and token geometry are generated from the active profile rather
than restated in prose.

A client-specific instruction exists only when that client has a distinct
runtime constraint. VS Code remains configuration-only unless a demonstrated
Copilot behavior requires an additional adapter. Claude and Codex local
preloads remain separate from repository instructions: the preload explains
the local execution environment, while the repository `AGENTS.md` explains how
to change the project.
