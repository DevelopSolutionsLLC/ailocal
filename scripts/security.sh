#!/usr/bin/env bash
# security.sh — container supply-chain posture for the images ailocal runs.
#
# Answers four questions that a vulnerability scanner alone does not:
#
#   1. Is every declared image pinned to an immutable digest? A `latest` or
#      `main-stable` tag is not a pin. Both floated under us during the audit
#      that produced this script: `main-stable` moved 1.92.0 -> 1.93.0 while the
#      docs still claimed 1.92.0, and `searxng:latest` was rebuilt mid-session.
#   2. Does the RUNNING image match the DECLARED one? They diverge silently —
#      editing a compose file does not restart a container, so a repo can look
#      patched while the old image keeps serving.
#   3. Are the services bound to loopback? Reachability is what turns a package
#      finding into an exposure, and it is not visible to a scanner.
#   4. What does Docker Scout report? Optional: Scout needs auth and network, so
#      its absence degrades the report rather than failing it.
#
# Exit: 0 = pinned, no drift.  1 = a real problem (drift, or a floating pin).
#       2 = degraded (could not check something, e.g. Docker or Scout absent).
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCAN=0
[[ "${1:-}" == "--scan" ]] && SCAN=1

fail=0; degraded=0
ok()   { printf '  \033[32mPASS\033[0m  %s\n' "$1"; }
bad()  { printf '  \033[31mFAIL\033[0m  %s\n' "$1"; fail=1; }
warn() { printf '  \033[33mWARN\033[0m  %s\n' "$1"; degraded=1; }
head_() { printf '\n\033[1m%s\033[0m\n' "$1"; }

if ! command -v docker >/dev/null 2>&1; then
  echo "docker not installed — cannot assess container posture."; exit 2
fi

# ── 1. every declared image is pinned by digest ─────────────────────────────
head_ "DECLARED IMAGES"
declared=()
while IFS= read -r line; do
  img="${line#*image:}"; img="$(echo "$img" | tr -d ' ')"
  [[ -z "$img" || "$img" == \$* ]] && continue
  declared+=("$img")
  if [[ "$img" == *"@sha256:"* ]]; then
    ok "pinned by digest: ${img%%@*}"
  else
    bad "NOT pinned (floating tag): $img"
  fi
done < <(grep -rh '^\s*image:' "$REPO"/deploy/*/docker-compose.yml 2>/dev/null || true)
[[ ${#declared[@]} -eq 0 ]] && warn "no images declared under deploy/*/docker-compose.yml"

# ── 2. running digest matches declared digest ───────────────────────────────
head_ "DECLARED vs RUNNING"
for img in "${declared[@]}"; do
  [[ "$img" != *"@sha256:"* ]] && continue
  repo="${img%%@*}"; repo="${repo%%:*}"; want="${img##*@}"
  cid="$(docker ps -q --filter "ancestor=$img" 2>/dev/null | head -1)"
  if [[ -z "$cid" ]]; then
    # Fall back to matching by repository, which catches the drift case: a
    # container running the SAME repo at a DIFFERENT digest is the failure
    # this check exists for, and an ancestor filter would simply miss it.
    cid="$(docker ps -q 2>/dev/null | while read -r c; do
             ri="$(docker inspect "$c" --format '{{.Config.Image}}' 2>/dev/null)"
             [[ "$ri" == "$repo"* ]] && echo "$c" && break
           done | head -1)"
  fi
  if [[ -z "$cid" ]]; then
    warn "$repo: not running (cannot compare)"; continue
  fi
  name="$(docker inspect "$cid" --format '{{.Name}}' | tr -d /)"
  got="$(docker inspect "$(docker inspect "$cid" --format '{{.Image}}')" \
          --format '{{range .RepoDigests}}{{.}} {{end}}' 2>/dev/null | tr ' ' '\n' | grep "^$repo@" | head -1)"
  got="${got##*@}"
  if [[ -z "$got" ]]; then
    warn "$name: running image has no repo digest (built locally?)"
  elif [[ "$got" == "$want" ]]; then
    ok "$name runs the declared digest (${want:7:12})"
  else
    bad "$name DRIFT — declared ${want:7:12}, running ${got:7:12}. Run 'ailocal start'."
  fi
done

# ── 3. reachability ─────────────────────────────────────────────────────────
# A finding in a package is only an exposure if something can reach it. Every
# ailocal service is loopback-only by policy; this asserts it rather than
# trusting the compose file to still say so.
head_ "REACHABILITY"
while read -r cid; do
  [[ -z "$cid" ]] && continue
  name="$(docker inspect "$cid" --format '{{.Name}}' | tr -d /)"
  ports="$(docker port "$cid" 2>/dev/null || true)"
  [[ -z "$ports" ]] && { ok "$name publishes no ports"; continue; }
  exposed="$(echo "$ports" | awk '{print $3}' | grep -v '^127\.0\.0\.1:' | grep -v '^\[::1\]:' || true)"
  if [[ -z "$exposed" ]]; then
    ok "$name bound to loopback only"
  else
    bad "$name reachable off-host: $(echo "$exposed" | tr '\n' ' ')"
  fi
done < <(docker ps -q 2>/dev/null)

# ── 4. optional scan ────────────────────────────────────────────────────────
if [[ $SCAN -eq 1 ]]; then
  head_ "SCOUT (fixable critical/high)"
  if ! docker scout version >/dev/null 2>&1; then
    warn "docker scout unavailable — no vulnerability data"
  else
    for img in "${declared[@]}"; do
      out="$(docker scout cves --only-fixed --only-severity critical,high "$img" 2>&1 || true)"
      if echo "$out" | grep -q "No vulnerable package detected"; then
        ok "${img%%@*}: no fixable critical/high"
      else
        n="$(echo "$out" | grep -cE '^ *[0-9]+C +[0-9]+H' || true)"
        warn "${img%%@*}: $n package(s) with fixable critical/high — see docs/security.md"
      fi
    done
  fi
else
  head_ "SCOUT"
  echo "  skipped — re-run with --scan (needs network and a Docker login)"
fi

echo
if [[ $fail -ne 0 ]]; then
  echo "SECURITY: problems found"; exit 1
elif [[ $degraded -ne 0 ]]; then
  echo "SECURITY: pinned and loopback-only; some checks degraded"; exit 2
fi
echo "SECURITY: all images pinned, no drift, loopback-only"
