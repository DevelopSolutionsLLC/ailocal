# Build checklist — local-model coding sessions

Local 32B models degrade past ~32-64K effective context. This checklist keeps
sessions short, diffs small, and claims verifiable. Follow it literally.

## Before editing

- Read the target file in this session before touching it. Never edit from
  memory of an earlier turn or a different session.
- Find existing patterns in the file/module first: naming, error handling,
  logging style, indentation. Match them; do not invent new conventions.
- Check callers: grep for the function/variable/config key you're about to
  change so you know who depends on it before you change its shape.
- For generated files (`config/litellm/config.yaml`, `model_catalog.json`,
  backend names in README/CLAUDE.md), edit the source (`config/models.yaml`)
  and regenerate — never hand-edit generated output.

## While editing

- One file, one concern per change. Keep diffs small enough to review in one
  pass.
- Reuse repo helpers instead of rewriting them (`info/warn/step/backup` in
  shell scripts, stdlib only in Python).
- No drive-by refactors. If you notice unrelated issues, note them, don't
  fix them in the same diff.
- Never commit `.env` files or secrets of any kind.
- Any service/port binding must be `127.0.0.1` only — never `0.0.0.0`.
- Shell scripts: `set -euo pipefail` at the top; no skipped hooks
  (`--no-verify`, `--no-gpg-sign`) unless explicitly told to.
- No Claude commit attribution in this repo — Victor's identity only.
- Never `git push` without explicit approval.

## Verification

- Run the actual build/test/lint command for what you changed — do not
  assert success without running something.
- Paste the real command output (or a faithful summary of it), not a
  guessed result.
- Any shell script you touch: run `bash -n <script>` before calling it done.
- Config changes: `./scripts/sync-models.sh` must produce zero diff after
  regeneration if `models.yaml` changed.
- Prefer `./scripts/doctor.sh` (0=healthy, 2=degraded) and
  `./scripts/smoke-test.sh` as final sanity checks when touching runtime
  config.
- If verification fails twice, stop and report the failure instead of
  guessing at another fix.

## Context discipline

- Use targeted reads and grep instead of `cat`-ing entire large files.
- Read only the line ranges relevant to the change.
- If a file changed outside this session (another process, another agent),
  re-read it before editing — don't trust a stale in-context copy.
- Summarize long command output (test logs, diffs) instead of repeating it
  verbatim back into context.
<!-- claude-only -->
- Keep each subagent handoff scoped to what that step needs, not the full
  task history.
<!-- /claude-only -->
