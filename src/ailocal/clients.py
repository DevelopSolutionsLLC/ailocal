"""Deploy the isolated client homes.

ailocal owns the files it ships, not the directories: foreign files and
symlinks are left alone, marked external blocks survive overwrites, and only
manifest names are pruned. Hosted Claude/Codex roots are never written.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path

from . import policy, runtime

BACKUP_KEEP = int(os.environ.get("BACKUP_KEEP", "5"))
TARGETS = ("vscode", "codex", "claude")

#: Optional consumers, never installed by ailocal; install commands verified
#: against vendor docs and `brew info` (August 2026).
CLIENTS: dict[str, tuple[str, str]] = {
    "claude": ("Claude Code", "brew install --cask claude-code"),
    "codex":  ("Codex CLI",   "brew install --cask codex"),
    "vscode": ("VS Code",     "brew install --cask visual-studio-code"),
}


def present(name: str) -> bool:
    """Return whether this client is on the machine.

    VS Code: `code` on PATH or an intact app bundle. A user directory alone is
    not presence: it survives deleting the app.
    """
    if name == "vscode":
        return shutil.which("code") is not None or _vscode_app() is not None
    return shutil.which({"claude": "claude", "codex": "codex"}[name]) is not None


def report_missing(names: Iterable[str]) -> None:
    """Name every absent client once, with the one command that installs it."""
    absent = [n for n in names if not present(n)]
    if not absent:
        return
    print("\n  Not installed (optional — ailocal runs without them):")
    for n in absent:
        label, how = CLIENTS[n]
        print(f"    {label:14} {how}")
    print("  Install any of them, then re-run: ailocal clients")


#: Not `local-agent`: Cadence owns that hosted name and drops it from clients it
#: is not scoped to, so sharing it would make each product remove the other's.
AGENT_MCP = "agent"
#: Former registration name. Removal condition: 0.18.
LEGACY_AGENT_MCP = "local-agent"


def provision_local_agent(names: Iterable[str] = ("claude", "codex")) -> bool:
    """Register the agent MCP in claude-local and codex-local, and nowhere else.

    Hosted roots belong to Cadence's `local-agent` declaration. Registration
    uses the native CLIs; a same-name server of another shape is preserved and
    reported, and a local `local-agent` entry of ailocal's shape is removed.
    """
    src = policy.data_root() / "integrations/local-agent"
    if not (src / "server.py").is_file():
        warn("bundled local-agent component missing from the package")
        return False
    if provision_mcp_runtime(src, "local-agent") is None:
        return False
    from .mcp import agent_registration
    entry = agent_registration()

    def matches(got: object) -> bool:
        return (isinstance(got, dict)
                and Path(got.get("command", "")).name == "ailocal"
                and list(got.get("args", [])) == entry["args"])

    success = True
    for client in names:
        if client not in ("claude", "codex") or not present(client):
            continue
        label = client + "-local"
        path = policy.client_mcp_path(client, local=True)
        env = dict(os.environ)
        env["CODEX_HOME" if client == "codex" else "CLAUDE_CONFIG_DIR"] = str(path.parent)

        def read_entry(name: str) -> object:
            if not path.exists():
                return None
            if client == "codex":
                data = tomllib.loads(path.read_text(encoding="utf-8"))
                return data.get("mcp_servers", {}).get(name)
            data = json.loads(path.read_text(encoding="utf-8"))
            return data.get("mcpServers", {}).get(name)

        try:
            if matches(read_entry(LEGACY_AGENT_MCP)):
                command = (["codex", "mcp", "remove", LEGACY_AGENT_MCP] if client == "codex"
                           else ["claude", "mcp", "remove", "--scope", "user", LEGACY_AGENT_MCP])
                result = subprocess.run(command, env=env, capture_output=True,
                                        text=True, timeout=30)
                if result.returncode or read_entry(LEGACY_AGENT_MCP) is not None:
                    raise ValueError("native removal did not persist")
                info(f"{label}: retired the former {LEGACY_AGENT_MCP} registration")
            existing = read_entry(AGENT_MCP)
            if existing is not None and not matches(existing):
                warn(f"{label}: {AGENT_MCP} MCP collision; existing registration preserved")
                success = False
                continue
            if existing is None:
                path.parent.mkdir(parents=True, exist_ok=True)
                if client == "codex":
                    command = ["codex", "mcp", "add", AGENT_MCP, "--",
                               entry["command"], *entry["args"]]
                else:
                    command = ["claude", "mcp", "add-json", "--scope", "user", AGENT_MCP,
                               json.dumps(entry)]
                result = subprocess.run(command, env=env, capture_output=True,
                                        text=True, timeout=30)
                if result.returncode or not matches(read_entry(AGENT_MCP)):
                    raise ValueError("native registration did not persist")
        except (OSError, ValueError, TypeError, AttributeError, subprocess.SubprocessError):
            warn(f"{label}: {AGENT_MCP} MCP registration failed; "
                 f"run ailocal clients {client} after correcting the client config")
            success = False
            continue
        info(f"{label}: {AGENT_MCP} MCP registration checked (hosted root not touched)")
    return success


VSCODE_VENDOR = "customendpoint"
VSCODE_PROVIDER_NAME = "ailocal"
COPILOT_GUIDE_START = "<!-- ailocal:model-guidance:start -->"
COPILOT_GUIDE_END = "<!-- ailocal:model-guidance:end -->"

#: 127.0.0.1, not `localhost`: Docker publishes IPv4 only, and macOS resolves
#: `localhost` to ::1 too, so a client trying ::1 first is refused.
BASE_URL = os.environ.get("AILOCAL_BASE_URL") or runtime.proxy_url()

#: Fixed markers another tool writes into files ailocal also writes; carried.
OVERLAY_START = "<!-- cadence:start -->"
OVERLAY_END = "<!-- cadence:end -->"
MANIFEST_NAME = ".ailocal-managed"

#: Blocks in a shared source that only Claude Code should receive.
_CLAUDE_ONLY = re.compile(r"<!-- claude-only -->.*?<!-- /claude-only -->\n?",
                          re.DOTALL)

from .report import BOLD, GREEN, RESET, YELLOW, info, skip, step, warn  # noqa: F401


def _code(*args: str, timeout: int = 120) -> str | None:
    """Run the VS Code CLI; None means it is not usable at all."""
    if not shutil.which("code"):
        return None
    try:
        r = subprocess.run(["code", *args], capture_output=True, text=True,
                           timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout if r.returncode == 0 else None


def _sh(*args: str | Path, timeout: int = 900) -> int:
    """Run a provisioning command; return its exit status. Never raises."""
    try:
        return subprocess.run([str(a) for a in args], timeout=timeout).returncode
    except (OSError, subprocess.SubprocessError):
        return 1


def backup(path: Path) -> bool:
    """Make a timestamped copy, keeping only the newest BACKUP_KEEP."""
    if not path.is_file():
        return False
    dest = path.with_name(f"{path.name}.bak.{datetime.now():%Y%m%d_%H%M%S}")
    shutil.copyfile(path, dest)
    warn(f"Backed up: {path.name} → {dest.name}")
    olds = sorted(path.parent.glob(f"{path.name}.bak.*"), reverse=True)
    for stale in olds[BACKUP_KEEP:]:
        stale.unlink(missing_ok=True)
    return True


#: Pre-manifest files; naming them is the only ownership proof.
#: Removal condition: 0.18.
RETIRED_UNMANAGED = {"prompts": ("analyze-repo.md", "local-build.md")}


def _remove_owned(target: Path) -> bool:
    """Remove one manifest-listed entry; a symlink is someone else's, kept."""
    if target.is_symlink() or not target.exists():
        return False
    if target.is_dir():
        shutil.rmtree(target, ignore_errors=True)
    else:
        target.unlink(missing_ok=True)
    return True


def retire_managed_dir(dst: Path) -> None:
    """Remove a directory ailocal no longer ships.

    Only manifest names are removed; a user's own file survives and keeps the
    directory alive.
    """
    if not dst.is_dir() or dst.is_symlink():
        return
    manifest = dst / MANIFEST_NAME
    names = (manifest.read_text().split() if manifest.is_file()
             else list(RETIRED_UNMANAGED.get(dst.name, ())))
    removed = manifest.is_file()
    for name in names:
        removed = _remove_owned(dst / name) or removed
    manifest.unlink(missing_ok=True)
    try:
        dst.rmdir()                       # only when empty
        info(f"retired {dst}")
    except OSError:
        if removed:
            info(f"retired ailocal content in {dst} (other files kept)")


def install_managed_dir(src: Path, dst: Path) -> None:
    """Copy a directory of managed files without taking ownership of it."""
    dst.mkdir(parents=True, exist_ok=True)
    manifest = dst / MANIFEST_NAME

    if manifest.is_file():
        for name in manifest.read_text().split():
            if not (src / name).exists():
                _remove_owned(dst / name)

    shipped = sorted(p for p in src.iterdir())
    manifest.write_text("".join(f"{p.name}\n" for p in shipped))
    for entry in shipped:
        target = dst / entry.name
        if entry.is_dir():
            shutil.rmtree(target, ignore_errors=True)
            shutil.copytree(entry, target)
            continue

        # Carry another tool's marked block across the overwrite.
        carried = ""
        if target.is_file() and not target.is_symlink():
            text = target.read_text(encoding="utf-8", errors="replace")
            if OVERLAY_START in text and OVERLAY_END in text:
                start = text.index(OVERLAY_START)
                end = text.index(OVERLAY_END) + len(OVERLAY_END)
                carried = text[start:end]

        # Never write through a symlink: it edits the linked file in its repo.
        if target.is_symlink():
            target.unlink()
        shutil.copyfile(entry, target)
        if carried:
            with open(target, "a", encoding="utf-8") as fh:
                fh.write(f"\n{carried}\n")
            info(f"  preserved external block in {entry.name}")


def _master_key() -> str:
    from . import runtime
    key = runtime.env_value("LITELLM_MASTER_KEY")
    if not key:
        raise SystemExit(
            "  ✗ LITELLM_MASTER_KEY is not set — run ailocal install first")
    return key


_CONFIGURE_LINE = (
    '[[ -r "${XDG_CONFIG_HOME:-$HOME/.config}/ailocal/configure.zsh" ]] && '
    'source "${XDG_CONFIG_HOME:-$HOME/.config}/ailocal/configure.zsh"'
    '  # ailocal-configure')
_FINALIZE_LINE = (
    '[[ -r "${XDG_CONFIG_HOME:-$HOME/.config}/ailocal/finalize.zsh" ]] && '
    'source "${XDG_CONFIG_HOME:-$HOME/.config}/ailocal/finalize.zsh"'
    '    # ailocal-finalize')


def ensure_shell_sourcing() -> None:
    """Deploy the client home and add two idempotent ~/.zshrc lines.

    Those marker lines are ailocal's only rc footprint; `--remove` drops them.
    """
    step(f"Setting up {policy.deployed_client_root()}")
    cfg = policy.deployed_client_root()
    cfg.mkdir(parents=True, exist_ok=True)
    cfg.chmod(0o700)

    # No key projection: env.sh reads the canonical generated file.
    data = policy.data_root()
    shutil.copyfile(data / "clients" / "finalize.zsh", cfg / "finalize.zsh")
    # The client execs these hooks: executable whatever the source mode.
    for hook in ("scratchpad-hook.sh", "compact-hook.sh"):
        shutil.copyfile(data / "clients" / hook, cfg / hook)
        (cfg / hook).chmod(0o755)
    info(f"finalize.zsh / scratchpad-hook.sh / compact-hook.sh deployed to {cfg}")

    rc = Path(os.environ.get("ZDOTDIR") or Path.home()) / ".zshrc"
    if not rc.exists():
        # No rc file yet: skipping would leave the wrappers unavailable.
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


#: Added only if absent. Only keys REQUIRED for a local model to answer; user
#: preferences (auto-approve, agent toggles) are not ailocal's to write.
RECOMMENDED_SETTINGS = {
    # Without the location the deployed instruction files are dead weight.
    "github.copilot.chat.codeGeneration.useInstructionFiles": True,
    "chat.instructionsFilesLocations": {"~/.copilot/instructions": True},
}


def _vscode_app() -> Path | None:
    """Return the VS Code bundle, unless only an emptied `.app` remains."""
    for apps in (Path("/Applications"), Path.home() / "Applications"):
        bundle = apps / "Visual Studio Code.app"
        if (bundle / "Contents/Info.plist").is_file():
            return bundle
    return None


def _vscode_user_dir() -> Path | None:
    """Return the VS Code user dir; AILOCAL_VSCODE_USER overrides for tests."""
    if os.environ.get("AILOCAL_VSCODE_USER"):
        return Path(os.environ["AILOCAL_VSCODE_USER"])
    for candidate in (Path.home() / "Library/Application Support/Code/User",
                      Path.home() / ".config/Code/User"):
        if candidate.is_dir():
            return candidate
    return None


def native_vscode_available() -> bool:
    bundle = _vscode_app()
    try:
        package = json.loads((bundle / "Contents/Resources/app/extensions/copilot/package.json").read_text()) if bundle else {}
        return any(g.get("vendor") == VSCODE_VENDOR for g in package.get("contributes", {}).get("languageModelChatProviders", []))
    except (OSError, ValueError):
        return False


def _write_json(path: Path, data: object, indent: int | str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                     prefix=path.name + '.', delete=False) as stream:
        tmp = Path(stream.name)
        try:
            stream.write(json.dumps(data, indent=indent))
            stream.flush()
            tmp.chmod(mode)
            os.replace(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)


def vscode_owned(group: object) -> bool:
    """Return whether a group is ailocal's or the retired one for this proxy."""
    return isinstance(group, dict) and (
        (group.get("vendor") == VSCODE_VENDOR and group.get("name") == VSCODE_PROVIDER_NAME)
        or (group.get("vendor") == "litellm-connector" and group.get("name") == "LiteLLM"
            and str(group.get("baseUrl", "")).rstrip("/") == BASE_URL.rstrip("/")))


def vscode_provider_document(existing: list, models: dict) -> list:
    """Project native BYOK groups, keeping foreign groups and the key reference.

    Explicit models carry exact geometry; /v1/models does not supply limits.
    """
    if not isinstance(existing, list) or any(not isinstance(g, dict) for g in existing):
        raise ValueError("chatLanguageModels.json must be an array of provider objects")
    owned = [g for g in existing if vscode_owned(g)]
    keys = [g["apiKey"] for g in owned if isinstance(g.get("apiKey"), str)]
    group = {"name": VSCODE_PROVIDER_NAME, "vendor": VSCODE_VENDOR,
             "apiType": "chat-completions", "models": []}
    if keys:
        group["apiKey"] = keys[0]
    for role, info in models.items():
        if role == "completion":
            continue  # FIM is an API capability, not a picker model.
        geometry = policy.geometry(info.get("context_input"), info.get("max_output"))
        model = {"id": "ailocal-" + role,
                 "name": policy.capability_description(info, info.get("guidance", {})),
                 "url": BASE_URL.rstrip("/") + "/v1/chat/completions",
                 "toolCalling": True,
                 "vision": "image" in info.get("guidance", {}).get("input_modalities", ["text"]),
                 "streaming": True,
                 "thinking": bool(info.get("reasoning")),
                 "maxInputTokens": geometry["context_input"],
                 "maxOutputTokens": geometry["max_output"],
                 "modelOptions": {key: info[key] for key in ("temperature", "top_p")
                                  if info.get(key) is not None}}
        if info.get("reasoning"):
            model["supportsReasoningEffort"] = ["none", "low", "medium", "high"]
            model["reasoningEffortFormat"] = "chat-completions"
        group["models"].append(model)
    return [g for g in existing if not vscode_owned(g)] + [group]


def _provider_group(models_json: Path, dry: bool) -> None:
    """Configure native Custom Endpoint without touching secret values."""
    try:
        existing = json.loads(models_json.read_text(encoding="utf-8")) or []
    except FileNotFoundError:
        existing = []
    from . import generation
    models = generation.load_models_yaml(policy.profile_path(policy.active_tier()))
    merged = vscode_provider_document(existing, models)

    if dry:
        print(f"  would write: {json.dumps(merged)}")
        return
    _write_json(models_json, merged, "\t")
    if merged[-1].get("apiKey"):
        info("native Custom Endpoint configured; API key reference preserved")
    else:
        # target_vscode prints the instruction once.
        print(f"  {BOLD}{YELLOW}ACTION NEEDED{RESET} provider written, but no API "
              "key is initialized yet (see below).")


def _ensure_recommended(settings_json: Path) -> None:
    text = settings_json.read_text(encoding="utf-8") if settings_json.exists() else "{}"
    missing = {k: v for k, v in RECOMMENDED_SETTINGS.items()
               if f'"{k}"' not in text}
    if not missing:
        info("recommended Copilot settings already present")
        return
    backup(settings_json)
    # Textual insertion, not a reparse, so user comments and format survive.
    if "{" not in text:
        text = "{}"
    i = text.index("{")
    ins = "".join(f'\n    "{k}": {json.dumps(v)},' for k, v in missing.items())
    settings_json.write_text(text[:i + 1] + ins + text[i + 1:], encoding="utf-8")
    info(f"recommended Copilot settings added: {', '.join(missing)}")


#: ABSENT (expected end state) and FAILED (a defect) are both non-zero.
VSCODE_OK = 0
VSCODE_FAILED = 1
VSCODE_ABSENT = 2


def install_vscode(argv: list[str]) -> int:
    """Configure VS Code for the local stack without hand-editing the UI.

    Returns:
        VSCODE_OK, VSCODE_ABSENT (missing or never launched) or VSCODE_FAILED.
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

    if not native_vscode_available():
        warn("VS Code's native Custom Endpoint provider is unavailable. Update VS Code "
             "to a build that includes it, then run ailocal clients vscode.")
        return VSCODE_FAILED

    # An unwritable user dir is FAILED, never reported as absence.
    try:
        _provider_group(user_dir / "chatLanguageModels.json", dry)
        if not dry:
            _ensure_recommended(user_dir / "settings.json")
            if not provision_vscode_research(user_dir / "mcp.json"):
                return VSCODE_FAILED
            if not provision_vscode_agent(user_dir / "mcp.json"):
                return VSCODE_FAILED
            if not provision_copilot_mcp():
                return VSCODE_FAILED
    except (OSError, ValueError) as exc:
        warn(f"VS Code configuration failed under {user_dir}: {exc}")
        return VSCODE_FAILED

    print("\n  NOT verifiable from a script, and not claimed: whether a VS Code")
    print("  CHAT TURN reaches the model. That needs the GUI — run")
    return VSCODE_OK


#: SHA-256 of past Copilot instruction heads; a match is provably ailocal's.
#: tests/clients.py recomputes the set from git history (#108).
RETIRED_COPILOT_HEADS = frozenset({
    "88df9fba7fbcae78e1f0c194a249902ecd2a324f453319c01b7bea48f2259807",  # c73ea03
})


def copilot_instructions_path() -> Path:
    return (Path(os.environ.get("COPILOT_HOME") or Path.home() / ".copilot")
            / "instructions" / "ailocal.instructions.md")


def copilot_instructions_head() -> str:
    """Return the protocol file plus shared checklist (pre-guidance head)."""
    data = policy.data_root()
    return ((data / "clients/copilot/ailocal.instructions.md").read_text(encoding="utf-8")
            + _CLAUDE_ONLY.sub("", (data / "clients/claude/references/build-checklist.md")
                               .read_text(encoding="utf-8")))


def copilot_instructions_text(models: dict[str, dict] | None = None) -> str:
    return copilot_instructions_head() + "\n\n" + copilot_model_guidance(models)


def copilot_instructions_owned(text: str) -> bool:
    """Return whether ailocal wrote this head, in this or an earlier release.

    Past heads count too, so upgrades refresh and teardown removes them (#108).
    """
    import hashlib
    head = text.split("\n\n" + COPILOT_GUIDE_START, 1)[0]
    if head == copilot_instructions_head():
        return True
    return hashlib.sha256(head.encode("utf-8")).hexdigest() in RETIRED_COPILOT_HEADS


def _copilot_instructions() -> None:
    target = copilot_instructions_path()
    dest = target.parent
    dest.mkdir(parents=True, exist_ok=True)
    target.write_text(copilot_instructions_text(), encoding="utf-8")
    # One instruction file; the primer duplicated it. Removal condition: 0.18.
    (dest / "session-primer.md").unlink(missing_ok=True)
    info("Copilot instruction files deployed to ~/.copilot/instructions/")


def copilot_model_guidance(models: dict[str, dict] | None = None) -> str:
    """Render profile-owned advice as native instructions, usable w/o MCP."""
    if models is None:
        from . import generation
        models = generation.load_models_yaml(policy.profile_path(policy.active_tier()))
    lines = [COPILOT_GUIDE_START, "# Current ailocal model guidance",
             "Generated by ailocal from profiles/<active tier>.toml; do not edit this block.",
             "Apply only the entry matching the selected ailocal alias. For a hosted model, "
             "use this advice only when preparing a local delegation. Guidance is not proof of correctness."]
    grouped = {}
    for role, cfg in models.items():
        if role != "completion":
            grouped.setdefault(cfg["active"], []).append((role, cfg))
    for backend, entries in grouped.items():
        lines += ["", "Backend: " + backend]
        for role, cfg in entries:
            geometry = policy.geometry(cfg.get("context_input"), cfg.get("max_output"))
            preset = ", ".join(f"{key}={cfg[key]}" for key in (
                "temperature", "top_p", "draft_num_predict") if cfg.get(key) is not None)
            lines.append(f"ailocal-{role}: {geometry['context_input']} input, "
                         f"{geometry['max_output']} output; thinking={bool(cfg.get('reasoning'))}; {preset}.")
        guide = entries[0][1].get("guidance", {})
        lines += [guide.get("summary", "No exact-backend guidance recorded.")]
        lines += ["Prompting advice: " + item for item in guide.get("prompting", [])]
        lines += ["Known limitation: " + item for item in guide.get("limitations", [])]
    lines += ["", "For current source links and scoped evaluation evidence, use agent.model_guide "
              "or run ailocal profile guide <selected-alias>. Do not infer a tier from installed RAM.",
              COPILOT_GUIDE_END]
    return "\n".join(lines) + "\n"


# Merged, never overwritten: `apiKey` is a SecretStorage reference no script
# can recreate, so overwriting would discard a key entered by hand.
def target_vscode() -> None:
    step("Configuring VS Code Copilot Chat")
    # Absent → silent (no key ritual for missing software); failure → loud.
    outcome = install_vscode([])
    if outcome == VSCODE_ABSENT:
        return
    if outcome != VSCODE_OK:
        warn("VS Code configuration failed — Copilot instruction files and the "
             "key step are skipped. Fix the above, then: ailocal clients vscode")
        return
    _copilot_instructions()
    # Nothing supported writes VS Code SecretStorage: a boundary, not a bug.
    print("\n  ONE MANUAL STEP — VS Code keeps model keys in its own encrypted")
    print("  storage, which no supported interface lets ailocal write.")
    print("    grep LITELLM_MASTER_KEY ~/.local/state/ailocal/env")
    print("    Copilot Chat → model picker → \"Manage Language Models\" → \"ailocal\" → "
          "paste it")
    print(f"  (native Custom Endpoint; API URL: {BASE_URL}/v1/chat/completions)")
    print("  Launcher: `ailocal-code [path]` opens VS Code in the profile configured above.")


#: SHA-256 of every codex AGENTS.md rendering ailocal wrote (from git history);
#: a match is provably ailocal's. Removal condition: 0.18.
RETIRED_CODEX_INSTRUCTIONS = frozenset({
    "cd9d7186d5404430e2d5b3870861a28d30f32a2864dd252b84ae73ad864f1c8d",  # e890d0e
    "b68953f1b74959c61453cd038c9391cb37d3785e643b7f3f7365a7ebff3f1c54",  # 553ecf2
    "f480053c0f0fcf776ee613dff50f7c551ad944549a9ecc6a74a3c5fd88845d05",  # 5688e98
    "4007254f61233a2921bc94c1a3dd8535cca6a3d2d3a2e50a7e3ae7120616a711",  # 789e4b3
    "586319be0e9fcfa81400b44087fc7afc92a2d9596233acc733d1686a6a14e467",  # 468c296
    "91927507b339859f0b10285d55a15b36f41d2022faee4ca08e95dad347abbec8",  # 0c21438
    "eb401ce7c1e495670842f5f9c872e7109e8206015f207630c8c01bfe17abf867",  # ece767f
    "1fe8a20fc52dea7489d2b8e2c5eae8527ef4d5d57253fd5829eefaac828f86e2",  # b81ddd9
    "b4d7551adb6db2359bf3b20aa5f7f81f40493d73348b168826d1ee6a6a922b28",  # 50b677f
})


def retire_codex_instructions(path: Path) -> bool:
    """Remove the codex root's AGENTS.md only when ailocal provably wrote it.

    Byte identity is the only proof: the file never had an owner header.
    """
    if path.is_symlink() or not path.is_file():
        return False
    from .primitives import sha256_file
    if sha256_file(path) not in RETIRED_CODEX_INSTRUCTIONS:
        skip(f"{path} is not an ailocal rendering — left in place")
        return False
    path.unlink()
    info(f"retired {path}: ailocal no longer writes codex instruction policy")
    return True


def target_codex() -> None:
    """Set up CODEX_HOME for the codex-local wrapper; never touch ~/.codex."""
    home = policy.deployed_client_root() / "codex"
    home.mkdir(parents=True, exist_ok=True)
    step(f"Installing Codex config ({home})")
    data = policy.data_root()

    # config.toml and the catalog come from generation. Instruction policy is
    # Cadence's: both writing AGENTS.md made each install overwrite the other.
    retire_codex_instructions(home / "AGENTS.md")
    retire_managed_dir(home / "prompts")
    research = provision_mcp_runtime(data / 'integrations/local-research', 'local-research')
    install_managed_dir(data / 'integrations/local-research/skill', home / 'skills/local-research')
    if research is not None:
        info("research MCP enabled for codex-local (native dispatch measured on Codex 0.163.0)")

    if (Path.home() / ".codex" / "config.toml").is_file():
        warn("~/.codex/config.toml still exists — plain 'codex' keeps using it "
             "(cloud, unaffected).")
    if not shutil.which("codex"):
        warn(f"Codex CLI is not installed — {CLIENTS['codex'][1]}")
    print("  Launch with: codex-local exec 'say ok'  (source ~/.zshrc first)")


def _artifact_source() -> Path:
    """Return the bundled artifact component directory."""
    return policy.data_root() / "integrations/local-artifacts"


def provision_mcp_runtime(src: Path, name: str) -> Path | None:
    """Provision a bundled MCP component's own venv, separate from core."""
    venv = policy.state_root() / name / '.venv'
    py = venv / 'bin/python'
    req = src / 'requirements.txt'
    if not py.is_file():
        if _sh(sys.executable, '-m', 'venv', '--clear', str(venv)) or not py.is_file():
            warn(f'could not create {name} runtime')
            return None
    stamp = venv / '.requirements.sha'
    from .primitives import sha256_file
    want = sha256_file(req)
    if not stamp.is_file() or stamp.read_text().strip() != want:
        if _sh(str(py), '-m', 'pip', 'install', '-q', '-r', str(req)):
            warn(f'could not install {name} runtime dependencies')
            return None
        stamp.write_text(want + '\n')
    return py


def provision_local_artifacts(home: Path) -> bool:
    """Install the bundled artifact MCP for claude-local and register it.

    The component needs `mcp`, so it gets its own interpreter: ailocal stays
    stdlib-only. Registration merges one key; user servers survive.
    """
    src = _artifact_source()
    if not (src / "server.py").is_file():
        warn("bundled artifact component missing from the package")
        return False

    py = provision_mcp_runtime(src, 'local-artifacts')
    if py is None:
        return False

    cj = home / ".claude.json"
    try:
        data = json.loads(cj.read_text()) if cj.is_file() else {}
    except Exception:
        warn(f"{cj} is not valid JSON — artifact MCP not registered")
        return False
    servers = data.setdefault("mcpServers", {})
    entry = {"type": "stdio", "command": str(py), "args": [str(src / "server.py")],
             "env": {}, "alwaysLoad": True}
    if servers.get("artifact") != entry:
        servers["artifact"] = entry
        cj.write_text(json.dumps(data, indent=2) + "\n")
        info("artifact MCP registered in .claude.json (other servers preserved)")
    else:
        skip("artifact MCP already registered")

    skill_src = src / "skill" / "SKILL.md"
    if skill_src.is_file():
        dst = home / "skills" / "local-artifact"
        dst.mkdir(parents=True, exist_ok=True)
        (dst / "SKILL.md").write_text(skill_src.read_text())
        info(f"{dst}/SKILL.md written")

    if not shutil.which("node"):
        warn("node is not on PATH — every artifact format works except "
             "'architecture', which uses the bundled elkjs for layout")
    return True


def provision_local_research(home: Path) -> bool:
    """Merge only ailocal's research entry; never displace a foreign server."""
    src = policy.data_root() / 'integrations/local-research'
    cj = home / '.claude.json'
    try:
        data = json.loads(cj.read_text()) if cj.is_file() else {}
        servers = data.setdefault('mcpServers', {})
        if not isinstance(servers, dict):
            raise ValueError
        from .mcp import registration
        entry = registration()
        existing = servers.get('research')
        if existing is not None and existing != entry:
            warn('research MCP registration collision; existing server preserved')
            return False
    except (ValueError, AttributeError):
        warn('invalid .claude.json; research MCP registration left untouched')
        return False
    if provision_mcp_runtime(src, 'local-research') is None:
        return False
    if existing != entry:
        # Native write, isolated: default user scope would hit the hosted home.
        try:
            result = subprocess.run(
                ['claude', 'mcp', 'add-json', '--scope', 'user', 'research', json.dumps(entry)],
                env=dict(os.environ, CLAUDE_CONFIG_DIR=str(home)),
                capture_output=True, text=True, timeout=30)
            if result.returncode or json.loads(cj.read_text()).get('mcpServers', {}).get('research') != entry:
                raise ValueError
        except (OSError, ValueError, AttributeError, subprocess.SubprocessError):
            warn('native research MCP registration failed; install Claude Code, then run: ailocal clients claude')
            return False
    install_managed_dir(src / 'skill', home / 'skills/local-research')
    return True


def _provision_vscode_mcp(mcp_json: Path, name: str, component: str) -> bool:
    """Register an MCP via VS Code's native CLI for its Local harness.

    The launcher path is absolute: a Dock launch inherits launchd's minimal
    PATH. A same-name server that is not this launcher is left in place.
    """
    launcher = shutil.which('ailocal')
    if not launcher:
        warn(f'ailocal is not on PATH; VS Code {name} MCP not registered')
        return False
    entry = {'command': launcher, 'args': ['mcp', name]}
    try:
        data = json.loads(mcp_json.read_text(encoding='utf-8')) if mcp_json.is_file() else {}
        existing = data.get('servers', {}).get(name)
    except (ValueError, AttributeError):
        warn(f'invalid {mcp_json}; VS Code {name} MCP registration left untouched')
        return False
    if existing is not None and (not isinstance(existing, dict) or existing.get('args') != entry['args']
                                 or Path(existing.get('command', '')).name != 'ailocal'):
        warn(f'VS Code already has a different `{name}` MCP server; it is preserved')
        return False
    if provision_mcp_runtime(policy.data_root() / 'integrations' / component, component) is None:
        return False
    if existing != entry:
        _code('--add-mcp', json.dumps({'name': name, **entry}))
        try:
            written = json.loads(mcp_json.read_text(encoding='utf-8'))['servers'][name]
        except (OSError, ValueError, KeyError, TypeError):
            written = None
        if written != entry:
            warn(f'VS Code {name} MCP registration failed; run: ailocal clients vscode')
            return False
    info(f'{name} MCP registered for VS Code Local harness')
    return True


def provision_vscode_research(mcp_json: Path) -> bool:
    return _provision_vscode_mcp(mcp_json, 'research', 'local-research')


def provision_vscode_agent(mcp_json: Path) -> bool:
    return _provision_vscode_mcp(mcp_json, 'agent', 'local-agent')


def provision_copilot_mcp() -> bool:
    """Register research and agent in Copilot's portable global scope.

    No secrets or input variables; foreign servers and operator options are
    kept, only our command and args are updated.
    """
    path = policy.client_mcp_path('copilot', local=False)
    launcher = shutil.which('ailocal')
    if not launcher:
        warn('ailocal is not on PATH; Copilot global MCP not registered')
        return False
    try:
        data = json.loads(path.read_text()) if path.is_file() else {}
        if not isinstance(data, dict) or not isinstance(data.get('mcpServers', {}), dict):
            raise ValueError
        servers = data.setdefault('mcpServers', {})
        for name in ('research', 'agent'):
            entry = servers.get(name)
            if entry is not None and (not isinstance(entry, dict)
                    or Path(entry.get('command', '')).name != 'ailocal'
                    or entry.get('args') != ['mcp', name]):
                warn(f'Copilot global `{name}` MCP collision; file preserved')
                return False
        changed = False
        for name in ('research', 'agent'):
            entry = {**servers.get(name, {}), 'command': launcher, 'args': ['mcp', name]}
            if servers.get(name) != entry:
                servers[name] = entry
                changed = True
        if changed:
            if path.is_file():
                backup(path)
            _write_json(path, data, 2)
        info('research and agent MCP registered in Copilot global scope')
        return True
    except (OSError, ValueError, TypeError):
        warn('Copilot global MCP configuration invalid or unwritable; file preserved')
        return False


def target_claude() -> None:
    """Set up CLAUDE_CONFIG_DIR for claude-local; never touch ~/.claude.

    No instruction-policy file: composing it is not ailocal's to own.
    """
    home = policy.deployed_client_root() / "claude"
    home.mkdir(parents=True, exist_ok=True)
    step(f"Installing Claude Code config ({home})")

    # settings.json comes from generation; no secret (key is process env).
    # No agents or commands ship. Removal condition: 0.18, w/ RETIRED_UNMANAGED.
    retire_managed_dir(home / "agents")
    retire_managed_dir(home / "commands")
    install_managed_dir(policy.data_root() / "clients/claude/references",
                        home / "references")
    info(f"{home}/references written (external overlays preserved)")

    # .claude.json holds real session state: seed only when absent.
    claude_json = home / ".claude.json"
    if claude_json.exists():
        skip(f"{claude_json} already exists — left untouched")
    else:
        claude_json.write_text('{"hasCompletedOnboarding": true}\n')
        info(f"{claude_json} seeded (skips first-run onboarding)")

    provision_local_artifacts(home)
    if provision_local_research(home):
        info("research MCP registered for claude-local; other registrations preserved")

    print("  Launch with: claude-local  (source ~/.zshrc first)")
    print("  Plain 'claude' keeps its hosted inference settings.")

    step("LSP baseline (ailocal-owned minimum)")
    lsp_baseline(home, "claude-local")
    # Never ~/.claude: plugin state is per root; the hosted root is the user's.
    info("language servers: ailocal enables the PLUGIN for a server you already "
         "have, in its OWN root only — it never installs a language ecosystem "
         "and never touches ~/.claude")


#: language -> (plugin, server binary, its install command). ailocal enables
#: plugins only; a missing binary is skipped so no LSP is advertised unanswered.
#: No shell entry: `claude-plugins-official` publishes no bash LSP plugin.
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
    """Install and enable LSP plugins in an isolated Claude root.

    Plugin state is per config root, so each plugin is installed into `root`
    via the official `claude plugin` CLI. Never point this at ~/.claude.
    """
    if not shutil.which("claude"):
        return skip(f"claude not on PATH — no LSP baseline ({label})")
    if not root.is_dir():
        return skip(f"{root} missing — no LSP baseline ({label})")

    env = {**os.environ, "CLAUDE_CONFIG_DIR": str(root)}

    def claude(*args: str) -> tuple[int, str]:
        try:
            r = subprocess.run(["claude", *args], capture_output=True, text=True,
                               env=env, timeout=120)
        except (OSError, subprocess.SubprocessError):
            return 1, ""
        return r.returncode, r.stdout

    def enabled(listing: str, plugin: str) -> bool:
        """Return whether `plugin` is present and enabled in `listing`.

        Reads the plugin's whole block, not a fixed line count.
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

    # A fresh root knows no marketplace: `add` (OWNER/REPO form) before update.
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
        # Verify state, not exit codes: `enable` on an enabled plugin exits 1,
        # and a live session can race the write.
        if not enabled(claude("plugin", "list")[1], plugin):
            claude("plugin", "enable", plugin)
        if enabled(claude("plugin", "list")[1], plugin):
            info(f"{lang} LSP installed and enabled ({label})")
        else:
            warn(f"{plugin} not enabled — {label} has no working {lang} LSP")
            warn(f"  If a live 'claude' session has {root} open, close it.")


_TARGETS = {"vscode": target_vscode, "codex": target_codex, "claude": target_claude}


def remove_client_writes(dry: bool = False) -> list[str]:
    """Reverse each write outside ailocal's roots that is provably ailocal's.

    Unproven items are reported and left; `dry` reports without writing.

    Returns:
        One line per action.
    """
    verb = "would remove" if dry else "removed"
    lines: list[str] = []

    rc = Path(os.environ.get("ZDOTDIR") or Path.home()) / ".zshrc"
    if rc.is_file():
        text = rc.read_text(encoding="utf-8")
        kept = [ln for ln in text.splitlines(keepends=True)
                if "# ailocal-configure" not in ln and "# ailocal-finalize" not in ln]
        if "".join(kept) != text:
            if not dry:
                backup(rc)
                rc.write_text("".join(kept).lstrip("\n"), encoding="utf-8")
            lines.append(f"{verb} the ailocal lines from {rc}")

    instructions = copilot_instructions_path()
    if instructions.is_file():
        head = (policy.data_root() / "clients" / "copilot" / "ailocal.instructions.md").read_text(encoding="utf-8")
        text = instructions.read_text(encoding="utf-8")
        if copilot_instructions_owned(text) or text.startswith(head):
            if not dry:
                instructions.unlink()
            lines.append(f"{verb} {instructions}")
        else:
            lines.append(f"KEPT {instructions}: not provably ailocal's")

    def _ours(entry: object) -> bool:
        return (isinstance(entry, dict) and Path(str(entry.get("command", ""))).name == "ailocal"
                and isinstance(entry.get("args"), list) and entry["args"][:1] == ["mcp"])

    for path, key in ((policy.client_mcp_path("copilot", local=False), "mcpServers"),
                      *(((d / "mcp.json", "servers"),) if (d := _vscode_user_dir()) else ())):
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            servers = data.get(key) if isinstance(data, dict) else None
        except ValueError:
            lines.append(f"KEPT {path}: not parseable")
            continue
        if not isinstance(servers, dict):
            continue
        ours = [n for n in ("research", "agent") if _ours(servers.get(n))]
        foreign = [n for n in ("research", "agent") if n in servers and n not in ours]
        if ours:
            if not dry:
                backup(path)
                for n in ours:
                    servers.pop(n)
                _write_json(path, data, 2)
            lines.append(f"{verb} {', '.join(ours)} from {path}")
        for n in foreign:
            lines.append(f"KEPT `{n}` in {path}: not the ailocal launcher")

    user_dir = _vscode_user_dir()
    if user_dir is not None:
        models = user_dir / "chatLanguageModels.json"
        if models.is_file():
            try:
                existing = json.loads(models.read_text(encoding="utf-8")) or []
                owned = [g for g in existing if isinstance(g, dict) and vscode_owned(g)]
            except ValueError:
                owned, existing = [], None
                lines.append(f"KEPT {models}: not parseable")
            if owned:
                if not dry:
                    backup(models)
                    _write_json(models, [g for g in existing if g not in owned], "\t")
                lines.append(f"{verb} the {VSCODE_PROVIDER_NAME} provider group from {models}")
        settings = user_dir / "settings.json"
        if settings.is_file():
            raw = settings.read_text(encoding="utf-8")
            stripped = re.sub(r",(\s*[}\]])", r"\1", re.sub(r"//[^\n]*", "", raw))
            try:
                doc = json.loads(stripped)
            except ValueError:
                doc = None
                lines.append(f"KEPT {settings}: not parseable")
            if isinstance(doc, dict):
                ours_keys = [k for k, v in RECOMMENDED_SETTINGS.items() if doc.get(k) == v]
                if ours_keys:
                    if not dry:
                        backup(settings)
                        for k in ours_keys:
                            doc.pop(k)
                        _write_json(settings, doc, 2)
                    lines.append(f"{verb} {', '.join(ours_keys)} from {settings} (still at the shipped value)")
    return lines or ["nothing of ailocal's outside its roots"]


def main(argv: list[str]) -> int:
    if "--remove" in argv:
        for line in remove_client_writes(dry="--dry-run" in argv):
            print(f"  {line}")
        return 0
    selected = [a for a in argv if not a.startswith("-")]
    for name in selected:
        if name not in _TARGETS and name != "all":
            print(f"  ✗ Unknown target: {name!r}. Valid: "
                  f"{'  '.join(TARGETS)}  all", file=sys.stderr)
            return 1
    # `all` names every client, present or not; empty argv = "what I have".
    if "all" in selected:
        selected = list(TARGETS)
    # Named → honoured even if absent (configure before install is valid).
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

    if "--dry-run" in argv:
        print("Dry run: would regenerate client configuration, update shell launchers,")
        print(f"and configure {', '.join(selected)} under {policy.deployed_client_root()}.")
        if any(name in ("claude", "codex") for name in selected):
            print("Would also register the agent MCP (delegate to ailocal-cyber) in the claude-local and codex-local roots only.")
        print("No files, extensions, plugins or MCP registrations were changed.")
        return 0

    # Regenerate first, or a client is configured for an inactive profile.
    from . import generation
    if generation.main([]):
        return 1

    _master_key()                 # no key, no client to configure
    ensure_shell_sourcing()
    # Only bundled integrations are provisioned here, never a global MCP sync.
    for name in selected:
        _TARGETS[name]()

    if not provision_local_agent([n for n in selected if n in ("claude", "codex")]):
        return 1

    report_missing(TARGETS)

    if not shutil.which("ailocal"):
        warn("ailocal is not on PATH. Install the command:  pipx install .")
    print("\n  New shells pick up claude-local/codex-local/ailocal-code "
          "automatically. For this shell: source ~/.zshrc")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
