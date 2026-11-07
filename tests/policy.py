#!/usr/bin/env python3
"""Test policy fail-closed, generation outside hosted roots, env file ownership.

Writes only temp roots. Generation keeps the real HOME; every hosted client root
is pointed at a temp dir that must stay empty.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness import RESOURCES, Suite  # noqa: E402
from ailocal import generation as G, policy as P  # noqa: E402

S = Suite()
check = S.check
TMP = Path(tempfile.mkdtemp(prefix="ailocal-policy-"))
GOOD = '[primary]\nrole = "Lead"\nactive = "m:1b"\ncontext_input = 100\nmax_output = 20\n'


@contextlib.contextmanager
def env_set(**values):
    """Set env vars, then restore prior values; a pop repoints later checks."""
    def apply(pairs):
        for k, v in pairs.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, str(v))
    prior = {k: os.environ.get(k) for k in values}
    apply(values)
    try:
        yield
    finally:
        apply(prior)


def box(marker=None, text=None, name="profiles/64gb.toml") -> Path:
    root = Path(tempfile.mkdtemp(dir=TMP))
    (root / "profiles").mkdir()
    if marker is not None:
        target = P.active_profile_path(root / "state")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(marker)
    if text is not None:
        (root / name).write_text(text)
    return root


def code_of(fn, *args):
    try:
        fn(*args)
        return "OK"
    except P.ProfileError as exc:
        return exc.code


def _cli_profile_error() -> tuple[int, str]:
    """Run `ailocal check` against a command that raises; return (rc, code)."""
    from ailocal import cli

    def boom(argv):
        raise P.ProfileError(P.PROFILE_INVALID, "x")
    sys.modules["_raises_profile"] = types.SimpleNamespace(main=boom)
    saved, cli.COMMANDS["check"] = cli.COMMANDS["check"], ("_raises_profile", ())
    err = io.StringIO()
    try:
        with contextlib.redirect_stderr(err):
            rc = cli.main(["check"])
    except Exception as exc:  # report a crash as a mismatch, not a suite abort
        return 1, repr(exc)
    finally:
        cli.COMMANDS["check"] = saved
        sys.modules.pop("_raises_profile", None)
    return rc, P.PROFILE_INVALID if P.PROFILE_INVALID in err.getvalue() else err.getvalue()


def fails_closed() -> None:
    S.section("POLICY FILES FAIL CLOSED")
    want, got = {}, {}
    for label, marker, code in (("missing marker", None, P.ACTIVE_PROFILE_MISSING),
                                ("unknown tier", "999gb", P.ACTIVE_PROFILE_INVALID)):
        root = box(marker)
        want[label], got[label] = code, code_of(P.resolve_active_tier, root, root / "state")
    head = '[primary]\nrole = "L"\nactive = "m:1b"\n'
    profiles = [
        ("missing profile file", None, P.PROFILE_FILE_MISSING),
        ("malformed profile", "[primary\n", P.PROFILE_INVALID),
        ("non-integer context_input", head + 'context_input = "x"\n', P.ROLE_CONFIG_INVALID),
        ("missing required field", '[primary]\nrole = "L"\ncontext_input = 10\n', P.PROFILE_SCHEMA_INVALID),
        ("duplicate key", GOOD + "context_input = 999\n", P.PROFILE_INVALID),
        # Unknown keys must fail: a dropped typo reads as set but is not.
        ("mistyped key", GOOD + "temprature = 0.1\n", P.PROFILE_SCHEMA_INVALID),
    ]
    for label, text, code in profiles:
        want[label], got[label] = code, code_of(P.load_profile, "64gb", box("64gb", text))
    clients = {"unknown client": '[nosuchclient]\ndefault = "fast"\n',
               "non-TOML clients.toml": "bogus:\n  default: fast\n",
               "duplicate clients.toml key": '[claude]\ndefault = "fast"\ndefault = "primary"\n',
               "bare dotted key": '[compat]\ngpt-5.5 = "primary"\n'}   # nests instead of naming
    for label, text in clients.items():
        want[label] = P.CLIENT_POLICY_INVALID
        got[label] = code_of(P.load_client_policy, box(text=text, name="profiles/clients.toml"))
    want["missing clients.toml"], got["missing clients.toml"] = P.CLIENT_POLICY_MISSING, code_of(P.load_client_policy, box())
    want.update({t: "OK" for t in P.TIERS})
    got.update({t: code_of(P.load_profile, t) for t in P.TIERS})
    want["command raises ProfileError"] = (2, P.PROFILE_INVALID)
    got["command raises ProfileError"] = _cli_profile_error()
    check(got == want, "every shipped tier loads; a missing or malformed profile or clients.toml fails closed "
          "with its code", repr({k: v for k, v in got.items() if want[k] != v}))


def _generate(tier: str) -> dict:
    """Run the generator for one tier, every hosted root at a temp dir."""
    b = Path(tempfile.mkdtemp(dir=TMP))
    shutil.copytree(RESOURCES / "profiles", b / "cfg/profiles")
    P.active_profile_path(b / "state").parent.mkdir(parents=True, exist_ok=True)
    P.active_profile_path(b / "state").write_text(tier)
    hosted = {k: b / "hosted" / k for k in ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "COPILOT_HOME",
                                            "AILOCAL_VSCODE_USER", "ZDOTDIR")}
    for d in hosted.values():
        d.mkdir(parents=True)
    env = {**os.environ, "AILOCAL_CONFIG": str(b / "cfg"), "AILOCAL_CLIENTS": str(b / "clients"),
           "AILOCAL_STATE": str(b / "state"), **{k: str(v) for k, v in hosted.items()}}
    r = subprocess.run([sys.executable, "-m", "ailocal.generation"], env=env, capture_output=True, text=True)
    return {"root": b, "rc": r.returncode, "stderr": r.stderr.strip()[-300:],
            "hosted": [str(p) for d in hosted.values() for p in d.rglob("*")]}


def generated() -> None:
    S.section("GENERATION PER TIER")
    runs = {t: _generate(t) for t in P.TIERS}
    bad = {t: g["stderr"] or g["hosted"] for t, g in runs.items()
           if g["rc"] != 0 or g["hosted"] or not (g["root"] / "clients/claude/settings.json").exists()}
    check(not bad, "every tier generates claude-local and leaves every hosted root empty", repr(bad))
    if bad:
        return
    launch = P.load_client_policy(RESOURCES).get("claude", {}).get("launch_default", "primary")
    # Claude Code defaults to 32000 output tokens; a mismatch loops.
    got, want = {}, {}
    for tier, g in runs.items():
        cenv = json.loads((g["root"] / "clients/claude/settings.json").read_text()).get("env", {})
        role = P.resolve_role(tier, launch, RESOURCES)
        got[tier] = (cenv.get("CLAUDE_CODE_MAX_OUTPUT_TOKENS"), cenv.get("CLAUDE_CODE_MAX_CONTEXT_TOKENS"))
        want[tier] = (str(role["max_output"]), str(role["context_input"]))
    models, clients = G.load_models_yaml(RESOURCES / "profiles/64gb.toml"), G.load_clients_yaml()
    models[launch] = {k: v for k, v in models[launch].items() if k != "max_output"}
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            G.regen_claude_settings(models, clients)
        rejected = "returned"
    except SystemExit as exc:
        rejected = str(exc)
    check(got == want and "max_output" in rejected,
          f"Claude's output and window are {launch}'s max_output and context_input; a launch role without "
          "max_output is rejected rather than left to Claude's 32000", f"{got} vs {want}; {rejected}")


def env_ownership() -> None:
    from ailocal import environment as e
    mode = lambda p: stat.S_IMODE(p.stat().st_mode)  # noqa: E731
    S.section("ENVIRONMENT OWNERSHIP")
    base = Path(tempfile.mkdtemp(dir=TMP))
    cfg, state = base / "config", base / "state"
    cfg.mkdir(), state.mkdir()
    gen_file, user_file = state / "env", cfg / ".env.local"
    with env_set(AILOCAL_CONFIG=cfg, AILOCAL_STATE=state):
        e.ensure()
        gen = e.parse(gen_file)
        before = (gen_file.read_text(), user_file.read_text())
        e.ensure()
        check(all(gen.get(k) for k in e.GENERATED) and mode(gen_file) == mode(user_file) == 0o600
              and before == (gen_file.read_text(), user_file.read_text()),
              "generated secrets and the user file are 0600, and ensure() never rotates a secret")
        loopback = e.bind() == e.BIND_LOCAL
        e.set_ollama_patch("")
        e.set_bind("10.0.0.5")
        user = e.parse(user_file)
        check(loopback and e.bind() == "10.0.0.5" and user.get("AILOCAL_OLLAMA_PATCH") == ""
              and mode(user_file) == 0o600,
              "the proxy binds loopback unless told otherwise; set_bind keeps the user's other overrides (#103)")


def main() -> int:
    try:
        for section in (fails_closed, generated, env_ownership):
            try:
                section()
            except Exception as exc:  # noqa: BLE001
                S.error(section.__name__, exc)
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    return S.report()


if __name__ == "__main__":
    sys.exit(main())
