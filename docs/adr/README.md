# Architecture Decision Records

Only major, durable, non-obvious decisions. Implementation detail lives in
`docs/architecture.md`.

| ADR | Decision |
|---|---|
| [`004-tool-gateway`](004-tool-gateway.md) | ADR 004 — Tool gateway and task classification |
| [`008-local-vs-hosted`](008-local-vs-hosted.md) | ADR 008 — Local and hosted models side by side |
| [`010-policy-format`](010-policy-format.md) | ADR 010 — Host policy is TOML, read by `tomllib` |

All three are Accepted; none is superseded.

Numbers are historical and are never reused or renumbered. A gap is a decision
recorded elsewhere in the stack or one that was never taken — not a missing file.
