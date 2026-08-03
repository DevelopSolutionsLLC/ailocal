#!/usr/bin/env python3
"""profile-config.py — one fail-closed profile resolver, and no second parser.

The failure this guards against is not hypothetical. Profile parsing had four
independent implementations, and every shell entry point shared this shape:

    _TIER="$(cat config/active-profile 2>/dev/null || echo 64gb)"

A missing, empty or unreadable marker silently selected the 64 GB tier — on a
32 GB machine that installs models that do not fit, with no error anywhere. It
is the same shape as the defect the planner benchmark was built around.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO / "scripts" / "lib"))
import profile_config as P  # noqa: E402

failures: list[str] = []


def check(cond: object, label: str) -> None:
    print(f"  {'✓' if cond else '✗'} {label}")
    if not cond:
        failures.append(label)


def sandbox(marker=None, profile_text=None) -> Path:
    """A throwaway repo root. The REAL config/active-profile is never touched."""
    root = Path(tempfile.mkdtemp(prefix="pcfg-"))
    (root / "config" / "profiles").mkdir(parents=True)
    if marker is not None:
        (root / "config" / "active-profile").write_text(marker)
    if profile_text is not None:
        (root / "config" / "profiles" / "64gb.yaml").write_text(profile_text)
    return root


GOOD = "architecture:\n  role: Lead\n  active: m:1\n  context: 100\n"


def main() -> int:
    print("ALL PROFILES LOAD")
    for t in P.TIERS:
        prof = P.load_profile(t)
        roles = [r for r in P.ROLES if r in prof]
        check(len(roles) >= 4, f"{t} loads with {len(roles)} capability roles")
    check(P.resolve_active_tier() in P.TIERS,
          f"active tier resolves ({P.resolve_active_tier()})")

    print("\nFAIL CLOSED — NEVER DEFAULTS TO 64gb")
    cases = [
        ("missing marker", sandbox(), P.ACTIVE_PROFILE_MISSING, "tier"),
        ("empty marker", sandbox(marker="   \n"), P.ACTIVE_PROFILE_EMPTY, "tier"),
        ("unknown tier", sandbox(marker="999gb"), P.ACTIVE_PROFILE_INVALID, "tier"),
        ("missing profile file", sandbox(marker="64gb"), P.PROFILE_FILE_MISSING, "load"),
        ("malformed yaml", sandbox(marker="64gb", profile_text="  stray: 1\n"),
         P.PROFILE_YAML_INVALID, "load"),
        ("non-integer context",
         sandbox(marker="64gb",
                 profile_text="architecture:\n  role: L\n  active: m\n  context: x\n"),
         P.ROLE_CONFIG_INVALID, "load"),
        ("role missing required field",
         sandbox(marker="64gb", profile_text="architecture:\n  role: L\n  context: 10\n"),
         P.PROFILE_SCHEMA_INVALID, "load"),
    ]
    for label, root, expect, kind in cases:
        try:
            if kind == "tier":
                P.resolve_active_tier(root)
            else:
                P.load_profile("64gb", root)
            got = "NO ERROR"
        except P.ProfileError as e:
            got = e.code
        check(got == expect, f"{label} ⇒ {expect} (got {got})")
        shutil.rmtree(root, ignore_errors=True)

    root = sandbox(marker="64gb", profile_text=GOOD)
    try:
        P.resolve_role("64gb", "review", root)
        got = "NO ERROR"
    except P.ProfileError as e:
        got = e.code
    check(got == P.ROLE_MISSING, f"absent role ⇒ ROLE_MISSING (got {got})")
    check(P.resolve_role("64gb", "architecture", root)["model"] == "m:1",
          "a value containing a colon survives parsing")
    shutil.rmtree(root, ignore_errors=True)

    print("\nERRORS CARRY A CODE, NOT FILE CONTENTS")
    root = sandbox(marker="64gb",
                   profile_text=GOOD + "  secret_token: abc123SECRET\n")
    try:
        P.load_profile("64gb", root)
        msg = ""
    except P.ProfileError as e:
        msg = str(e)
    check("abc123SECRET" not in msg, "profile values never appear in an error string")
    shutil.rmtree(root, ignore_errors=True)

    print("\nNO SECOND PARSER, NO SILENT FALLBACK")
    shells = ["install-models.sh", "start.sh", "validate.sh", "doctor.sh"]
    for name in shells:
        src = (REPO / "scripts" / name).read_text()
        check("echo 64gb" not in src, f"{name} has no hardcoded 64gb fallback")
        check(not re.search(r"cat .*active-profile", src),
              f"{name} does not read active-profile directly")
        check(src.count('profile-config" active-tier') == 1,
              f"{name} resolves the tier exactly once")
    doctor = (REPO / "scripts" / "doctor.sh").read_text()
    check(not re.search(r"sed -n .*profiles/", doctor),
          "doctor.sh no longer parses profile YAML with sed")

    sync = (REPO / "scripts" / "sync-models.py").read_text()
    check("import profile_config" in sync, "sync-models uses the shared resolver")
    check('return "64gb"' not in sync, "sync-models no longer defaults to 64gb")
    bench = (REPO / "scripts" / "lib" / "benchmark.py").read_text()
    check("import profile_config" in bench, "benchmark uses the shared resolver")
    check("re.finditer" not in bench.split("def parse_profile")[1].split("def ")[0],
          "benchmark's parse_profile no longer parses YAML itself")
    check("effective_tiers" in bench,
          "benchmark reads the generated artifact (all tiers)")

    print("\nBENCHMARK AND PRODUCTION AGREE ON EVERY ROLE")
    import benchmark as B
    for t in P.TIERS:
        a = B.parse_profile(t)
        b = {r: {"active": P.resolve_role(t, r)["active"],
                 "context": P.resolve_role(t, r)["context"],
                 "enabled": P.resolve_role(t, r)["enabled"]}
             for r in P.ROLES if r in P.load_profile(t)}
        check(a == b, f"{t}: benchmark and resolver return identical role values")

    print("\nCLI IS USABLE FROM A SHELL AND FAILS NON-ZERO")
    cli = str(REPO / "scripts" / "profile-config")
    r = subprocess.run([cli, "active-tier"], capture_output=True, text=True)
    check(r.returncode == 0 and r.stdout.strip() in P.TIERS,
          f"active-tier prints a bare scalar ({r.stdout.strip()})")
    r = subprocess.run([cli, "role", "architecture", "--field", "model"],
                       capture_output=True, text=True)
    check(r.returncode == 0 and ":" in r.stdout,
          "role --field prints a bare scalar")
    r = subprocess.run([cli, "role", "embeddings", "--field", "top_p"],
                       capture_output=True, text=True)
    check(r.returncode == 0 and r.stdout.strip() == "",
          "an absent optional field prints empty, not the string 'None'")
    r = subprocess.run([cli, "role", "nosuchrole"], capture_output=True, text=True)
    check(r.returncode != 0 and P.ROLE_MISSING in r.stderr,
          "an unknown role exits non-zero with a code on stderr")
    r = subprocess.run([cli, "validate"], capture_output=True, text=True)
    check(r.returncode == 0, "validate passes for the current repository")



    print("\nPRODUCTION ADMISSION RESPECTS PHYSICAL GEOMETRY")
    import importlib.util as _il2
    _sp2 = _il2.spec_from_file_location("_sm2", REPO / "scripts" / "sync-models.py")
    _sm2 = _il2.module_from_spec(_sp2); _sp2.loader.exec_module(_sm2)

    # num_ctx holds prompt AND generation. Advertising the whole window as
    # admissible input is KNOWN_ISSUES #19: accepted by pre-call, then trimmed
    # from the FRONT by Ollama with HTTP 200 and no error.
    admit, note = _sm2.admission_for("architecture", 98304, 16384)
    check(admit == 81920 and not note, "finite reserve: 98304-16384 = 81920")
    admit, note = _sm2.admission_for("review", 24576, 4096)
    check(admit == 20480, "finite reserve: 24576-4096 = 20480")

    # -1 is Ollama INFINITE: no arithmetic is possible, so none is invented.
    admit, note = _sm2.admission_for("implementation", 24576, -1)
    check(admit == 24576 and note == _sm2.OUTPUT_RESERVE_UNBOUNDED,
          "num_predict -1 preserves num_ctx and is MARKED unsafe, not silently reserved")

    # Invalid geometry must fail generation, never clamp.
    for ctx, np_, label in ((4096, 4096, "reserve == window"),
                            (4096, 8192, "reserve > window"),
                            (0, 128, "zero window")):
        try:
            _sm2.admission_for("x", ctx, np_); raised = False
        except SystemExit:
            raised = True
        check(raised, f"invalid geometry rejected, not clamped ({label})")

    # Every generated finite-output alias must satisfy the invariant.
    import re as _re
    cfg = (REPO / "config" / "litellm" / "config.yaml").read_text()
    checked = 0
    for blk in cfg.split("  - model_name: ")[1:]:
        name = blk.split("\n")[0].strip()
        g = lambda k: (int(m.group(1)) if (m := _re.search(rf"{k}:\s*(-?\d+)", blk)) else None)
        ctx, np_, mit = g("num_ctx"), g("num_predict"), g("max_input_tokens")
        if ctx is None or mit is None or np_ is None or np_ <= 0:
            continue
        checked += 1
        check(mit <= ctx - np_,
              f"{name}: admits {mit} <= {ctx}-{np_} = {ctx-np_}")
    check(checked >= 3, f"invariant checked on {checked} finite-output aliases")

    print("\nGENERATED ARTIFACT IS AUTHORITATIVE AT RUNTIME")
    eff = P.load_effective()
    check(eff["schema_version"] in P.SUPPORTED_SCHEMA_VERSIONS,
          f"schema_version is supported ({eff['schema_version']})")
    for k in ("tier", "roles", "source_profile", "source_profile_sha256",
              "active_profile_sha256", "config_sha256", "generated_at"):
        check(k in eff, f"artifact records {k}")
    arch = eff["roles"]["architecture"]
    for k in ("model", "context", "num_predict", "reasoning", "temperature",
              "top_p", "top_k", "repeat_penalty", "keep_alive", "persona"):
        check(k in arch, f"role carries {k}")
    check(P.active_tier() == eff["tier"], "runtime tier comes from the artifact")


    print("\nALL TIERS COME FROM GENERATED DATA — NO RUNTIME YAML")
    tiers = P.effective_tiers()
    check(sorted(tiers) == sorted(P.TIERS), f"all four tiers normalized {sorted(tiers)}")
    for t in P.TIERS:
        blk = tiers[t]
        check(blk["source_profile_sha256"], f"{t} records its source hash")
        check(len(blk["roles"]) >= 4, f"{t} carries {len(blk['roles'])} roles")
    check(P.effective_role_for_tier("32gb", "architecture")["model"],
          "a non-active tier resolves from generated data")
    try:
        P.effective_role_for_tier("999gb", "architecture"); got = "NO ERROR"
    except P.ProfileError as e:
        got = e.code
    check(got == P.EFFECTIVE_PROFILE_SCHEMA_INVALID, "unknown tier fails closed")

    import benchmark as _B
    for t in P.TIERS:
        a = _B.parse_profile(t)
        b = {r: {"active": c["model"], "context": c["context"], "enabled": c["enabled"]}
             for r, c in tiers[t]["roles"].items()}
        check(a == b, f"{t}: benchmark cross-tier == generated data")
    bsrc = (REPO / "scripts" / "lib" / "benchmark.py").read_text()
    check("effective_tiers" in bsrc and "re.finditer" not in
          bsrc.split("def parse_profile")[1].split("def ")[0],
          "benchmark parse_profile reads generated data, parses no YAML")

    # Shell consumers must not parse YAML either.
    for name in ("start.sh", "doctor.sh", "install-models.sh"):
        src = (REPO / "scripts" / name).read_text()
        check("active:" not in src or "grep -E" not in src.split("active:")[0][-80:],
              f"{name} does not grep|sed profile YAML")
    check("effective-profile.json" in (REPO / "scripts" / "install-models.sh").read_text(),
          "install-models.sh consumes the generated artifact")

    print("\nSTALENESS AND CORRUPTION FAIL CLOSED")
    import shutil as _sh
    box = Path(tempfile.mkdtemp(prefix="eff-"))
    (box / "config" / "profiles").mkdir(parents=True)
    _sh.copy(REPO / "config" / "effective-profile.json", box / "config")
    _sh.copy(REPO / "config" / "active-profile", box / "config")
    # ALL tiers: the artifact now normalizes every tier and validates every
    # source hash, so a fixture carrying only the active profile is incomplete.
    for _t in P.TIERS:
        _sh.copy(REPO / "config" / "profiles" / f"{_t}.yaml", box / "config" / "profiles")
    check(P.load_effective(box)["tier"] == eff["tier"], "a faithful copy validates")

    def expect(code, mutate, label):
        b = Path(tempfile.mkdtemp(prefix="eff-"))
        _sh.copytree(box / "config", b / "config")
        mutate(b)
        try:
            P.load_effective(b)
            got = "NO ERROR"
        except P.ProfileError as e:
            got = e.code
        check(got == code, f"{label} ⇒ {code} (got {got})")
        _sh.rmtree(b, ignore_errors=True)

    expect(P.EFFECTIVE_PROFILE_MISSING,
           lambda b: (b / "config" / "effective-profile.json").unlink(),
           "artifact deleted")
    expect(P.EFFECTIVE_PROFILE_STALE_TIER,
           lambda b: (b / "config" / "active-profile").write_text("32gb\n"),
           "active-profile changed")
    expect(P.EFFECTIVE_PROFILE_STALE_SOURCE,
           lambda b: (b / "config" / "profiles" / f"{eff['tier']}.yaml")
                     .write_text("architecture:\n  role: x\n  active: y\n  context: 1\n"),
           "source profile edited")

    def corrupt(b):
        f = b / "config" / "effective-profile.json"
        d = json.loads(f.read_text())
        d["config_sha256"] = "0" * 64
        f.write_text(json.dumps(d))
    expect(P.EFFECTIVE_PROFILE_HASH_INVALID, corrupt, "config hash corrupted")

    def bad_schema(b):
        f = b / "config" / "effective-profile.json"
        d = json.loads(f.read_text())
        d["schema_version"] = 99
        f.write_text(json.dumps(d))
    expect(P.EFFECTIVE_PROFILE_SCHEMA_INVALID, bad_schema, "unsupported schema")
    _sh.rmtree(box, ignore_errors=True)

    print("\nNO RUNTIME YAML FALLBACK, NO REDUNDANT WRAPPER")
    cli_src = (REPO / "scripts" / "profile-config").read_text()
    check("effective_role" in cli_src and "active_tier" in cli_src,
          "the CLI reads the artifact")
    check(not (REPO / "scripts" / "profile-json").exists(),
          "scripts/profile-json is deleted (jq is already a dependency)")
    check("_legacy_load_models_yaml" not in sync,
          "the dead legacy parser is deleted")
    # Behavioural, not textual: typed values must survive generation without a
    # string round-trip. A prose mention of "reserialize" is not the defect.
    import importlib.util as _il
    _sp = _il.spec_from_file_location("_sm", REPO / "scripts" / "sync-models.py")
    _sm = _il.module_from_spec(_sp)
    _sp.loader.exec_module(_sm)
    _models = _sm.load_models_yaml(REPO / "config" / "profiles" /
                                   f"{P.active_tier()}.yaml")
    _pref = _models["architecture"]["preferred"]
    check(isinstance(_pref, list),
          f"lists stay typed through generation (preferred is {type(_pref).__name__})")
    check(_sm.flow_list(_pref) is _pref,
          "flow_list is idempotent — no list→string→list round-trip")
    check(_sm.flow_list("[a, b]") == ["a", "b"],
          "flow_list still parses raw strings for config/clients.yaml")
    check("jq -r" in (REPO / "scripts" / "doctor.sh").read_text(),
          "doctor.sh uses jq, not a bespoke extractor")

    print("\nGENERATION IS ATOMIC AND INSTALL FAILS CLOSED")
    check("flush_stage" in sync and "os.replace" in sync,
          "outputs are staged and swapped atomically")
    inst = (REPO / "scripts" / "install.sh").read_text()
    check("sync-models.py\" || true" not in inst and "|| true" not in
          inst.split("Syncing model config")[1].split("step ")[0],
          "install.sh no longer swallows a generation failure")
    check("stopping before any model is pulled" in inst,
          "install.sh stops before install-models.sh on generation failure")

    print("\nGENERATED FILES ARE OUTPUTS, NOT SOURCES")
    for gen in ("config/capabilities.generated.json", "config/integration-contract.json"):
        check((REPO / gen).exists(), f"{gen} exists")
    caps = json.loads((REPO / "config/capabilities.generated.json").read_text())
    tier = P.resolve_active_tier()
    arch = P.resolve_role(tier, "architecture")
    blob = json.dumps(caps)
    check(arch["model"] in blob,
          "the generated capability file reflects the resolved profile model")

    print()
    if failures:
        print(f"PROFILE CONFIG: {len(failures)} FAILED")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("PROFILE CONFIG: all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
