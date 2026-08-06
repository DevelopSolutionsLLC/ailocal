#!/usr/bin/env bash
# install.sh — bootstrap host tools, generate .env, verify Docker & Ollama
# Idempotent: safe to run multiple times.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE="$ROOT_DIR/.env"

# Single source of truth for how this stack is composed (deploy/litellm + deploy/searxng).
AILOCAL_ROOT="$ROOT_DIR"
. "$ROOT_DIR/lib/compose.sh"

# ── Helpers ────────────────────────────────────────────────────────────────

. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/output.sh"


# Prompt for a value; shows default in brackets; returns default if user hits Enter.
# Usage: prompt_value "Prompt text" "default_value"  → sets $REPLY
prompt_value() {
  local prompt="$1"
  local default="${2:-}"
  if [ -n "$default" ]; then
    read -r -p "  $prompt [$default]: " REPLY
    REPLY="${REPLY:-$default}"
  else
    read -r -p "  $prompt: " REPLY
  fi
}

# Prompt for a secret (no echo); skips prompt if already set in env.

# ── Preflight: what is missing, what it costs ──────────────────────────────
#
# A bare Mac may have none of these. The old flow discovered that one tool at a
# time and curl|bash'd Homebrew with no prompt, so the first thing a new user saw
# was an unexplained sudo password request from a script they had just cloned.
#
# Everything missing is reported FIRST, with whether it needs administrator
# rights, then a single consent prompt, then one sudo authorisation up front.
# --yes runs unattended.
ASSUME_YES=false
PROFILE_OVERRIDE=""
_prev=""
for a in "$@"; do
  case "$a" in
    -h|--help)
      cat <<'USAGE'
usage: ailocal install [--yes] [--profile <16gb|32gb|64gb|128gb>]

Bootstraps the stack: prerequisites, .env, profile selection, generation,
models, service and client configuration. Safe to re-run; it is idempotent.

  --yes              unattended; also enables production autostart
  --profile <tier>   override the tier detected from installed memory
USAGE
      exit 0 ;;
  esac
  [ "$a" = "--yes" ] && ASSUME_YES=true
  [ "$_prev" = "--profile" ] && PROFILE_OVERRIDE="$a"
  _prev="$a"
done

# git ships with the Xcode Command Line Tools on macOS; jq and the rest come from
# Homebrew. Ordered by dependency: CLT -> brew -> everything else.
# Which of these actually need administrator rights, verified rather than assumed:
#
#   git / CLT       YES  `softwareupdate -i` runs as root (Homebrew's own installer
#                        uses execute_sudo for exactly this call)
#   Homebrew        YES  creates /opt/homebrew
#   jq              no   a formula, installed into the user-owned Homebrew prefix
#   Docker Desktop  YES  installs root:wheel helpers in /Library/PrivilegedHelperTools
#                        and symlinks into /usr/local/bin
#   Ollama          no   a cask; the app drops into /Applications, which is
#                        drwxrwxr-x root:admin and so writable by an admin user
#                        without sudo, and its binary lands in the Homebrew prefix.
#                        (Ollama.app separately offers to create a root symlink at
#                        /usr/local/bin/ollama — ailocal does not need it and uses
#                        the Homebrew one.)
MISSING=()
NEEDS_ADMIN=false
has git    || { MISSING+=("git (Xcode Command Line Tools) [admin]"); NEEDS_ADMIN=true; }
has brew   || { MISSING+=("Homebrew [admin]");                       NEEDS_ADMIN=true; }
has jq     || MISSING+=("jq")
has docker || { MISSING+=("Docker Desktop [admin]");                 NEEDS_ADMIN=true; }
has ollama || MISSING+=("Ollama")

step "Preflight"
if [ ${#MISSING[@]} -eq 0 ]; then
  info "all prerequisites present (git, brew, jq, docker, ollama)"
else
  echo "  This machine is missing:"
  for m in "${MISSING[@]}"; do echo "    - $m"; done
  echo
  # A standard (non-admin) account cannot install these AT ALL: it cannot sudo,
  # and /Applications is only group-writable by admin. Saying so here beats
  # failing several minutes into a cask install with a permissions error.
  if $NEEDS_ADMIN && ! id -Gn | tr ' ' '\n' | grep -qx admin; then
    error "$(id -un) is not an administrator, and these need administrator rights:"
    for m in "${MISSING[@]}"; do case "$m" in *"[admin]"*) echo "    - $m" >&2 ;; esac; done
    echo "  Install them from an admin account, or have an admin do it, then re-run." >&2
    exit 1
  fi
  if $NEEDS_ADMIN; then
    echo "  The items marked [admin] need administrator rights:"
    echo "    Command Line Tools and Docker Desktop install system-wide;"
    echo "    Homebrew creates /opt/homebrew. There is no user-local path for"
    echo "    these on macOS, so sudo is unavoidable — asked for ONCE, now,"
    echo "    rather than surprising you halfway through."
    echo
  fi
  if ! $ASSUME_YES; then
    read -r -p "  Install them? [y/N]: " REPLY
    case "$REPLY" in
      y|Y|yes|Yes) : ;;
      *) error "Declined. Install the tools above, then re-run."; exit 1 ;;
    esac
  fi
  if $NEEDS_ADMIN; then
    sudo -v || { error "Administrator authorisation declined."; exit 1; }
    # Keep the timestamp warm so a long brew/cask run does not re-prompt.
    while true; do sudo -n true 2>/dev/null; sleep 50; kill -0 "$$" 2>/dev/null || exit; done &
    SUDO_KEEPALIVE=$!
    trap 'kill "$SUDO_KEEPALIVE" 2>/dev/null || true' EXIT
  fi
fi

# ── Command Line Tools (provides git) and Homebrew ─────────────────────────
#
# ORDER MATTERS. Homebrew's own installer provisions the Command Line Tools
# SILENTLY — it seeds /tmp/.com.apple.dt.CommandLineTools.installondemand.in-progress,
# reads the CLT label out of `softwareupdate -l`, and installs it headlessly. So
# when both are missing, installing Homebrew first gets git too, with no GUI
# dialog and no second password prompt (we already hold sudo from the preflight).
#
# This used to run `xcode-select --install` first, which pops a macOS dialog the
# user has to click and then polls for up to twenty minutes. That path is now the
# FALLBACK, for the narrow case where brew exists but git somehow does not.
#
# There is no sudo-free way to install the Command Line Tools: `softwareupdate -i`
# requires root, which is why the preflight asks once, up front.
install_clt_silently() {
  local placeholder="/tmp/.com.apple.dt.CommandLineTools.installondemand.in-progress"
  local label
  touch "$placeholder" 2>/dev/null || true
  label="$(/usr/sbin/softwareupdate -l 2>/dev/null \
           | grep -B1 -E 'Command Line Tools' \
           | awk -F'*' '/^ *\*/ {print $2}' \
           | sed -e 's/^ *Label: //' -e 's/^ *//' \
           | sort -V | tail -n1)"
  if [ -n "$label" ]; then
    echo "  Installing $label (headless)..."
    sudo /usr/sbin/softwareupdate -i "$label" >/dev/null 2>&1 || true
    sudo /usr/bin/xcode-select --switch /Library/Developer/CommandLineTools 2>/dev/null || true
  fi
  rm -f "$placeholder" 2>/dev/null || true
  has git
}

step "Checking Homebrew"
if ! has brew; then
  echo "  Installing Homebrew (this also installs the Command Line Tools, which provide git)..."
  NONINTERACTIVE=1 /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)" \
    || { error "Homebrew install failed. See https://brew.sh"; exit 1; }
  BREW_BIN="/opt/homebrew/bin/brew"; [ -x "$BREW_BIN" ] || BREW_BIN="/usr/local/bin/brew"
  # Append the shellenv line ONCE. This used to append unconditionally, so every
  # re-run added another copy to ~/.zprofile.
  if ! grep -qs "$BREW_BIN shellenv" ~/.zprofile 2>/dev/null; then
    echo "eval \"\$($BREW_BIN shellenv)\"" >> ~/.zprofile
  fi
  eval "$($BREW_BIN shellenv)"
fi
info "Homebrew present ($(brew --version 2>/dev/null | head -1))"

step "Checking git"
if ! has git; then
  echo "  git not found — installing the Command Line Tools."
  if ! install_clt_silently; then
    # Headless install can fail on a machine whose software-update catalogue is
    # unreachable. Fall back to the GUI, which is slower but always available.
    warn "Headless install did not produce git — falling back to the macOS dialog."
    xcode-select --install 2>/dev/null || true
    echo "  Accept the dialog; waiting for git to appear..."
    for _ in $(seq 1 120); do has git && break; sleep 10; done
  fi
  has git || { error "git still unavailable. Finish the Command Line Tools install, then re-run."; exit 1; }
fi
info "git present ($(git --version 2>/dev/null | awk '{print $3}'))"


# Batched so Homebrew escalates once per command instead of once per package.
# Homebrew prompts for its own sudo when a cask needs it; the preflight asked
# earlier only so that prompt is not a surprise mid-run.
step "Installing Homebrew packages"
BREW_FORMULAS=(); BREW_CASKS=()
has jq     || BREW_FORMULAS+=("jq")
# cosign verifies image PROVENANCE -- a digest proves the bytes are unchanged, not
# who published them. Provisioned here so a fresh machine can verify signatures
# from day one; `ailocal security` still refuses to install it at runtime, because
# adding a verification tool to a RUNNING system is the operator's call. Batched,
# so it costs nothing extra.
has cosign || BREW_FORMULAS+=("cosign")
has docker || BREW_CASKS+=("docker-desktop")
has ollama || BREW_CASKS+=("ollama-app")

if [ ${#BREW_FORMULAS[@]} -gt 0 ]; then
  echo "  formulas: ${BREW_FORMULAS[*]}"
  brew install "${BREW_FORMULAS[@]}" || { error "Could not install: ${BREW_FORMULAS[*]}"; exit 1; }
fi
if [ ${#BREW_CASKS[@]} -gt 0 ]; then
  echo "  casks: ${BREW_CASKS[*]}"
  brew install --cask "${BREW_CASKS[@]}" || {
    error "Could not install: ${BREW_CASKS[*]}"
    echo "  Docker Desktop: https://www.docker.com/products/docker-desktop/" >&2
    echo "  Ollama:         https://ollama.com/download" >&2
    exit 1
  }
fi
[ ${#BREW_FORMULAS[@]} -eq 0 ] && [ ${#BREW_CASKS[@]} -eq 0 ] && info "jq, Docker and Ollama already present"

# ── Docker ─────────────────────────────────────────────────────────────────
# The licence is accepted by pre-seeding Docker's OWN settings file before first
# launch — the same key Docker writes when you click Accept. It lives under
# ~/Library/Group Containers and is owned by the user, so this needs no sudo.
# Previously the script installed Docker, told the user to open it, accept the
# terms and re-run, then exited 0 — a success code for an incomplete install.
docker_accept_license() {
  local dir="$HOME/Library/Group Containers/group.com.docker"
  mkdir -p "$dir"
  python3 - "$dir" <<'PYEOF'
import json, os, sys
d = sys.argv[1]
# Two files, two key spellings: settings-store.json is current, settings.json is
# the legacy name. Writing both keeps older and newer Docker Desktop happy.
for name, key in (("settings-store.json", "LicenseTermsVersion"),
                  ("settings.json",       "licenseTermsVersion")):
    p = os.path.join(d, name)
    try:
        cfg = json.load(open(p)) if os.path.exists(p) else {}
    except Exception:
        cfg = {}
    if cfg.get(key):
        continue                      # already accepted — do not rewrite
    cfg[key] = 2
    json.dump(cfg, open(p, "w"), indent=2)
    print(f"  ✓ Docker licence terms recorded in {name}")
PYEOF
}

# ── ollama on the system PATH ──────────────────────────────────────────────
#
# The cask puts ollama in the Homebrew prefix, which is on PATH for the user who
# installed it and nowhere else. /usr/local/bin IS on the minimal PATH that
# launchd jobs and hooks get (/usr/gnu/bin:/usr/local/bin:/bin:/usr/bin), where
# /opt/homebrew/bin is not — so a symlink there is what makes `ollama` resolvable
# from a non-login context and for other accounts on the machine.
#
# Ollama.app offers to create this itself with its own authorisation dialog. We
# create it under the sudo already granted in the preflight, so it costs no extra
# prompt. Skipped entirely if something is already there — we do not adopt or
# overwrite a path we did not create.
step "Putting ollama on the system PATH"
if [ -e /usr/local/bin/ollama ]; then
  info "/usr/local/bin/ollama already present ($(readlink /usr/local/bin/ollama 2>/dev/null || echo 'regular file'))"
else
  OLLAMA_TARGET=""
  for c in "/Applications/Ollama.app/Contents/Resources/ollama" "$(command -v ollama 2>/dev/null || true)"; do
    [ -n "$c" ] && [ -x "$c" ] && { OLLAMA_TARGET="$c"; break; }
  done
  if [ -n "$OLLAMA_TARGET" ]; then
    sudo mkdir -p /usr/local/bin 2>/dev/null || true
    if sudo ln -sfn "$OLLAMA_TARGET" /usr/local/bin/ollama 2>/dev/null; then
      info "/usr/local/bin/ollama -> $OLLAMA_TARGET"
    else
      warn "Could not create /usr/local/bin/ollama — ollama stays on the Homebrew PATH only"
    fi
  else
    warn "ollama binary not found — skipping the /usr/local/bin symlink"
  fi
fi

step "Checking Docker"
docker_accept_license
if ! docker ps >/dev/null 2>&1; then
  echo "  Starting Docker Desktop..."
  open -a Docker 2>/dev/null || true
  for _ in $(seq 1 60); do docker ps >/dev/null 2>&1 && break; sleep 5; done
fi
if ! docker ps >/dev/null 2>&1; then
  error "Docker daemon did not start. Open Docker Desktop, finish first-run setup, re-run."
  exit 1
fi
info "Docker present and running ($(docker --version | awk '{print $3}' | tr -d ,))"

# ── Ollama ─────────────────────────────────────────────────────────────────
# Installed above with the other Homebrew packages; this only verifies it.
# The old block re-tried `--cask ollama` then fell back to the FORMULA, which
# gives a CLI with no app — a different install shape reached by accident.
step "Checking Ollama"
if has ollama; then
  info "Ollama CLI present ($(ollama --version 2>/dev/null | awk '{print $NF}'))"
  if ! ollama list >/dev/null 2>&1; then
    warn "Ollama daemon is not running."
    echo "  Start the MANAGED service (not the GUI app, which competes for :11434):"
    echo "    launchctl kickstart -k gui/$(id -u)/com.ailocal.ollama"
    echo "  Models will be pulled by lib/install-models.sh after Ollama is running."
  else
    info "Ollama daemon is responding"
  fi
else
  warn "Ollama CLI not found after install attempt — proceed manually."
fi

# ── Ollama runtime env (keep-alive + parallel models) ─────────────────────
# IMPORTANT: the Ollama macOS app is a GUI/launchd process — it does NOT read
# ~/.zshrc. Env vars must be set via launchctl (persisted with a LaunchAgent).
# lib/setup-ollama-env.sh does that. Checking the shell env here would be
# misleading, so we check launchctl (what the app actually sees).
step "Configuring Ollama runtime env (launchctl, not ~/.zshrc)"
echo "  Two ways to run Ollama:"
echo "    [1] Production autostart — a launchd LaunchAgent runs 'ollama serve' at"
echo "        login (auto-restart, logs, env baked in incl. OLLAMA_MODELS on"
echo "        /Users/Shared) and preloads the coder model. Disables Ollama.app"
echo "        'launch at login' to avoid a port 11434 conflict. (lib/setup-startup.sh)"
echo "    [2] Env-only — keep using Ollama.app; just set runtime env vars via"
echo "        launchctl so models don't unload. (lib/setup-ollama-env.sh)"
if $ASSUME_YES; then
  AUTOSTART=y   # --yes picks autostart: it bakes OLLAMA_MODELS into the LaunchAgent
  echo "  --yes: setting up production autostart"
else
  read -r -p "  Set up production autostart? [y/N]: " AUTOSTART
fi
if [[ "${AUTOSTART:-}" =~ ^[Yy]$ ]]; then
  bash "$ROOT_DIR/lib/setup-startup.sh" --model architecture \
    || warn "Could not set up autostart — run ailocal autostart manually."
else
  # Check EVERY variable that matters, not one as a proxy for the rest. This used
  # to test OLLAMA_KEEP_ALIVE alone and treat it as "already configured", so a
  # machine with keep-alive set but OLLAMA_MODELS unset was reported as done —
  # and every model then landed in ~/.ollama instead of the shared store, which
  # only shows up as a surprise 40 GB in a home directory.
  CUR_KA=$(launchctl getenv OLLAMA_KEEP_ALIVE 2>/dev/null || true)
  CUR_MD=$(launchctl getenv OLLAMA_MODELS 2>/dev/null || true)
  if [ -n "$CUR_KA" ] && [ -n "$CUR_MD" ]; then
    info "OLLAMA_KEEP_ALIVE=$CUR_KA  OLLAMA_MAX_LOADED_MODELS=$(launchctl getenv OLLAMA_MAX_LOADED_MODELS 2>/dev/null || echo '?')"
    info "OLLAMA_MODELS=$CUR_MD"
  else
    warn "Ollama env not set where the app can see it (launchctl) — models would unload after 5 min."
    bash "$ROOT_DIR/lib/setup-ollama-env.sh" \
      || warn "Could not configure Ollama env — run ailocal ollama-env manually."
    echo "  ▶ Restart Ollama (menubar → Quit, then reopen) for it to take effect."
  fi
fi

# ── Hardware profile selection ─────────────────────────────────────────────
step "Detecting hardware profile"

RAM_BYTES=$(sysctl -n hw.memsize 2>/dev/null || echo 0)
RAM_GB=$((RAM_BYTES / 1024 / 1024 / 1024))

# NEVER ROUND UP. This used to select the tier at 75% of its name — 24 GB got the
# 32gb profile, 48 GB got 64gb, 96 GB got 128gb — so a machine was routinely given
# models sized for memory it did not have. A tier is chosen only when the machine
# actually has that much memory.
#
#   >= 128   128gb
#   64-127   64gb
#   32-63    32gb
#   16-31    16gb
#   < 16     unsupported
if   [ "$RAM_GB" -ge 128 ]; then RAM_TIER="128gb"
elif [ "$RAM_GB" -ge 64 ];  then RAM_TIER="64gb"
elif [ "$RAM_GB" -ge 32 ];  then RAM_TIER="32gb"
elif [ "$RAM_GB" -ge 16 ];  then RAM_TIER="16gb"
else
  error "${RAM_GB} GB of unified memory — ailocal requires at least 16 GB."
  echo "  The smallest profile runs a 4b primary model at 64K context; below 16 GB" >&2
  echo "  that does not fit alongside macOS. No profile is offered rather than one" >&2
  echo "  that would swap or OOM." >&2
  exit 1
fi

# Explicit override, validated. --profile wins over detection, but an override that
# exceeds physical memory is refused unattended and must be confirmed interactively:
# the failure mode is a machine thrashing on models it cannot hold.
if [ -n "${PROFILE_OVERRIDE:-}" ]; then
  case "$PROFILE_OVERRIDE" in
    16gb|32gb|64gb|128gb) ;;
    *) error "unknown profile '$PROFILE_OVERRIDE' (expected 16gb, 32gb, 64gb or 128gb)"; exit 1 ;;
  esac
  OVERRIDE_GB="${PROFILE_OVERRIDE%gb}"
  if [ "$OVERRIDE_GB" -gt "$RAM_GB" ]; then
    warn "--profile $PROFILE_OVERRIDE exceeds detected memory (${RAM_GB} GB)."
    warn "  Models sized for ${OVERRIDE_GB} GB will swap or fail to load."
    if $ASSUME_YES; then
      error "Refusing an unsafe override under --yes. Re-run interactively to confirm."
      exit 1
    fi
    read -r -p "  Use $PROFILE_OVERRIDE anyway? [y/N]: " OK
    case "$OK" in y|Y|yes|Yes) : ;; *) error "aborted"; exit 1 ;; esac
  fi
  info "profile overridden: $RAM_TIER -> $PROFILE_OVERRIDE"
  RAM_TIER="$PROFILE_OVERRIDE"
fi

PROFILE_SRC="$ROOT_DIR/profiles/${RAM_TIER}.yaml"
# The policy owner spells this path, so moving it needs no installer change.
ACTIVE_PROFILE="$(python3 "$ROOT_DIR/lib/profile-config" active-profile-path)"

info "Detected ${RAM_GB} GB RAM → profile: ${RAM_TIER}"

# Record the active tier as a one-line marker (machine-specific, gitignored). sync-models.py reads
# profiles/<tier>.yaml directly — there is no intermediate models.yaml copy to drift or to
# edit-then-lose. Edit the tracked profile itself to change models.
if [ ! -f "$PROFILE_SRC" ]; then
  warn "profile $RAM_TIER not found at $PROFILE_SRC — cannot continue"
  exit 1
fi
CURRENT="$(cat "$ACTIVE_PROFILE" 2>/dev/null || echo none)"
if [ "$CURRENT" = "$RAM_TIER" ]; then
  info "active profile already $RAM_TIER"
else
  [ "$CURRENT" != "none" ] && warn "active profile was '$CURRENT' — switching to $RAM_TIER"
  mkdir -p "$(dirname "$ACTIVE_PROFILE")"
  echo "$RAM_TIER" > "$ACTIVE_PROFILE"
  info "active profile set to $RAM_TIER ($ACTIVE_PROFILE)"
fi
# Report the whole plan before anything is pulled. A disk warning that just says
# "80 GB required" is unauditable; every number below names where it came from.
# From the resolver, not a grep of the YAML: this was the last profile-YAML
# read left in a shell entry point.
PROFILE_STATUS=""

# GENERATE BEFORE REPORTING. The plan below is rendered from the generated
# artifact, so generation has to have succeeded first -- and a generation
# failure must stop the install before any model is pulled.
#
# The plan is read through policy.py, never parsed here: a second profile
# parser in a shell entry point is exactly what policy.py exists to prevent,
# and it silently prints None once the schema moves.
#
# and would have kept printing plausible-looking stale numbers had the field
# names survived. Shell entry points ask the resolver; they do not parse YAML.
if ! has python3; then
  error "python3 is required to generate model configuration"
  exit 1
fi
if [ ! -f "$ROOT_DIR/lib/sync-models.py" ]; then
  error "lib/sync-models.py is missing — cannot generate model configuration"
  exit 1
fi
echo
step "Syncing model config"
if ! python3 "$ROOT_DIR/lib/sync-models.py"; then
  error "model configuration generation FAILED — stopping before any model is pulled,"
  error "and before the install plan. Existing generated files were left"
  error "untouched for diagnosis, and are marked unusable: their recorded source"
  error "hashes no longer match."
  exit 1
fi

echo
_PLAN="$(python3 "$ROOT_DIR/lib/profile-config" profile-summary)"
echo "  architecture:        $(uname -m)"
echo "  physical memory:     ${RAM_GB} GB"
PROFILE_STATUS="$(printf '%s' "$_PLAN" | jq -r '.status // "unknown"')"
echo "  selected profile:    ${RAM_TIER}  (${PROFILE_STATUS})"
if ! python3 "$ROOT_DIR/lib/profile-config" profile-summary >/dev/null 2>&1; then
  error "the generated profile is unreadable — refusing to print an assumed plan"
  exit 1
fi
# jq, not an embedded parser: it is already a hard install dependency (checked
# in the preflight above) and `ailocal doctor` queries the same summary with it.
_jq() { printf '%s' "$_PLAN" | jq -r "$1"; }
_SHARED_MODEL="$(_jq '[.roles[].model] | group_by(.) | max_by(length) | .[0]')"
_SHARED_ROLES="$(_jq --arg m "$_SHARED_MODEL" '[.roles|to_entries[]|select(.value.model==$m)|.key]|sort|join(", ")' 2>/dev/null \
                 || printf '%s' "$_PLAN" | jq -r "[.roles|to_entries[]|select(.value.model==\"$_SHARED_MODEL\")|.key]|sort|join(\", \")")"
_FIRST_ROLE="$(printf '%s' "$_SHARED_ROLES" | cut -d, -f1)"
echo "  primary model:       $_SHARED_MODEL"
echo "  shared across:       $_SHARED_ROLES"
printf '%s' "$_PLAN" | jq -r --arg m "$_SHARED_MODEL" \
  '.roles | to_entries | map(select(.value.model != $m))
   | group_by(.value.model)[] | "  \(.[0].key + " model:" | .[0:20] | . + " " * (21 - length))\(.[0].value.model)"'
echo "  context_input:       $(_jq ".roles.\"$_FIRST_ROLE\".context_input") (a maximum, not a per-request reservation)"
echo "  max_output:          $(_jq ".roles.\"$_FIRST_ROLE\".max_output")"
echo "  total_context:       $(_jq ".roles.\"$_FIRST_ROLE\" | .context_input + .max_output")"
echo "  unique models:       $(_jq '[.roles[].model] | unique | length')"
echo "  parallelism:         ${OLLAMA_NUM_PARALLEL:-2} (Ollama divides a runner's context across"
echo "                       parallel sequences — two full-context requests are not guaranteed)"
case "$RAM_TIER" in
  16gb) echo "  tier notes:          one shared 4b primary; quality below larger tiers by design" ;;
  32gb) echo "  tier notes:          one shared 9b primary; targets daily coding and agent work" ;;
  128gb) echo "  tier notes:          mirrors the validated 64gb configuration; 128 GB-specific"
         echo "                       tuning deferred until measured on matching hardware" ;;
esac
[ "$PROFILE_STATUS" != "measured" ] && warn "profile '$RAM_TIER' is $PROFILE_STATUS — not measured on matching hardware"

# ── Directory structure ────────────────────────────────────────────────────
step "Creating directory structure"
# Runtime state lives outside the checkout (see lib/compose.sh). Only the
# backup directory needs pre-creating, because it holds .env snapshots from
# update.sh and must be locked down before anything writes one.
mkdir -p "$AILOCAL_STATE/backups"
chmod 700 "$AILOCAL_STATE/backups"
info "Directories ready"

# ── .env generation ────────────────────────────────────────────────────────
step "Configuring environment (.env)"

# Run the service stack, client install, and healthcheck automatically.
run_next_steps() {
  # Sync models.yaml → litellm config so LiteLLM sees the latest model choices.
  # Configuration was already generated above, BEFORE the install plan was
  # printed -- the plan is rendered from that generated artifact, so it cannot
  # run against stale or unparsed state. Generation failure exits there.

  echo
  # Fetches the PINNED digests, not "latest" -- every image in deploy/ is
  # digest-pinned, so `dc pull` resolves nothing and a fresh install receives
  # exactly the validated images. It cannot silently start on a newer LiteLLM.
  # To learn whether a newer release exists: ailocal security --check-updates
  step "Pulling pinned Docker images"
  dc pull

  echo
  step "Starting Docker services"
  bash "$ROOT_DIR/lib/lifecycle.sh" start --no-wait

  # Always restart LiteLLM so it picks up any config or model changes.
  echo
  step "Reloading LiteLLM"
  dc restart litellm searxng
  if ! ailocal_wait_ready 30 progress; then
    warn "LiteLLM did not become ready — check: docker logs ailocal-litellm"
  fi
  echo ""
  info "LiteLLM ready"

  echo
  step "Pulling Ollama models"
  bash "$ROOT_DIR/lib/install-models.sh"

  echo
  step "Checking health"
  python3 "$ROOT_DIR/lib/checks/run.py" doctor || true

  # Client configs are OPT-IN — installing them rewrites/merges existing
  # Claude Code / Codex / VS Code settings, which can disrupt a customized
  # setup. Ask instead of doing it automatically.
  echo
  step "Client configs (optional)"
  echo "  ailocal can point Claude Code, Codex, and VS Code at the local proxy."
  echo "  ⚠ This backs up, then rewrites/merges existing client configs."
  echo "    Choose: all | claude | codex | vscode  (space-separated) — or Enter to skip"
  if $ASSUME_YES; then
    CLIENTS=skip   # deploying into a user's client roots is never implied by --yes
    echo "  --yes: skipping client configs (run 'ailocal clients' when ready)"
  else
    read -r -p "  Install which client configs? [skip]: " CLIENTS
  fi
  CLIENTS="${CLIENTS:-skip}"
  case "$CLIENTS" in
    skip|"")
      info "Skipped — run later with:  ailocal clients [all|claude|codex|vscode]" ;;
    all)
      bash "$ROOT_DIR/lib/install-clients.sh" || warn "Client install reported issues." ;;
    *)
      # shellcheck disable=SC2086
      bash "$ROOT_DIR/lib/install-clients.sh" $CLIENTS \
        || warn "Client install reported issues — check target names (claude codex vscode)." ;;
  esac

  echo
  step "Done"
  echo "  LiteLLM proxy is ready at http://localhost:4000"
  echo "  Verify a real request:  ailocal smoke"
}

if [ -f "$ENV_FILE" ]; then
  echo "  .env already exists."
  if $ASSUME_YES; then
    REGEN=n   # never overwrite an existing .env unattended — it holds the master key
    echo "  --yes: keeping the existing .env"
  else
    read -r -p "  Re-generate it? Existing values will be overwritten. [y/N]: " REGEN
  fi
  if [[ ! "${REGEN:-}" =~ ^[Yy]$ ]]; then
    echo "  Keeping existing .env."
    run_next_steps
    exit 0
  fi
fi

echo
echo "  Generating the LiteLLM master key and SearXNG secret..."

# LiteLLM master key — must start with sk- for OpenAI SDK compatibility
LITELLM_MASTER_KEY="sk-$(openssl rand -hex 24)"

# SearXNG refuses to start without a real secret. Generated per install so the
# placeholder in deploy/searxng/settings.yml is never used.
SEARXNG_SECRET="$(openssl rand -hex 32)"


# Write the .env file from scratch (no fragile sed replacements)
cat > "$ENV_FILE" <<EOF
# ailocal — generated by install.sh on $(date)
# Do NOT commit this file to version control.

# ── General ────────────────────────────────────────────────────────────────
AILOCAL_ENV=local

# ── Ollama ─────────────────────────────────────────────────────────────────
OLLAMA_URL=http://host.docker.internal:11434

# ── LiteLLM proxy key ──────────────────────────────────────────────────────
# Use this as your ANTHROPIC_API_KEY / OPENAI_API_KEY when pointing clients
# at http://localhost:4000 instead of the real cloud APIs.
LITELLM_MASTER_KEY=${LITELLM_MASTER_KEY}

# ── SearXNG (local web search backend) ─────────────────────────────────────
# Consumed by deploy/searxng/compose.yaml. LiteLLM reaches SearXNG over
# the ailocal_net bridge at http://searxng:8080 — no API key, nothing external.
SEARXNG_SECRET=${SEARXNG_SECRET}

# ── Cloud fallbacks (disabled by default) ─────────────────────────────────
# Set ENABLE_CLOUD=true and uncomment the relevant model block in
# the generated litellm/config.yaml to enable cloud fallback for a specific model.
ENABLE_CLOUD=false
# To enable cloud fallback: add your key here, set ENABLE_CLOUD=true,
# and uncomment the relevant model block in the generated litellm/config.yaml.
# Then: ailocal start  (or: dc restart litellm)
ANTHROPIC_API_KEY=
OPENAI_API_KEY=
EOF

chmod 600 "$ENV_FILE"
info ".env written with the LiteLLM master key (chmod 600)"

run_next_steps
