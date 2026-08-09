# Releasing ailocal

From v0.9.0 onward, `main` is a public contract. Improving it is expected; changing it out from under an installed machine is not. Anything user-facing — CLI commands and flags, configuration layout, the install flow, generated client files, profile behaviour — needs a migration story, a release note, and the right version bump decided BEFORE it merges, not after someone's `ailocal check` turns red.

## What each bump means

**Patch (0.9.x)**

- Bug fixes
- Documentation improvements
- Performance improvements
- Internal refactoring with no behavioural change

**Minor (0.x+1)**

- New commands
- New client support
- New model profiles
- New capabilities

**Major (1.x)**

- Breaking CLI changes
- Configuration migrations
- Behavioural changes requiring user action

## Every release must

- [ ] pass the full gate — `python3 tests/gate.py --full`
- [ ] pass a clean install on a machine that has never run ailocal (still manual — see below)
- [ ] pass `ailocal check`
- [ ] include release notes in `CHANGELOG.md`
- [ ] update `README.md` if installation changed
- [ ] update `AGENTS.md` if the developer workflow changed
- [ ] bump the version in BOTH `pyproject.toml` and `src/ailocal/__init__.py`

Breaking changes are documented in the release notes. A breaking change that ships without one is a defect, whatever the version number says.

### The clean-install check: what it is now, and what should replace it

**Current requirement — MANUAL.** A human installs onto a machine that has never run ailocal and confirms it comes up. Nothing in this repository automates it. `tests/installed-runtime.py` is the closest thing and is *not* a substitute: it proves the wheel carries its own assets and needs no checkout, but it runs beside an existing installation, so it cannot exercise first-run behaviour.

Because it is manual, it is the one item that gets skipped. It was skipped for **v0.9.1** — no such machine was available. That is recorded here rather than left unstated, since a checklist item quietly skipped is indistinguishable from one that passed.

**Future replacement — NOT YET BUILT.** The intended fix is an automated clean-state install gate: fresh `HOME`, `XDG_*` and config roots, install from the built wheel, no repository checkout anywhere on the path, no prior generated state, then assert the stack comes up. Nothing of this exists today; when it does, it replaces the manual item above and this section goes with it.

## Compatibility promise

Future releases aim to preserve compatibility for:

- CLI commands
- configuration layout
- generated client configuration
- profile behaviour

## Cutting the release

```sh
python3 tests/gate.py --full
ailocal check
git tag -a vX.Y.Z -m "ailocal vX.Y.Z"
git push origin main --follow-tags
gh release create vX.Y.Z --notes-file <(sed -n '/## vX.Y.Z/,/^## /p' CHANGELOG.md)
```
