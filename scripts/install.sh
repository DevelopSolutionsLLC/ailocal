#!/usr/bin/env bash
# install.sh — bootstrap host tools, generate .env, verify Docker & Ollama
# Idempotent: safe to run multiple times.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE="$ROOT_DIR/.env"

# Single source of truth for how this stack is composed (deploy/litellm + deploy/searxng).
AILOCAL_ROOT="$ROOT_DIR"
. "$ROOT_DIR/scripts/lib/compose.sh"

# ── Helpers ────────────────────────────────────────────────────────────────

has() { command -v "$1" >/dev/null 2>&1; }

info()  { echo "  ✓ $*"; }
warn()  { echo "  ⚠ $*" >&2; }
error() { echo "  ✗ $*" >&2; }
step()  { echo; echo "▶ $*"; }

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
prompt_secret() {
  local varname="$1"
  local prompt="$2"
  read -r -s -p "  $prompt (leave blank to auto-generate): " REPLY
  echo
}

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
for a in "$@"; do [ "$a" = "--yes" ] && ASSUME_YES=true; done

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

# ── CLI tools ──────────────────────────────────────────────────────────────
# ── Everything Homebrew provides, in one pass ──────────────────────────────
#
# Two commands, not one, and the distinction is load-bearing:
# `brew install docker` resolves to the CLI-only FORMULA — no daemon, no VM — so
# a mixed `brew install jq docker ollama` would leave `has docker` true while
# `docker ps` fails with nothing listening. The casks are named explicitly
# (docker-desktop, ollama-app) rather than relying on alias resolution.
#
# Batched so Homebrew escalates once per command instead of once per package.
# Homebrew prompts for its own sudo when a cask needs it; the preflight asked
# earlier only so that prompt is not a surprise mid-run.
step "Installing Homebrew packages"
BREW_FORMULAS=(); BREW_CASKS=()
has jq     || BREW_FORMULAS+=("jq")
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
    echo "  Start it with: ollama serve   (or open /Applications/Ollama.app)"
    echo "  Models will be pulled by scripts/install-models.sh after Ollama is running."
  else
    info "Ollama daemon is responding"
  fi
else
  warn "Ollama CLI not found after install attempt — proceed manually."
fi

# ── Ollama runtime env (keep-alive + parallel models) ─────────────────────
# IMPORTANT: the Ollama macOS app is a GUI/launchd process — it does NOT read
# ~/.zshrc. Env vars must be set via launchctl (persisted with a LaunchAgent).
# scripts/setup-ollama-env.sh does that. Checking the shell env here would be
# misleading, so we check launchctl (what the app actually sees).
step "Configuring Ollama runtime env (launchctl, not ~/.zshrc)"
echo "  Two ways to run Ollama:"
echo "    [1] Production autostart — a launchd LaunchAgent runs 'ollama serve' at"
echo "        login (auto-restart, logs, env baked in incl. OLLAMA_MODELS on"
echo "        /Users/Shared) and preloads the coder model. Disables Ollama.app"
echo "        'launch at login' to avoid a port 11434 conflict. (scripts/setup-startup.sh)"
echo "    [2] Env-only — keep using Ollama.app; just set runtime env vars via"
echo "        launchctl so models don't unload. (scripts/setup-ollama-env.sh)"
if $ASSUME_YES; then
  AUTOSTART=y   # --yes picks autostart: it bakes OLLAMA_MODELS into the LaunchAgent
  echo "  --yes: setting up production autostart"
else
  read -r -p "  Set up production autostart? [y/N]: " AUTOSTART
fi
if [[ "${AUTOSTART:-}" =~ ^[Yy]$ ]]; then
  bash "$ROOT_DIR/scripts/setup-startup.sh" --model coder \
    || warn "Could not set up autostart — run ./scripts/setup-startup.sh manually."
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
    bash "$ROOT_DIR/scripts/setup-ollama-env.sh" \
      || warn "Could not configure Ollama env — run ./scripts/setup-ollama-env.sh manually."
    echo "  ▶ Restart Ollama (menubar → Quit, then reopen) for it to take effect."
  fi
fi

# ── Hardware profile selection ─────────────────────────────────────────────
step "Detecting hardware profile"

RAM_BYTES=$(sysctl -n hw.memsize 2>/dev/null || echo 0)
RAM_GB=$((RAM_BYTES / 1024 / 1024 / 1024))

if   [ "$RAM_GB" -ge 96 ]; then RAM_TIER="128gb"
elif [ "$RAM_GB" -ge 48 ]; then RAM_TIER="64gb"
elif [ "$RAM_GB" -ge 24 ]; then RAM_TIER="32gb"
else                             RAM_TIER="16gb"
fi

PROFILE_SRC="$ROOT_DIR/config/profiles/${RAM_TIER}.yaml"
ACTIVE_PROFILE="$ROOT_DIR/config/active-profile"

info "Detected ${RAM_GB} GB RAM → profile: ${RAM_TIER}"

# Record the active tier as a one-line marker (machine-specific, gitignored). sync-models.py reads
# config/profiles/<tier>.yaml directly — there is no intermediate models.yaml copy to drift or to
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
  echo "$RAM_TIER" > "$ACTIVE_PROFILE"
  info "active profile set to $RAM_TIER (config/active-profile)"
fi
[ "$RAM_TIER" != "64gb" ] && warn "profile '$RAM_TIER' is marked status: unverified — validate with 'ailocal validate' before relying on it"

# ── Directory structure ────────────────────────────────────────────────────
step "Creating directory structure"
# Runtime state lives outside the checkout (see scripts/lib/compose.sh). Only the
# backup directory needs pre-creating, because it holds .env snapshots from
# update.sh and must be locked down before anything writes one.
mkdir -p "${AILOCAL_STATE:-${XDG_STATE_HOME:-$HOME/.local/state}/ailocal}/backups"
chmod 700 "${AILOCAL_STATE:-${XDG_STATE_HOME:-$HOME/.local/state}/ailocal}/backups"
info "Directories ready"

# ── .env generation ────────────────────────────────────────────────────────
step "Configuring environment (.env)"

# Run the service stack, client install, and healthcheck automatically.
run_next_steps() {
  # Sync models.yaml → litellm config so LiteLLM sees the latest model choices.
  if has python3 && [ -f "$ROOT_DIR/scripts/sync-models.py" ]; then
    echo
    step "Syncing model config"
    python3 "$ROOT_DIR/scripts/sync-models.py" || true
  fi

  echo
  # Initial setup pulls the latest image so a fresh install starts on current LiteLLM
  # (bug fixes land in main-stable frequently). Routine start.sh never auto-pulls —
  # that keeps a reboot reproducible; use update.sh (or `./scripts/update.sh`) to refresh.
  step "Pulling latest Docker images"
  dc pull

  echo
  step "Starting Docker services"
  bash "$ROOT_DIR/scripts/start.sh" --no-wait

  # Always restart LiteLLM so it picks up any config or model changes.
  echo
  step "Reloading LiteLLM"
  dc restart litellm searxng
  attempts=0
  until curl -sSf --max-time 3 http://localhost:4000/health/liveliness >/dev/null 2>&1; do
    attempts=$((attempts + 1))
    if [ $attempts -ge 30 ]; then
      warn "LiteLLM did not become ready — check: docker logs ailocal-litellm"
      break
    fi
    printf "  Waiting... (%ds)\r" $((attempts * 3))
    sleep 3
  done
  echo ""
  info "LiteLLM ready"

  echo
  step "Pulling Ollama models"
  bash "$ROOT_DIR/scripts/install-models.sh"

  echo
  step "Checking health"
  bash "$ROOT_DIR/scripts/doctor.sh" || true

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
      bash "$ROOT_DIR/scripts/install-clients.sh" || warn "Client install reported issues." ;;
    *)
      # shellcheck disable=SC2086
      bash "$ROOT_DIR/scripts/install-clients.sh" $CLIENTS \
        || warn "Client install reported issues — check target names (claude codex vscode)." ;;
  esac

  echo
  step "Done"
  echo "  LiteLLM proxy is ready at http://localhost:4000"
  echo "  Verify a real request:  ./scripts/smoke-test.sh"
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
# ailocal — generated by scripts/install.sh on $(date)
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
# Consumed by deploy/searxng/docker-compose.yml. LiteLLM reaches SearXNG over
# the ailocal_net bridge at http://searxng:8080 — no API key, nothing external.
SEARXNG_SECRET=${SEARXNG_SECRET}

# ── Cloud fallbacks (disabled by default) ─────────────────────────────────
# Set ENABLE_CLOUD=true and uncomment the relevant model block in
# config/litellm/config.yaml to enable cloud fallback for a specific model.
ENABLE_CLOUD=false
# To enable cloud fallback: add your key here, set ENABLE_CLOUD=true,
# and uncomment the relevant model block in config/litellm/config.yaml.
# Then: ailocal start  (or: dc restart litellm)
ANTHROPIC_API_KEY=
OPENAI_API_KEY=
EOF

chmod 600 "$ENV_FILE"
info ".env written with the LiteLLM master key (chmod 600)"

run_next_steps
