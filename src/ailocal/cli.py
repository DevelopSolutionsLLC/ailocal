"""cli.py — the single public command surface.

Dispatch is a TABLE, not a chain of branches: every command differs only by
target and fixed arguments, so the differences are data. Help is rendered from
that same table, which is the only way the two cannot drift.
"""
from __future__ import annotations

import json
import sys


#: command -> (implementing module, fixed args prepended to argv)
COMMANDS: dict[str, tuple[str, tuple]] = {
    "install":        ("ailocal.install", ("install",)),
    "status":         ("ailocal.runtime", ("status",)),
    "doctor":         ("ailocal.checks.run", ("doctor",)),
    "validate":       ("ailocal.checks.run", ("validate",)),
    "smoke":          ("ailocal.checks.run", ("smoke",)),
    "security":       ("ailocal.checks.run", ("security",)),
    "test":           ("ailocal.checks.run", ("test",)),

    "sync":           ("ailocal.generation", ()),
    "start":          ("ailocal.runtime", ("start",)),
    "stop":           ("ailocal.runtime", ("stop",)),
    "update":         ("ailocal.runtime", ("update",)),
    "teardown":       ("ailocal.runtime", ("teardown",)),
    "clients":        ("ailocal.clients", ()),
    "vscode":         ("ailocal.clients", ("--vscode-only",)),
    "models-install": ("ailocal.install", ("models",)),
    "audit":          ("ailocal.install", ("audit",)),
    "cleanup":        ("ailocal.install", ("cleanup",)),
    "autostart":      ("ailocal.install", ("autostart",)),
    "update-check":   ("ailocal.install", ("update-check",)),
    "trace":          ("ailocal.runtime", ("trace",)),
    "metrics":        ("ailocal.runtime", ("metrics",)),
}

#: Dispatched but NOT advertised: single-purpose diagnostics and host setup
#: with no documented user workflow. They stay reachable because deleting a
#: working diagnostic is not a simplification, but they do not belong in the
#: surface a new user reads.
INTERNAL = frozenset({"trace", "metrics", "update-check"})

#: Help layout: (heading, [command...]). Every non-internal command appears
#: exactly once; the gate asserts it.
GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("bootstrap the stack",        ("install",)),
    ("inspect",                    ("status", "doctor", "validate", "smoke")),
    ("container supply chain",     ("security",)),
    ("lifecycle",                  ("start", "stop", "update", "sync")),
    ("deploy",                     ("clients", "vscode", "models-install")),
    ("installation",               ("audit", "cleanup", "teardown")),
    ("host setup",                 ("autostart",)),
    ("the regression gate",        ("test",)),
    ("the active profile",         ("profile",)),
)


def _usage() -> str:
    rows = []
    for heading, names in GROUPS:
        rows.append((" | ".join(names), heading))
    width = max(len(left) for left, _ in rows) + 2
    return "\n".join(["ailocal — local model runtime", ""]
                     + [f"  {left:<{width}}{heading}" for left, heading in rows])


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
            P.load_effective()
            print(f"ok: {P.active_tier()} active; generated profile state is valid")
        else:
            print("usage: ailocal profile <active-tier|state-root|config-root|"
                  "data-root|active-profile-path|role NAME [--field F] [--tier T]|"
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
