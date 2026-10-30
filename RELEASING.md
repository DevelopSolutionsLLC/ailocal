# Releasing ailocal

Versioning follows Cadence's shared standard (`versioning.md` in Cadence's references: SemVer, Keep a Changelog, the 0.x rule, the release steps). This file holds only what is specific to ailocal.

From v0.9.0, `main` is a public contract. ailocal's published interface is its CLI commands and flags, the configuration layout, the install flow, the generated client configuration and profile behaviour. A change to any of them carries its migration note and its bump in the PR that makes it.

## ailocal specifics

- The version lives in `pyproject.toml` and `src/ailocal/__init__.py`; the gate fails unless they agree and `CHANGELOG.md` has a section for it.
- Full gate: `python3 tests/gate.py --full`, then `ailocal check`.
- There is one install path: `pipx install git+https://github.com/DevelopSolutionsLLC/ailocal.git@vX.Y.Z`. The README's install block and the release notes carry that one command for the release being cut; the wheel is attached to the release as the downloadable artifact, not offered as a second path.
- Update `README.md` when installation changes and `AGENTS.md` when the developer workflow changes.

### The clean-install check

**Manual, and the step most often skipped.** A human installs the built artifact on a Mac that has never run ailocal and confirms `ailocal check` passes. `tests/installed-runtime.py` is not a substitute: it proves the wheel needs no checkout but runs beside an existing installation. It was skipped for v0.9.1, v0.10.0 and v0.15.0 because no such machine was available; a skipped step is recorded in the release notes, never reported as passed. The replacement, an automated clean-state install gate with fresh `HOME`, `XDG_*` and config roots, is not built; when it is, it replaces this item.

## Cutting the release

```sh
sed -i '' 's/ailocal.git@v[0-9.]*/ailocal.git@vX.Y.Z/' README.md   # the one install command
python3 tests/gate.py --full
python3 tests/installed-runtime.py
ailocal check
python3 -m pip wheel . --no-deps -w /tmp/ailocal-dist   # outside the checkout: the gate rejects any untracked root entry
git tag -a vX.Y.Z -m "ailocal vX.Y.Z"
git push origin main --follow-tags
gh release create vX.Y.Z /tmp/ailocal-dist/ailocal-X.Y.Z-py3-none-any.whl --notes-file <(sed -n '/## vX.Y.Z/,/^## /p' CHANGELOG.md)
```
