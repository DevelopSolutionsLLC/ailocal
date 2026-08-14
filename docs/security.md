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
| `BRAVE_API` | `.env.local` (yours; optional, empty disables the engine) | rendered into SearXNG settings at start |
| `SEARXNG_SECRET` | `$AILOCAL_STATE/env` (generated) | gitignored |
| Rendered SearXNG settings | `$AILOCAL_STATE/searxng/settings.yml` | mode `0600`, **outside the checkout** |

**Why the rendered settings live outside the repository.** SearXNG has no environment interpolation for an engine's `api_key`: its settings loader supports a fixed allow-list of variables and no `${VAR}` substitution, so the Brave key cannot be passed the way `SEARXNG_SECRET` is. The tracked `resources/deploy/searxng/settings.yml` therefore carries a placeholder and the rendered copy is written to the state root. Being outside Git's tree makes committing it impossible rather than merely discouraged.

No secret appears in the generation manifest, in generated artifacts, in logs, or in captured evidence, which redacts key-shaped content before persisting.

---

## Permissions

| Path | Mode | Why |
|---|---|---|
| `$AILOCAL_STATE` | `0700` | contains credentials and machine state |
| `$AILOCAL_STATE/active-profile` | `0600` | selects what the machine runs |
| `$AILOCAL_STATE/searxng/settings.yml` | `0600` | carries the Brave key |
| `$AILOCAL_STATE/env` | `0600` expected | the generated secrets |
| `.env.local` | `0600` expected | your provider keys |

Verify with `ailocal check`, which reports either file readable by other users.

---

## Authored versus generated

A tracked file is authored source or a template. It is never a runtime artifact that generation rewrites, which means generation cannot dirty the working tree and cannot introduce a secret into a commit.

Generated output is disposable: deleting `$AILOCAL_STATE` and re-running `ailocal start` fully recovers it. Nothing of value is stored only there except capture history.

---

## Container boundaries

The proxy container sees only what it needs:

| Mount | Access | Contents |
|---|---|---|
| `/app/config` | read-only | authored hooks, registry, template |
| `/app/instructions` | read-only | personas |
| `/app/generated` | read-only | generated proxy configuration |
| `/app/captures` | writable | the only path the proxy may write |

Profiles, client templates and the repository itself are not mounted. SearXNG receives its rendered settings read-only and one authored limiter policy.

---

## Supply chain

Both images are pinned **by digest**, not by tag, so a moving tag cannot change what runs without a commit:

- `ghcr.io/berriai/litellm@sha256:a1745e62…`
- `searxng/searxng@sha256:79c2be18…`

`ailocal check` gates the running build against the validated version, and the regression gate fails if they diverge.

`ailocal check` includes the supply chain: every image pinned by digest, the running image identical to the declared one, loopback-only binding, and provenance where a publisher signs. `ailocal check --updates` additionally asks upstream what exists; it never pulls over a running service and never rewrites a pin. A check that could not complete reports as such — an absent scanner is not a pass.

**Upgrading an image** means changing the digest, re-running the scan, running the full gate, and confirming the proxy still serves every alias with the expected geometry. A tag bump without a digest change is not an upgrade.

---

## Commit attribution

Commits carry one identity: the configured human author, with no assistant attribution trailers or session identifiers.

Session URLs are permanent public metadata that cannot be recalled once pushed — a force-push removes them from a branch, but the objects can remain reachable by SHA. The hook is the mechanical guarantee that a written rule is not.

Commits carry one identity: the configured human author.

---

## Reporting

Report a vulnerability privately to the maintainer rather than opening a public issue.

Ownership: **DevelopSolutions, LLC**, Apache-2.0. Maintained by **Victor T. Chevalier**.
