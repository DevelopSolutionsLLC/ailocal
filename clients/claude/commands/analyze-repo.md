---
description: "Read-only repo audit via a phased scratchpad workflow — produces a full report, no edits"
---

Perform a complete, READ-ONLY audit of this project. Make NO edits, creates, deletes,
or renames of project files. Your only writes go to the scratchpad under `/tmp/scratchpad`.

Focus (optional): $ARGUMENTS

The project may be larger than your context window. Do not load it all at once. Build
understanding incrementally and persist everything to the scratchpad — context is
temporary, the scratchpad is your durable memory. Once a file is summarized, keep the
summary, not the whole file.

# Scratchpad

Create `/tmp/scratchpad` if missing. Maintain and append to (never blindly overwrite):
`overview.md`, `directory_index.md`, `architecture.md`, `components.md`,
`dependencies.md`, `redundancy.md`, `technical_debt.md`, `questions.md`, `progress.md`,
`implementation_plan.md`. Update assumptions when they change.

# Phases (finite state machine — one active at a time; don't do a later phase's work)

1. DISCOVERY — list dirs, read source/configs/tests/docs. For each directory: purpose,
   1-3 sentence summary per file, major classes/functions, responsibilities, public
   interfaces, dependencies, config. After each directory, update `directory_index.md`,
   `overview.md`, `progress.md`. Gate: every directory indexed, overview written, open
   questions listed.
2. ARCHITECTURE — component/dependency graph, data flow, ownership boundaries, public
   APIs, design patterns. Find: duplicate utils/parsers/validation/logging/config, dead
   code, unused modules, circular deps, over-abstraction, god classes, large files, weak
   cohesion, strong coupling, naming inconsistencies. Record in `architecture.md`,
   `redundancy.md`, `technical_debt.md`. Gate: dependency graph + redundancy + debt done.
3. PLANNING — write `implementation_plan.md`. No code. Each proposed change lists: reason,
   benefits, risks, files affected, dependencies, alternatives, migration order, expected
   outcome. Prioritize: safe improvements → structural → behavior → cleanup.

# Scratchpad maintenance

Every ~5 directories, re-read ONLY the scratchpad (not the project), compress duplicate
notes, drop obsolete assumptions, and rewrite `overview.md` into a concise summary.

# Redundancy tracking (`redundancy.md`)

Per duplicate: File A, File B, similarity (low/medium/high/near-identical), evidence,
impact, recommendation.

# Final report

When every directory is analyzed, read ONLY the scratchpad (do not re-read the project)
and produce: 1) Executive summary 2) High-level architecture 3) Component responsibilities
4) Dependency overview 5) Redundancy report 6) Dead code 7) Files to merge 8) Files to
split 9) Naming inconsistencies 10) Technical debt 11) Refactoring roadmap 12) Highest-risk
areas 13) Quick wins 14) Long-term improvements 15) Remaining unknowns.

Be conservative. Evidence over assumptions. Never invent functionality. This command does
not modify the project — to act on the plan, use `/local-build`.
