# VS Code client

Deployed by `./scripts/install-vscode.sh` (also reachable as
`./scripts/install-clients.sh vscode`). Validated by
`./scripts/validate-vscode-e2e.sh`.

## Why VS Code spans three directories

Unlike `claude/` and `codex/`, which each drive one client, VS Code has three
independent surfaces that are configured in different places. They are kept in
separate directories because they deploy to different destinations, not because
they are different clients:

| Directory | Surface | Deploys to |
|---|---|---|
| `vscode/` (here) | Chat model provider | `<VS Code User>/chatLanguageModels.json` |
| `copilot/` | Copilot instruction files | `~/.copilot/instructions/` |
| `continue/` | Continue: chat + autocomplete + embeddings | `~/.continue/config.json` |

## The provider group

`chatLanguageModels.json` here is a TEMPLATE. The installer merges it into the
real file rather than overwriting, because the live file carries an `apiKey`
field that is a reference into VS Code's SecretStorage:

```json
"apiKey": "${input:chat.lm.secret.-3031591c}"
```

The secret VALUE is Keychain-backed and cannot be written by a script. The
installer therefore **preserves an existing reference**, so a key entered once
never has to be entered again. That is why the template omits `apiKey`: it is
supplied by whatever is already on the machine.

## What was deprecated, and why this file exists at all

The earlier setup configured VS Code through settings that are no longer used.
Researched, not inferred:

| Old approach | Status |
|---|---|
| `litellm-connector.baseUrl`, `.backends` | Deprecated by the extension in favour of VS Code Language Models provider groups + SecretStorage |
| `github.copilot.chat.customOAIModels` | Deprecated by VS Code |
| "OpenAI Compatible" BYOK provider | Deprecated, replaced by "Custom Endpoint" (Chat Completions / Responses / **Messages** API types) |
| `github.copilot.agent.autoApprove` | Never a real setting |
| `github.copilot.chat.tools.terminal.autoApprove` | Never a real setting |

Sources:
- <https://github.com/gethnet/litellm-connector-copilot/>
- <https://code.visualstudio.com/docs/agent-customization/language-models>

The installer removes those keys. Leaving them was not harmless: they made the
configuration look complete while doing nothing, which is how
`litellm-connector.baseUrl = null` went unnoticed.

## Worth knowing

VS Code's **Custom Endpoint** provider supports the `Messages` API type — the same
`/v1/messages` route Claude Code uses, and the route where this stack's gateway is
best proven. If the third-party connector ever becomes a problem, that is the
native path to move to.

BYOK models work without a GitHub sign-in or Copilot plan, but Copilot's semantic
search and inline suggestions still require a subscription.
