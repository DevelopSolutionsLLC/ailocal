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
