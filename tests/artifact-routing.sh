#!/usr/bin/env bash
# Measure whether a real local model calls the artifact tool (needs the stack).
# Asserts aggregate rate, no phrase at 0/N, every negative clean.
# Usage: ./tests/artifact-routing.sh [runs-per-positive]   (default 3)
set -uo pipefail
RUNS="${1:-3}"
FAILS=0 POS_HITS=0 POS_RUNS=0
CLIENTS="$(PYTHONPATH="$(cd "$(dirname "$0")/.." && pwd)/src" python3 -c 'from ailocal import policy; print(policy.deployed_client_root())')"

# Print 1 if one fresh session over the turns wrote an artifact, else 0.
attempt() {
  local d flag="" n=0 turn tools=mcp__artifact__publish,Read
  [ $# -gt 1 ] && tools="$tools,Bash"
  d="$(mktemp -d)"
  cat > "$d/pipeline.py" <<'PY'
class Source:
    def read(self): ...
class Transformer:
    def __init__(self, s): self.s = s
class Validator:
    def __init__(self, t): self.t = t
class Sink:
    def __init__(self, v): self.v = v
PY
  for turn in "$@"; do
    ( cd "$d" && timeout 400 zsh -c "source '$CLIENTS/configure.zsh' >/dev/null 2>&1; \
        claude-local -p $flag \"$turn\" --allowedTools $tools \
        --output-format json" >/dev/null 2>&1 </dev/null )
    flag="--continue"
  done
  [ -d "$d/.artifacts" ] && n=$(find "$d/.artifacts" -type f | wc -l | tr -d ' ')
  rm -rf "$d"
  [ "$n" -gt 0 ] && echo 1 || echo 0
}

positive() {  # positive <label> <turn>...
  local label="$1" hits=0 i
  shift
  for ((i = 0; i < RUNS; i++)); do
    [ "$(attempt "$@")" = 1 ] && hits=$((hits + 1))
  done
  POS_HITS=$((POS_HITS + hits)) POS_RUNS=$((POS_RUNS + RUNS))
  if [ "$hits" -eq 0 ]; then
    printf "  FAIL  %-46s %s/%s (total miss)\n" "$label" "$hits" "$RUNS"
    FAILS=$((FAILS + 1))
  else
    printf "  ok    %-46s %s/%s\n" "$label" "$hits" "$RUNS"
  fi
}

negative() {  # must publish nothing
  if [ "$(attempt "$2")" = 0 ]; then printf "  PASS  %-46s no artifact\n" "$1"
  else printf "  FAIL  %-46s PUBLISHED (should not)\n" "$1"; FAILS=$((FAILS + 1)); fi
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
# The live miss was a later turn after prose exploration, not a cold prompt.
positive "explore-then-draw, the real failing shape" \
  "explore this repo and explain the code hierarchy to me" \
  "can you draw me a code architecture diagram of the classes and objects in this pipeline"
negative "prose answer stays prose" \
  "In two sentences, describe what this pipeline does. Do not create any files or diagrams."
negative "explicit request for SOURCE" \
  "Show me the raw Mermaid source for this pipeline as text in your reply. Do not publish anything."
negative "a plain list is not a diagram"        "List the class names in pipeline.py. Nothing else."
negative "explain the architecture stays prose"  "Explain the architecture of this pipeline in prose. Do not draw anything."
negative "explain the class hierarchy"           "Explain the class hierarchy in this pipeline in words only."

RATE=$((POS_HITS * 100 / POS_RUNS))
printf "\n  aggregate positive routing: %s/%s = %s%%  (floor 70%%)\n" "$POS_HITS" "$POS_RUNS" "$RATE"
if [ "$RATE" -lt 70 ]; then echo "  FAIL  aggregate positive routing below floor"; FAILS=$((FAILS + 1)); fi
if [ "$FAILS" -eq 0 ]; then echo "ARTIFACT ROUTING: all checks passed"; else echo "ARTIFACT ROUTING: $FAILS FAILED"; fi
exit $((FAILS > 0))
