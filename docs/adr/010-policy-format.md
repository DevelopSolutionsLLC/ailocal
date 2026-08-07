# ADR 010 — Host policy is TOML, read by `tomllib`

**Status:** Accepted (2026-08-07)

## Context

The five host-policy files — `profiles/{16,32,64,128}gb` and `profiles/clients`
— were YAML, read by a constrained parser written into `policy.py`: about 185
lines covering two levels, scalars, flow lists, flow mappings, comment
stripping and scalar coercion. It existed because PyYAML is absent from the
host interpreter and provisioning a virtual environment to read five small
files was disproportionate.

That reasoning held while the alternative was PyYAML. It does not hold against
`tomllib`, which is in the standard library from Python 3.11 and needs no
environment at all.

## Decision

The five files are TOML. `tomllib` reads them. The hand-written parser, its
scalar coercion and its comment stripping are deleted, with no YAML fallback
and no PyYAML dependency. `deploy/litellm/registry.yaml` and the generated
LiteLLM configuration stay YAML: LiteLLM defines that format.

## Evidence

Every construct the policy files use round-trips identically, and the two
failure modes the old parser was written to catch are caught by `tomllib`
itself:

| Construct | Result |
|---|---|
| `"gpt-5.5" = "implementation"` | key preserved verbatim |
| `keep_alive = -1` | int |
| `keep_alive = "6h"` | str |
| `reasoning = true` | bool |
| `purpose = [...]` | list of str |
| `slots = {opus = "architecture"}` | inline table |
| `disk_gb = 40` before any table | top-level scalar |
| comments | dropped |
| duplicate table | `TOMLDecodeError` |
| duplicate key | `TOMLDecodeError` |
| malformed file | `TOMLDecodeError` |

Unknown-key rejection is schema validation in `policy._validate_role`, not a
property of the format, and is unchanged.

Generation is a fixed point across the change. Regenerating every artefact from
the YAML sources and from the converted TOML sources produced byte-identical
`litellm/config.yaml`, `model_catalog.json`, `codex/config.toml`,
`configure.zsh`, `integration-contract.json` and the Copilot instructions. Only
the recorded provenance differs, which is correct: the source file changed.

## The one hazard, and its guard

An **unquoted** key containing a dot is a *dotted key* in TOML:
`gpt-5.5 = "implementation"` silently nests into
`{"gpt-5": {"5": "implementation"}}` rather than naming a model. It parses
cleanly, so nothing would report it. `load_client_policy` therefore requires
every `compat` value to be a string, and the test suite asserts the bare form is
rejected.

## Consequences

`policy.py` loses 223 lines net. There is no second parser to keep in step with
the schema, and no scalar-coercion table to reason about when a value looks
like a number. Policy files gain quoting they did not need before; the comments
that carry the measurement evidence are unaffected.
