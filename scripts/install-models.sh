#!/usr/bin/env bash
# install-models.sh — pull or update Ollama models for ailocal
# Usage: ./scripts/install-models.sh
#
# Model list is derived automatically from the generated config.yaml — no
# separate list to maintain here. To add or change a model, update the config.
#
# Run this after 'ollama serve' is confirmed running.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
AILOCAL_STATE="${AILOCAL_STATE:-$("$ROOT_DIR/scripts/profile-config" state-root)}"

OLLAMA="${OLLAMA_CLI:-ollama}"
# Fail closed: no implicit tier. A suppressed read falling through to a
# hardcoded 64gb is what silently installs the wrong model set on a
# smaller machine. Resolve once, here, and reuse.
_TIER="$("$ROOT_DIR/scripts/profile-config" active-tier)" || {
  echo "  ✗ cannot resolve the active profile (see error above)" >&2; exit 1; }
# The GENERATED artifact, not the profile. install.sh runs sync-models before
# this script, so generated state exists by now — and reading it removes the
# fourth hand-rolled YAML parser in this repository.
EFFECTIVE_JSON="$AILOCAL_STATE/litellm/effective-profile.json"

# ── Helpers ────────────────────────────────────────────────────────────────

. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/output.sh"

# ── Pre-flight ─────────────────────────────────────────────────────────────

step "Pre-flight checks"

if ! has "$OLLAMA"; then
  error "Ollama CLI not found. Install from: https://ollama.ai/download"
  exit 1
fi
info "Ollama CLI present"

if ! "$OLLAMA" list >/dev/null 2>&1; then
  error "Ollama daemon is not responding."
  echo "  Start the MANAGED service (not the GUI app, which competes for :11434):" >&2
  echo "    launchctl kickstart -k gui/$(id -u)/com.ailocal.ollama" >&2
  exit 1
fi
info "Ollama daemon responding"

if [ ! -f "$EFFECTIVE_JSON" ]; then
  error "Generated model configuration not found: $EFFECTIVE_JSON"
  error "Run 'ailocal sync' first — this script does not parse profiles."
  exit 1
fi
info "Model manifest found"

# ── Model set and disk requirement ─────────────────────────────────────────
#
# Both were wrong before. The model list was every `active:` line in the profile,
# so a DISABLED capability was still pulled, and four capabilities sharing one
# backend counted that backend four times. The disk figure was a hand-maintained
# `disk_gb:` that matched no actual model set — the 16gb profile claimed 20 GB
# while listing the same ~40 GB of models as 64gb, and the README claimed 85 GB,
# a third number agreeing with neither.
#
# Now: enabled capabilities only, deduplicated by tag, sized from what Ollama
# reports, and reduced by what is already installed. A machine that already holds
# the models needs no additional space, and must not be rejected as if it did.
MODEL_PLAN="$(python3 - "$EFFECTIVE_JSON" <<'PYEOF'
import json, subprocess, sys

data = json.load(open(sys.argv[1]))
caps = {}
for cap, cfg in sorted(data["roles"].items()):
    if not cfg.get("enabled"):
        continue                      # disabled: not exposed, not pulled, not sized
    caps.setdefault(cfg["model"], []).append(cap)

# `ollama list` sizes what is already on disk; anything absent is a download.
installed = {}
try:
    out = subprocess.run(["ollama", "list"], capture_output=True, text=True, timeout=20).stdout
    for line in out.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 4:
            val, unit = parts[2], parts[3].upper()
            gb = float(val) / 1000 if unit.startswith("MB") else float(val)
            installed[parts[0]] = gb
except Exception:
    pass

def size_of(tag):
    for name, gb in installed.items():
        if name == tag or name.split(":")[0] == tag.split(":")[0] and name.startswith(tag):
            return gb, True
    return None, False

total = have = need = 0.0
lines = []
for tag, users in sorted(caps.items()):
    gb, present = size_of(tag)
    label = f"{gb:.1f} GB" if gb else "size unknown until pulled"
    mark = "present" if present else "to download"
    lines.append(f"    {tag}  ({', '.join(users)})  {label}, {mark}")
    if gb:
        total += gb
        (have := have) if False else None
        if present: have += gb
        else:       need += gb

print("UNIQUE\t%d" % len(caps))
print("TOTAL\t%.1f" % total)
print("HAVE\t%.1f" % have)
print("NEED\t%.1f" % need)
for l in lines:
    print("LINE\t%s" % l)
PYEOF
)"
_plan_field() { printf '%s\n' "$MODEL_PLAN" | awk -F'\t' -v k="$1" '$1==k {print $2; exit}'; }
UNIQUE_N="$(_plan_field UNIQUE)"; TOTAL_GB="$(_plan_field TOTAL)"
HAVE_GB="$(_plan_field HAVE)";    NEED_GB="$(_plan_field NEED)"

# Supporting services + pull/extract headroom. Named so the number is auditable
# rather than an unexplained reserve.
SERVICES_GB=3      # LiteLLM + SearXNG images
HEADROOM_GB=5      # extraction headroom and a margin
REQUIRED_GB=$(printf '%.0f' "$(echo "${NEED_GB:-0} + $SERVICES_GB + $HEADROOM_GB" | bc 2>/dev/null || echo 8)")
FREE_GB=$(df -g "$HOME" 2>/dev/null | awk 'NR==2 {print $4}' || echo 0)

step "Model set for this profile"
printf '%s\n' "$MODEL_PLAN" | awk -F'\t' '$1=="LINE" {print $2}'
info "unique models: ${UNIQUE_N:-?}   installed size total: ${TOTAL_GB:-?} GB"
info "already present: ${HAVE_GB:-0} GB   still to download: ${NEED_GB:-0} GB"
info "supporting services: ${SERVICES_GB} GB   headroom: ${HEADROOM_GB} GB"
info "free space required now: ${REQUIRED_GB} GB   available: ${FREE_GB} GB"
if [ "${FREE_GB:-0}" -lt "${REQUIRED_GB:-0}" ]; then
  warn "Only ${FREE_GB} GB free; this profile needs ~${REQUIRED_GB} GB"
  warn "  = ${NEED_GB} GB to download + ${SERVICES_GB} GB services + ${HEADROOM_GB} GB headroom"
fi

# ── Pull models ────────────────────────────────────────────────────────────
#
# The SAME set the calculation above reported: enabled only, deduplicated by tag.
# It used to be every `active:` line in the profile, so a disabled capability was
# pulled anyway and four capabilities sharing one backend pulled it four times.
# Deriving both from one plan means what is shown and what is fetched cannot drift.
MODELS=()
while IFS= read -r _m; do [ -n "$_m" ] && MODELS+=("$_m"); done < <(
  printf '%s\n' "$MODEL_PLAN" | awk -F'\t' '$1=="LINE" {print $2}' | awk '{print $1}'
)

step "Installing/updating Ollama models"

# Get currently installed model names (first column, skip header row)
INSTALLED=$("$OLLAMA" list 2>/dev/null | awk 'NR>1 {print $1}')

for model in "${MODELS[@]}"; do
  if echo "$INSTALLED" | grep -qF "$model"; then
    info "$model  (already installed)"
  else
    echo "  ↓ Pulling $model ..."
    if "$OLLAMA" pull "$model"; then
      info "$model  pulled"
    else
      warn "$model  failed to pull — skipping"
    fi
  fi
done

# ── Summary ────────────────────────────────────────────────────────────────

step "Installed models"
"$OLLAMA" list

step "Done."
