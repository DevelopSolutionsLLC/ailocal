"""cli.py — the single public command surface.

Dispatch is a TABLE, not a chain of branches: every command differs only by
target and fixed arguments, so the differences are data. Help is rendered from
that same table, which is the only way the two cannot drift.
"""
from __future__ import annotations

import io
import contextlib
import fcntl
import json
import os
import sys

from . import __version__


@contextlib.contextmanager
def _lifecycle_lock():
    """Two independent install/start/stop commands must not race over services."""
    from . import policy
    root = policy.state_root()
    root.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(root / "lifecycle.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("Another ailocal lifecycle command is running. "
                             "Let it finish, then retry.") from None
        yield
    finally:
        os.close(descriptor)


#: command -> (implementing module, fixed args prepended to argv)
COMMANDS: dict[str, tuple[str, tuple]] = {
    "install":        ("ailocal.install", ("install",)),
    "status":         ("ailocal.runtime", ("status",)),
    "check":          ("ailocal.checks.run", ("check",)),
    "update":         ("ailocal.update", ()),
    "audit":          ("ailocal.audit", ()),

    "start":          ("ailocal.runtime", ("start",)),
    "stop":           ("ailocal.runtime", ("stop",)),
    "teardown":       ("ailocal.runtime", ("teardown",)),
    "clients":        ("ailocal.clients", ()),
    "mcp":            ("ailocal.mcp", ()),
}

#: `ailocal <command> --help`, answered here so no command can mistake the flag
#: for a request: `clients --help` used to configure every client and
#: `start --help` to start the stack. `install` owns a longer usage, and `check`
#: owns its own because only checks/run.py may name the metered search flag.
USAGE: dict[str, str] = {
    "status":   "usage: ailocal status [--dashboard|--table|--models]",
    "update":   "usage: ailocal update [--upstream-only] [--json]",
    "audit":    "usage: ailocal audit [--json]",
    "start":    "usage: ailocal start [--no-wait]",
    "stop":     "usage: ailocal stop [--volumes]",
    "teardown": "usage: ailocal teardown [--images]",
    "clients":  "usage: ailocal clients [vscode|codex|claude|all ...] [--dry-run]",
    "mcp":      "usage: ailocal mcp research [--enable-answer]",
}

#: Help layout: (heading, [command...]). Every dispatchable command appears
#: exactly once; the gate asserts it. There is no hidden command: a surface
#: that dispatches what it does not advertise is a surface nobody can trust.
GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("bootstrap the stack",        ("install",)),
    ("inspect",                    ("status", "check", "update", "audit")),
    ("lifecycle",                  ("start", "stop")),
    ("point a client at it",       ("clients",)),
    ("bundled research tools",     ("mcp",)),
    ("remove it",                  ("teardown",)),
    ("the active profile",         ("profile",)),
)


def _usage() -> str:
    rows = []
    for heading, names in GROUPS:
        rows.append((" | ".join(names), heading))
    width = max(len(left) for left, _ in rows) + 2
    return "\n".join([f"ailocal {__version__} — local model runtime", ""]
                     + [f"  {left:<{width}}{heading}" for left, heading in rows]
                     + ["", "  --version".ljust(width + 2) + "the installed version"])


#: `ailocal profile <query>`. Scalars print bare so `$(...)` captures them
#: cleanly; every failure prints the error CODE to stderr and exits non-zero.
#: The login preload agent is the runtime consumer of `role --field`.
def _profile(argv: list[str]) -> int:
    from . import policy as P
    query = argv[0] if argv else ""
    rest = argv[1:]

    def opt(name):
        return rest[rest.index(name) + 1] if name in rest else None

    try:
        if query == "guide":
            from . import guidance
            return guidance.main(rest)
        if query == "use":
            from . import install
            if not rest:
                print("usage: ailocal profile use <tier>", file=sys.stderr)
                return 2
            install.select_tier(rest[0], assume_yes=False)
            from . import generation
            return generation.main([])
        if query == "state-root":
            print(P.state_root())
        elif query == "config-root":
            print(P.config_root())
        elif query == "data-root":
            print(P.data_root())
        elif query == "active-profile-path":
            print(P.active_profile_path())
        elif query == "active-tier":
            print(P.active_tier())
        elif query == "role":
            tier = opt("--tier")
            cfg = P.resolve_role(tier, rest[0]) if tier else P.effective_role(rest[0])
            field = opt("--field")
            if field is None:
                print(json.dumps(cfg, indent=1, sort_keys=True))
            elif field not in cfg:
                print(f"{P.ROLE_CONFIG_INVALID}: no field {field!r}", file=sys.stderr)
                return 2
            else:
                # An absent optional field is not an error, but it must not
                # print as the string "None" into a shell variable.
                print("" if cfg[field] is None else cfg[field])
        elif query == "profile-summary":
            tier = opt("--tier")
            print(json.dumps(P.profile_summary(tier) if tier
                             else P.effective_summary(), indent=1, sort_keys=True))
        elif query == "validate":
            P.effective_summary()
            print(f"ok: {P.active_tier()} active; profile policy is valid")
        else:
            print("usage: ailocal profile <use TIER|active-tier|state-root|"
                  "config-root|data-root|active-profile-path|"
                  "role NAME [--field F] [--tier T]|"
                  "profile-summary [--tier T]|guide [MODEL] [--tier T] [--json]|validate>", file=sys.stderr)
            return 2
    except P.ProfileError as e:
        print(f"{e.code}: {e.detail}", file=sys.stderr)
        return 2
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd = argv.pop(0) if argv else "help"
    # Redirected stdout is block-buffered, while the docker, brew and code
    # children it runs write straight to the descriptor: a saved install log
    # printed their output before the step that started them.
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(line_buffering=True)

    if cmd in ("help", "-h", "--help"):
        print(_usage())
        return 0

    # Answered here, beside help, rather than added to COMMANDS: both are
    # questions about the CLI itself, not a module to dispatch to. Bare, so
    # `$(ailocal --version)` is the version and nothing else — the first thing
    # anyone asks for in a bug report.
    if cmd in ("--version", "-V", "version"):
        print(__version__)
        return 0

    # `profile` is a few lines over policy, answered here rather than given a
    # module of its own.
    if cmd == "profile":
        return _profile(argv)

    if cmd not in COMMANDS:
        print(f"ailocal: unknown command '{cmd}' — run 'ailocal help'",
              file=sys.stderr)
        return 1

    if cmd in USAGE and {"-h", "--help"} & set(argv):
        print(USAGE[cmd])
        return 0

    # A package command runs in THIS process: it is a function, not a program,
    # so there is one dispatch mechanism and no second interpreter to choose.
    import importlib
    module, fixed = COMMANDS[cmd]
    with (_lifecycle_lock() if cmd in ("install", "start", "stop", "teardown")
          and not {"-h", "--help"}.intersection(argv)
          else contextlib.nullcontext()):
        return importlib.import_module(module).main([str(a) for a in [*fixed, *argv]])


if __name__ == "__main__":
    sys.exit(main())
