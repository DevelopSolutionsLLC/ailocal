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

from . import policy

BACKUP_KEEP = int(os.environ.get("BACKUP_KEEP", "5"))
TARGETS = ("vscode", "codex", "claude")

CONNECTOR_EXT = "Gethnet.litellm-connector-copilot"
BASE_URL = os.environ.get("AILOCAL_BASE_URL", "http://localhost:4000")

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
            "  ✗ LITELLM_MASTER_KEY not set in .env — run ailocal install first")
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

    env_path = cfg / "env"
    env_path.write_text(f"AILOCAL_BASE_URL={BASE_URL}\nAILOCAL_API_KEY={key}\n")
    env_path.chmod(0o600)
    info(f"{env_path} written (chmod 600)")

    state, data = policy.state_root(), policy.data_root()
    shutil.copyfile(state / "clients" / "configure.zsh", cfg / "configure.zsh")
    shutil.copyfile(data / "clients" / "finalize.zsh", cfg / "finalize.zsh")
    # Client-invoked hooks: the client execs these, so the deployed copy must be
    # executable regardless of the mode the source carries.
    for hook in ("scratchpad-hook.sh", "compact-hook.sh"):
        shutil.copyfile(data / "clients" / hook, cfg / hook)
        (cfg / hook).chmod(0o755)
    info("configure.zsh / finalize.zsh / scratchpad-hook.sh / compact-hook.sh "
         f"deployed to {cfg}")

    # The published description of this runtime, at a stable path, so an
    # external consumer never has to know where ailocal lives and never parses
    # generated Markdown for a fact.
    contract = state / "integration-contract.json"
    if contract.is_file():
        shutil.copyfile(contract, cfg / "integration-contract.json")
        info(f"{cfg / 'integration-contract.json'} published (runtime schema)")
    else:
        warn("integration-contract.json missing — run ailocal sync")

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
)

#: Added to settings.json ONLY if absent, so the user's own choices and their
#: comments survive. inactivityTimeout matters most: a 35B model cold-loads
#: ~30 GB emitting no tokens, which trips the 60s default watchdog.
RECOMMENDED_SETTINGS = {
    "litellm-connector.inactivityTimeout": 300,
    "litellm-connector.enableResponsesApi": False,
    "litellm-connector.disableCaching": True,
    # BYOK utility-model fix (VS Code 1.128+ regression): keep title/summary
    # "utility" calls on the selected local model.
    "chat.byokUtilityModelDefault": "mainAgent",
    "github.copilot.chat.codeGeneration.useInstructionFiles": True,
    "chat.instructionsFilesLocations": {"~/.copilot/instructions": True},
    "chat.editing.autoAcceptDelay": 0,
    "github.copilot.chat.agent.runTasks": True,
    "github.copilot.chat.agent.autoFix": True,
    # The ONLY valid global auto-approve key. The other spellings are not real
    # settings — VS Code silently ignores them.
    "chat.tools.global.autoApprove": True,
    # Auto-approve everything EXCEPT broad process kills and rm -rf: pkill of
    # node in the integrated terminal takes down VS Code's own extension host
    # and the connector with it.
    "chat.tools.terminal.autoApprove": {
        "/^.*/": True,
        "/\\b(pkill|kill|killall)\\b/": False,
        "/\\brm\\s+-rf\\b/": False,
    },
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
        print(f"  {BOLD}{YELLOW}ACTION NEEDED{RESET} no API key reference found. "
              "The key value lives in VS Code's")
        print("     SecretStorage (Keychain) and cannot be written from a script.")
        print("     Enter it ONCE:  Command Palette -> 'Chat: Manage Language "
              "Models' -> LiteLLM")


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


def install_vscode(argv: list[str]) -> int:
    """Configure VS Code for the local stack without hand-editing the UI."""
    dry = "--dry-run" in argv
    user_dir = _vscode_user_dir()
    version = (_code("--version") or "").splitlines()
    if version:
        print(f"==> VS Code {version[0]}, user dir: {user_dir}")
    if user_dir is None:
        warn("VS Code user directory not found — is VS Code installed?")
        return 1

    installed = _extensions()
    if not dry:
        # ms-python/golang.go are VS Code's own way of having language
        # intelligence. Copilot Chat's agent mode consumes their language
        # features directly, so the mcpls bridge is not handed to VS Code and
        # there is no second symbol path (ADR 008). No shell entry: there is
        # no first-party shell language server. Not claimed, not faked.
        for ext in (CONNECTOR_EXT, "ms-python.python", "golang.go"):
            _install_extension(ext, installed)

    _provider_group(user_dir / "chatLanguageModels.json", dry)
    _prune_deprecated(user_dir / "settings.json", dry)
    if not dry:
        _ensure_recommended(user_dir / "settings.json")

    print("\n  NOT verifiable from a script, and not claimed: whether a VS Code")
    print("  CHAT TURN reaches the model. That needs the GUI — run")
    return 0


def _copilot_instructions() -> None:
    data, state = policy.data_root(), policy.state_root()
    dest = Path.home() / ".copilot" / "instructions"
    dest.mkdir(parents=True, exist_ok=True)
    _concat_shared(data / "clients/copilot/ailocal.instructions.md",
                   data / "clients/claude/references/build-checklist.md",
                   dest / "ailocal.instructions.md", claude_only=False)
    shutil.copyfile(data / "clients/copilot/session-primer.md",
                    dest / "session-primer.md")
    info("Copilot instruction files deployed to ~/.copilot/instructions/")



def _continue_config(key: str) -> None:
    """Continue gives VS Code local tab-autocomplete (FIM) that Copilot cannot.

    Chat/edit go through the proxy; autocomplete goes DIRECT to Ollama, because
    FIM through the proxy is unreliable (continuedev/continue#2907).

    Conditional on the extension being present: a keyed config for absent
    software is a needless place for a secret to sit. AILOCAL_CONTINUE=1 opts
    in; Continue is never installed on the user's behalf."""
    cfg = Path.home() / ".continue" / "config.json"
    present = (os.environ.get("AILOCAL_CONTINUE")
               or "continue.continue" in _extensions()
               # Already managed here previously: keep it current rather than
               # stranding a stale key in a file we wrote.
               or cfg.is_file())
    if not present:
        info("Continue extension not installed — skipping ~/.continue/config.json")
        info("  install 'continue.continue' then re-run, or set AILOCAL_CONTINUE=1")
        return
    cfg.parent.mkdir(parents=True, exist_ok=True)
    backup(cfg)
    template = (policy.state_root() / "clients/continue/config.json"
                ).read_text(encoding="utf-8")
    cfg.write_text(template.replace("__LITELLM_KEY__", key), encoding="utf-8")
    cfg.chmod(0o600)
    info("Continue config deployed to ~/.continue/config.json")


def target_vscode(key: str) -> None:
    step("Configuring VS Code Copilot Chat")
    install_vscode([])
    _copilot_instructions()
    _continue_config(key)
    print("\n  Final step — enter the key ONCE (encrypted SecretStorage):")
    print("    Copilot Chat → model picker → \"Manage Models…\" → "
          "\"LiteLLM Connector\"")
    print(f"      Base URL:  {BASE_URL}")
    print("      API Key:   the LITELLM_MASTER_KEY from your .env")
    print("  Then: Cmd+Shift+P → \"LiteLLM: Reload Models\".")
    print("  Launcher: `ailocal-code [path]` opens the isolated 'ailocal' profile.")


# ── Codex CLI ───────────────────────────────────────────────────────────────

def target_codex(key: str) -> None:
    """CODEX_HOME for the codex-local wrapper. ~/.codex is NEVER touched."""
    home = policy.deployed_client_root() / "codex"
    home.mkdir(parents=True, exist_ok=True)
    step(f"Installing Codex config ({home})")
    data, gen = policy.data_root(), policy.state_root() / "clients" / "codex"

    # Managed files: always overwritten, so the latest generated routing,
    # wire_api and sandbox settings actually land.
    config = home / "config.toml"
    config.write_text(
        (gen / "config.toml").read_text(encoding="utf-8")
        .replace("${CODEX_HOME}", str(home)), encoding="utf-8")
    info(f"{config} written")

    catalog = home / "model_catalog.json"
    backup(catalog)
    shutil.copyfile(policy.state_root() / "clients" / "model_catalog.json", catalog)
    info(f"{catalog} written")

    # The template carries a .template extension so the /AGENTS.md gitignore
    # rule cannot swallow the tracked source.
    _concat_shared(data / "clients/codex/AGENTS.md.template",
                   data / "clients/claude/references/build-checklist.md",
                   home / "AGENTS.md", claude_only=False)
    info(f"{home / 'AGENTS.md'} written (protocol + build checklist)")

    prompts = home / "prompts"
    prompts.mkdir(exist_ok=True)
    for src in sorted((data / "clients/codex/prompts").glob("*.md")):
        shutil.copyfile(src, prompts / src.name)
    for name in ("plan.config.toml", "review.config.toml"):
        shutil.copyfile(gen / name, home / name)
    info("prompts/ + plan/review profiles written")

    if (Path.home() / ".codex" / "config.toml").is_file():
        warn("~/.codex/config.toml still exists — plain 'codex' keeps using it "
             "(cloud, unaffected).")
    if not shutil.which("codex"):
        warn("codex binary not found on PATH")
    print("  Launch with: codex-local exec 'say ok'  (source ~/.zshrc first)")


# ── Claude Code ─────────────────────────────────────────────────────────────

def target_claude(key: str) -> None:
    """CLAUDE_CONFIG_DIR for the claude-local wrapper. ~/.claude is NEVER touched.

    No instruction-policy file is written into this root: ailocal publishes the
    integration contract and stops there. Composing client instruction policy is
    outside its ownership, and writing one here would fight whatever composes
    this root.
    """
    home = policy.deployed_client_root() / "claude"
    home.mkdir(parents=True, exist_ok=True)
    step(f"Installing Claude Code config ({home})")

    # settings.json carries no secret: the key reaches Claude through the
    # claude-local wrapper's process-scoped env, never through a file.
    settings = home / "settings.json"
    backup(settings)
    shutil.copyfile(policy.state_root() / "clients/claude/settings.json", settings)
    info(f"{settings} written")

    for name in ("agents", "commands", "references"):
        install_managed_dir(policy.data_root() / "clients/claude" / name,
                            home / name)
    info(f"{home}/{{agents,commands,references}} written "
         "(external overlays preserved)")

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

    step("Python LSP baseline (ailocal-owned minimum)")
    lsp_baseline(home, "claude-local")
    lsp_baseline(Path.home() / ".claude", "claude (cloud)")
    info("language servers: Python baseline only — anything broader is "
         "provisioned by its own owner")


def lsp_baseline(root: Path, label: str) -> None:
    """The minimum local-client compatibility baseline ailocal owns.

    settings.json sets ENABLE_LSP_TOOL=1, but a plugin is what puts a language
    server behind that tool, so without this an ailocal-only machine advertises
    a capability that cannot answer.

    ONE LANGUAGE, DELIBERATELY: Python. Applied to the isolated root AND to
    ~/.claude — this wires up a binary the user already has, not routing
    configuration, so cloud client CONFIG is still never touched.
    """
    plugin = "pyright-lsp@claude-plugins-official"
    if not shutil.which("claude"):
        return skip(f"claude not on PATH — no LSP baseline ({label})")
    if not root.is_dir():
        return skip(f"{root} missing — no LSP baseline ({label})")
    if not shutil.which("pyright-langserver"):
        warn(f"pyright-langserver not installed — {label} has NO Python LSP.")
        return warn("  Install it, then re-run:  npm i -g pyright")

    env = {**os.environ, "CLAUDE_CONFIG_DIR": str(root)}

    def claude(*args) -> tuple[int, str]:
        try:
            r = subprocess.run(["claude", *args], capture_output=True, text=True,
                               env=env, timeout=120)
        except (OSError, subprocess.SubprocessError):
            return 1, ""
        return r.returncode, r.stdout

    # `plugin install` does NOT enable, so presence alone is not enough: a
    # disabled plugin would report "already present" forever.
    listing = claude("plugin", "list")[1]
    if plugin in listing:
        after = listing.split(plugin, 1)[1].splitlines()[:3]
        if any("enabled" in l for l in after):
            return info(f"Python LSP baseline already present and enabled ({label})")
        warn(f"pyright-lsp plugin present but disabled ({label}) — enabling")
        # A live session on this root races the plugin-state write; one retry
        # covers that without masking a real failure.
        for attempt in (1, 2):
            if claude("plugin", "enable", plugin)[0] == 0:
                return info(f"Python LSP baseline enabled ({label})")
            if attempt == 1:
                __import__("time").sleep(2)
        warn(f"pyright-lsp enable failed — {label} has no working Python LSP")
        return warn(f"  If a live 'claude' session has {root} open, close it.")

    claude("plugin", "marketplace", "update", "claude-plugins-official")
    if claude("plugin", "install", plugin)[0] == 0 and \
            claude("plugin", "enable", plugin)[0] == 0:
        info(f"Python LSP baseline installed and enabled ({label})")
    else:
        warn(f"pyright-lsp install/enable failed — {label} has no Python LSP")


# ── entry point ─────────────────────────────────────────────────────────────

_TARGETS = {"vscode": target_vscode, "codex": target_codex, "claude": target_claude}


def main(argv: list[str]) -> int:
    selected = [a for a in argv if not a.startswith("-")]
    for name in selected:
        if name not in _TARGETS:
            print(f"  ✗ Unknown target: {name!r}. Valid: {'  '.join(TARGETS)}",
                  file=sys.stderr)
            return 1
    selected = selected or list(TARGETS)
    print(f"Targets: {' '.join(selected)}")

    # Every derived artifact first: deploying from stale generated state is how
    # a client ends up configured for a profile that is no longer active.
    from . import generation
    if generation.main([]):
        return 1

    key = _master_key()
    ensure_shell_sourcing(key)
    for name in selected:
        _TARGETS[name](key)

    # Codex MCP is WITHHELD BY POLICY, not missing: an empty [mcp_servers.*]
    # section is the correct outcome, and nothing here may invoke another
    # tool's global MCP sync to "repair" it.
    info("Codex MCP intentionally withheld (Codex cannot dispatch namespaced tools)")
    info("  claude-local MCP registrations in .claude.json are preserved.")

    if not shutil.which("ailocal"):
        warn("ailocal is not on PATH. Install the command:  pipx install .")
    print("\n  New shells pick up claude-local/codex-local/ailocal-code "
          "automatically. For this shell: source ~/.zshrc")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
