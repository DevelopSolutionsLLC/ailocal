#!/usr/bin/env python3
"""Policy files fail closed, generation stays out of hosted roots, and the environment files' ownership.

Writes only temp roots. Generation keeps the real HOME; every hosted client root
is pointed at a temp dir that must stay empty."""
from __future__ import annotations

import contextlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness import RESOURCES, Suite  # noqa: E402
from ailocal import policy as P  # noqa: E402

S = Suite()
check = S.check
TMP = Path(tempfile.mkdtemp(prefix="ailocal-policy-"))
GOOD = '[primary]\nrole = "Lead"\nactive = "m:1b"\ncontext_input = 100\nmax_output = 20\n'


@contextlib.contextmanager
def env_set(**values):
    """Set env vars and restore the prior values, never drop them: the gate
    wrapper declares the isolated roots and a pop would repoint later checks."""
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
        # Unknown keys were silently dropped before 2026-08-04, so a typo read as set and was not.
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
    check(got == want, "every shipped tier loads; a missing or malformed profile or clients.toml fails closed "
          "with its code", repr({k: v for k, v in got.items() if want[k] != v}))


def _generate(tier: str) -> dict:
    """Run the generator for one tier into temp roots, every hosted root pointed at an empty dir."""
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
    # #56: subtracting the guide hook from the published budget left Claude less than Cadence's policy.
    got = {t: json.loads((g["root"] / "clients/integration-contract.json").read_text()).get("instructions")
           for t, g in runs.items()}
    want = {t: {"claude": {"max_bytes": (b := P.instruction_budgets(runs[t]["root"] / "cfg", t))["claude"],
                           "file": "CLAUDE.md"},
                "codex": {"max_bytes": b["codex"], "file": "AGENTS.md"}} for t in runs}
    check(got == want, "the integration contract publishes each tier's full instruction budget (#56)",
          f"{got} vs {want}")


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
