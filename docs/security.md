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

**Is the pin still safe?** `ailocal audit` checks every declared pin, and the Ollama base actually running, against live advisory sources, before a release and in weekly maintenance. That question changes daily and needs a network, so the audit is not part of `ailocal check` or the gate. Container images have no OSV ecosystem; the audit maps each image to its publisher's GitHub repository explicitly, and reports an unmapped image as unverified. Each run is recorded under the state root and published through the integration contract, so the machine-wide weekly audit (`cadence audit --local`) reports these findings once a week beside everything else on the machine.

**Auditing a component.** Required for every tool ailocal installs, provisions, bundles or relies on: SearXNG, LiteLLM, Ollama, Claude Code, Codex, VS Code/Copilot, Docker, Cadence, grepai, language servers, MCP servers, plugins, interpreters and vendored browser libraries, including transitive dependencies and enabled extensions. Before adoption, upgrade or release: inventory actual versions and digests; check upstream advisories and affected/fixed ranges; scan dependency inventories and images against a current advisory database; review provenance, permissions, network exposure and the first-party integration code affected. Record the date, commands, database freshness, sources, findings and coverage gaps in the issue or PR, and resolve or disposition every applicable finding before promotion. An unavailable scan or an unassessed remote service stays unverified; a clean scan is never proof the code is free of vulnerabilities.

Follow [AI development maintenance](maintenance.md) for upstream issue review, candidate validation, and promotion evidence.

---

## Model artifacts

The shipped 16 GB profile serves standard Qwen 4B MTP from Ollama's library and unrestricted Qwen 4B MTP from the community publisher `localslm` on Hugging Face. The latter is a third-party derivative, not an official Qwen release. Model cards, refusal counts and names are publisher claims rather than security certification or independently reproduced quality evidence.

The [source importer](../src/ailocal/model_sources.py) uses native Ollama pull/create for GGUF and asset-only Safetensors imports. It does not execute downloaded repository Python, installation scripts or pickle checkpoints. Snapshot recipes bind a repository commit and verify Git/LFS content hashes. Native recipes bind the target weight SHA-256 and any explicitly declared draft SHA-256, require the selected built-in renderer/parser, and record the resulting alias manifest digest. A changed recipe or resulting manifest invalidates the receipt. Successful empty-message loading is a compatibility check, not a malware scan or full-window memory/quality test.

**Medium — native source pin verification, fixed 2026-10-27.** The former verifier searched the entire generated Modelfile for matching `FROM`/`DRAFT` lines. A publisher-controlled multiline template or license could contain such text and impersonate a reviewed asset, potentially causing an unreviewed target to pass the pin check. [APPROX] A simulated daemon response reproduced that false acceptance; no malicious real-world model was demonstrated. Verification now reads only Ollama's leading asset header, requires the first `FROM` to be the target, checks the exact external-draft list, and revalidates asset bindings even for existing receipts. Regression cases cover template/license spoofing, projector confusion, fake draft directives and old receipts. [REAL] All four existing native source-backed selections passed the stricter binding check against the actual daemon.

**Medium — incomplete full-artifact locking, remaining limit.** Standard library tags are mutable, and native recipes currently pin the main weights and declared heads rather than every projector and metadata layer in the source manifest. An upstream change could therefore alter an unpinned component without changing the reviewed main-weight hash. Fully locking the model supply chain requires reviewed complete-manifest/component pins and an explicit update procedure. An alias receipt detects a change after import; it does not independently authenticate its publisher or make an unpinned source tag immutable.

Hashes establish byte identity, not absence of poisoned behavior, backdoors or unsafe generated code. [Hugging Face's security features](https://huggingface.co/docs/hub/security) provide additional scanning and account protections, and [Safetensors](https://huggingface.co/docs/safetensors/index) avoids pickle-style serialization execution; neither establishes that a model behaves safely. GGUF/Safetensors still enter native parsers and inference libraries. This review did not fuzz those parsers, independently audit training data, certify community publishers, or perform a fresh full-toolchain advisory/container scan. Unrestricted weights have fewer refusal constraints; client tool permissions and isolation remain separate controls.

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
