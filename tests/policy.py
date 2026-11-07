#!/usr/bin/env python3
"""Profile, client-policy and environment-ownership invariants.

Writes only temp roots. Generation keeps the real HOME; every hosted client root
is pointed at a temp dir that must stay empty."""
from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
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


def code_of(fn, *args, message=False):
    try:
        fn(*args)
        return "OK"
    except P.ProfileError as exc:
        return str(exc) if message else exc.code


def yaml_int(block, key):
    m = re.search(rf"(?m)^\s*{re.escape(key)}:\s*(-?\d+)\s*$", block)
    return int(m.group(1)) if m else None


def resolver() -> None:
    S.section("PROFILES FAIL CLOSED")
    want, got = {}, {}
    for label, marker, code in (("missing marker", None, P.ACTIVE_PROFILE_MISSING),
                                ("empty marker", "   \n", P.ACTIVE_PROFILE_EMPTY),
                                ("unknown tier", "999gb", P.ACTIVE_PROFILE_INVALID)):
        root = box(marker)
        want[label], got[label] = code, code_of(P.resolve_active_tier, root, root / "state")
    head = '[primary]\nrole = "L"\nactive = "m:1b"\n'
    cases = [
        ("missing profile file", None, P.PROFILE_FILE_MISSING),
        ("malformed policy", "[primary\n", P.PROFILE_INVALID),
        ("non-integer context_input", head + 'context_input = "x"\n', P.ROLE_CONFIG_INVALID),
        ("missing required field", '[primary]\nrole = "L"\ncontext_input = 10\n', P.PROFILE_SCHEMA_INVALID),
        ("max_output -1", head + "context_input = 100\nmax_output = -1\n", P.ROLE_CONFIG_INVALID),
        ("duplicate key", GOOD + "context_input = 999\n", P.PROFILE_INVALID),
        ("duplicate section", GOOD + '[primary]\nrole = "X"\nactive = "o:1b"\n', P.PROFILE_INVALID),
        ("unclosed list", GOOD + "preferred = [a, b\n", P.PROFILE_INVALID),
        # A quantization token in a selection names an artefact, not a model (#93).
        ("quantization token", GOOD.replace("m:1b", "qwen3.5:4b-mtp-q4_K_M"), P.PROFILE_SCHEMA_INVALID),
    ]
    # Unknown keys were silently dropped before 2026-08-04, so a typo read as set and was not.
    for frag in ("temprature = 0.1", "topk = 20", "num_predict = 512", "draft_num_predict = true"):
        cases.append((f"unknown or mistyped `{frag}`", GOOD + frag + "\n", P.PROFILE_SCHEMA_INVALID))
    for label, text, code in cases:
        want[label], got[label] = code, code_of(P.load_profile, "64gb", box("64gb", text))
    root = box("64gb", GOOD)
    want["absent role"], got["absent role"] = P.ROLE_MISSING, code_of(P.resolve_role, "64gb", "fast", root)
    want.update({t: "OK" for t in P.TIERS})
    got.update({t: code_of(P.load_profile, t) for t in P.TIERS})
    check(got == want, "every shipped tier loads; a missing, malformed or mistyped profile fails closed with "
          "its code", repr({k: v for k, v in got.items() if want[k] != v}))
    msg = code_of(P.load_profile, "64gb", box("64gb", GOOD + 'secret_token = "abc123SECRET"\n'), message=True)
    check(msg != "OK" and "abc123SECRET" not in msg, "a profile error carries a code, never profile values", msg)

    # One resolver once answered both, so pointing policy at the checkout committed generated files.
    S.section("ROOTS")
    with env_set(AILOCAL_CONFIG="/tmp/ailocal-policy-probe", AILOCAL_CLIENTS=None):
        check("ailocal-policy-probe" not in str(P.deployed_client_root()) and P.state_root() != P.config_root(),
              "AILOCAL_CONFIG moves neither generated client output nor state")


def client_policy() -> None:
    S.section("CLIENT POLICY FAILS CLOSED")
    cases = {"unknown client": '[nosuchclient]\ndefault = "fast"\n',
             "non-TOML": "bogus:\n  default: fast\n",
             "duplicate section": '[claude]\na = "fast"\n[claude]\nb = "fast"\n',
             "duplicate key": '[claude]\ndefault = "fast"\ndefault = "primary"\n',
             "unclosed list": '[continue]\nchat = ["a", "b"\n',
             "unclosed mapping": '[claude]\nslots = {opus = "fast"\n',
             "bare dotted key": '[compat]\ngpt-5.5 = "primary"\n'}   # nests instead of naming
    got = {n: code_of(P.load_client_policy, box(text=t, name="profiles/clients.toml")) for n, t in cases.items()}
    got["missing"] = code_of(P.load_client_policy, box())
    want = {**{n: P.CLIENT_POLICY_INVALID for n in cases}, "missing": P.CLIENT_POLICY_MISSING}
    check(got == want, "a missing or malformed clients.toml fails closed with its code",
          repr({k: v for k, v in got.items() if want[k] != v}))


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
    launch = P.load_client_policy(RESOURCES).get("claude", {}).get("launch_default", "primary")

    # Claude Code 2.1.224 defaults to 32000 output; a mismatch looped on max_tokens for ~27 min.
    got, want = {}, {}
    for tier, g in runs.items():
        cenv = json.loads((g["root"] / "clients/claude/settings.json").read_text()).get("env", {})
        role = P.resolve_role(tier, launch, RESOURCES)
        got[tier] = (cenv.get("CLAUDE_CODE_MAX_OUTPUT_TOKENS"), cenv.get("CLAUDE_CODE_MAX_CONTEXT_TOKENS"))
        want[tier] = (str(role["max_output"]), str(role["context_input"]))
    models, clients = G.load_models_yaml(RESOURCES / "profiles/64gb.toml"), G.load_clients_yaml()
    models[launch] = {k: v for k, v in models[launch].items() if k != "max_output"}
    try:
        G.regen_claude_settings(models, clients)
        rejected = "returned"
    except SystemExit as exc:
        rejected = str(exc)
    check(got == want and "max_output" in rejected,
          f"Claude's output and window are {launch}'s max_output and context_input; a launch role without "
          "max_output is rejected rather than left to Claude's 32000", f"{got} vs {want}; {rejected}")

    bad = []
    for tier, g in runs.items():
        litellm = (g["root"] / "state/litellm/config.yaml").read_text()
        finite = 0
        for blk in litellm.split("  - model_name: ")[1:]:
            ctx, npr, mit, mot = (yaml_int(blk, k) for k in
                                  ("num_ctx", "num_predict", "max_input_tokens", "max_output_tokens"))
            if None not in (ctx, npr, mit) and npr > 0:
                finite += 1
                if mit > ctx - npr or (mot is not None and mot != npr):
                    bad.append(f"{tier}:{blk.split(chr(10))[0].strip()}")
        if finite < 3:
            bad.append(f"{tier}: only {finite} finite aliases")
    check(not bad, "the proxy admits at most num_ctx - num_predict and advertises the output it enforces",
          ", ".join(bad))

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
    with env_set(AILOCAL_CONFIG=cfg, AILOCAL_STATE=state):
        e.ensure()
        gen, user = e.parse(state / "env"), e.parse(cfg / ".env.local")
        before = ((state / "env").read_text(), (cfg / ".env.local").read_text())
        e.ensure(), e.ensure()
        check(sorted(gen) == sorted(e.GENERATED) and all(gen[k] for k in e.GENERATED)
              and all(user.get(k) == ("false" if k == "ENABLE_CLOUD" else "") for k in e.USER_ALWAYS)
              and mode(state / "env") == mode(cfg / ".env.local") == 0o600
              and before == ((state / "env").read_text(), (cfg / ".env.local").read_text())
              and e.value("LITELLM_MASTER_KEY") == gen["LITELLM_MASTER_KEY"],
              "generated secrets live under state and user keys under config, both 0600; ensure() never rotates")
        loopback = e.bind() == e.BIND_LOCAL
        e.set_ollama_patch("")
        e.set_bind(e.BIND_LAN)
        e.set_bind("10.0.0.5")
        user = e.parse(cfg / ".env.local")
        check(loopback and e.bind() == "10.0.0.5" and user["AILOCAL_BIND"] == "10.0.0.5"
              and user.get("AILOCAL_OLLAMA_PATCH") == "" and all(k in user for k in e.USER_ALWAYS)
              and mode(cfg / ".env.local") == 0o600,
              "the proxy binds loopback unless told otherwise; set_bind keeps every other override (#103)")


def main() -> int:
    try:
        for section in (resolver, client_policy, generated, env_ownership):
            try:
                section()
            except Exception as exc:  # noqa: BLE001
                S.error(section.__name__, exc)
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    return S.report()


if __name__ == "__main__":
    sys.exit(main())
