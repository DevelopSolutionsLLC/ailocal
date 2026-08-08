# Releasing ailocal

From v0.9.0 onward, `main` is a public contract. Improving it is expected;
changing it out from under an installed machine is not. Anything user-facing —
CLI commands and flags, configuration layout, the install flow, generated client
files, profile behaviour — needs a migration story, a release note, and the
right version bump decided BEFORE it merges, not after someone's `ailocal check`
turns red.

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
- [ ] pass a clean install on a machine that has never run ailocal
- [ ] pass `ailocal check`
- [ ] include release notes in `CHANGELOG.md`
- [ ] update `README.md` if installation changed
- [ ] update `AGENTS.md` if the developer workflow changed
- [ ] bump the version in BOTH `pyproject.toml` and `src/ailocal/__init__.py`

Breaking changes are documented in the release notes. A breaking change that
ships without one is a defect, whatever the version number says.

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
