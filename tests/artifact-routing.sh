#!/usr/bin/env bash
# artifact-routing.sh — does a real local model actually CALL the artifact tool?
#
# OUT OF THE GATE, deliberately, like installed-runtime and measure_geometry: it
# needs the stack up and spends minutes of local inference. Run it when the tool
# description, its alwaysLoad/searchHint metadata, or the routing contract in
# server.py changes.
#
# WHY IT EXISTS. The routing contract is a STRING, so it regresses silently --
# nothing type-checks a description. Every phrase below is one a user actually
# typed or a near neighbour of one, and each failing case here was a real miss:
#
#   [REAL] "Publish a flowchart" / "Create a flowchart" / "Publish a diagram"
#          scored 0/3 before the vocabulary line named those words.
#   [REAL] "draw me a code architecture diagram of the classes and objects in
#          this pipeline" -- typed by the user in a live session -- returned
#          fenced Mermaid instead of publishing. Its neighbours scored 2/4
#          ("draw a diagram showing how these classes interact") and 1/4
#          ("sketch the class relationships") because draw/sketch/make were
#          absent from the description. After stating both the verbs and the
#          rule: 5/6 and 3/3.
#
# The negative controls matter as much: asking for SOURCE must still return
# source. A description that publishes everything is not a fix.
#
# THIS IS A MEASUREMENT, NOT A CONTRACT TEST. It samples a stochastic model and
# reports a rate. What the description actually SAYS is asserted deterministically
# by test_routing_contract.py, which needs no inference and gates in CI. Read a
# miss here as model-dependent routing reliability, not as a missing trigger
# word: [REAL] gemma4:26b-mlx returned fenced Mermaid for a phrase the
# description already names verbatim, then quoted the rule back correctly when
# corrected. That is instruction-following, and more synonyms do not fix it.
#
# Usage: ./tests/artifact-routing.sh [runs-per-positive]   (default 3)
set -uo pipefail
RUNS="${1:-3}"
FAILS=0

fixture() {
  cat > "$1/pipeline.py" <<'PY'
class Source:
    def read(self): ...
class Transformer:
    def __init__(self, s): self.s = s
class Validator:
    def __init__(self, t): self.t = t
class Sink:
    def __init__(self, v): self.v = v
PY
}

# One fresh claude-local session. Prints 1 if an artifact reached disk, else 0.
attempt() {
  local prompt="$1" d
  d="$(mktemp -d)"
  fixture "$d"
  ( cd "$d" && timeout 400 zsh -c \
      "source ~/.config/ailocal/configure.zsh >/dev/null 2>&1; \
       claude-local -p \"$prompt\" --allowedTools mcp__artifact__publish,Read \
       --output-format json" >/dev/null 2>/dev/null </dev/null )
  local n=0
  [ -d "$d/.artifacts" ] && n=$(find "$d/.artifacts" -type f | wc -l | tr -d ' ')
  rm -rf "$d"
  [ "$n" -gt 0 ] && echo 1 || echo 0
}

# The natural two-turn shape, which is how the real miss actually happened: the
# model explores and answers in PROSE first, then is asked for a picture. The
# single-turn cases above never exercise a session that already has a long
# tool-and-prose history behind it, and [REAL] that is the run that missed --
# turn 3 of a live session, not a cold -p prompt.
attempt2() {
  local t1="$1" t2="$2" d
  d="$(mktemp -d)"
  fixture "$d"
  ( cd "$d" && timeout 400 zsh -c \
      "source ~/.config/ailocal/configure.zsh >/dev/null 2>&1; \
       claude-local -p \"$t1\" --allowedTools mcp__artifact__publish,Read,Bash \
       --output-format json" >/dev/null 2>/dev/null </dev/null )
  ( cd "$d" && timeout 400 zsh -c \
      "source ~/.config/ailocal/configure.zsh >/dev/null 2>&1; \
       claude-local -p --continue \"$t2\" --allowedTools mcp__artifact__publish,Read,Bash \
       --output-format json" >/dev/null 2>/dev/null </dev/null )
  local n=0
  [ -d "$d/.artifacts" ] && n=$(find "$d/.artifacts" -type f | wc -l | tr -d ' ')
  rm -rf "$d"
  [ "$n" -gt 0 ] && echo 1 || echo 0
}

two_turn() {
  local label="$1" t1="$2" t2="$3" hits=0 i
  for ((i = 0; i < RUNS; i++)); do
    [ "$(attempt2 "$t1" "$t2")" = "1" ] && hits=$((hits + 1))
  done
  POS_HITS=$((POS_HITS + hits))
  POS_RUNS=$((POS_RUNS + RUNS))
  if [ "$hits" -eq 0 ]; then
    printf "  FAIL  %-46s %s/%s (total miss)\n" "$label" "$hits" "$RUNS"
    FAILS=$((FAILS + 1))
  else
    printf "  ok    %-46s %s/%s\n" "$label" "$hits" "$RUNS"
  fi
}

# THRESHOLDS. Per-phrase majority-of-3 was tried first and is itself flaky:
# [REAL] two consecutive runs of this suite disagreed on WHICH phrase failed
# (3 failures then 1, with "make me a code architecture diagram" going 3/3 then
# 1/3) with no change to the description between them. Local sampling variance
# at n=3 is larger than the effect being measured, so a per-phrase gate would
# fail for reasons that have nothing to do with routing.
#
# What IS stable, and what this therefore asserts:
#   * the AGGREGATE positive rate across every phrase
#   * no phrase scoring a total zero -- a real vocabulary miss looks like 0/N,
#     which is how the pre-fix "sketch the class relationships" (1/4) showed up
#   * every negative clean, every time -- over-publishing is not a fix
POS_HITS=0
POS_RUNS=0

positive() {
  local label="$1" prompt="$2" hits=0 i
  for ((i = 0; i < RUNS; i++)); do
    [ "$(attempt "$prompt")" = "1" ] && hits=$((hits + 1))
  done
  POS_HITS=$((POS_HITS + hits))
  POS_RUNS=$((POS_RUNS + RUNS))
  if [ "$hits" -eq 0 ]; then
    printf "  FAIL  %-46s %s/%s (total miss)\n" "$label" "$hits" "$RUNS"
    FAILS=$((FAILS + 1))
  else
    printf "  ok    %-46s %s/%s\n" "$label" "$hits" "$RUNS"
  fi
}

negative() {                      # must publish NOTHING, ever
  local label="$1" prompt="$2"
  if [ "$(attempt "$prompt")" = "0" ]; then
    printf "  PASS  %-46s no artifact\n" "$label"
  else
    printf "  FAIL  %-46s PUBLISHED (should not)\n" "$label"
    FAILS=$((FAILS + 1))
  fi
}

echo "ARTIFACT ROUTING (${RUNS} runs per positive, real local inference)"
positive "the phrase a user actually typed" \
  "draw me a code architecture diagram of the classes and objects in this pipeline"
positive "draw me an architecture diagram"      "draw me an architecture diagram"
positive "draw a diagram showing interaction"   "draw a diagram showing how these classes interact"
positive "sketch the class relationships"       "sketch the class relationships"
positive "make me a code architecture diagram"  "make me a code architecture diagram"
positive "publish a flowchart"                  "Publish a flowchart showing A -> B -> C."
positive "visualize the process"                "Visualize the process where A leads to B leads to C."

echo
echo "  -- the natural two-turn workflow (explore in prose, then ask for a picture) --"
two_turn "explore-then-draw, the real failing shape" \
  "explore this repo and explain the code hierarchy to me" \
  "can you draw me a code architecture diagram of the classes and objects in this pipeline"

echo
negative "prose answer stays prose" \
  "In two sentences, describe what this pipeline does. Do not create any files or diagrams."
negative "explicit request for SOURCE" \
  "Show me the raw Mermaid source for this pipeline as text in your reply. Do not publish anything."
negative "a plain list is not a diagram"        "List the class names in pipeline.py. Nothing else."
negative "explain the architecture stays prose"  "Explain the architecture of this pipeline in prose. Do not draw anything."
negative "explain the class hierarchy"           "Explain the class hierarchy in this pipeline in words only."

echo
RATE=$((POS_HITS * 100 / POS_RUNS))
printf "  aggregate positive routing: %s/%s = %s%%  (floor 70%%)\n" \
       "$POS_HITS" "$POS_RUNS" "$RATE"
if [ "$RATE" -lt 70 ]; then
  echo "  FAIL  aggregate positive routing below floor"
  FAILS=$((FAILS + 1))
fi

echo
if [ "$FAILS" -eq 0 ]; then echo "ARTIFACT ROUTING: all checks passed"; else
  echo "ARTIFACT ROUTING: $FAILS FAILED"; fi
exit $((FAILS > 0))
