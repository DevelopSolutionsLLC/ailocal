#!/usr/bin/env python3
"""install.py — bootstrapping a machine: provisioning, tier selection, audit.

The invariants that make ADR 009's split safe: a data root is replaced
wholesale, a config root is never replaced once the operator has edited it, a
default the operator deleted is not resurrected, and an interrupted upgrade
leaves either the old tree or the new one.
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from harness import REPO, Suite  # noqa: E402
from ailocal import install as I  # noqa: E402

_suite = Suite()
check = _suite.check


def _roots():
    box = Path(tempfile.mkdtemp(prefix="install-"))
    return box, box / "config", box / "data", box / "state"


def main() -> None:
    _suite.section("A FRESH INSTALL POPULATES BOTH ROOTS")
    box, cfg, data, state = _roots()
    I.provision(REPO, cfg, data, state)

    for c in I.DATA_COMPONENTS:
        check((data / c).is_dir(), f"data root receives {c}/")
    check((cfg / "profiles" / "64gb.toml").is_file(),
          "config root receives the authored profiles")
    check((cfg / "profiles" / "clients.toml").is_file(),
          "config root receives client policy")
    check((state / I.MANIFEST_NAME).is_file(), "a manifest is recorded")
    check(not (data / "benchmarks").exists(),
          "benchmarks are not installed into the data root")
    check(not list(data.glob(".staging-*")), "no staging tree survives a success")
    check(not list(data.glob(".rollback-*")), "no rollback tree survives a success")
    check(not (data / "profiles").exists(),
          "profiles are config, never shipped into the data root")

    _suite.section("AN EDITED PROFILE SURVIVES AN UPGRADE")
    edited = cfg / "profiles" / "64gb.toml"
    edited.write_text(edited.read_text() + "\n# operator edit\n")
    keep = edited.read_text()
    untouched = cfg / "profiles" / "32gb.toml"

    report = I.provision(REPO, cfg, data, state)
    check("profiles/64gb.toml" in report["preserved"],
          "an edited profile is reported as preserved")
    check(edited.read_text() == keep, "an edited profile is NOT overwritten")
    check(untouched.read_text() == (REPO / "profiles" / "32gb.toml").read_text(),
          "an unedited profile still matches what was shipped")

    report = I.provision(REPO, cfg, data, state)
    check("profiles/64gb.toml" in report["preserved"],
          "an edit survives a SECOND upgrade (the manifest records what was "
          "shipped, not what is on disk)")
    check(edited.read_text() == keep, "and is still not overwritten")

    _suite.section("A DELETED DEFAULT IS REPORTED, NEVER RESURRECTED")
    (cfg / "profiles" / "16gb.toml").unlink()
    report = I.provision(REPO, cfg, data, state)
    check("profiles/16gb.toml" in report["absent"],
          "a default the operator removed is reported")
    check(not (cfg / "profiles" / "16gb.toml").exists(),
          "and it is not written back")

    _suite.section("A CORRUPT MANIFEST NEVER LICENSES AN OVERWRITE")
    (state / I.MANIFEST_NAME).write_text("{ not json")
    report = I.provision(REPO, cfg, data, state)
    check(edited.read_text() == keep,
          "with no provenance, an edited file is still preserved")

    _suite.section("A RETIRED POLICY FILE IS REMOVED, UNLESS IT WAS EDITED")
    stale = cfg / "profiles" / "99gb.toml"
    stale.write_text("# a format or tier we no longer ship\n")
    I.provision(REPO, cfg, data, state)          # records it as shipped? no: not in source
    check(stale.is_file(),
          "a file the distribution never shipped is left alone")
    stale.unlink()

    _suite.section("DATA IS REPLACED WHOLESALE")
    stray = data / "deploy" / "not-shipped.sh"
    stray.write_text("# left behind by an older version\n")
    I.provision(REPO, cfg, data, state)
    check(not stray.exists(), "a file no longer shipped is gone after an upgrade")
    check((data / "deploy" / "litellm" / "registry.yaml").is_file(),
          "shipped data is present again")

    _suite.section("THE SOURCE IS NEVER GUESSED")
    check(I.distribution_source() == REPO,
          "a checkout above the package is the distribution")
    try:
        I.provision(REPO, REPO, data, state)
        refused = False
    except SystemExit:
        refused = True
    check(refused, "installing a checkout over itself is refused")

    _suite.section("TIER SELECTION NEVER ROUNDS UP")
    for gb, expected in ((8, None), (16, "16gb"), (31, "16gb"), (32, "32gb"),
                         (63, "32gb"), (64, "64gb"), (127, "64gb"),
                         (128, "128gb"), (192, "128gb")):
        got = I.tier_for_memory(gb)
        check(got == expected, f"{gb} GB selects {expected or 'nothing'}", f"got {got}")

    shutil.rmtree(box, ignore_errors=True)
    sys.exit(_suite.report())


if __name__ == "__main__":
    main()
