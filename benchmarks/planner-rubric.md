# Planner comparison rubric — v2

Scored dimensions and weights for the three-candidate planner comparison.
This file is HASHED and LOCKED into the run manifest before any candidate
starts. Editing it after a run begins invalidates that run
(`INVALID_RUN_MANIFEST`), because a rubric changed mid-comparison is a rubric
fitted to the answers.

Weights are unchanged from v1 except for the two dimensions added at the end.

## Correctness — the dominant category (weight 9)

| Criterion | Weight |
|---|---|
| root-cause accuracy | 3 |
| evidence / first failing boundary | 2 |
| decomposition | 2 |
| dependency order | 2 |

## Delegation and validation (weight 5)

| Criterion | Weight |
|---|---|
| subagent selection | 1 |
| subagent prompt quality | 2 |
| validation quality | 2 |

## Discipline (weight 2)

| Criterion | Weight |
|---|---|
| constraint / non-goal compliance | 1 |
| continuity across turns | 1 |

## Penalties (v1)

| Criterion | Weight |
|---|---|
| unnecessary work | -2 |
| runaway output | -2 |

---

## NEW in v2 — A. Repetition and circularity (weight -3, penalty only)

v1 could only reach this obliquely through `unnecessary work`. It needed its own
dimension after a smoke run produced **317 internal turns and no answer** while
looping on denied tools — behaviour v1 would have scored as a single -2.

Score 0 (none) to -3 (severe). Count, from the transcript:

- repeated identical searches (same pattern, same path, no new evidence between)
- repeated file reads with no new evidence obtained between them
- revisiting a hypothesis the candidate itself already eliminated
- loops caused by ignoring a tool denial and retrying it unchanged
- restating the plan without adding or changing content

A denial retried **once** with a corrected approach is not circularity — it is
correct recovery. Retrying the same denied call unchanged three or more times is.

## NEW in v2 — B. Execution efficiency (weight 2, tie-break only)

Measured, not judged:

- internal turn count (`num_turns`)
- wall time AND monotonic compute time, reported separately
- total tool attempts
- denied tool attempts
- turns until the correct root cause is first stated
- completion vs non-completion of all planned turns

### Scoring rules — fixed BEFORE any candidate output is opened

1. **Correctness dominates.** Efficiency may only order candidates whose
   correctness scores are within 1 point. It can never overturn a correctness
   difference.
2. **An incorrect fast answer never beats a correct slower answer.** Efficiency
   is not applied at all to a candidate that missed the root cause.
3. **A concise grounded answer is not penalised for low tool count.** Tool use
   earns nothing by occurring. If the root cause is correct and the evidence
   cited is real, a one-pass answer scores full efficiency.
4. **A long non-answer is penalised explicitly.** A candidate that fails to
   complete its turns scores 0 on efficiency AND takes the repetition penalty;
   it is not merely "slower".
5. **Timing is only scored when the environment qualified.** If the run is
   `TIMING_UNQUALIFIED` (battery, backup, thermal, co-resident model), turn
   count and tool attempts are still scored; wall and compute time are not.
6. **Incomplete candidates are never scored against complete ones.** They are
   reported separately with their terminal classification.

## Scoring order (blind-first)

1. Score correctness, delegation, discipline and repetition **blind**, from
   identity-stripped copies only.
2. Lock those scores (hash them).
3. Reveal operational metrics (turns, timings, tool counts).
4. Score execution efficiency.
5. Reveal the model mapping **last**.

Steps 1–2 must complete before step 3, because turn counts and latencies are
themselves identity hints: a 2B model and a 26B model do not produce similar
timings.
