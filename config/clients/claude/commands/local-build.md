---
description: "Plan -> implement -> review a feature with local model roles"
---

Run this feature/fix through plan -> implement -> review using local-model
subagents. Keep the main session's own context lean: pass each subagent only
what that step needs (the relevant prose, not the whole conversation).

Request: $ARGUMENTS

Steps:

1. Delegate to the `planner` subagent with the request above. Planner reads
   files but makes no edits and produces a numbered plan only (files, ordered
   steps, risks, verification commands).

2. Show the plan to the user before proceeding.

3. For each step in the plan, delegate to the `implementer` subagent: give it
   that one step (plus the file paths it needs), not the whole plan text
   verbatim if it's long. Implementer must Read before Edit, keep diffs
   small and scoped to one file/concern, and run the step's verification
   command, pasting real output.

4. After all steps (or after each risky step, implementer's judgment),
   delegate to the `reviewer` subagent with the current `git diff` and the
   build checklist. Reviewer returns `APPROVE` or `FIX REQUIRED` plus a
   numbered fix list — nothing else.

5. If `FIX REQUIRED`: send the fix list back to `implementer`, then back to
   `reviewer`. Repeat at most twice. If still not approved after two loops,
   stop and surface the outstanding findings to the user instead of looping
   further.

6. On `APPROVE`, summarize what changed (files + one-line each) to the user.
   Do not commit or push unless the user explicitly asks.
