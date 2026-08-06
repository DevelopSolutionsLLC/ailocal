# Architecture Decision Records

Use an ADR for a durable, non-obvious architectural decision whose rationale
will matter after the implementation context is gone.

## Naming

Use `NNNN-short-decision-title.md`. Numbers are permanent and are not reused.

## Status

- Proposed
- Accepted
- Superseded
- Deprecated

A superseded record remains in the repository and links to its replacement.

## Template

```markdown
# NNNN: Decision title

- Status: Proposed
- Date: YYYY-MM-DD
- Supersedes: none
- Superseded by: none

## Context

## Decision

## Consequences

## Alternatives considered
```

## Boundaries

Use an ADR for architecture, ownership, compatibility boundaries, or a
costly-to-reverse technical choice. Use `docs/troubleshooting.md` for
operational symptoms and fixes, `benchmarks/README.md` for reproducibility, and
Git history for routine implementation detail. Do not preserve session
narratives in an ADR.
