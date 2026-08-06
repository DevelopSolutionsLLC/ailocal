# Operations

## Scope

This document owns lifecycle, synchronization, validation, deployment, and
recovery procedures. Architecture belongs in `architecture.md`; symptoms and
diagnosis belong in `troubleshooting.md`.

## Normal lifecycle

```sh
ailocal status
ailocal start
ailocal stop
ailocal update
```

`ailocal` is the only supported public entry point. Do not invoke modules under
`lib/` as operational interfaces.

## Configuration changes

1. Edit an authored source: a profile, client policy, registry, or template.
2. Run `ailocal sync`.
3. Run `ailocal validate`.
4. Run `ailocal clients` when rendered client output changed.
5. Run the smallest relevant test section, then `ailocal test` before commit.

Generated output lives under `${AILOCAL_STATE:-~/.local/state/ailocal}` and is
never edited directly.

## Recovery

Deleting the state root and running `ailocal sync` is a supported recovery.
The generator stages and validates replacements before publishing them and
writes its completion marker last.

Use:

```sh
ailocal doctor
ailocal validate
ailocal smoke
```

- `doctor` diagnoses environment and runtime health.
- `validate` checks deterministic configuration consistency and works while the
  stack is stopped.
- `smoke` performs a bounded live model request.

Do not treat a transient failure as success. Investigate it, and run
timing-sensitive checks twice when the repository contract requires it.

## Client deployment

```sh
ailocal clients
ailocal vscode
```

Client templates under `clients/` are authored sources. Rendered client
configuration is deployed outside the checkout. Local-client preloads describe
the ailocal runtime and compatibility boundary only; repository-specific rules
remain in each repository's `AGENTS.md`.

## Secrets and exposure

Secrets belong in the configured environment files and rendered state, never
in tracked source. Generated files carrying credentials use restrictive
permissions. Services bind to `127.0.0.1` unless the security architecture is
explicitly changed and reviewed.

See `security.md` for the complete security contract.
