"""cli.py — the single public command surface.

Dispatch is a TABLE, not a chain of branches: every command differs only by
interpreter, target and fixed arguments, so the differences are data. The help
text is rendered from that same table, which is the only way the two cannot
drift -- three separate copies of the command list had already gone stale
before this existed.

Implementations still live under lib/ and are executed as subprocesses. ADR 009
phases 7 onward move them into this package; until then this module owns the
surface and nothing else.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

BASH = "/bin/bash"


def _root() -> Path:
    """Where lib/ and the deploy assets live: the data root, and nothing else.

    This used to walk up from __file__, which is only ever right inside a
    checkout -- installed under site-packages it produced a path with no lib/
    in it. policy.py owns every root; asking it is the whole point.
    """
    from . import policy
    return policy.data_root()


#: command -> (interpreter, target relative to root, fixed args, forwards argv)
#: `models` deliberately does not forward: it is a fixed table rendering, and
#: accepting arguments it then ignores would be worse than refusing them.
PY, SH, MOD = "py", "sh", "mod"
COMMANDS: dict[str, tuple] = {
    "install":        (SH, "lib/install.sh", (), True),
    "status":         (SH, "lib/status.sh", (), True),
    "models":         (SH, "lib/status.sh", ("--table",), False),
    "doctor":         (MOD, "ailocal.checks.run", ("doctor",), True),
    "validate":       (MOD, "ailocal.checks.run", ("validate",), True),
    "smoke":          (MOD, "ailocal.checks.run", ("smoke",), True),
    "security":       (SH, "lib/security.sh", (), True),
    "test":           (SH, "lib/test-all.sh", (), True),
    "profile":        (PY, "lib/profile-config", (), True),
    "sync":           (PY, "lib/sync-models.py", (), True),
    "resolve":        (PY, "lib/sync-models.py", ("--resolve",), True),
    "start":          (SH, "lib/lifecycle.sh", ("start",), True),
    "stop":           (SH, "lib/lifecycle.sh", ("stop",), True),
    "update":         (SH, "lib/lifecycle.sh", ("update",), True),
    "teardown":       (SH, "lib/lifecycle.sh", ("teardown",), True),
    "clients":        (SH, "lib/install-clients.sh", (), True),
    "vscode":         (SH, "lib/install-vscode.sh", (), True),
    "models-install": (SH, "lib/install-models.sh", (), True),
    "audit":          (SH, "lib/audit-installation.sh", (), True),
    "cleanup":        (SH, "lib/cleanup-installation.sh", (), True),
    "autostart":      (SH, "lib/setup-startup.sh", (), True),
    "ollama-env":     (SH, "lib/setup-ollama-env.sh", (), True),
    "trace":          (SH, "lib/request-trace.sh", (), True),
    "metrics":        (PY, "lib/gateway-metrics.py", (), True),
    "verify-session": (PY, "lib/diagnostics/verify-session.py", (), True),
}

#: Nested surfaces: `ailocal <command> <target> [args]`.
NESTED: dict[str, dict[str, tuple]] = {
    "benchmark": {
        "models":  (PY, "benchmarks/models.py"),
        "planner": (PY, "benchmarks/planner.py"),
        "gateway": (SH, "benchmarks/gateway.sh"),
    },
    "e2e": {
        "claude": (SH, "lib/validate-claude-e2e.sh"),
        "codex":  (SH, "lib/validate-codex-e2e.sh"),
        "vscode": (SH, "lib/validate-vscode-e2e.sh"),
    },
}

#: Dispatched but NOT advertised. These are single-purpose developer and
#: host-setup tools with no documented user workflow: they stay reachable
#: because deleting a working diagnostic is not a simplification, but they do
#: not belong in the surface a new user reads. `verify-session` in particular
#: is the documented consumer of session_observer traces -- see
#: deploy/litellm/hooks/session_observer.py -- and is undiscoverable, not dead.
INTERNAL = frozenset({
    "resolve", "verify-session", "trace", "metrics", "ollama-env",
})

#: Help layout: (heading, [command...]). Every non-internal command appears
#: exactly once; the gate asserts it.
GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("bootstrap the stack",        ("install",)),
    ("inspect",                    ("status", "models", "doctor", "validate", "smoke")),
    ("container supply chain",     ("security",)),
    ("lifecycle",                  ("start", "stop", "update", "sync")),
    ("deploy",                     ("clients", "vscode", "models-install")),
    ("installation",               ("audit", "cleanup", "teardown")),
    ("host setup",                 ("autostart",)),
    ("diagnostics",                ("e2e",)),
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


def _exec(kind: str, target, args) -> None:
    """Replace this process, so exit codes and signals pass through unchanged."""
    # Dispatched implementations import `ailocal.policy` for every path they
    # resolve. They run as subprocesses, so the package has to be importable
    # from wherever THIS module was loaded -- site-packages when installed, the
    # checkout's src/ when not. Without this they would each rediscover a root,
    # which is the duplication policy.py exists to prevent.
    pkg_parent = str(Path(__file__).resolve().parents[1])
    existing = os.environ.get("PYTHONPATH", "")
    os.environ["PYTHONPATH"] = (f"{pkg_parent}{os.pathsep}{existing}"
                                if existing else pkg_parent)
    tail = [str(a) for a in args]
    if kind == MOD:
        os.execv(sys.executable, [sys.executable, "-m", str(target)] + tail)
    if not target.exists():
        sys.exit(f"ailocal: missing implementation {target}\n"
                 f"Set AILOCAL_DATA to the directory containing lib/.")
    argv = ([BASH, str(target)] if kind == SH
            else [sys.executable, str(target)]) + tail
    os.execv(argv[0], argv)


def _opt(argv: list[str], name: str):
    return argv[argv.index(name) + 1] if name in argv else None


def _provision(argv: list[str]) -> int:
    """Install authored assets into the config and data roots.

    The source is the distribution being installed FROM -- a checkout, or an
    unpacked release. It is deliberately not the data root: that is the
    destination, and conflating them is how an install overwrites itself.
    """
    from . import policy as P, provision as prov
    source = Path(_opt(argv, "--from") or Path(__file__).resolve().parents[2])
    config = Path(_opt(argv, "--config") or P.config_root())
    data = Path(_opt(argv, "--data") or P.data_root())
    state = Path(_opt(argv, "--state") or P.state_root())
    if config == source or data == source:
        print("provision: refusing to install a checkout over itself.\n"
              "Pass --config/--data, or set AILOCAL_CONFIG/AILOCAL_DATA.",
              file=sys.stderr)
        return 2
    report = prov.provision(source, config, data, state)
    print(f"data     {data}: {', '.join(report['data_components'])}")
    print(f"config   {config}: {len(report['installed'])} file(s) installed")
    for rel in report["preserved"]:
        print(f"  kept   {rel} (edited since install)")
    for rel in prov.missing_defaults(source, config):
        print(f"  absent {rel} (shipped default not present)")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd = argv.pop(0) if argv else "help"

    if cmd in ("help", "-h", "--help"):
        print(_usage())
        return 0

    if cmd == "provision":
        return _provision(argv)

    if cmd in NESTED:
        targets = NESTED[cmd]
        target = argv.pop(0) if argv else "help"
        if target not in targets:
            print(f"usage: ailocal {cmd} <{'|'.join(targets)}> [options]",
                  file=sys.stderr)
            return 0 if target in ("help", "-h", "--help") else 2
        kind, rel = targets[target]
        _exec(kind, _root() / rel, argv)

    if cmd not in COMMANDS:
        print(f"ailocal: unknown command '{cmd}' — run 'ailocal help'",
              file=sys.stderr)
        return 1

    kind, rel, fixed, forwards = COMMANDS[cmd]
    target = rel if kind == MOD else _root() / rel
    _exec(kind, target, list(fixed) + (argv if forwards else []))
    return 0  # unreachable: _exec replaces the process


if __name__ == "__main__":
    sys.exit(main())
