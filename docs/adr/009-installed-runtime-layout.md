# 009: Installed runtime layout

- Status: Proposed
- Date: 2026-08-06
- Supersedes: none
- Superseded by: none

## Context

ailocal runs from its Git checkout. The checkout is not a build input; it is the
running installation. Three mechanisms encode this:

- `lib/compose.sh` derives `AILOCAL_ROOT` from its own file location and passes
  `--project-directory "$AILOCAL_ROOT"` to Docker Compose.
- `deploy/litellm/compose.yaml` bind-mounts `./deploy/litellm:/app/config:ro`,
  resolved against that project directory.
- Compose auto-discovers `.env` from the project directory, and
  `lib/compose.sh` reads `$AILOCAL_ROOT/.env` directly for `BRAVE_API`.

Consequences of checkout-as-runtime: a `git checkout` of another branch changes
the running configuration; moving or deleting the checkout breaks a working
installation; two checkouts cannot be reasoned about; and there is no upgrade or
uninstall path other than manipulating a working tree.

Packaging the CLI via `[project.scripts]` removes the root dispatcher but does
not by itself resolve this. An installed console script in `site-packages` has
no checkout, so the question of where runtime assets live must be answered
first. Every later migration phase encodes the answer.

Four questions are independent and must not collapse into one root:

1. Where does installed Python code live?
2. Where do authored runtime assets live (`deploy/`, `clients/`)?
3. Where does user-editable configuration live (`profiles/`, `.env`)?
4. Where does generated and mutable state live?

Question 4 is already answered and is not reopened here: generated state lives
under `${AILOCAL_STATE:-~/.local/state/ailocal}`, and `ailocal sync` is its only
writer. That contract works and is retained.

## Decision

Adopt a **managed installation root**. The Python package owns code only.
Authored assets are installed into user-owned XDG locations at install time.

```
Python package            pipx / Homebrew / managed venv (code only)

${XDG_CONFIG_HOME:-~/.config}/ailocal/
    profiles/             user-editable policy
    clients.yaml
    .env                  secrets, mode 0600

${XDG_DATA_HOME:-~/.local/share}/ailocal/
    deploy/litellm/       hooks, registry, config template, compose
    deploy/searxng/
    clients/              authored client templates

${XDG_STATE_HOME:-~/.local/state}/ailocal/
    active-profile
    generated/            config.yaml, capabilities.json, effective-profile.json
    captures/
    benchmark/
```

The source checkout becomes a development input, not part of a running
installation. `ailocal install` copies shipped defaults into the config and data
roots; `ailocal sync` continues to write only into the state root.

Compose runs with `--project-directory ${XDG_DATA_HOME}/ailocal` and an explicit
`--env-file ${XDG_CONFIG_HOME}/ailocal/.env`. The relative mount
`./deploy/litellm` continues to resolve, and `.env` is no longer required to sit
beside the compose files. Without the explicit `--env-file`, splitting config
from data silently drops `.env` discovery.

### Path policy: XDG, consistently

One policy, applied everywhere: **XDG base directories**, with the documented
`AILOCAL_CONFIG`, `AILOCAL_DATA` and `AILOCAL_STATE` overrides. macOS-native
`~/Library/Application Support` is not wrong in principle — it is the platform
equivalent — but the state root already resolves to `~/.local/state/ailocal`,
so XDG is the established precedent and mixing the two is the actual defect.

`~/Library/Application Support/ailocal/preload.sh` is therefore a straggler, not
a second convention. It moves to the data root in the same phase that provisions
that root. No component may introduce a macOS-native location without
superseding this record.

### Shipped defaults versus user configuration: copy-on-install

Profiles and client policy are **copy-on-install editable files**. `ailocal
install` copies shipped defaults into the config root on first installation and
never overwrites them afterward without the rules below.

The layered model — immutable shipped defaults plus a user override file — is
cleaner long-term, but it introduces a merge step, a precedence order, and a
second thing to read when debugging why a value took effect. `lib/policy.py`
fails closed today precisely because there is exactly one place a value can come
from. Copy-on-install preserves that property. Layering may supersede this ADR
once the packaging migration is stable.

Upgrade behavior, stated explicitly:

- **What ailocal may replace.** Only files under the data root
  (`deploy/`, `clients/`, installed static assets). These are shipped assets
  with no supported edit surface; they are replaced wholesale on upgrade.
- **What ailocal may never replace automatically.** Anything under the config
  root: `profiles/`, client policy, `.env`.
- **How edits are detected.** `ailocal install` writes
  `$XDG_STATE_HOME/ailocal/install-manifest.json` recording the SHA-256 of every
  file it installed. On upgrade, a config file whose digest still matches its
  manifest entry is byte-identical to what was shipped and is replaced silently;
  a file whose digest has diverged is user-edited, is left untouched, and is
  reported. Provenance, not location, decides.
- **How new default keys reach existing installations.** They do not arrive by
  file replacement. `ailocal validate` already rejects unknown fields and
  fails closed on missing ones; it gains the inverse check — a shipped default
  key absent from a user-edited profile is reported with the value that would
  apply. The operator merges deliberately. Silent key injection into an edited
  policy file is the failure mode this avoids.
- **Backups.** Before any replacement in either root, the prior file is copied
  to `$XDG_STATE_HOME/ailocal/backups/<iso8601>/`. Retained for the last three
  upgrades.

### Data-root installation is atomic

The data root is installed and upgraded as a transaction, matching the
guarantee `ailocal sync` already provides for generated state:

1. Stage the new tree under `$XDG_DATA_HOME/ailocal/.staging-<pid>`.
2. Validate: every required file present, digests match the incoming manifest,
   compose files parse, hook modules parse.
3. Swap directories into place per top-level component (`deploy/`, `clients/`),
   retaining the previous tree as `.rollback`.
4. On any failure before the swap, discard the staging tree and change nothing.
   On failure during the swap, restore from `.rollback` and report.

A partially replaced `deploy/` tree — new compose file against old hooks — is
the specific outcome this prevents. `ailocal doctor` reports a `.staging-*` or
`.rollback` directory that outlived its transaction.

### Benchmarks are developer-only

`ailocal benchmark` is **not an end-user product capability**. This is already
the declared contract: *"Benchmarks are a developer utility: they never modify a
profile, and they are not part of install, update or the regression gate."*

Consequently benchmark assets are **not installed** — not into the data root and
not as package data. They retain an explicit checkout dependency and are
excluded from ordinary installation. `benchmarks/` stays in the repository and
runs from a checkout only. Should benchmarks ever become an end-user capability,
their assets move under the data root and this paragraph is superseded.

**Profile format is deferred to ADR 010** and is not decided here. It is a
separate question with a separate blast radius, and coupling it to packaging
would make both harder to revert. Evidence gathered for that decision is
recorded under *Profile format* below.

## Consequences

Gained: a real installed command; no permanent Git-checkout dependency; normal
Compose paths; editable profiles that survive upgrade; working uninstall and
rollback; clean separation of authored configuration, installed assets, and
generated state.

Costs and new obligations:

- Installation gains a copy step and a manifest. `ailocal install` becomes
  responsible for a filesystem layout it previously inherited.
- Three roots must be resolved by one owner. `lib/policy.py` currently owns
  policy-path resolution; that role extends to config, data, and state roots,
  and nothing else may compute them.
- A stale data root can disagree with installed code. `ailocal doctor` must
  report the installed version against the data-root manifest version.
- `AILOCAL_ROOT` disappears as a public concept. Every consumer in the
  dependency map below changes with it.
- Development and installed runtime diverge. A developer running from a
  checkout must be able to point at that checkout deliberately, which is what
  the transitional support below provides.

Not changed: the state root and `ailocal sync` as its only writer; profiles as
first-class authored policy; `deploy/` and `clients/` as data rather than code;
the eight LiteLLM hooks as a container-loaded subsystem.

## Alternatives considered

**A. Package all assets (`importlib.resources`).** Rejected. `site-packages` is
frequently read-only, and Homebrew's is owned by the package manager, so
user-edited profiles have nowhere to live. Bind-mounting out of `site-packages`
is possible but couples container mounts to Python's install layout, and every
upgrade discards profile edits. Fails the "profiles are authored policy"
premise.

**B. Installed CLI with remembered repository root.** Rejected as a final
architecture; accepted as a bounded transitional stage. It preserves
checkout-as-runtime under a new name: relocating the checkout breaks the
install, a branch change silently alters running configuration, multiple
checkouts remain ambiguous, and deleting the checkout leaves an installed
command with no assets. It minimizes the first migration diff, which is not a
sufficient reason to make a Git checkout a permanent runtime contract.

*Transitional support:* `AILOCAL_DEV_ROOT` may point at a checkout so developers
can run uninstalled. It is opt-in, absent from `ailocal help`, and reported by
`ailocal doctor` when set. **Removal criteria:** deleted once (1) `ailocal
install` provisions all three roots from a package with no checkout present,
(2) the gate passes against an installed package with the checkout moved aside,
and (3) the migration path in Phase 5 has run once on a real installation. It is
not a supported configuration and carries no compatibility guarantee.

**C. Editable install (`pip install -e .`).** Development mode only. It is how a
contributor works on the package; it is not product distribution, because it
requires a checkout and offers no upgrade or uninstall semantics.

## Profile format

Evidence for the deferred decision (ADR 010).

`lib/policy.py` hand-rolls a narrow YAML parser (`parse_profile_text`,
`_strip_comment`, `_coerce`) to keep the host path dependency-free. It parses
exactly five files: `profiles/{16,32,64,128}gb.yaml` and `profiles/clients.yaml`
(665 lines total). Every other YAML file in the repository is consumed by
something else and is out of scope:

- `deploy/litellm/registry.yaml` is read inside the container by real PyYAML.
- `deploy/litellm/config.template.yaml` and the generated `config.yaml` are
  LiteLLM's format.
- `deploy/*/compose.yaml` is Docker Compose's format.
- `deploy/searxng/settings.yml` is SearXNG's format.
- `benchmarks/*.yaml` is read by the benchmark suites.

So the custom parser serves five files and nothing else. Options:

1. **Retain the narrow parser.** No migration. Keeps a bespoke parser that must
   be maintained and is a second YAML dialect by definition.
2. **PyYAML.** Removes the parser, adds the first host runtime dependency, and
   requires a venv for `ailocal test`, which currently runs on bare `python3`.
3. **TOML + `tomllib`.** Removes the parser with no runtime dependency.
   `tomllib` is standard library from Python 3.11; the development host runs
   3.14.6. `sync-models.py` already emits TOML for the Codex templates, so the
   format is not foreign.

Option 3 is preferred if semantic equivalence is practical. The profiles are
two-level maps of scalars and inline string lists with no anchors, aliases, or
block scalars, so they map cleanly. Two migration hazards are known and must be
handled explicitly rather than discovered:

- `profiles/clients.yaml` contains compat keys with dots (`gpt-5.5`,
  `claude-opus-4-8-20251101`). A bare dotted key in TOML denotes nesting, so
  every such key must be quoted.
- `keep_alive` holds both `6h` and `-1`. YAML coerces loosely; TOML does not.
  The field must be typed deliberately, most likely as a string.

Comment fidelity is a genuine cost: the profiles carry substantial provenance
comments (`config-verified, NOT measured on 16 GB hardware`) that the evidence
policy requires. TOML preserves comments in the file, but any generator that
rewrites a profile would drop them. Profiles are authored, never generated, so
this is acceptable — but it must stay true.

ADR 010 must prove **semantic equality**, not plausibility: load all five policy
files through the current parser and through `tomllib`, normalize both to the
same policy object, and compare. It must explicitly cover quoted dotted keys,
mixed scalar types (`6h` and `-1`), comment retention, strict validation
(unknown-field and duplicate-key rejection must survive the format change), and
rollback. Conversion is considered only after the runtime-path migration is
stable, and never inside the same commit as a packaging change.

## Migration phases

0. This ADR, plus ADR 010 for profile format. No code.
1. **Path APIs.** `config_root()`, `data_root()`, `state_root()` land in
   `policy.py` as the sole owners, all still resolving to the checkout.
   Behavior unchanged; only derivation moves. Nothing else may compute a root.
2. **Fix generated scheduled jobs.** The update-check LaunchAgent stops
   embedding `$REPO/lib/security.sh` and invokes the installed `ailocal`
   command; until that entry point exists, `--install-schedule` refuses rather
   than writing a checkout path. See *Early defect* below.
3. `pyproject.toml`, `src/ailocal/`, `[project.scripts]`. `cli.py` dispatches to
   existing implementations; root `ailocal` becomes a thin shim.
4. `ailocal install` provisions the config and data roots, writes the manifest,
   and performs the atomic swap. `preload.sh` moves out of Application Support.
5. Compose switches to `--project-directory` on the data root plus explicit
   `--env-file`.
6. **Prove operation with the checkout moved.** The decisive test below.
7. Lifecycle, clients, generation, and checks move into the package, one phase
   each, gate green between phases.
8. Migration for existing installations: detect a checkout-based install, copy
   assets into the new roots, verify, report.
9. Delete the root shim and `AILOCAL_DEV_ROOT` once the removal criteria are met.
10. Profile format (ADR 010) only after runtime-path migration is stable.

### Early defect

`ailocal security --install-schedule` generates a runner under the state root
that hard-codes `"$REPO/lib/security.sh" --check-updates` (`lib/security.sh:61`).
A generated, scheduled artifact therefore already depends on the checkout not
moving — a defect under the current architecture, not merely a migration item.
It is scheduled at Phase 2, before packaging, because a scheduled job that
embeds a repository implementation path fails silently and on someone else's
timetable. **Generated scheduled jobs must invoke the installed `ailocal`
command and must never embed a checkout path.**

## Rollback

Phases 1 through 4 are revertible by Git revert; the running installation is
unaffected because assets have not moved. Phase 3 is the first irreversible-ish
step: it writes outside the checkout. It must be reversible by
`ailocal cleanup`, which already removes installation artifacts and must learn
the two new roots in the same change. Phase 5 must not delete the checkout or
the old state root until the operator confirms.

## Validation requirements

### The decisive proof

The migration is complete only when, with the source checkout **renamed or moved
out of reach**, an installed package and installed static assets still serve:

```
ailocal help
ailocal validate
ailocal sync
ailocal doctor
ailocal start
```

Nothing else demonstrates that checkout coupling is gone. Passing the gate from
inside a checkout cannot show this, because the checkout supplies the assets.
This runs as an opt-in suite — it moves a real installation — and is the exit
criterion for Phase 6 and for deleting `AILOCAL_DEV_ROOT`.

### Ongoing

- `ailocal sync` remains a fixed point across all three roots.
- An edited profile survives an upgrade; an unedited shipped default is
  replaced. Both directions asserted, driven by the manifest digest.
- A shipped default key absent from a user-edited profile is reported by
  `validate`, never injected.
- A data-root upgrade interrupted mid-swap leaves either the old tree or the new
  tree, never a mixture; `.rollback` restores.
- Uninstall leaves no LaunchAgent, no container, and no file in any of the three
  roots.
- No generated scheduled job contains a path inside a Git checkout.
- Two installed versions do not share a data root without `doctor` reporting the
  mismatch.
- `.env` is never copied into the data root and never appears in a bind mount.
- No component resolves a root except via `config_root()`, `data_root()` or
  `state_root()`.
