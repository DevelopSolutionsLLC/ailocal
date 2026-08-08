"""cli.py — the single public command surface.

Dispatch is a TABLE, not a chain of branches: every command differs only by
target and fixed arguments, so the differences are data. Help is rendered from
that same table, which is the only way the two cannot drift.
"""
from __future__ import annotations

import json
import sys

from . import __version__


#: command -> (implementing module, fixed args prepended to argv)
COMMANDS: dict[str, tuple[str, tuple]] = {
    "install":        ("ailocal.install", ("install",)),
    "status":         ("ailocal.runtime", ("status",)),
    "check":          ("ailocal.checks.run", ("check",)),

    "start":          ("ailocal.runtime", ("start",)),
    "stop":           ("ailocal.runtime", ("stop",)),
    "teardown":       ("ailocal.runtime", ("teardown",)),
    "clients":        ("ailocal.clients", ()),
}

#: Help layout: (heading, [command...]). Every dispatchable command appears
#: exactly once; the gate asserts it. There is no hidden command: a surface
#: that dispatches what it does not advertise is a surface nobody can trust.
GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("bootstrap the stack",        ("install",)),
    ("inspect",                    ("status", "check")),
    ("lifecycle",                  ("start", "stop")),
    ("point a client at it",       ("clients",)),
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
                  "profile-summary [--tier T]|validate>", file=sys.stderr)
            return 2
    except P.ProfileError as e:
        print(f"{e.code}: {e.detail}", file=sys.stderr)
        return 2
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd = argv.pop(0) if argv else "help"

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

    # A package command runs in THIS process: it is a function, not a program,
    # so there is one dispatch mechanism and no second interpreter to choose.
    import importlib
    module, fixed = COMMANDS[cmd]
    return importlib.import_module(module).main([str(a) for a in [*fixed, *argv]])


if __name__ == "__main__":
    sys.exit(main())
