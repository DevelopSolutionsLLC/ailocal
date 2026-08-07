"""install.py — getting a machine into a state where the runtime can run.

Host prerequisites, asset provisioning, the active tier, .env, the Ollama model
set, the login agents, and the audit/cleanup pair that reports and repairs an
installation.

Provenance rule (ADR 009): the data root holds shipped assets with no supported
edit surface and is replaced wholesale; the config root holds user-editable
policy and is replaced only where the file still matches the digest recorded
when it was installed.
"""
from __future__ import annotations

import hashlib
import json
import os
import plistlib
import shutil
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone
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

DATA_COMPONENTS = ("lib", "deploy", "clients")
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
    """The tree being installed FROM. Never guessed.

    Under an installed console script there is no checkout above this module,
    and a guess there silently provisions an empty tree while reporting success.
    """
    root = Path(__file__).resolve().parents[2]
    if all((root / c).is_dir() for c in DATA_COMPONENTS):
        return root
    raise SystemExit(
        "install: no distribution to install from. Run `ailocal install` from a\n"
        "checkout, or pass --from <checkout>.")


def provision(source: Path, config: Path, data: Path, state: Path) -> dict:
    """Install assets. Raises rather than half-applying."""
    for p in (config, data, state):
        p.mkdir(parents=True, exist_ok=True)
    if config == source or data == source:
        raise SystemExit("install: refusing to install a checkout over itself.")

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

    # Per component, not per file: a half-replaced deploy/ tree is a new compose
    # file against old hooks.
    staging = data / f".staging-{os.getpid()}"
    shutil.rmtree(staging, ignore_errors=True)
    for c in DATA_COMPONENTS:
        shutil.copytree(source / c, staging / c,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    done = []
    try:
        for c in DATA_COMPONENTS:
            live, old = data / c, data / f".rollback-{c}"
            shutil.rmtree(old, ignore_errors=True)
            if live.exists():
                live.rename(old)
            (staging / c).rename(live)
            done.append((live, old))
    except OSError:
        for live, old in reversed(done):
            shutil.rmtree(live, ignore_errors=True)
            old.rename(live)
        raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    for _, old in done:
        shutil.rmtree(old, ignore_errors=True)

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
    record = {"config": dict(shipped), "data": {}}
    for c in CONFIG_COMPONENTS:
        record["config"].update({rel: d for rel, d in _tree(config, c).items()
                                 if rel not in preserved})
    for c in DATA_COMPONENTS:
        record["data"].update(_tree(data, c))
    (state / MANIFEST_NAME).write_text(json.dumps(record, indent=1, sort_keys=True))
    return {"installed": installed, "preserved": preserved, "absent": absent}


# ── host prerequisites ──────────────────────────────────────────────────────

def _preflight(assume_yes: bool) -> bool:
    """Report everything missing, take one consent and one sudo authorisation.

    Verified administrator requirements: the Command Line Tools (`softwareupdate
    -i` runs as root), Homebrew (creates /opt/homebrew) and Docker Desktop
    (privileged helpers) need it; jq and the Ollama cask do not.
    """
    missing, admin = [], False
    for cmd, label, needs_admin in (
            ("git", "git (Xcode Command Line Tools)", True),
            ("brew", "Homebrew", True),
            ("jq", "jq", False),
            ("docker", "Docker Desktop", True),
            ("ollama", "Ollama", False)):
        if not _has(cmd):
            missing.append(f"{label}{' [admin]' if needs_admin else ''}")
            admin = admin or needs_admin

    step("Preflight")
    if not missing:
        ok("all prerequisites present (git, brew, jq, docker, ollama)")
        return False
    print("  This machine is missing:")
    for m in missing:
        print(f"    - {m}")
    # A standard account cannot install these at all: it cannot sudo, and
    # /Applications is only group-writable by admin.
    if admin and "admin" not in _out("id", "-Gn").split():
        raise SystemExit(f"  {_out('id', '-un')} is not an administrator, and some of "
                         "these need administrator rights. Install them from an "
                         "admin account, then re-run.")
    if admin:
        print("\n  The [admin] items install system-wide; sudo is asked for once, "
              "now, rather than surprising you mid-run.\n")
    if not assume_yes and not _yes("Install them?"):
        raise SystemExit("  Declined. Install the tools above, then re-run.")
    if admin and _run("sudo", "-v").returncode:
        raise SystemExit("  Administrator authorisation declined.")
    return admin


def _install_prerequisites(assume_yes: bool) -> None:
    admin = _preflight(assume_yes)

    # Homebrew's installer provisions the Command Line Tools silently, so with
    # both missing, brew first gets git too with no GUI dialog.
    if not _has("brew"):
        step("Installing Homebrew")
        script = urllib.request.urlopen(
            "https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh",
            timeout=60).read()
        if _run("/bin/bash", "-c", script.decode(),
                env={**os.environ, "NONINTERACTIVE": "1"}).returncode:
            raise SystemExit("  Homebrew install failed. See https://brew.sh")
        for prefix in ("/opt/homebrew", "/usr/local"):
            if (Path(prefix) / "bin" / "brew").is_file():
                os.environ["PATH"] = f"{prefix}/bin:{os.environ['PATH']}"
                break
    if not _has("git"):
        step("Installing the Command Line Tools")
        _run("xcode-select", "--install")
        raise SystemExit("  Accept the macOS dialog, then re-run.")

    formulas = [f for f in ("jq", "cosign") if not _has(f)]
    casks = ([] if _has("docker") else ["docker-desktop"]) + \
            ([] if _has("ollama") else ["ollama-app"])
    if formulas or casks:
        step("Installing Homebrew packages")
        if formulas and _run("brew", "install", *formulas).returncode:
            raise SystemExit(f"  Could not install: {' '.join(formulas)}")
        # Never fall back from the cask to the formula: that yields a CLI with
        # no app, a different install shape reached by accident.
        if casks and _run("brew", "install", "--cask", *casks).returncode:
            raise SystemExit(f"  Could not install: {' '.join(casks)}")
    else:
        ok("jq, cosign, Docker and Ollama already present")

    # /usr/local/bin is on the minimal PATH launchd jobs get; /opt/homebrew/bin
    # is not. Never adopt or overwrite a path we did not create.
    if admin and not Path("/usr/local/bin/ollama").exists():
        target = next((c for c in ("/Applications/Ollama.app/Contents/Resources/ollama",
                                   shutil.which("ollama")) if c and Path(c).is_file()), None)
        if target:
            _run("sudo", "mkdir", "-p", "/usr/local/bin")
            _run("sudo", "ln", "-sfn", target, "/usr/local/bin/ollama")

    step("Checking Docker")
    _accept_docker_license()
    if _run("docker", "ps", capture=True).returncode:
        _run("open", "-a", "Docker")
        if not _wait(lambda: _run("docker", "ps", capture=True).returncode == 0, 60, 5):
            raise SystemExit("  Docker daemon did not start. Open Docker Desktop, "
                             "finish first-run setup, then re-run.")
    ok("Docker present and running")


def _accept_docker_license() -> None:
    """Pre-seed the key Docker writes when you click Accept.

    The file is user-owned, so this needs no sudo, and it is what keeps the
    install non-interactive. Two spellings cover current and legacy versions.
    """
    d = Path.home() / "Library" / "Group Containers" / "group.com.docker"
    d.mkdir(parents=True, exist_ok=True)
    for name, key in (("settings-store.json", "LicenseTermsVersion"),
                      ("settings.json", "licenseTermsVersion")):
        p = d / name
        try:
            cfg = json.loads(p.read_text()) if p.is_file() else {}
        except (OSError, ValueError):
            cfg = {}
        if not cfg.get(key):
            cfg[key] = 2
            p.write_text(json.dumps(cfg, indent=2))


def _wait(predicate, attempts: int, delay: float) -> bool:
    import time
    for _ in range(attempts):
        if predicate():
            return True
        time.sleep(delay)
    return predicate()


# ── Ollama environment and login agents ─────────────────────────────────────

def _write_agent(label: str, program: list[str], *, keep_alive: bool = False,
                 env: dict | None = None) -> Path:
    LA_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    plist = {"Label": label, "ProgramArguments": program, "RunAtLoad": True,
             "StandardOutPath": str(LOG_DIR / f"{label}.log"),
             "StandardErrorPath": str(LOG_DIR / f"{label}.err.log")}
    if env:
        plist["EnvironmentVariables"] = env
    if keep_alive:
        plist.update(KeepAlive=True, ThrottleInterval=10, ProcessType="Interactive")
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
    nothing and silently re-downloads tens of gigabytes with the originals still
    on disk. Same volume, so each move is a rename; one file at a time, so an
    interruption leaves a resumable state.
    """
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


def cmd_autostart(argv: list[str]) -> int:
    """Login agents for Ollama, and optionally a native LiteLLM.

    Two servers must not fight over :11434, so the Ollama.app GUI, its embedded
    watchdog agent and its login item are all stopped before launchd takes the
    port.
    """
    if "--uninstall" in argv:
        step("Removing ailocal startup LaunchAgents")
        for label in AGENTS:
            _bootout(label)
            (LA_DIR / f"{label}.plist").unlink(missing_ok=True)
            ok(f"removed {label}")
        return 0

    _prepare_model_store()
    _migrate_model_store()

    if "--env-only" in argv:
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

    # The agent must never carry a baked model tag: it went on warming a model
    # the profile had replaced, failing silently with nobody looking. Resolve
    # through the installed command at run time.
    role = argv[argv.index("--model") + 1] if "--model" in argv else "architecture"
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


def cmd_models(argv: list[str]) -> int:
    """Pull the model set for the active tier.

    Enabled capabilities only, deduplicated by tag, sized from what Ollama
    reports and reduced by what is already on disk: a machine that already holds
    the models needs no additional space and must not be rejected as if it did.
    """
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
    """Never round up: a tier is chosen only when the machine has that memory.

    Selecting at a fraction of the tier's name hands a machine models sized for
    memory it does not have.
    """
    return next((t for t in ("128gb", "64gb", "32gb", "16gb")
                 if gb >= int(t[:-2])), None)


def _select_tier(override: str, assume_yes: bool) -> str:
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
#
# Two configs per client are CORRECT here: ailocal keeps the cloud root separate
# from the local one so both coexist. Flagging that pair would push someone into
# deleting a working setup, so every finding is classified rather than listed.
#
# Out of scope by design: ~/.claude and ~/.codex, VS Code SecretStorage, anything
# git tracks, and any container not named ailocal-*.

def audit() -> list:
    """Read-only. Returns findings; never deletes, moves or rewrites anything."""
    findings: list = []

    def flag(klass, item, location, action):
        findings.append((klass, item, str(location), action))
        print(f"  {klass} {item}\n      location: {location}\n      action:   {action}")

    cfg = policy.deployed_client_root()
    step("Clients")
    for name, probe, fix in (("claude", cfg / "claude" / ".claude.json", "claude"),
                             ("codex", cfg / "codex" / "config.toml", "codex")):
        if probe.is_file():
            ok(f"{name:8} {probe.parent}")
        else:
            flag("MISSING", f"{name} local config", probe, f"run ailocal clients {fix}")
    configure = cfg / "configure.zsh"
    if configure.is_file() and "CLAUDE_CONFIG_DIR" in configure.read_text():
        ok("isolation  configure.zsh sets CLAUDE_CONFIG_DIR")
    else:
        flag("DUPLICATE", "claude-local may share the cloud config root", configure,
             "re-run ailocal clients claude")
    if "gethnet.litellm-connector-copilot" in _out("code", "--list-extensions").lower():
        ok("vscode   connector extension installed")
    elif _has("code"):
        flag("MISSING", "VS Code connector extension", "VS Code", "run ailocal vscode")

    step("Installation state")
    backups = policy.state_root() / "backups"
    count = len(list(backups.iterdir())) if backups.is_dir() else 0
    if count > 20:
        flag("STALE", f"{count} files in {backups}", backups, "prune the oldest")
    else:
        ok(f"state backups   {count} file(s)")

    step("Login services (launchd)")
    for label in ("com.ailocal.ollama", "com.ailocal.ollama-env", "com.ailocal.preload"):
        plist = LA_DIR / f"{label}.plist"
        if not plist.is_file():
            dim(f"{label} not installed")
        elif label.endswith(("-env", "preload")):
            ok(f"{label} installed (one-shot: not-running is correct)")
        elif "state = running" in _out("launchctl", "print", f"gui/{os.getuid()}/{label}"):
            ok(f"{label} running")
        else:
            flag("STALE", f"{label} is installed but not running", plist,
                 f"launchctl bootstrap gui/{os.getuid()} '{plist}'")

    # A process count does not prove ownership of the port: the GUI app can hold
    # :11434 while the agent is unloaded, so the stack looks healthy until the
    # app is quit and nothing managed restarts the backend.
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
        ok(f"port 11434 owned by the managed LaunchAgent (pid {holder})")

    print()
    if findings:
        warn(f"{len(findings)} actionable finding(s). Nothing was modified.")
        print("     ailocal cleanup            # dry run\n"
              "     ailocal cleanup --apply    # backs up first")
    else:
        ok("No actionable findings.")
    return findings


def cmd_audit(argv: list[str]) -> int:
    return 3 if audit() else 0


def cmd_cleanup(argv: list[str]) -> int:
    """Act on the audit's findings. A dry run is the default: a cleanup tool
    whose default is destructive is a trap. Nothing is removed without a backup.
    """
    apply = "--apply" in argv
    include_notes = "--include-notes" in argv
    findings = audit()
    if not findings:
        return 0
    stamp = f"{datetime.now(timezone.utc):%Y%m%d-%H%M%S}"
    backup_root = policy.state_root() / "backups" / f"cleanup-{stamp}"
    did = held = 0

    step(f"Cleanup — {'applying' if apply else 'DRY RUN, nothing will be modified'}")
    for klass, item, location, action in findings:
        path = Path(location)
        if klass != "STALE":
            # Duplicates, missing pieces and unmanaged ports need a human: picking
            # a winner automatically is the guess that breaks a working setup.
            print(f"  HOLD   {item}\n         {action}")
            held += 1
            continue
        if not path.exists():
            dim(f"already gone: {item}")
            continue
        if path.suffix in (".md", ".json", ".txt", ".log") and not include_notes:
            print(f"  HOLD   looks like notes — needs your decision: {item}")
            print("         re-run with --include-notes to move it to backups/")
            held += 1
            continue
        print(f"  {'DONE  ' if apply else 'WOULD '} back up + remove: {item}")
        if apply:
            dest = backup_root / path.name
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(path, dest) if path.is_dir() else shutil.copyfile(path, dest)
            shutil.rmtree(path) if path.is_dir() else path.unlink()
        did += 1

    dead = _out("docker", "ps", "-a", "--filter", "name=ailocal-",
                "--filter", "status=exited", "--format", "{{.Names}}").split()
    for container in dead:
        print(f"  {'DONE  ' if apply else 'WOULD '} remove exited container {container}")
        if apply:
            _run("docker", "rm", container, capture=True)
        did += 1

    print()
    if apply:
        print(f" {did} action(s) taken, {held} held.")
        if did:
            print(f" Backup: {backup_root}")
    else:
        print(f" DRY RUN. {did} action(s) would be taken, {held} held for your decision.")
        print(" Re-run with --apply to act.")
    return 0


# ── install ─────────────────────────────────────────────────────────────────

USAGE = """usage: ailocal install [--yes] [--profile <16gb|32gb|64gb|128gb>] [--from DIR]

Bootstraps the stack: prerequisites, assets, .env, profile selection,
generation, models, services and client configuration. Idempotent.

  --yes              unattended; also enables production autostart
  --profile <tier>   override the tier detected from installed memory
  --from <dir>       the distribution to install from (default: this checkout)
"""


def _opt(argv: list[str], name: str, default=None):
    return argv[argv.index(name) + 1] if name in argv else default


def cmd_install(argv: list[str]) -> int:
    if {"-h", "--help"} & set(argv):
        print(USAGE)
        return 0
    assume_yes = "--yes" in argv

    _install_prerequisites(assume_yes)

    step("Installing assets")
    source = Path(_opt(argv, "--from") or distribution_source())
    report = provision(source, policy.config_root(), policy.data_root(),
                       policy.state_root())
    ok(f"data {policy.data_root()}: {', '.join(DATA_COMPONENTS)}")
    ok(f"config {policy.config_root()}: {len(report['installed'])} file(s)")
    for rel in report["preserved"]:
        dim(f"kept {rel} (edited since install)")
    for rel in report["absent"]:
        dim(f"absent {rel} (shipped default you removed)")

    step("Configuring Ollama")
    if assume_yes or _yes("Set up production autostart (launchd runs ollama serve "
                          "at login)?"):
        cmd_autostart(["--model", "architecture"])
    else:
        cmd_autostart(["--env-only"])

    step("Detecting hardware profile")
    ram = _memory_gb()
    tier = _select_tier(_opt(argv, "--profile", ""), assume_yes)

    step("Configuring environment (.env)")
    _write_env(assume_yes)

    # Generation must succeed before anything is pulled, and the plan below is
    # rendered from the generated artifact so it cannot report stale numbers.
    step("Generating configuration")
    _run(sys.executable, policy.data_root() / "lib" / "sync-models.py", check=True)
    print()
    _print_plan(tier, f"physical memory:     {ram} GB")

    (policy.state_root() / "backups").mkdir(parents=True, exist_ok=True)
    (policy.state_root() / "backups").chmod(0o700)

    step("Pulling pinned Docker images")
    runtime.compose("pull")
    runtime.cmd_start(["--no-wait"])
    runtime.compose("restart", "litellm", "searxng")
    if not runtime.wait_ready(30, progress=True):
        warn("LiteLLM did not become ready — check: docker logs ailocal-litellm")

    cmd_models([])
    _run(sys.executable, "-m", "ailocal.checks.run", "doctor")

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
    print("  Verify a real request:  ailocal smoke")
    return 0


COMMANDS = {"install": cmd_install, "models": cmd_models, "audit": cmd_audit,
            "cleanup": cmd_cleanup, "autostart": cmd_autostart}


def main(argv: list[str]) -> int:
    if not argv or argv[0] not in COMMANDS:
        print(f"usage: python -m ailocal.install <{'|'.join(COMMANDS)}>",
              file=sys.stderr)
        return 2
    return COMMANDS[argv[0]](argv[1:])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
