# Architecture

## Purpose

AGENTS.md owns the repository map, the ownership rules and the engineering
invariants; `ailocal help` owns the command list. This document answers only
what none of those can: how a request moves through the running system.

ailocal runs AI coding clients against local models on Apple Silicon. Ollama
serves the models on the host GPU; LiteLLM fronts them on `127.0.0.1:4000` as an
OpenAI- and Anthropic-compatible proxy that exposes **capabilities**
(`architecture`, `implementation`, `review`, `fast`, `completion`, `embeddings`)
rather than model tags. A hardware profile decides which model answers each
capability, and every client configuration is generated from that one decision.

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

## Containers

**LiteLLM**

| Mount | Kind | Why |
|---|---|---|
| `$XDG_DATA_HOME/ailocal/deploy/litellm → /app/config:ro` | authored | hooks, capability registry, personas, template |
| `$AILOCAL_STATE/litellm → /app/generated:ro` | generated | the config the proxy actually loads |
| `$AILOCAL_STATE/captures → /app/captures` | writable | the only path the proxy may write |

The proxy starts with `--config /app/generated/config.yaml`. Callback modules
are named by absolute path because LiteLLM resolves them relative to the config
file, which lives on a different mount from the hooks.

Profiles and client templates are not visible to the container.

**SearXNG**

| Mount | Kind | Why |
|---|---|---|
| `$AILOCAL_SEARXNG_SETTINGS → /etc/searxng/settings.yml:ro` | generated | carries the Brave key; rendered outside the checkout so committing it is impossible |
| `$XDG_DATA_HOME/ailocal/deploy/searxng/limiter.toml → /etc/searxng/limiter.toml:ro` | authored | bot-detection policy |

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
then `ailocal sync && ailocal install`.

**Add a profile** — create `profiles/<tier>.toml` and add the tier to
`policy.TIERS`. Selection thresholds live in `src/ailocal/install.py`.

**Add a capability** — add the role to the profiles and to `policy.ROLES`, then
map client surfaces to it in `profiles/clients.toml`.

**Add a client template** — place the authored template under
`resources/clients/<client>/`, emit its rendered output to
`$AILOCAL_STATE/clients/<client>/` from the generator, and deploy it from
`src/ailocal/clients.py`. Never write generated output beside the template.

---

## Decisions

Durable decisions and their consequences are in [adr/](adr/). This document
describes the system as it is; history belongs to Git.

---
