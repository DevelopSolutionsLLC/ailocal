"""cli.py — the single public command surface.

Dispatch is a TABLE, not a chain of branches: every command differs only by
target and fixed arguments, so the differences are data. Help is rendered from
that same table, which is the only way the two cannot drift.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

BASH = "/bin/bash"


def _root() -> Path:
    """Where the deploy assets live: the data root, and nothing else."""
    from . import policy
    return policy.data_root()


#: command -> (implementing module, fixed args, forwards argv)
PY, SH = "py", "sh"
COMMANDS: dict[str, tuple[str, tuple, bool]] = {
    "install":        ("ailocal.install", ("install",), True),
    "status":         ("ailocal.runtime", ("status",), True),
    "doctor":         ("ailocal.checks.run", ("doctor",), True),
    "validate":       ("ailocal.checks.run", ("validate",), True),
    "smoke":          ("ailocal.checks.run", ("smoke",), True),
    "security":       ("ailocal.checks.run", ("security",), True),
    "test":           ("ailocal.checks.run", ("test",), True),

    "sync":           ("ailocal.generation", (), True),
    "start":          ("ailocal.runtime", ("start",), True),
    "stop":           ("ailocal.runtime", ("stop",), True),
    "update":         ("ailocal.runtime", ("update",), True),
    "teardown":       ("ailocal.runtime", ("teardown",), True),
    "compose":        ("ailocal.runtime", ("compose",), True),
    "clients":        ("ailocal.clients", (), True),
    "vscode":         ("ailocal.clients", ("--vscode-only",), True),
    "models-install": ("ailocal.install", ("models",), True),
    "audit":          ("ailocal.install", ("audit",), True),
    "cleanup":        ("ailocal.install", ("cleanup",), True),
    "autostart":      ("ailocal.install", ("autostart",), True),
    "update-check":   ("ailocal.install", ("update-check",), True),
    "trace":          ("ailocal.runtime", ("trace",), True),
    "metrics":        ("ailocal.runtime", ("metrics",), True),
    "verify-session": ("ailocal.checks.run", ("verify-session",), True),
}

#: Nested surfaces: `ailocal <command> <target> [args]`.
NESTED: dict[str, dict[str, tuple]] = {
    "benchmark": {
        "models":  (PY, "benchmarks/models.py"),
        "planner": (PY, "benchmarks/planner.py"),
        "gateway": (SH, "benchmarks/gateway.sh"),
    },
}

#: Dispatched but NOT advertised. These are single-purpose developer and
#: host-setup tools with no documented user workflow: they stay reachable
#: because deleting a working diagnostic is not a simplification, but they do
#: not belong in the surface a new user reads. `verify-session` in particular
#: is the documented consumer of session_observer traces -- see
#: deploy/litellm/hooks/session_observer.py -- and is undiscoverable, not dead.
INTERNAL = frozenset({
    "verify-session", "trace", "metrics", "compose", "update-check",
})

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
    ("developer benchmarks",       ("benchmark",)),
    ("the active profile",         ("profile",)),
)


def _usage() -> str:
    rows = []
    for heading, names in GROUPS:
        shown = [f"{n} <{'|'.join(NESTED[n])}>" if n in NESTED else n
                 for n in names]
        rows.append((" | ".join(shown), heading))
    width = max(len(left) for left, _ in rows) + 2
    return "\n".join(["ailocal — local model runtime", ""]
                     + [f"  {left:<{width}}{heading}" for left, heading in rows])


def _call(module: str, args) -> int:
    """A package command runs in THIS process: it is a function, not a program."""
    import importlib
    return importlib.import_module(module).main([str(a) for a in args])


def _exec_script(kind: str, target: Path, args) -> None:
    """Replace this process for a data-root script, so signals pass through.

    Benchmarks import `ailocal` for every path they resolve, so the package has
    to be importable from wherever THIS module was loaded -- site-packages when
    installed, the checkout's src/ when not.
    """
    pkg_parent = str(Path(__file__).resolve().parents[1])
    existing = os.environ.get("PYTHONPATH", "")
    os.environ["PYTHONPATH"] = (f"{pkg_parent}{os.pathsep}{existing}"
                                if existing else pkg_parent)
    if not target.exists():
        sys.exit(f"ailocal: missing implementation {target}\n"
                 f"Set AILOCAL_DATA to the installed data root.")
    argv = ([BASH, str(target)] if kind == SH
            else [sys.executable, str(target)]) + [str(a) for a in args]
    os.execv(argv[0], argv)


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


#: Commands answered in this process rather than by exec: they are a few lines
#: over policy, and a subprocess for them would be the only reason to keep a
#: separate script.
HANDLERS = {"profile": _profile}


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd = argv.pop(0) if argv else "help"

    if cmd in ("help", "-h", "--help"):
        print(_usage())
        return 0

    if cmd in HANDLERS:
        return HANDLERS[cmd](argv)

    if cmd in NESTED:
        targets = NESTED[cmd]
        target = argv.pop(0) if argv else "help"
        if target not in targets:
            print(f"usage: ailocal {cmd} <{'|'.join(targets)}> [options]",
                  file=sys.stderr)
            return 0 if target in ("help", "-h", "--help") else 2
        kind, rel = targets[target]
        _exec_script(kind, _root() / rel, argv)

    if cmd not in COMMANDS:
        print(f"ailocal: unknown command '{cmd}' — run 'ailocal help'",
              file=sys.stderr)
        return 1

    module, fixed, forwards = COMMANDS[cmd]
    return _call(module, list(fixed) + (argv if forwards else []))


if __name__ == "__main__":
    sys.exit(main())
