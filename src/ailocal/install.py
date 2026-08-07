"""install.py — getting a machine into a state where the runtime can run.

Prerequisite verification, asset provisioning, the active tier, .env, the Ollama model
set, the login agents, and the read-only audit of an installation that
`ailocal check` reports.

Provenance rule: shipped assets are read in place from the package and are never
copied anywhere; the config root holds user-editable policy and is replaced only
where the file still matches the digest recorded when it was installed.
"""
from __future__ import annotations

import hashlib
import json
import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

from . import policy, runtime
from .runtime import dim, ok, step, warn

LA_DIR = Path.home() / "Library" / "LaunchAgents"
LOG_DIR = Path.home() / "Library" / "Logs" / "ailocal"
MODEL_STORE = "/Users/Shared/ollama/models"
AGENTS = ("com.ailocal.ollama", "com.ailocal.preload", "com.ailocal.litellm",
          "com.ailocal.ollama-env")

#: Ollama's runtime environment, in one place. KEEP_ALIVE is the GLOBAL DEFAULT
#: and governs only direct callers that send no per-request keep_alive — in
#: practice the embedder, which is infrastructure other tools depend on being
#: resident. Generation models go through LiteLLM, which sends a per-role
#: keep_alive that overrides this. MAX_LOADED caps COUNT, not size: Ollama
#: refuses a model that will not fit, so it never OOMs the machine.
OLLAMA_ENV = {
    "OLLAMA_HOST": "127.0.0.1:11434",
    "OLLAMA_MODELS": MODEL_STORE,
    "OLLAMA_KEEP_ALIVE": "-1",
    "OLLAMA_MAX_LOADED_MODELS": "5",
    "OLLAMA_NUM_PARALLEL": "2",
    "OLLAMA_FLASH_ATTENTION": "1",
    "OLLAMA_KV_CACHE_TYPE": "q8_0",
}


def _run(*args, check: bool = False, capture: bool = False, **kw):
    return subprocess.run([str(a) for a in args], check=check, text=True,
                          capture_output=capture, **kw)


def _out(*args) -> str:
    try:
        r = _run(*args, capture=True)
    except (OSError, subprocess.SubprocessError):
        return ""
    return r.stdout.strip() if r.returncode == 0 else ""


def _has(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def _ask(prompt: str, default: str = "") -> str:
    try:
        return input(f"  {prompt}: ").strip() or default
    except EOFError:
        return default


def _yes(prompt: str) -> bool:
    return _ask(f"{prompt} [y/N]").lower().startswith("y")


# ── provisioning ────────────────────────────────────────────────────────────

CONFIG_COMPONENTS = ("profiles",)
MANIFEST_NAME = "install-manifest.json"


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _tree(root: Path, component: str) -> dict:
    base = root / component
    return {str(p.relative_to(root)): digest(p)
            for p in sorted(base.rglob("*"))
            if p.is_file() and "__pycache__" not in p.parts} if base.is_dir() else {}


def distribution_source() -> Path:
    """The tree being installed FROM: the package's own resources."""
    root = policy.data_root()
    if all((root / c).is_dir() for c in ("deploy", "clients")):
        return root
    raise SystemExit(f"install: the package carries no resources at {root}; "
                     "the installation is incomplete.")


def provision(source: Path, config: Path, state: Path) -> dict:
    """Install the user-editable policy defaults. Nothing else is copied.

    deploy/ and clients/ are read straight out of the package (policy.data_root),
    so there is no shipped-asset tree to stage, swap or roll back, and no way for
    a running container to be mounted on a directory this function is replacing.

    A config file is replaced only where it still matches the digest recorded
    when it was installed; anything else is the operator's and is preserved.
    """
    for p in (config, state):
        p.mkdir(parents=True, exist_ok=True)
    for c in CONFIG_COMPONENTS:
        dest = config / c
        if dest.exists() and (source / c).resolve() == dest.resolve():
            raise SystemExit(
                f"install: {dest} is the shipped source itself; refusing to "
                "install a tree over itself.")

    try:
        manifest = json.loads((state / MANIFEST_NAME).read_text())
        shipped = manifest["config"]
        provenance = True
    except (OSError, ValueError, KeyError, TypeError):
        shipped, provenance = {}, False

    if provenance:
        preserved = [rel for rel, want in sorted(shipped.items())
                     if (config / rel).is_file() and digest(config / rel) != want]
    else:
        # Without provenance nothing can prove a file is untouched, so every
        # existing config file is treated as edited. A fresh install has none.
        preserved = sorted(str(p.relative_to(config))
                           for c in CONFIG_COMPONENTS
                           for p in (config / c).rglob("*") if p.is_file())

    installed, absent = [], []
    for c in CONFIG_COMPONENTS:
        for src in sorted((source / c).rglob("*")):
            if not src.is_file() or "__pycache__" in src.parts:
                continue
            rel = str(src.relative_to(source))
            if rel in preserved:
                continue
            dest = config / rel
            # Deleting a shipped default is a decision, not damage: once the
            # manifest proves it was installed, an upgrade reports it rather
            # than resurrecting it.
            if not dest.exists() and rel in shipped:
                absent.append(rel)
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dest)
            installed.append(rel)

    # The manifest records what was SHIPPED, never what is on disk: adopting an
    # edited file's own digest would make it look untouched, and the next
    # upgrade would overwrite the edit it had just promised to keep.
    # A policy file we shipped and no longer ship is removed, but only while it
    # still matches the digest we recorded: an edited file is the operator's,
    # whatever the distribution now contains.
    retired = []
    for rel, want in sorted(shipped.items()):
        live = config / rel
        if (source / rel).exists() or not live.is_file():
            continue
        if digest(live) == want:
            live.unlink()
            retired.append(rel)
        else:
            preserved.append(rel)

    record = {"config": dict(shipped)}
    for c in CONFIG_COMPONENTS:
        record["config"].update({rel: d for rel, d in _tree(config, c).items()
                                 if rel not in preserved})
    (state / MANIFEST_NAME).write_text(json.dumps(record, indent=1, sort_keys=True))
    for rel in retired:
        record["config"].pop(rel, None)
    return {"installed": installed, "preserved": sorted(set(preserved)),
            "absent": absent, "retired": retired}


# ── host prerequisites ──────────────────────────────────────────────────────

#: command -> how to get it. ailocal does not install other people's software:
#: a package manager it did not choose, running as root, is not a thing this
#: project should own. It states exactly what is missing and stops.
PREREQUISITES = (
    ("docker", "Docker Desktop", "brew install --cask docker-desktop"),
    ("ollama", "Ollama", "brew install --cask ollama-app"),
    ("jq", "jq", "brew install jq"),
)


def require_prerequisites() -> None:
    """Refuse to install onto a machine that cannot run the result."""
    step("Prerequisites")
    missing = [(label, how) for cmd, label, how in PREREQUISITES if not _has(cmd)]
    if missing:
        print("  This machine is missing:")
        for label, how in missing:
            print(f"    - {label:16} {how}")
        raise SystemExit("\n  Install those, then re-run ailocal install.")
    if _run("docker", "ps", capture=True).returncode:
        raise SystemExit("  Docker is installed but the daemon is not responding.\n"
                         "    open -a Docker      # then re-run ailocal install")
    ok(f"{', '.join(label for _, label, _ in PREREQUISITES)} present, Docker running")


def _wait(predicate, attempts: int, delay: float) -> bool:
    import time
    for _ in range(attempts):
        if predicate():
            return True
        time.sleep(delay)
    return predicate()


# ── Ollama environment and login agents ─────────────────────────────────────

def _write_agent(label: str, program: list[str], *, keep_alive: bool = False,
                 env: dict | None = None, calendar: dict | None = None) -> Path:
    LA_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    plist = {"Label": label, "ProgramArguments": program, "RunAtLoad": True,
             "StandardOutPath": str(LOG_DIR / f"{label}.log"),
             "StandardErrorPath": str(LOG_DIR / f"{label}.err.log")}
    if env:
        plist["EnvironmentVariables"] = env
    if keep_alive:
        plist.update(KeepAlive=True, ThrottleInterval=10, ProcessType="Interactive")
    if calendar:
        plist["StartCalendarInterval"] = calendar
        plist["RunAtLoad"] = False
        # launchd gives a job no interactive shell and a minimal PATH.
        plist["EnvironmentVariables"] = {"PATH": "/usr/local/bin:/opt/homebrew/bin:"
                                                 "/usr/bin:/bin:/usr/sbin:/sbin"}
    path = LA_DIR / f"{label}.plist"
    path.write_bytes(plistlib.dumps(plist))
    _bootout(label)
    domain = f"gui/{os.getuid()}"
    if _run("launchctl", "bootstrap", domain, path, capture=True).returncode:
        _run("launchctl", "load", path, capture=True)
    return path


def _bootout(label: str) -> None:
    _run("launchctl", "bootout", f"gui/{os.getuid()}/{label}", capture=True)


def _migrate_model_store() -> None:
    """Move ~/.ollama/models into the shared store before repointing OLLAMA_MODELS.

    Repointing without migrating orphans what is already there: Ollama finds
    nothing and silently re-downloads tens of gigabytes. Same volume, so each
    move is a rename; one file at a time, so an interruption is resumable."""
    src, dst = Path.home() / ".ollama" / "models", Path(MODEL_STORE)
    if not src.is_dir() or not any(src.iterdir()) or src.resolve() == dst.resolve():
        return
    step("Migrating existing models to the shared store")
    if _out("pgrep", "-x", "ollama") or _out("pgrep", "-f", "Ollama.app"):
        _run("osascript", "-e", 'quit app "Ollama"', capture=True)
        _run("pkill", "-x", "ollama", capture=True)
        _wait(lambda: not _out("pgrep", "-x", "ollama"), 6, 0.5)
    moved = kept = 0
    for entry in sorted(p for p in src.rglob("*") if p.is_file()):
        dest = dst / entry.relative_to(src)
        if dest.exists():
            kept += 1
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        entry.rename(dest)
        moved += 1
    ok(f"moved {moved} file(s)" + (f", {kept} already present" if kept else ""))
    for d in sorted((p for p in src.rglob("*") if p.is_dir()), reverse=True):
        if not any(d.iterdir()):
            d.rmdir()
    if not any(src.iterdir()):
        src.rmdir()


def _prepare_model_store() -> None:
    """Group-writable and setgid so a second account can pull, not only read."""
    store = Path(MODEL_STORE)
    store.mkdir(parents=True, exist_ok=True)
    if os.access(store, os.W_OK):
        _run("chmod", "g+rwxs", store, capture=True)
    else:
        warn(f"{store} is not writable by {_out('id', '-un')} — models will not pull here.")


def remove_login_agents() -> None:
    """The uninstall half, reached from `ailocal teardown`."""
    step("Removing ailocal startup LaunchAgents")
    for label in AGENTS:
        _bootout(label)
        (LA_DIR / f"{label}.plist").unlink(missing_ok=True)
        ok(f"removed {label}")


def configure_ollama_services(env_only: bool, role: str = "architecture") -> int:
    """Login agents for Ollama — a step of `ailocal install`.

    Two servers must not fight over :11434, so the Ollama.app GUI, its embedded
    watchdog agent and its login item are all stopped before launchd takes the
    port.
    """
    _prepare_model_store()
    _migrate_model_store()

    if env_only:
        # Ollama.app is a launchd process and never reads a shell rc file, so
        # its environment has to be set where it actually looks.
        step("Setting Ollama env for this login session and every future one")
        for k, v in OLLAMA_ENV.items():
            _run("launchctl", "setenv", k, v)
            ok(f"{k}={v}")
        setenv = "; ".join(f"launchctl setenv {k} {v}" for k, v in OLLAMA_ENV.items())
        _write_agent("com.ailocal.ollama-env", ["/bin/sh", "-c", setenv])
        print("\n  ▶ Quit Ollama (menubar → Quit) and reopen it; the server reads "
              "these only at startup.")
        return 0

    binary = next((c for c in ("/Applications/Ollama.app/Contents/Resources/ollama",
                               "/usr/local/bin/ollama", shutil.which("ollama"))
                   if c and Path(c).is_file()), None)
    if not binary:
        raise SystemExit("  ollama binary not found — install Ollama first.")
    command = shutil.which("ailocal")
    if not command:
        raise SystemExit("  ailocal is not on PATH; a login agent must not name a "
                         "checkout path. Install the command, then re-run.")

    _run("launchctl", "disable", f"gui/{os.getuid()}/com.ollama.ollama", capture=True)
    _run("osascript", "-e", 'quit app "Ollama"', capture=True)
    for pattern in ("/Applications/Ollama.app/Contents/MacOS/Ollama",
                    "Ollama.app/Contents/Resources/ollama serve"):
        _run("pkill", "-9", "-f", pattern, capture=True)
    _wait(lambda: not _out("lsof", "-i", ":11434"), 20, 0.5)

    step("Installing com.ailocal.ollama")
    _write_agent("com.ailocal.ollama", [binary, "serve"], keep_alive=True,
                 env=OLLAMA_ENV)
    ok(f"ollama serve managed by launchd (env baked in, logs in {LOG_DIR})")

    # The agent must never carry a baked model tag, or it warms a model the
    # profile has since replaced. Resolve through the command at run time.
    agents_dir = policy.state_root() / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    preload = agents_dir / "preload.sh"
    preload.write_text(f"""#!/bin/sh
O=http://127.0.0.1:11434
for _ in $(seq 1 60); do curl -fsS -m 3 "$O/api/version" >/dev/null 2>&1 && break; sleep 2; done
curl -fsS -m 3 "$O/api/version" >/dev/null 2>&1 || exit 0
M=$("{command}" profile role {role} --field model 2>/dev/null)
[ -n "$M" ] || exit 0
K=$("{command}" profile role {role} --field keep_alive 2>/dev/null)
[ -n "$K" ] || K=-1
case "$K" in -1) KJ=-1 ;; *) KJ="\\"$K\\"" ;; esac
curl -fsS -m 5 "$O/api/ps" 2>/dev/null | grep -q "\\"$M\\"" && exit 0
curl -fsS -m 300 "$O/api/generate" -d "{{\\"model\\":\\"$M\\",\\"keep_alive\\":$KJ}}" >/dev/null 2>&1
""")
    preload.chmod(0o755)
    _write_agent("com.ailocal.preload", [str(preload)])
    ok(f"'{role}' preloads at login, resolved at run time")
    print(f"  Verify: launchctl list | grep ailocal   •   logs in {LOG_DIR}")
    return 0


# ── the model set ───────────────────────────────────────────────────────────

SERVICES_GB, HEADROOM_GB = 3, 5


def _installed_models() -> dict:
    sizes = {}
    for line in _out("ollama", "list").splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 4:
            try:
                gb = float(parts[2])
            except ValueError:
                continue
            sizes[parts[0]] = gb / 1000 if parts[3].upper().startswith("MB") else gb
    return sizes


def pull_models() -> int:
    """Pull the model set for the active tier — a step of `ailocal install`.

    Enabled capabilities only, deduplicated by tag, sized from what Ollama
    reports and reduced by what is already on disk."""
    if not _has("ollama"):
        raise SystemExit("  Ollama CLI not found. Install from https://ollama.ai/download")
    if not _out("ollama", "list"):
        raise SystemExit("  Ollama daemon is not responding.\n"
                         f"    launchctl kickstart -k gui/{os.getuid()}/com.ailocal.ollama")

    roles = policy.effective_summary()["roles"]
    wanted: dict[str, list[str]] = {}
    for cap, cfg in sorted(roles.items()):
        if cfg.get("enabled", True):
            wanted.setdefault(cfg["model"], []).append(cap)

    have = _installed_models()
    def size_of(tag):
        for name, gb in have.items():
            if name == tag or name.startswith(tag):
                return gb, True
        return 0.0, False

    step("Model set for this profile")
    present_gb = need_gb = 0.0
    for tag, users in sorted(wanted.items()):
        gb, present = size_of(tag)
        label = f"{gb:.1f} GB" if gb else "size unknown until pulled"
        print(f"    {tag}  ({', '.join(users)})  {label}, "
              f"{'present' if present else 'to download'}")
        present_gb += gb if present else 0
        need_gb += 0 if present else gb
    required = round(need_gb + SERVICES_GB + HEADROOM_GB)
    free = shutil.disk_usage(Path.home()).free // 1024 ** 3
    ok(f"{len(wanted)} unique models; {present_gb:.1f} GB present, "
       f"{need_gb:.1f} GB to download")
    ok(f"free space required now: {required} GB (incl. {SERVICES_GB} GB services "
       f"+ {HEADROOM_GB} GB headroom)   available: {free} GB")
    if free < required:
        warn(f"Only {free} GB free; this profile needs ~{required} GB")

    step("Installing/updating Ollama models")
    for tag in sorted(wanted):
        if size_of(tag)[1]:
            ok(f"{tag}  (already installed)")
        elif _run("ollama", "pull", tag).returncode == 0:
            ok(f"{tag}  pulled")
        else:
            warn(f"{tag}  failed to pull — skipping")
    return 0


# ── tier and .env ───────────────────────────────────────────────────────────

def _memory_gb() -> int:
    return int(_out("sysctl", "-n", "hw.memsize") or 0) // 1024 ** 3


def tier_for_memory(gb: int) -> str | None:
    """Never round up: a tier is chosen only when the machine has that memory,
    or it is handed models sized for memory it does not have."""
    return next((t for t in ("128gb", "64gb", "32gb", "16gb")
                 if gb >= int(t[:-2])), None)


def select_tier(override: str, assume_yes: bool) -> str:
    """The one writer of the active-profile marker."""
    gb = _memory_gb()
    tier = tier_for_memory(gb)
    if tier is None:
        raise SystemExit(f"  {gb} GB of unified memory — ailocal requires at least 16 GB.")
    if override:
        if override not in policy.TIERS:
            raise SystemExit(f"  unknown profile {override!r} "
                             f"(expected {', '.join(policy.TIERS)})")
        if int(override[:-2]) > gb:
            warn(f"--profile {override} exceeds detected memory ({gb} GB); models "
                 "sized for it will swap or fail to load.")
            if assume_yes:
                raise SystemExit("  Refusing an unsafe override under --yes.")
            if not _yes(f"Use {override} anyway?"):
                raise SystemExit("  aborted")
        tier = override
    ok(f"Detected {gb} GB RAM → profile: {tier}")
    marker = policy.active_profile_path()
    marker.parent.mkdir(parents=True, exist_ok=True)
    current = marker.read_text().strip() if marker.is_file() else ""
    if current != tier:
        if current:
            warn(f"active profile was {current!r} — switching to {tier}")
        marker.write_text(f"{tier}\n")
    return tier


def _print_plan(tier: str, ram_note: str) -> None:
    summary = policy.effective_summary()
    roles = summary["roles"]
    models = [c["model"] for c in roles.values()]
    primary = max(set(models), key=models.count)
    shared = sorted(r for r, c in roles.items() if c["model"] == primary)
    first = roles[shared[0]]
    print(f"  architecture:        {os.uname().machine}")
    print(f"  {ram_note}")
    print(f"  selected profile:    {tier}  ({policy.profile_summary(tier)['status']})")
    print(f"  primary model:       {primary}")
    print(f"  shared across:       {', '.join(shared)}")
    for role, cfg in sorted(roles.items()):
        if cfg["model"] != primary:
            print(f"  {role + ' model:':<21}{cfg['model']}")
    print(f"  context_input:       {first['context_input']} "
          "(a maximum, not a per-request reservation)")
    print(f"  max_output:          {first['max_output']}")
    print(f"  total_context:       {first['context_input'] + first['max_output']}")
    print(f"  unique models:       {len(set(models))}")


ENV_TEMPLATE = """# ailocal — generated by `ailocal install`. Do NOT commit.
AILOCAL_ENV=local
OLLAMA_URL=http://host.docker.internal:11434

# Use this as ANTHROPIC_API_KEY / OPENAI_API_KEY when pointing a client at the
# proxy instead of the real cloud APIs.
LITELLM_MASTER_KEY={key}

# SearXNG refuses to start without a real secret.
SEARXNG_SECRET={secret}

# Cloud fallback: set ENABLE_CLOUD=true, add a key, and uncomment the matching
# model block in the generated litellm/config.yaml.
ENABLE_CLOUD=false
ANTHROPIC_API_KEY=
OPENAI_API_KEY=
"""


def _write_env(assume_yes: bool) -> None:
    env_file = runtime.env_file()
    if env_file.is_file():
        # Never overwrite unattended: it holds the master key.
        if assume_yes or not _yes("Re-generate .env? Existing values are lost."):
            ok("keeping the existing .env")
            return
    env_file.parent.mkdir(parents=True, exist_ok=True)
    env_file.write_text(ENV_TEMPLATE.format(
        key="sk-" + os.urandom(24).hex(), secret=os.urandom(32).hex()))
    env_file.chmod(0o600)
    ok(".env written with the LiteLLM master key (chmod 600)")


# ── the audit / cleanup pair ────────────────────────────────────────────────
# Two configs per client are CORRECT: the cloud root stays separate from the
# local one, so findings are CLASSIFIED rather than listed — flagging that pair
# would push someone into deleting a working setup.
#
# Out of scope by design: ~/.claude and ~/.codex, VS Code SecretStorage,
# anything git tracks, and any container not named ailocal-*.

def _vscode_findings() -> list:
    """The VS Code side of "can a local model answer here?".

    Three things, and only three: the connector points at the proxy URL this
    installation actually serves, the API key reference survived (its VALUE
    lives in SecretStorage and cannot be read from outside VS Code), and no
    setting is present that routes Copilot's utility/`tools` calls onto the
    selected local model. That last one is not cosmetic: it fires several
    concurrent 50k-token requests at one model that serves them one at a time,
    and the ones that queue die as "fetch failed" / rootCause ECONNRESET.

    This inspects the USER's VS Code, which is host state, not managed
    configuration — which is why it belongs to the audit and not to the
    deterministic config layer. Silent when VS Code is not installed: not
    using VS Code is not a broken installation.
    """
    from . import clients
    from .checks import CheckResult, SKIP, WARN, passed

    user_dir = clients._vscode_user_dir()
    if user_dir is None:
        return [CheckResult("vscode", SKIP, "vscode   not installed")]

    out: list = []
    models_json = user_dir / "chatLanguageModels.json"
    try:
        groups = json.loads(models_json.read_text(encoding="utf-8")) or []
    except (OSError, ValueError):
        groups = []
    group = next((g for g in groups if isinstance(g, dict)
                  and g.get("vendor") == "litellm-connector"), None)
    want = runtime.proxy_url().rstrip("/")
    if group is None:
        out.append(CheckResult("vscode-provider", WARN,
                               "MISSING no LiteLLM provider group",
                               str(models_json), "run ailocal clients vscode"))
    else:
        have = str(group.get("baseUrl", "")).rstrip("/")
        out.append(passed("vscode-provider", f"vscode   connector baseUrl {have}")
                   if have == want else
                   CheckResult("vscode-provider", WARN,
                               f"STALE connector baseUrl {have}, proxy is {want}",
                               str(models_json), "run ailocal clients vscode"))
        out.append(passed("vscode-key", "vscode   API key reference present")
                   if group.get("apiKey") else
                   CheckResult("vscode-key", WARN,
                               "MISSING no API key reference; chat will 401",
                               str(models_json),
                               "Command Palette → 'Chat: Manage Language Models' → LiteLLM"))

    settings = user_dir / "settings.json"
    text = settings.read_text(encoding="utf-8") if settings.is_file() else ""
    bad = [k for k in clients.DEPRECATED_SETTINGS if f'"{k}"' in text]
    out.append(passed("vscode-settings", "vscode   no deprecated or harmful settings")
               if not bad else
               CheckResult("vscode-settings", WARN,
                           f"STALE settings that VS Code ignores or that break "
                           f"local chat: {', '.join(bad)}",
                           str(settings), "run ailocal clients vscode"))
    return out


def audit() -> list:
    """Read-only. Returns CheckResults; never deletes, moves or rewrites
    anything. A finding's `detail` is the path it is about, which is what
    cleanup acts on."""
    from .checks import CheckResult, SKIP, WARN, passed

    results: list = []

    def flag(klass, item, location, action):
        results.append(CheckResult(klass.lower(), WARN, f"{klass} {item}",
                                   str(location), action))

    def fine(summary):
        results.append(passed("install", summary))

    def note(summary):
        results.append(CheckResult("install", SKIP, summary))

    cfg = policy.config_root()
    for name, probe, fix in (("claude", cfg / "claude" / ".claude.json", "claude"),
                             ("codex", cfg / "codex" / "config.toml", "codex")):
        if probe.is_file():
            fine(f"{name:8} {probe.parent}")
        else:
            flag("MISSING", f"{name} local config", probe, f"run ailocal clients {fix}")
    configure = cfg / "configure.zsh"
    if configure.is_file() and "CLAUDE_CONFIG_DIR" in configure.read_text():
        fine("isolation  configure.zsh sets CLAUDE_CONFIG_DIR")
    else:
        flag("DUPLICATE", "claude-local may share the cloud config root", configure,
             "re-run ailocal clients claude")
    if "gethnet.litellm-connector-copilot" in _out("code", "--list-extensions").lower():
        fine("vscode   connector extension installed")
    elif _has("code"):
        flag("MISSING", "VS Code connector extension", "VS Code",
                 "run ailocal clients vscode")
    results += _vscode_findings()

    for label in ("com.ailocal.ollama", "com.ailocal.ollama-env", "com.ailocal.preload"):
        plist = LA_DIR / f"{label}.plist"
        if not plist.is_file():
            note(f"{label} not installed")
        elif label.endswith(("-env", "preload")):
            fine(f"{label} installed (one-shot: not-running is correct)")
        elif "state = running" in _out("launchctl", "print", f"gui/{os.getuid()}/{label}"):
            fine(f"{label} running")
        else:
            flag("STALE", f"{label} is installed but not running", plist,
                 f"launchctl bootstrap gui/{os.getuid()} '{plist}'")

    # A process count does not prove ownership of the port: the GUI app can
    # hold :11434 while the agent is unloaded.
    holder = next((l.split()[1] for l in
                   _out("lsof", "-nP", "-iTCP:11434", "-sTCP:LISTEN").splitlines()[1:]), "")
    agent = next((l.split()[0] for l in _out("launchctl", "list").splitlines()
                  if l.endswith("com.ailocal.ollama")), "")
    if not holder:
        flag("MISSING", "nothing is listening on 11434", "launchd",
             f"launchctl kickstart -k gui/{os.getuid()}/com.ailocal.ollama")
    elif agent in ("", "-"):
        flag("UNMANAGED", f"port 11434 held by pid {holder}, com.ailocal.ollama not loaded",
             "Ollama.app or an ad-hoc serve", "bootstrap the LaunchAgent")
    elif agent != holder:
        flag("UNMANAGED", f"port 11434 held by pid {holder}, not agent pid {agent}",
             "a competing Ollama instance", "quit the Ollama menu-bar app")
    else:
        fine(f"port 11434 owned by the managed LaunchAgent (pid {holder})")

    return results


# ── install ─────────────────────────────────────────────────────────────────

USAGE = """usage: ailocal install [--yes] [--profile <16gb|32gb|64gb|128gb>]

One-time host setup, then a normal start. What only install does: verify
prerequisites, install the policy defaults, take :11434 with a launchd agent,
prepare the shared model store, write the master key, pick the tier, fetch the
pinned images and pull the model set. Everything after that is `ailocal start`.
Idempotent.

  --yes              unattended; also enables production autostart
  --profile <tier>   override the tier detected from installed memory
"""


def _opt(argv: list[str], name: str, default=None):
    return argv[argv.index(name) + 1] if name in argv else default


def cmd_install(argv: list[str]) -> int:
    if {"-h", "--help"} & set(argv):
        print(USAGE)
        return 0
    assume_yes = "--yes" in argv

    require_prerequisites()

    step("Installing assets")
    source = distribution_source()
    report = provision(source, policy.config_root(), policy.state_root())
    ok(f"shipped assets read in place from {source}")
    ok(f"config {policy.config_root()}: {len(report['installed'])} file(s)")
    for rel in report["preserved"]:
        dim(f"kept {rel} (edited since install)")
    for rel in report["absent"]:
        dim(f"absent {rel} (shipped default you removed)")
    for rel in report["retired"]:
        dim(f"retired {rel} (no longer shipped)")

    step("Configuring Ollama")
    if assume_yes or _yes("Set up production autostart (launchd runs ollama serve "
                          "at login)?"):
        configure_ollama_services(env_only=False)
    else:
        configure_ollama_services(env_only=True)

    step("Detecting hardware profile")
    ram = _memory_gb()
    tier = select_tier(_opt(argv, "--profile", ""), assume_yes)

    step("Configuring environment (.env)")
    _write_env(assume_yes)

    # Generation must succeed before anything is pulled; the plan below renders
    # from the generated artifact, so it cannot report stale numbers.
    step("Generating configuration")
    from . import generation
    if generation.main([]):
        raise SystemExit("  generation failed — nothing was pulled")
    print()
    _print_plan(tier, f"physical memory:     {ram} GB")

    # The ONLY image fetch in the product. Every tag is digest-pinned, so this
    # is a first-fetch, not an upgrade; `ailocal start` deliberately runs
    # offline against whatever is already on disk.
    step("Pulling pinned Docker images")
    runtime.compose("pull")
    runtime.cmd_start([])

    pull_models()
    from .checks import run as checks_run
    checks_run.main(["check"])

    # Deploying into a user's client roots is never implied by --yes.
    step("Client configs (optional)")
    print("  ailocal can point Claude Code, Codex and VS Code at the local proxy.")
    print("  ⚠ This backs up, then rewrites/merges existing client configs.")
    targets = "" if assume_yes else _ask("Install which? [all|claude|codex|vscode, "
                                         "Enter to skip]")
    if targets:
        from . import clients
        clients.main([] if targets == "all" else targets.split())
    else:
        ok("Skipped — run later with: ailocal clients [claude|codex|vscode]")

    step("Done")
    print(f"  LiteLLM proxy is ready at {runtime.proxy_url()}")
    print("  Verify a real request:  ailocal check")
    return 0


COMMANDS = {"install": cmd_install}


def main(argv: list[str]) -> int:
    if not argv or argv[0] not in COMMANDS:
        print(f"usage: python -m ailocal.install <{'|'.join(COMMANDS)}>",
              file=sys.stderr)
        return 2
    return COMMANDS[argv[0]](argv[1:])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
