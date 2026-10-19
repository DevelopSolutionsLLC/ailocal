# Security

What is protected, why, and how. Repairs are in [troubleshooting.md](troubleshooting.md); system structure is in [architecture.md](architecture.md).

---

## Trust boundaries

ailocal runs entirely on one machine. There is no multi-tenancy, no remote access, and no authentication beyond a single local key.

| Boundary | Protection |
|---|---|
| Host ↔ network | Every published port binds `127.0.0.1`. Nothing is reachable off-host. |
| Client ↔ proxy | One shared key, `LITELLM_MASTER_KEY`. An unauthenticated request is rejected. |
| Proxy ↔ SearXNG | Internal compose network only; SearXNG is not published for external use. |
| Proxy ↔ Ollama | Host loopback. Ollama has no authentication and must not be exposed. |
| Repository ↔ runtime | Generated state lives outside the checkout, so a secret cannot be committed. |

The threat model is a developer workstation: it protects against accidental exposure and accidental disclosure, not against a local attacker who already has the user's account.

---

## Secrets

| Secret | Lives in | Protection |
|---|---|---|
| `LITELLM_MASTER_KEY` | `$AILOCAL_STATE/env` (generated) | gitignored; the only credential clients present |
| `BRAVE_API` | `.env.local` (yours; optional, empty means keyless research) | passed only to the research MCP process, which removes it from its environment and sends it only to Brave's API; never rendered, logged or stored in the ledger |
| `GITHUB_SEARCH_TOKEN` | `.env.local` (yours; optional, empty disables GitHub code search) | fine-grained PAT for public repositories, read-only, with an expiry; never the `gh` CLI token with `repo` scope |
| `SEARXNG_SECRET` | `$AILOCAL_STATE/env` (generated) | gitignored |
| Rendered SearXNG settings | `$AILOCAL_STATE/searxng/settings.yml` | mode `0600`, **outside the checkout** |

SearXNG's settings loader supports no `${VAR}` substitution for engine credentials. The tracked `resources/deploy/searxng/settings.yml` carries the `__GITHUB_SEARCH_TOKEN__` placeholder; the rendered copy holding configured credentials is written to the state root, outside Git's tree. Unset credentials disable their respective engines without failing startup.

No secret appears in the generation manifest, in generated artifacts, in logs, or in captured evidence, which redacts key-shaped content before persisting.

---

## Permissions

| Path | Mode | Why |
|---|---|---|
| `$AILOCAL_STATE` | `0700` | contains credentials and machine state |
| `$AILOCAL_STATE/active-profile` | `0600` | selects what the machine runs |
| `$AILOCAL_STATE/searxng/settings.yml` | `0600` | may carry the GitHub search token |
| `$AILOCAL_STATE/env` | `0600` expected | the generated secrets |
| `.env.local` | `0600` expected | your provider keys |

Verify with `ailocal check`, which reports either file readable by other users.

---

## Authored versus generated

Ownership of the four roots — who writes each, and what a delete recovers — is
stated once, in [AGENTS.md](../AGENTS.md#generated-state). The security-relevant
consequence: a tracked file is authored source or a template, never a runtime
artifact that generation rewrites, so generation cannot dirty the working tree
or introduce a secret into a commit.

---

## Container boundaries

The proxy container sees only what it needs:

| Mount | Access | Contents |
|---|---|---|
| `/app/config` | read-only | authored hooks, registry, config template |
| `/app/generated` | read-only | generated proxy configuration |
| `/app/captures` | writable | the only path the proxy may write |

Profiles, client templates and the repository itself are not mounted. SearXNG receives its rendered settings read-only and one authored limiter policy.

---

## Supply chain

Both images are pinned **by digest**, not by tag, so a moving tag cannot change what runs without a commit. The authoritative references are in the [LiteLLM Compose declaration](../src/ailocal/resources/deploy/litellm/compose.yaml) and [SearXNG Compose declaration](../src/ailocal/resources/deploy/searxng/compose.yaml); do not duplicate their digests in prose.

`ailocal check` gates the running build against the validated version, and the regression gate fails if they diverge.

`ailocal check` includes the supply chain: every image pinned by digest, the running image identical to the declared one, loopback-only binding, and provenance where a publisher signs. `ailocal check --updates` additionally asks upstream what exists; it never pulls over a running service and never rewrites a pin. A check that could not complete reports as such — an absent scanner is not a pass.

**Upgrading an image** means changing the digest, re-running the scan, running the full gate, and confirming the proxy still serves every alias with the expected geometry. A tag bump without a digest change is not an upgrade.

Follow [AI development maintenance](maintenance.md) for upstream issue review, candidate validation, and promotion evidence.

---

## Commit attribution

The rule and its rationale live in
[AGENTS.md](../AGENTS.md#change-discipline). Note that enforcement is by
convention: the repository ships no commit hook, deliberately — installing Git
policy was removed from the product installer, because a user installing local
inference did not ask for their Git configuration to change.

---

## Reporting

Report a vulnerability privately to the maintainer rather than opening a public issue.

Ownership: **DevelopSolutions, LLC**, Apache-2.0. Maintained by **Victor T. Chevalier**.
