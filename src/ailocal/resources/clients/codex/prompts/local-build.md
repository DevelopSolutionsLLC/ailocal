---
description: "Plan -> implement -> self-review a task using the local phase protocol"
argument-hint: "<task description>"
---

Run this task through the full phase protocol from AGENTS.md, no shortcuts:

Task: $ARGUMENTS

1. PLAN: read the relevant files (targeted reads, not whole-file dumps) and
   output a numbered plan — files to touch, ordered steps (one file/concern
   each), risks, exact verification command per step. Show the plan and wait
   for my go-ahead before editing anything.
2. IMPLEMENT each step: read before edit, smallest safe diff, run the step's
   verification command, show real output. Two failures on a step = stop and
   report.
3. REVIEW: diff everything changed (`git diff`), check it against the build
   checklist in AGENTS.md, output `APPROVE` or `FIX REQUIRED` plus a numbered
   file:line fix list, and fix before declaring done.

Keep context lean throughout: grep and ranged reads over full files,
summarize long outputs instead of echoing them.
