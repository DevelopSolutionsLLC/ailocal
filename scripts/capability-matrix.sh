#!/usr/bin/env bash
# capability-matrix.sh — what each model can ACTUALLY do, read from Ollama.
#
# Every column comes from `/api/show` on the locally pulled model. Nothing here
# is taken from a model card, a blog ranking, or a family name. "Newer" tells you
# nothing about capability: qwen3.5/3.6 are newer than qwen2.5-coder and have NO
# fill-in-middle, which makes them unable to do inline autocomplete at all.
#
# Columns:
#   insert    Ollama's fill-in-middle flag. REQUIRED for inline autocomplete.
#   tools     native tool calling. REQUIRED for any agentic tier.
#   think     emits reasoning content.
#   vision    image input.
#   ctx       the model's own trained context length (NOT what we configure).
#   params    parameter count as the model reports it.
#   quant     quantization level.
#
# Usage: ./scripts/capability-matrix.sh [model ...]
set -uo pipefail
OLLAMA="${OLLAMA_HOST_URL:-http://127.0.0.1:11434}"

if [ $# -gt 0 ]; then MODELS="$*"; else
  MODELS="$(curl -sf -m 10 "$OLLAMA/api/tags" | python3 -c "
import sys, json
print(' '.join(m['name'] for m in json.load(sys.stdin).get('models', [])))")"
fi

echo "══════════════════════════════════════════════════════════════════════════════════════════"
echo " CAPABILITY MATRIX — read from Ollama /api/show, not from model cards"
echo "══════════════════════════════════════════════════════════════════════════════════════════"
printf '%-40s %-6s %-6s %-6s %-6s %9s %8s %-8s\n' \
  MODEL insert tools think vision ctx params quant
printf '%-40s %-6s %-6s %-6s %-6s %9s %8s %-8s\n' \
  "───────────────────────────────────────" "──────" "─────" "─────" "──────" "────────" "───────" "───────"

for m in $MODELS; do
  curl -sf -m 25 "$OLLAMA/api/show" -d "{\"model\":\"$m\"}" 2>/dev/null \
  | MODEL="$m" python3 -c "
import sys, json, os
name = os.environ['MODEL']
try:
    d = json.load(sys.stdin)
except Exception:
    print(f'{name:40} {\"?\":6} {\"?\":6} {\"?\":6} {\"?\":6} {\"?\":>9} {\"?\":>8} ?')
    raise SystemExit
caps = set(d.get('capabilities') or [])
info = d.get('model_info') or {}
det = d.get('details') or {}
ctx = next((v for k, v in info.items() if k.endswith('.context_length')), None)
# A missing reading prints '?', never 'no' — 'could not read' and 'unsupported'
# are different facts and must not look the same.
def flag(x):
    return 'YES' if x in caps else ('no' if caps else '?')
print(f'{name:40} {flag(\"insert\"):6} {flag(\"tools\"):6} {flag(\"thinking\"):6} '
      f'{flag(\"vision\"):6} {(str(ctx) if ctx else \"?\"):>9} '
      f'{det.get(\"parameter_size\",\"?\"):>8} {det.get(\"quantization_level\",\"?\")}')
"
done

echo
echo " insert = fill-in-middle. Without it a model CANNOT serve inline autocomplete,"
echo " however strong it is at chat. This is the single most misread capability:"
echo " swapping a FIM model for a stronger chat model silently breaks completion."
