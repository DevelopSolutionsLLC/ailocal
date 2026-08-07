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
    """Where lib/ and the deploy assets live.

    AILOCAL_DATA wins, so an installed package can be pointed at its assets.
    Otherwise walk up from this file, which resolves inside a checkout
    (src/ailocal/cli.py -> repo root) and is what every current entry point
    assumes. ADR 009 phase 4 makes the installed data root the default.
    """
    override = os.environ.get("AILOCAL_DATA")
    return Path(override) if override else Path(__file__).resolve().parents[2]


#: command -> (interpreter, target relative to root, fixed args, forwards argv)
#: `models` deliberately does not forward: it is a fixed table rendering, and
#: accepting arguments it then ignores would be worse than refusing them.
PY, SH = "py", "sh"
COMMANDS: dict[str, tuple] = {
    "install":        (SH, "lib/install.sh", (), True),
    "status":         (SH, "lib/status.sh", (), True),
    "models":         (SH, "lib/status.sh", ("--table",), False),
    "doctor":         (PY, "lib/checks/run.py", ("doctor",), True),
    "validate":       (PY, "lib/checks/run.py", ("validate",), True),
    "smoke":          (PY, "lib/checks/run.py", ("smoke",), True),
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
    "preload":        (SH, "lib/preload-model.sh", (), True),
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
    "resolve", "verify-session", "trace", "metrics", "preload", "ollama-env",
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


def _exec(kind: str, target: Path, args) -> None:
    """Replace this process, so exit codes and signals pass through unchanged."""
    if not target.exists():
        sys.exit(f"ailocal: missing implementation {target}\n"
                 f"Set AILOCAL_DATA to the directory containing lib/.")
    argv = ([BASH, str(target)] if kind == SH
            else [sys.executable, str(target)]) + [str(a) for a in args]
    os.execv(argv[0], argv)


def _policy():
    """policy.py still lives under lib/ (ADR 009 phase 7 moves it here)."""
    sys.path.insert(0, str(_root() / "lib"))
    import policy
    return policy


def _provision(argv: list[str]) -> int:
    """Install authored assets into the config and data roots."""
    from . import provision as prov
    P = _policy()
    source = Path(__file__).resolve().parents[2]
    config = Path(argv[argv.index("--config") + 1]) if "--config" in argv \
        else P.config_root()
    data = Path(argv[argv.index("--data") + 1]) if "--data" in argv \
        else P.data_root()
    state = Path(argv[argv.index("--state") + 1]) if "--state" in argv \
        else P.state_root()
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
    _exec(kind, _root() / rel, list(fixed) + (argv if forwards else []))
    return 0  # unreachable: _exec replaces the process


if __name__ == "__main__":
    sys.exit(main())
