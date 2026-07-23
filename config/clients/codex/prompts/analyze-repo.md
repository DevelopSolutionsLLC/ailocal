---
description: "Read-only repo audit via a phased scratchpad workflow — full report, no edits"
argument-hint: "[optional focus area]"
---

Perform a complete, READ-ONLY audit of this project. Make NO edits/creates/deletes/renames
of project files — your only writes go to the scratchpad under `/tmp/scratchpad`.

Focus (optional): $ARGUMENTS

The project may exceed your context window. Do not load it all at once. Build understanding
incrementally and persist everything to the scratchpad — context is temporary, the scratchpad
is durable memory. Once a file is summarized, keep the summary, not the whole file.

Scratchpad: create `/tmp/scratchpad` if missing; maintain and append to `overview.md`,
`directory_index.md`, `architecture.md`, `components.md`, `dependencies.md`, `redundancy.md`,
`technical_debt.md`, `questions.md`, `progress.md`, `implementation_plan.md`.

Phases (one active at a time; don't jump ahead):
1. DISCOVERY — walk directories; per file a 1-3 sentence summary; major classes/functions,
   responsibilities, public interfaces, deps, config. Update directory_index/overview/progress
   after each directory. Gate: every directory indexed, overview written, questions listed.
2. ARCHITECTURE — component + dependency graph, data flow, ownership, public APIs, patterns.
   Find duplicate utils/parsers/validation/logging/config, dead code, unused modules, circular
   deps, over-abstraction, god classes, large files, weak cohesion, tight coupling, naming
   drift. Record in architecture/redundancy/technical_debt. Gate: dep graph + redundancy + debt.
3. PLANNING — write implementation_plan.md (no code). Each change: reason, benefits, risks,
   files affected, dependencies, alternatives, migration order, expected outcome. Prioritize
   safe → structural → behavior → cleanup.

Every ~5 directories, re-read ONLY the scratchpad, compress notes, drop stale assumptions,
rewrite overview.md concise. Redundancy per entry: File A, File B, similarity, evidence,
impact, recommendation.

Final report (read ONLY the scratchpad, do not re-read the project): 1) Executive summary
2) Architecture 3) Component responsibilities 4) Dependencies 5) Redundancy 6) Dead code
7) Merge 8) Split 9) Naming inconsistencies 10) Technical debt 11) Refactoring roadmap
12) Highest-risk areas 13) Quick wins 14) Long-term improvements 15) Remaining unknowns.

Be conservative; evidence over assumptions; never invent. This prompt does not modify the
project — to act on the plan, use the local-build prompt.
