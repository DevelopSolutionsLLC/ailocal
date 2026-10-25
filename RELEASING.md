# Releasing ailocal

Versioning follows Cadence's shared standard (`versioning.md` in Cadence's references: SemVer, Keep a Changelog, the 0.x rule, the release steps). This file holds only what is specific to ailocal.

From v0.9.0, `main` is a public contract. ailocal's published interface is its CLI commands and flags, the configuration layout, the install flow, the generated client configuration and profile behaviour. A change to any of them carries its migration note and its bump in the PR that makes it.

## ailocal specifics

- The version lives in `pyproject.toml` and `src/ailocal/__init__.py`; the gate fails unless they agree and `CHANGELOG.md` has a section for it.
- Full gate: `python3 tests/gate.py --full`, then `ailocal check`.
- Update `README.md` when installation changes and `AGENTS.md` when the developer workflow changes.

### The clean-install check: what it is now, and what should replace it

**Current requirement — MANUAL.** A human installs onto a machine that has never run ailocal and confirms it comes up. Nothing in this repository automates it. `tests/installed-runtime.py` is the closest thing and is *not* a substitute: it proves the wheel carries its own assets and needs no checkout, but it runs beside an existing installation, so it cannot exercise first-run behaviour.

Because it is manual, it is the one item that gets skipped. It was skipped for **v0.9.1** and **v0.10.0** — no such machine was available. That is recorded here rather than left unstated, since a checklist item quietly skipped is indistinguishable from one that passed.

**Future replacement — NOT YET BUILT.** The intended fix is an automated clean-state install gate: fresh `HOME`, `XDG_*` and config roots, install from the built wheel, no repository checkout anywhere on the path, no prior generated state, then assert the stack comes up. Nothing of this exists today; when it does, it replaces the manual item above and this section goes with it.

## Cutting the release

```sh
python3 tests/gate.py --full
ailocal check
git tag -a vX.Y.Z -m "ailocal vX.Y.Z"
git push origin main --follow-tags
gh release create vX.Y.Z --notes-file <(sed -n '/## vX.Y.Z/,/^## /p' CHANGELOG.md)
```
