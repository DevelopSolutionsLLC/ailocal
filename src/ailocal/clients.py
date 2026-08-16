"""clients.py — deploying the isolated client homes.

ailocal provisions an isolated home per client under the config root and owns
the files it ships there. It does NOT own the directories: a user or another
tool may add content, and `install_managed_dir` is the contract that survives —
foreign files and foreign symlinks are left alone, a marked external block
inside a file ailocal also writes is carried across the overwrite, and only
names in ailocal's own manifest are ever pruned.

The three clients do not share a config format, so nothing here is generalised
into a templating engine. What they do share — backups, the managed-directory
contract, the shell sourcing, the key — lives once, at the top.

Never touched: ~/.claude, ~/.codex. Those are the user's cloud sessions.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from . import policy, runtime

BACKUP_KEEP = int(os.environ.get("BACKUP_KEEP", "5"))
TARGETS = ("vscode", "codex", "claude")

#: The three first-class clients are OPTIONAL CONSUMERS of the local proxy, not
#: dependencies of it: the runtime serves every one of them and needs none. So
#: each carries how to detect it and the exact supported command to get it, and
#: `ailocal clients` with no argument configures only the ones that are here.
#: ailocal does not install a client for you — same rule as PREREQUISITES in
#: install.py, for the same reason.
#:
#: Commands verified against the vendors' own current documentation and against
#: `brew info` (August 2026):
#:   Claude Code  code.claude.com/docs/en/quickstart — Homebrew cask, or
#:                curl -fsSL https://claude.ai/install.sh | bash
#:   Codex CLI    Homebrew cask, or npm i -g @openai/codex
#:   VS Code      Homebrew cask. Copilot Chat ships IN VS Code; there is no
#:                separate extension to install and no subscription needed —
#:                the connector serves BYOK models.
CLIENTS: dict[str, tuple[str, str]] = {
    "claude": ("Claude Code", "brew install --cask claude-code"),
    "codex":  ("Codex CLI",   "brew install --cask codex"),
    "vscode": ("VS Code",     "brew install --cask visual-studio-code"),
}


def present(name: str) -> bool:
    """Is this client on the machine? Detection is the client's own artefact.

    VS Code counts either way round: the `code` CLI without a user directory is
    an installation that has never been launched, which install_vscode reports
    precisely — treating it as absent would hide that.
    """
    if name == "vscode":
        return shutil.which("code") is not None or _vscode_user_dir() is not None
    return shutil.which({"claude": "claude", "codex": "codex"}[name]) is not None


def report_missing(names) -> None:
    """Name every absent client once, with the one command that installs it."""
    absent = [n for n in names if not present(n)]
    if not absent:
        return
    print("\n  Not installed (optional — ailocal runs without them):")
    for n in absent:
        label, how = CLIENTS[n]
        print(f"    {label:14} {how}")
    print("  Install any of them, then re-run: ailocal clients")

CONNECTOR_EXT = "Gethnet.litellm-connector-copilot"

#: One canonical proxy URL for every host client, owned by runtime. It is
#: 127.0.0.1 and not `localhost` on purpose: Docker publishes the port on IPv4
#: only, `localhost` on macOS also resolves to ::1, and a client whose runtime
#: tries ::1 first gets a bare connection refusal. Docker's own
#: host.docker.internal is a different question and is not affected.
BASE_URL = os.environ.get("AILOCAL_BASE_URL") or runtime.proxy_url()

#: Literal marker pair another tool appends into files ailocal also writes. The
#: strings are fixed because that tool writes them; ailocal only has to
#: recognise and carry the block, and works unchanged if nothing writes one.
OVERLAY_START = "<!-- cadence:start -->"
OVERLAY_END = "<!-- cadence:end -->"
MANIFEST_NAME = ".ailocal-managed"

#: Blocks in a shared source that only Claude Code should receive.
_CLAUDE_ONLY = re.compile(r"<!-- claude-only -->.*?<!-- /claude-only -->\n?",
                          re.DOTALL)

GREEN, YELLOW, RESET, BOLD = "\033[32m", "\033[33m", "\033[0m", "\033[1m"


def step(m): print(f"\n▶ {m}")
def info(m): print(f"  {GREEN}✓{RESET} {m}")
def warn(m): print(f"  {YELLOW}⚠{RESET} {m}", file=sys.stderr)
def skip(m): print(f"  — {m}")


def _code(*args, timeout: int = 120) -> str | None:
    """Run the VS Code CLI. None means it is not usable at all."""
    if not shutil.which("code"):
        return None
    try:
        r = subprocess.run(["code", *args], capture_output=True, text=True,
                           timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout if r.returncode == 0 else None


def _extensions() -> list[str]:
    out = _code("--list-extensions")
    return [l.strip().lower() for l in (out or "").splitlines() if l.strip()]


# ── shared mechanics ────────────────────────────────────────────────────────

def backup(path: Path) -> bool:
    """Timestamped copy, keeping the newest BACKUP_KEEP: rollback only ever
    reaches for a recent one, and unpruned copies turn the roots into landfill."""
    if not path.is_file():
        return False
    dest = path.with_name(f"{path.name}.bak.{datetime.now():%Y%m%d_%H%M%S}")
    shutil.copyfile(path, dest)
    warn(f"Backed up: {path.name} → {dest.name}")
    olds = sorted(path.parent.glob(f"{path.name}.bak.*"), reverse=True)
    for stale in olds[BACKUP_KEEP:]:
        stale.unlink(missing_ok=True)
    return True


#: Files ailocal shipped BEFORE the manifest existed, named here because that is
#: the only ownership proof available for them. `prompts/` was written with a
#: plain copy, so retirement cannot ask a manifest what we wrote; declaring the
#: exact names keeps the removal provable and leaves anything else alone.
RETIRED_UNMANAGED = {"prompts": ("analyze-repo.md", "local-build.md")}


def retire_managed_dir(dst: Path) -> None:
    """Remove a directory ailocal used to ship and no longer does.

    install_managed_dir() prunes by manifest, but only for directories it is
    still called for. A tree that stops being shipped is never visited again, so
    it sits on every already-installed machine forever — which is what the
    planner/implementer/reviewer agents and the local-build commands would have
    done. Ownership is proved the same way it is proved during install: the
    manifest names what we wrote, and nothing else is touched, so a user's own
    file in the same directory survives and keeps the directory alive.
    """
    manifest = dst / MANIFEST_NAME
    names = (manifest.read_text().split() if manifest.is_file()
             else list(RETIRED_UNMANAGED.get(dst.name, ())))
    if not names:
        return
    for name in names:
        target = dst / name
        if target.is_symlink():
            continue                      # someone else's link, not ours
        if target.is_dir():
            shutil.rmtree(target, ignore_errors=True)
        else:
            target.unlink(missing_ok=True)
    manifest.unlink(missing_ok=True)
    try:
        dst.rmdir()                       # only when we emptied it
        info(f"retired {dst}")
    except OSError:
        info(f"retired ailocal content in {dst} (other files kept)")


def install_managed_dir(src: Path, dst: Path) -> None:
    """Copy a directory of managed files without taking ownership of it."""
    dst.mkdir(parents=True, exist_ok=True)
    manifest = dst / MANIFEST_NAME

    # Prune what we shipped before and no longer ship. Only ever names in OUR
    # manifest, and never something that is now someone else's symlink.
    if manifest.is_file():
        for name in manifest.read_text().split():
            target = dst / name
            if (src / name).exists() or target.is_symlink():
                continue
            if target.is_dir():
                shutil.rmtree(target, ignore_errors=True)
            else:
                target.unlink(missing_ok=True)

    shipped = sorted(p for p in src.iterdir())
    manifest.write_text("".join(f"{p.name}\n" for p in shipped))
    for entry in shipped:
        target = dst / entry.name
        if entry.is_dir():
            shutil.rmtree(target, ignore_errors=True)
            shutil.copytree(entry, target)
            continue

        # Carry any marked block already present, so overwriting our file does
        # not silently drop what another tool appended.
        carried = ""
        if target.is_file() and not target.is_symlink():
            text = target.read_text(encoding="utf-8", errors="replace")
            if OVERLAY_START in text and OVERLAY_END in text:
                start = text.index(OVERLAY_START)
                end = text.index(OVERLAY_END) + len(OVERLAY_END)
                carried = text[start:end]

        # Never write THROUGH a symlink: that edits the linked file in its own
        # repository.
        if target.is_symlink():
            target.unlink()
        shutil.copyfile(entry, target)
        if carried:
            with open(target, "a", encoding="utf-8") as fh:
                fh.write(f"\n{carried}\n")
            info(f"  preserved external block in {entry.name}")


def _concat_shared(head: Path, checklist: Path, dest: Path,
                   claude_only: bool) -> None:
    """A client's own protocol file plus the one shared build checklist."""
    text = checklist.read_text(encoding="utf-8")
    if not claude_only:
        text = _CLAUDE_ONLY.sub("", text)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(head.read_text(encoding="utf-8") + text, encoding="utf-8")


def _master_key() -> str:
    from . import runtime
    key = runtime.env_value("LITELLM_MASTER_KEY")
    if not key:
        raise SystemExit(
            "  ✗ LITELLM_MASTER_KEY is not set — run ailocal install first")
    return key


# ── the shell entry points ──────────────────────────────────────────────────

_CONFIGURE_LINE = (
    '[[ -r "${XDG_CONFIG_HOME:-$HOME/.config}/ailocal/configure.zsh" ]] && '
    'source "${XDG_CONFIG_HOME:-$HOME/.config}/ailocal/configure.zsh"'
    '  # ailocal-configure')
_FINALIZE_LINE = (
    '[[ -r "${XDG_CONFIG_HOME:-$HOME/.config}/ailocal/finalize.zsh" ]] && '
    'source "${XDG_CONFIG_HOME:-$HOME/.config}/ailocal/finalize.zsh"'
    '    # ailocal-finalize')


def ensure_shell_sourcing(key: str) -> None:
    """The client home, the wrapper env, and two idempotent ~/.zshrc lines.

    Those two marker-commented lines are the ONLY footprint ailocal leaves in
    the user's rc file; uninstalling is removing them plus the config root."""
    step(f"Setting up {policy.deployed_client_root()}")
    cfg = policy.deployed_client_root()
    cfg.mkdir(parents=True, exist_ok=True)
    cfg.chmod(0o700)

    # NO KEY PROJECTION HERE. This used to write `env` holding AILOCAL_API_KEY,
    # a second copy of the master key that a rotation would leave stale while
    # still looking authoritative. env.sh reads the canonical generated file
    # instead: one owner, and nothing to keep in sync.
    from . import environment
    environment.legacy_projection().unlink(missing_ok=True)

    data = policy.data_root()
    shutil.copyfile(data / "clients" / "finalize.zsh", cfg / "finalize.zsh")
    # Client-invoked hooks: the client execs these, so the deployed copy must be
    # executable regardless of the mode the source carries.
    for hook in ("scratchpad-hook.sh", "compact-hook.sh"):
        shutil.copyfile(data / "clients" / hook, cfg / hook)
        (cfg / hook).chmod(0o755)
    info(f"finalize.zsh / scratchpad-hook.sh / compact-hook.sh deployed to {cfg}")

    rc = Path(os.environ.get("ZDOTDIR") or Path.home()) / ".zshrc"
    if not rc.exists():
        # A bare Mac has no rc file yet; skipping injection would leave the
        # wrappers permanently unavailable.
        rc.touch()
        info(f"Created {rc} (none existed)")
    text = rc.read_text(encoding="utf-8")

    # configure FIRST (before any instant-prompt), finalize LAST.
    if "# ailocal-configure" in text:
        skip("ailocal-configure line already in ~/.zshrc")
    else:
        backup(rc)
        rc.write_text(f"{_CONFIGURE_LINE}\n{text}", encoding="utf-8")
        text = rc.read_text(encoding="utf-8")
        info("Inserted ailocal-configure as the first line of ~/.zshrc")
    if "# ailocal-finalize" in text:
        skip("ailocal-finalize line already in ~/.zshrc")
    else:
        backup(rc)
        with open(rc, "a", encoding="utf-8") as fh:
            fh.write(f"\n{_FINALIZE_LINE}\n")
        info("Appended ailocal-finalize to the end of ~/.zshrc")


# ── VS Code / Copilot Chat ──────────────────────────────────────────────────
#
# DEPRECATED SETTINGS — do not reintroduce. The connector replaced
# litellm-connector.baseUrl/.backends with VS Code's Language Models provider
# groups plus SecretStorage; VS Code deprecated
# github.copilot.chat.customOAIModels and replaced the "OpenAI Compatible"
# provider with "Custom Endpoint".
#   https://github.com/gethnet/litellm-connector-copilot/
#   https://code.visualstudio.com/docs/agent-customization/language-models

DEPRECATED_SETTINGS = (
    "litellm-connector.baseUrl",                       # -> provider groups
    "litellm-connector.backends",                      # -> provider groups
    "github.copilot.chat.customOAIModels",             # -> Custom Endpoint
    "github.copilot.agent.autoApprove",                # never a real setting
    "github.copilot.chat.tools.terminal.autoApprove",  # never a real setting
    # ailocal used to set this to "mainAgent". That routes Copilot's utility
    # and `tools` calls onto the SELECTED model, so a single chat turn fires
    # several concurrent 50k-token requests at one local 26B. Ollama serves
    # them one at a time (a 26B at 98k context does not fit twice), so the
    # third waits minutes for its first byte and the socket is reset —
    # "fetch failed", rootCause "read ECONNRESET". The connector's own README
    # says to leave it at "GitHub Copilot"; ailocal now removes the override
    # instead of writing it.
    "chat.byokUtilityModelDefault",
)

#: Added to settings.json ONLY if absent, so the user's own choices and their
#: comments survive.
#:
#: Every key here must be REQUIRED for a local model to answer in VS Code.
#: ailocal is not a VS Code policy manager: auto-approve, auto-accept delays and
#: agent-behaviour toggles are the user's preferences, not connectivity, and a
#: tool that quietly writes them is a tool nobody can reason about.
#: inactivityTimeout is the one that earns its place — a 26B model cold-loads
#: tens of GB emitting no tokens, which trips the connector's 60s default.
RECOMMENDED_SETTINGS = {
    "litellm-connector.inactivityTimeout": 300,
    "litellm-connector.enableResponsesApi": False,
    "litellm-connector.disableCaching": True,
    # Paired with the instruction files ailocal deploys; without the location
    # the deployed files are dead weight.
    "github.copilot.chat.codeGeneration.useInstructionFiles": True,
    "chat.instructionsFilesLocations": {"~/.copilot/instructions": True},
}


def _vscode_user_dir() -> Path | None:
    for candidate in (Path.home() / "Library/Application Support/Code/User",
                      Path.home() / ".config/Code/User"):
        if candidate.is_dir():
            return candidate
    return None


def _write_json(path: Path, data, indent) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text(json.dumps(data, indent=indent), encoding="utf-8")
    os.replace(tmp, path)


def _provider_group(models_json: Path, dry: bool) -> None:
    """Write the provider group, preserving the SecretStorage key reference.

    Only the API key VALUE lives in SecretStorage and cannot be seeded from
    outside VS Code; the file holds a reference to it, and discarding that
    reference forces the user to re-enter the key. Model entries are not
    written: the connector discovers them from the proxy's /model/info."""
    vendor, name = "litellm-connector", "LiteLLM"
    try:
        existing = json.loads(models_json.read_text(encoding="utf-8")) or []
    except FileNotFoundError:
        existing = []
    except ValueError:
        warn("existing chatLanguageModels.json unparseable; it will be replaced "
             "and any API key reference in it lost")
        existing = []

    carried = next((e.get("apiKey") for e in existing
                    if isinstance(e, dict) and e.get("vendor") == vendor
                    and isinstance(e.get("apiKey"), str)), None)
    group = {"name": name, "vendor": vendor, "baseUrl": BASE_URL}
    if carried:
        group["apiKey"] = carried
    merged = [e for e in existing
              if not (isinstance(e, dict) and e.get("vendor") == vendor)] + [group]

    if dry:
        print(f"  would write: {json.dumps(merged)}")
        return
    _write_json(models_json, merged, "\t")
    if carried:
        info("provider group written, existing API key reference preserved "
             f"({carried[:28]}…)")
    else:
        # The instruction itself is printed once, by target_vscode, so this only
        # reports the state it found.
        print(f"  {BOLD}{YELLOW}ACTION NEEDED{RESET} provider written, but no API "
              "key is initialized yet (see below).")


def _prune_deprecated(settings_json: Path, dry: bool) -> None:
    """Remove settings VS Code no longer honours: they make the config look
    configured while doing nothing.

    settings.json permits comments and trailing commas, so a stripped copy is
    parsed to INSPECT and the file is rewritten only if something must go."""
    try:
        raw = settings_json.read_text(encoding="utf-8")
    except FileNotFoundError:
        print("  no settings.json; nothing to prune")
        return
    stripped = re.sub(r",(\s*[}\]])", r"\1", re.sub(r"//[^\n]*", "", raw))
    try:
        doc = json.loads(stripped)
    except ValueError:
        warn("could not parse settings.json; leaving it untouched rather than "
             "risk mangling it")
        return
    present = [k for k in DEPRECATED_SETTINGS if k in doc]
    if not present:
        info("no deprecated keys present")
        return
    if dry:
        for k in present:
            print(f"  would remove {k}")
        return
    for k in present:
        doc.pop(k)
    original = settings_json.with_suffix(settings_json.suffix + ".ailocal-bak")
    if not original.exists():
        original.write_text(raw, encoding="utf-8")
    _write_json(settings_json, doc, 2)
    for k in present:
        info(f"removed {k}")
    print("  NOTE settings.json was rewritten from parsed JSON, so any COMMENTS "
          f"in it are gone. Original saved at {original.name}")


def _ensure_recommended(settings_json: Path) -> None:
    text = settings_json.read_text(encoding="utf-8") if settings_json.exists() else "{}"
    missing = {k: v for k, v in RECOMMENDED_SETTINGS.items()
               if f'"{k}"' not in text}
    if not missing:
        info("recommended connector settings already present")
        return
    backup(settings_json)
    # Textual insertion, not a reparse: a user's comments and formatting are
    # worth more than tidy output, and only absent keys are added.
    if "{" not in text:
        text = "{}"
    i = text.index("{")
    ins = "".join(f'\n    "{k}": {json.dumps(v)},' for k, v in missing.items())
    settings_json.write_text(text[:i + 1] + ins + text[i + 1:], encoding="utf-8")
    info(f"recommended connector settings added: {', '.join(missing)}")


def _install_extension(ext: str, installed: list[str]) -> None:
    if ext.lower() in installed:
        info(f"{ext} already installed")
    elif _code("--install-extension", ext, "--force") is not None:
        info(f"installed {ext}")
    else:
        warn(f"{ext} install failed — install it from the Marketplace")


#: install_vscode's three outcomes. ABSENT and FAILED are BOTH non-zero, so a
#: caller that only asks "did this work?" keeps the old answer — but they are
#: distinct, because "there is no VS Code here" is an expected end state and "a
#: write into the user directory blew up" is a defect the caller must not
#: silently swallow.
VSCODE_OK = 0
VSCODE_FAILED = 1
VSCODE_ABSENT = 2


def install_vscode(argv: list[str]) -> int:
    """Configure VS Code for the local stack without hand-editing the UI.

    Returns VSCODE_OK, VSCODE_ABSENT (no VS Code, or never launched so it has
    no user directory yet) or VSCODE_FAILED (VS Code is here and configuring it
    did not work).
    """
    dry = "--dry-run" in argv
    user_dir = _vscode_user_dir()
    version = (_code("--version") or "").splitlines()
    if version:
        print(f"==> VS Code {version[0]}, user dir: {user_dir}")
    if user_dir is None:
        if shutil.which("code"):
            warn("VS Code is installed but has never been launched, so it has no "
                 "user directory yet.")
            warn("  Open VS Code once, then: ailocal clients vscode")
        else:
            warn(f"VS Code not installed — {CLIENTS['vscode'][1]}")
        return VSCODE_ABSENT

    installed = _extensions()
    if not dry:
        # The connector, and nothing else. Language extensions are the user's
        # choice about their editor, not something a local-model runtime needs
        # in order to answer a chat turn.
        _install_extension(CONNECTOR_EXT, installed)

    # A user directory that cannot be written is a real failure and says so.
    # Reporting it as absence would send the caller down the "you have no VS
    # Code" path for an editor that is sitting right there, half-configured.
    try:
        _provider_group(user_dir / "chatLanguageModels.json", dry)
        _prune_deprecated(user_dir / "settings.json", dry)
        if not dry:
            _ensure_recommended(user_dir / "settings.json")
    except OSError as exc:
        warn(f"VS Code configuration failed under {user_dir}: {exc}")
        return VSCODE_FAILED

    print("\n  NOT verifiable from a script, and not claimed: whether a VS Code")
    print("  CHAT TURN reaches the model. That needs the GUI — run")
    return VSCODE_OK


def _copilot_instructions() -> None:
    data = policy.data_root()
    dest = Path.home() / ".copilot" / "instructions"
    dest.mkdir(parents=True, exist_ok=True)
    _concat_shared(data / "clients/copilot/ailocal.instructions.md",
                   data / "clients/claude/references/build-checklist.md",
                   dest / "ailocal.instructions.md", claude_only=False)
    # ONE instruction file. session-primer.md was deployed beside this one with
    # the same `applyTo: "**"`, so both loaded on every turn and stated the
    # terminal protocol twice — in two wordings, which is worse than once.
    (dest / "session-primer.md").unlink(missing_ok=True)
    info("Copilot instruction files deployed to ~/.copilot/instructions/")



# WHY THIS TEMPLATE IS MERGED AND NEVER OVERWRITTEN. The live
# chatLanguageModels.json carries an `apiKey` field that is not a key but a
# reference into VS Code's SecretStorage — `"${input:chat.lm.secret.-3031591c}"`.
# The VALUE is Keychain-backed and no script can write it, so overwriting the
# file would silently discard a key the user entered by hand and cannot restore
# except through the UI. The shipped template therefore omits `apiKey` entirely
# and the installer preserves whatever reference is already there.
def target_vscode() -> None:
    step("Configuring VS Code Copilot Chat")
    # install_vscode's failure is the answer, not a warning to walk past: with no
    # VS Code there is nothing to read ~/.copilot/instructions, and printing the
    # "enter your key in SecretStorage" ritual for absent software is an
    # instruction the user cannot carry out. A CONFIGURATION FAILURE is a
    # different thing and is not filed under the same silence: it is said out
    # loud, because VS Code is here and half-configured.
    outcome = install_vscode([])
    if outcome == VSCODE_ABSENT:
        return
    if outcome != VSCODE_OK:
        warn("VS Code configuration failed — Copilot instruction files and the "
             "key step are skipped. Fix the above, then: ailocal clients vscode")
        return
    _copilot_instructions()
    # One ritual, worded once. VS Code keeps model API keys in SecretStorage and
    # exposes no supported way for another program to write there — not through
    # the connector (no key-import command, no URI handler, no env var) and not
    # through the `code` CLI. So this is the VS Code boundary, not a step ailocal
    # forgot, and it says so rather than reading as a failure.
    print("\n  ONE MANUAL STEP — VS Code keeps model keys in its own encrypted")
    print("  storage, which no supported interface lets ailocal write.")
    print("    grep LITELLM_MASTER_KEY ~/.local/state/ailocal/env")
    print("    Copilot Chat → model picker → \"Manage Models…\" → \"LiteLLM\" → "
          "paste it")
    print(f"  (base URL, if asked: {BASE_URL})")
    print("  Launcher: `ailocal-code [path]` opens the isolated 'ailocal' profile.")


# ── Codex CLI ───────────────────────────────────────────────────────────────

def target_codex() -> None:
    """CODEX_HOME for the codex-local wrapper. ~/.codex is NEVER touched."""
    home = policy.deployed_client_root() / "codex"
    home.mkdir(parents=True, exist_ok=True)
    step(f"Installing Codex config ({home})")
    data = policy.data_root()

    # config.toml, model_catalog.json and the plan/review profiles are written
    # here by generation, not copied here by this function.

    # The template carries a .template extension so the /AGENTS.md gitignore
    # rule cannot swallow the tracked source.
    _concat_shared(data / "clients/codex/AGENTS.md.template",
                   data / "clients/claude/references/build-checklist.md",
                   home / "AGENTS.md", claude_only=False)
    info(f"{home / 'AGENTS.md'} written (protocol + build checklist)")
    retire_managed_dir(home / "prompts")


    if (Path.home() / ".codex" / "config.toml").is_file():
        warn("~/.codex/config.toml still exists — plain 'codex' keeps using it "
             "(cloud, unaffected).")
    if not shutil.which("codex"):
        warn(f"Codex CLI is not installed — {CLIENTS['codex'][1]}")
    print("  Launch with: codex-local exec 'say ok'  (source ~/.zshrc first)")
    # Stated at deploy time, not only in the README: routing is correct here and
    # the failure is upstream, so someone whose session hangs should not go
    # looking for a mistake in this configuration.
    print("  KNOWN UPSTREAM LIMIT: interactive sessions do not finish streaming")
    print("    (BerriAI/litellm#27442). `codex-local exec` is unaffected.")


# ── Claude Code ─────────────────────────────────────────────────────────────

def target_claude() -> None:
    """CLAUDE_CONFIG_DIR for the claude-local wrapper. ~/.claude is NEVER touched.

    No instruction-policy file is written into this root: ailocal publishes the
    integration contract and stops there. Composing client instruction policy is
    outside its ownership, and writing one here would fight whatever composes
    this root.
    """
    home = policy.deployed_client_root() / "claude"
    home.mkdir(parents=True, exist_ok=True)
    step(f"Installing Claude Code config ({home})")

    # settings.json is written here by generation; it carries no secret, because
    # the key reaches Claude through the claude-local wrapper's process-scoped
    # env, never through a file.
    # NO agents OR commands. ailocal shipped planner/implementer/reviewer/
    # search/tester and a local-build workflow — generic software-engineering
    # roles that describe no local-inference fact. Measured: native Claude
    # matched or beat that workflow on the same tasks, and a local runtime has
    # no business teaching a client how to plan, implement or review. What
    # ailocal uniquely knows reaches the model through the injected persona and
    # the capability catalog, not through a role roster.
    retire_managed_dir(home / "agents")
    retire_managed_dir(home / "commands")
    install_managed_dir(policy.data_root() / "clients/claude/references",
                        home / "references")
    info(f"{home}/references written (external overlays preserved)")

    # .claude.json holds this root's MCP registrations and real session state:
    # seed onboarding only when absent, never rewrite.
    claude_json = home / ".claude.json"
    if claude_json.exists():
        skip(f"{claude_json} already exists — left untouched")
    else:
        claude_json.write_text('{"hasCompletedOnboarding": true}\n')
        info(f"{claude_json} seeded (skips first-run onboarding)")

    print("  Launch with: claude-local  (source ~/.zshrc first)")
    print("  Plain 'claude' still talks to Anthropic's cloud — untouched.")

    step("LSP baseline (ailocal-owned minimum)")
    lsp_baseline(home, "claude-local")
    lsp_baseline(Path.home() / ".claude", "claude (cloud)")
    info("language servers: ailocal enables the PLUGIN for a server you already "
         "have — it never installs a language ecosystem")


#: language -> (official plugin, server binary, the command that installs it).
#:
#: ailocal enables the PLUGIN; the BINARY belongs to its own ecosystem and is
#: never installed here. A language whose binary is absent is skipped with the
#: command to fix it — enabling a plugin with no server behind it would advertise
#: a capability that cannot answer, which is the thing this function exists to
#: prevent.
#:
#: Identifiers are the marketplace's own, verified against `claude-plugins-official`.
#: There is deliberately NO shell entry: the marketplace publishes 13 LSP plugins
#: and none is bash, so a bash-language-server on PATH is unreachable from Claude
#: Code. Shell is covered by ShellCheck, which is static analysis, not LSP.
LSP_PLUGINS = (
    ("Python",     "pyright-lsp",    "pyright-langserver",
     "npm i -g pyright"),
    ("TypeScript", "typescript-lsp", "typescript-language-server",
     "npm i -g typescript-language-server typescript"),
    ("Go",         "gopls-lsp",      "gopls",
     "brew install gopls"),
    ("C/C++",      "clangd-lsp",     "clangd",
     "xcode-select --install"),
)

MARKETPLACE = "claude-plugins-official"
MARKETPLACE_SOURCE = "anthropics/claude-plugins-official"


def lsp_baseline(root: Path, label: str) -> None:
    """The local-client compatibility baseline ailocal owns.

    The LSP tool is built into Claude Code and needs no enabling; ENABLE_LSP_TOOL
    was the gate before 2.0.74 and settings.json no longer writes it. What the
    tool still needs is a language server behind it, which is what a plugin
    provides — without this an ailocal-only machine advertises a capability that
    cannot answer.

    PLUGIN STATE IS PER CONFIG ROOT, not global. [REAL] a fresh CLAUDE_CONFIG_DIR
    reports "No marketplaces configured" and "No plugins installed" while
    ~/.claude has four; each root carries its own plugins/cache, its own
    installed_plugins.json and its own marketplace clone. So writing
    `enabledPlugins` alone would be a lie on a fresh root: the plugin must first
    be INSTALLED into that root. This runs the official `claude plugin` CLI per
    root rather than reimplementing any of that state.

    Applied to the isolated root AND to ~/.claude — this wires up a binary the
    user already has, not routing configuration, so cloud client CONFIG is still
    never touched.
    """
    if not shutil.which("claude"):
        return skip(f"claude not on PATH — no LSP baseline ({label})")
    if not root.is_dir():
        return skip(f"{root} missing — no LSP baseline ({label})")

    env = {**os.environ, "CLAUDE_CONFIG_DIR": str(root)}

    def claude(*args) -> tuple[int, str]:
        try:
            r = subprocess.run(["claude", *args], capture_output=True, text=True,
                               env=env, timeout=120)
        except (OSError, subprocess.SubprocessError):
            return 1, ""
        return r.returncode, r.stdout

    def enabled(listing: str, plugin: str) -> bool:
        """Is `plugin` present AND enabled in this root's listing?

        Reads the plugin's whole block, up to the next entry — NOT a fixed number
        of lines. `claude plugin list` gained a `Version:` line, which pushed
        `Status: enabled` out of a hard-coded three-line window, so an enabled
        plugin read as disabled and was "repaired" on every run.
        """
        if plugin not in listing:
            return False
        for line in listing.split(plugin, 1)[1].splitlines()[1:]:
            if line.strip().startswith("❯") or (line and not line[0].isspace()):
                break
            if "enabled" in line:
                return True
        return False

    wanted = [(lang, f"{p}@{MARKETPLACE}", binary, cmd)
              for lang, p, binary, cmd in LSP_PLUGINS if shutil.which(binary)]
    for lang, _p, binary, cmd in ((l, p, b, c) for l, p, b, c in LSP_PLUGINS
                                  if not shutil.which(b)):
        warn(f"{binary} not installed — {label} has NO {lang} LSP. Install it, "
             f"then re-run:  {cmd}")
    if not wanted:
        return

    # A FRESH config root knows no marketplaces at all, so `marketplace update`
    # fails with "not found" and the install can never succeed. Registering it
    # first is what makes this work on a machine that has never run Claude Code;
    # `add` on a root that already has it is a no-op, and it takes the OWNER/REPO
    # source form, not the bare marketplace name.
    listing = claude("plugin", "list")[1]
    if any(not enabled(listing, plugin) for _l, plugin, _b, _c in wanted):
        if MARKETPLACE not in claude("plugin", "marketplace", "list")[1]:
            if claude("plugin", "marketplace", "add", MARKETPLACE_SOURCE)[0] != 0:
                return warn(f"could not register {MARKETPLACE_SOURCE} — "
                            f"{label} has no LSP")
        claude("plugin", "marketplace", "update", MARKETPLACE)

    for lang, plugin, _binary, _cmd in wanted:
        if enabled(listing, plugin):
            info(f"{lang} LSP already present and enabled ({label})")
            continue
        claude("plugin", "install", plugin)
        # VERIFY THE STATE, DO NOT TRUST THE EXIT CODE. [REAL] `plugin install`
        # now enables what it installs, and `plugin enable` on an
        # already-enabled plugin exits 1 ("already enabled"). The old
        # `install == 0 and enable == 0` chain therefore reported failure on a
        # correctly provisioned FRESH root — the exact path it was written for.
        # A live session on this root can also race the plugin-state write, so
        # re-read rather than infer, once, before enabling explicitly.
        if not enabled(claude("plugin", "list")[1], plugin):
            claude("plugin", "enable", plugin)
        if enabled(claude("plugin", "list")[1], plugin):
            info(f"{lang} LSP installed and enabled ({label})")
        else:
            warn(f"{plugin} not enabled — {label} has no working {lang} LSP")
            warn(f"  If a live 'claude' session has {root} open, close it.")


# ── entry point ─────────────────────────────────────────────────────────────

_TARGETS = {"vscode": target_vscode, "codex": target_codex, "claude": target_claude}


def main(argv: list[str]) -> int:
    selected = [a for a in argv if not a.startswith("-")]
    for name in selected:
        if name not in _TARGETS and name != "all":
            print(f"  ✗ Unknown target: {name!r}. Valid: "
                  f"{'  '.join(TARGETS)}  all", file=sys.stderr)
            return 1
    # `all` is the EXPLICIT form of "every supported client", and it is a named
    # request like any other: it configures the full list whether or not each
    # client is on this machine. It is deliberately not what an EMPTY argv
    # means — that stays "configure what I have".
    if "all" in selected:
        selected = list(TARGETS)
    # NAMED is a request and is honoured whether or not the client is here:
    # writing a client home before installing the client is a legitimate order
    # to do things in, and refusing it would break `ailocal clients codex` on a
    # machine where Codex arrives next. UNNAMED is a question — "configure what
    # I have" — so it answers with the clients actually present and names the
    # rest instead of provisioning for software that does not exist.
    if selected:
        for name in selected:
            if not present(name):
                warn(f"{CLIENTS[name][0]} is not installed; configuring it anyway "
                     f"as you asked for it by name ({CLIENTS[name][1]})")
    else:
        selected = [n for n in TARGETS if present(n)]
        if not selected:
            print("Targets: none — no supported client is installed.")
            print("  The proxy itself is unaffected: ailocal start / check still work.")
            report_missing(TARGETS)
            return 0
    print(f"Targets: {' '.join(selected)}")

    # Every derived artifact first: deploying from stale generated state is how
    # a client ends up configured for a profile that is no longer active.
    from . import generation
    if generation.main([]):
        return 1

    key = _master_key()
    ensure_shell_sourcing(key)
    for name in selected:
        _TARGETS[name]()

    # Codex MCP is WITHHELD BY POLICY, not missing: an empty [mcp_servers.*]
    # section is the correct outcome, and nothing here may invoke another
    # tool's global MCP sync to "repair" it.
    info("Codex MCP intentionally withheld (Codex cannot dispatch namespaced tools)")
    info("  claude-local MCP registrations in .claude.json are preserved.")

    report_missing(TARGETS)

    if not shutil.which("ailocal"):
        warn("ailocal is not on PATH. Install the command:  pipx install .")
    print("\n  New shells pick up claude-local/codex-local/ailocal-code "
          "automatically. For this shell: source ~/.zshrc")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
