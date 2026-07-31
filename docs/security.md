# Container supply chain

Every image this stack runs, why it is pinned the way it is, and what remains
unfixed. `ailocal security` checks the mechanical parts of this document on
demand; the judgement calls are recorded here because a scanner cannot make them.

Run `ailocal security` for pins, drift and reachability, `--scan` to add Docker
Scout. Exit 0 clean, 1 a real problem, 2 degraded.

## Pinning policy

**A tag is not a pin.** `latest`, `main` and `main-stable` are mutable
references; the maintainer can move them under a running deployment, and both
did during the audit that produced this page:

- `ghcr.io/berriai/litellm:main-stable` moved 1.92.0 → 1.93.0 while the
  documentation still claimed 1.92.0. Behaviour recorded as "verified on 1.92.0"
  had been verified against a release we were no longer running.
- `searxng/searxng:latest` was rebuilt mid-session, so the digest observed at the
  start of the audit was not the digest observed at the end.

So every image is referenced by `@sha256:` digest. Where a human-readable version
adds value, it is written as `name:vX.Y.Z@sha256:…` — the tag documents, the
digest binds.

`ailocal security` fails on a floating reference and on **drift**, which is the
failure a scanner never reports: editing a compose file does not restart a
container, so the repository can look patched while the old image keeps serving.

## Current images

| Image | Pin | Fixable critical/high |
|---|---|---|
| `ghcr.io/berriai/litellm` | `sha256:28b10a63…` (1.94.1) | none |
| `searxng/searxng` | `sha256:79c2be18…` | none |
| `qdrant/qdrant` (Cadence) | `v1.18.3@sha256:0bd98fa7…` | 1 critical, 10 high — see below |

### LiteLLM 1.93.0 → 1.94.1

A security upgrade, adopted 2026-07-30. 1.93.0 carried five fixable HIGH
findings — `pyasn1` 0.6.3 (×3) and `pypdf` 6.13.3 (×2). 1.94.1 ships `pyasn1`
0.6.4 and `pypdf` 6.14.2, and Scout reports no vulnerable package. Versions were
confirmed inside the running container with `importlib.metadata`, not read off
the scanner. The base image is Wolfi (Chainguard), not Alpine.

Validated by the full gate — 17/17, including persona injection (the coupling
that breaks silently when routing changes) and a real end-to-end benchmark
against the local model.

**The `anthropic_stream_logging_fix.py` shim is still required.** It works around
an upstream crash in `_handle_anthropic_messages_response_logging`. Re-read from
the *installed source* of 1.94.1: the method still early-returns only for the
`ResponseCompletedEvent` family and `ResponsesAPIResponse`, then reaches
`AnthropicResponse.model_validate(result)` with no iterator guard. Unfixed. Check
the source again after any upgrade rather than assuming a version bump fixed it.

## Qdrant: accepted residual findings

Qdrant **v1.18.3 is the newest stable release** — there is no upgrade that
removes these. The pin resolves to the exact digest already running, so pinning
changed nothing at runtime.

Scout reports 1 critical and 10 high fixable findings. They fall into two groups,
and the distinction is what makes them acceptable:

**Not present in the runtime (1 critical, 4 high).** `tar` 7.5.16,
`brace-expansion`, `axios`, `js-yaml`, `postcss` are npm/JS entries recorded in
the image's SBOM (`/qdrant/qdrant.spdx.json`) as **build dependencies of the Web
UI bundle**. Verified by execution, not inference:

    docker exec cadence-qdrant sh -c 'command -v node tar npm'   # -> nothing

There is no `node`, `npm` or `tar` binary in the image. The only artefact those
packages produced is the static bundle under `/qdrant/static`. `axios` is
browser-side code; it executes in whoever opens the dashboard, not in the
container. **The single CRITICAL is in this group** — `tar` 7.5.16 is a build
tool with no binary present, so there is no process to exploit.

**Compiled into the server binary (6 high).** `quinn-proto` (QUIC),
`quick-xml`, and `pyo3` are Rust crates linked into `qdrant`. These are real
runtime code. No fixed Qdrant release exists yet.

**Reachability.** Qdrant publishes `6333`/`6334` on `127.0.0.1` only — asserted
by `ailocal security`, which fails if any container publishes off-host. Nothing
in the group above is reachable from another machine. `quinn-proto` additionally
requires QUIC traffic, which never crosses the loopback boundary.

**Acceptance.** Accepted until a Qdrant release ships the updated crates.
Re-evaluate on any Qdrant upgrade, and immediately if the loopback binding is
ever relaxed — exposing the port converts the six compiled findings from
theoretical to live, and that change alone would invalidate this acceptance.

Qdrant is cosign-signed; a `.sig` tag matching the pinned digest is published
upstream. `cosign` is not installed here, so signature verification is available
but not currently automated.

## Reachability is the deciding factor

Package presence is not exposure. Every service in this stack binds to
`127.0.0.1` — LiteLLM 4000, Qdrant 6333/6334, SearXNG 8080 — so no finding in any
of these images is remotely reachable. `ailocal security` asserts the binding on
every run rather than trusting a compose file to still say so, because that
property is the load-bearing one behind every acceptance on this page.

## Handling an intentional failure

`scripts/tests/compat-routes.sh` asserts that `/v1/models` rejects an
unauthenticated request. LiteLLM correctly logs an ERROR traceback for that 401,
which reads like a fault in an otherwise-passing gate.

Suppressing it was declined: silencing production auth logging to make a test
read cleanly would remove real observability. Instead the traceback is
**bracketed in the proxy log itself** — the test writes `BEGIN`/`END
EXPECTED-AUTH-FAILURE` markers to the container's own stdout, so
`docker logs ailocal-litellm` shows the boundary inline around the traceback.
The gate's own output stays a single `PASS` line.

## Provenance and its limit

A digest proves the bytes did not change. It does **not** prove who published
them. `ailocal security` and `cadence security` report signature status, and
`install.sh` provisions `cosign` so a fresh machine can verify from day one.
`ailocal security` still refuses to install it at runtime — adding a verification
tool to a *running* system is the operator's call.

| Image | Signature |
|---|---|
| `ghcr.io/berriai/litellm` | published (+ attestations) |
| `qdrant/qdrant` | published |
| `searxng/searxng` | **none** |

**Do not use `cosign triangulate` to prove a signature exists.** It only computes
the expected `.sig` tag name from the digest and succeeds for unsigned images —
it reported a signature for SearXNG, which publishes none. It is also deprecated.
`cosign tree` queries the registry and is what these checks use.

### Deferred: identity-constrained verification

Sigstore's guidance is explicit that verification must include identity
constraints, not merely "is signed". That is **not implemented**, and presence is
the honest ceiling here.

- **Affected images:** `ghcr.io/berriai/litellm@sha256:28b10a63…`,
  `qdrant/qdrant@sha256:0bd98fa7…`. SearXNG is unaffected — it signs nothing.
- **Why:** neither publisher documents a signing identity, and
  `cosign verify --certificate-identity-regexp 'https://github.com/BerriAI/litellm/.*'
  --certificate-oidc-issuer https://token.actions.githubusercontent.com`
  fails with `no matching signatures: error verifying bundle: empty key` — the
  signature is not a standard keyless Fulcio bundle.
- **Manual check available today:**
  `cosign tree <image>@<digest>`
- **Re-evaluate when:** either publisher documents a signing identity or key, or
  an image moves to standard keyless signing. Recheck on any image upgrade.

Until then, the load-bearing controls remain the immutable digest, the
declared-vs-running drift check, and loopback binding.

## Update awareness

**Nothing here updates itself.** A pinned digest is fetched, never resolved
again, so `docker compose pull` on a pinned reference is a no-op and neither
`ailocal update` nor `cadence install` can advance an image. That is deliberate:
an image replaced without validation is an untested production change.

The cost of that safety is staleness, which is what the update check exists to
answer:

    ailocal security --check-updates     # LiteLLM, SearXNG   (ailocal owns these)
    cadence security --check-updates     # Qdrant             (Cadence owns this)

Each product checks **only its own images**; neither edits the other's pin. They
share a contract, not an implementation — a shared library across two
independently installable repositories would be a cross-repo dependency, a worse
trade than a few dozen lines of shell each.

Both report configured digest, running digest, newest upstream release, whether
the configured release is behind, and the exact apply/rollback commands. Exit
codes are identical:

| Exit | Meaning |
|---|---|
| 0 | current, or no actionable update |
| 1 | action required — drift, a floating pin, or an update available |
| 2 | check incomplete — Docker, network, or scanner unavailable |

**A newer release is not a failure.** It is reported, and a human decides.
Discovery never pulls a candidate over a running service.

One subtlety worth knowing: comparing digests alone is wrong. The LiteLLM pin
tracks `main-stable`, which runs *ahead* of the newest tagged GitHub release, so
a digest-only comparison proposed v1.94.0 as an "update" to the 1.94.1 already
running — a downgrade. The check compares versions wherever a version is knowable.

### Optional weekly check

Off unless installed:

    ailocal security --install-schedule      # Mondays 10:00, reports only
    ailocal security --uninstall-schedule

One LaunchAgent, not two: ailocal owns the schedule and invokes *each product's
own* check, so Cadence keeps ownership of its logic while the machine gets a
single wakeup. It upgrades nothing, writes state under
`$XDG_STATE_HOME/ailocal/`, and raises a notification only when a check exits 1.
Executables are resolved absolutely, because launchd provides no interactive
shell and a minimal PATH.

## Upgrading an image

1. `docker pull` the candidate; read its versions from inside it.
2. `docker scout cves --only-fixed --only-severity critical,high` on old and new.
3. Update the digest, and any version variable beside it, in the same commit.
4. `ailocal start` — a compose edit alone does not restart the container.
5. `scripts/test-all.sh --full`.
6. `ailocal security` to confirm declared and running digests agree.
