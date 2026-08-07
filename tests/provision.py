#!/usr/bin/env python3
"""provision.py — installing authored assets into the config and data roots.

The invariants that make ADR 009's split safe: a data root is replaced
wholesale, a config root is never replaced once the operator has edited it, and
an interrupted upgrade leaves either the old tree or the new one.
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from harness import REPO, Suite  # noqa: E402
from ailocal import provision as prov  # noqa: E402

_suite = Suite()
check = _suite.check


def _roots():
    box = Path(tempfile.mkdtemp(prefix="prov-"))
    return box, box / "config", box / "data", box / "state"


def main() -> None:
    _suite.section("A FRESH INSTALL POPULATES BOTH ROOTS")
    box, cfg, data, state = _roots()
    report = prov.provision(REPO, cfg, data, state)

    for c in prov.DATA_COMPONENTS:
        check((data / c).is_dir(), f"data root receives {c}/")
    check((cfg / "profiles" / "64gb.yaml").is_file(),
          "config root receives the authored profiles")
    check((cfg / "profiles" / "clients.yaml").is_file(),
          "config root receives client policy")
    check((state / prov.MANIFEST_NAME).is_file(), "a manifest is recorded")
    check(not list(data.glob(".staging-*")), "no staging tree survives a success")
    check(not list(data.glob(".rollback-*")), "no rollback tree survives a success")
    # Authored policy must not also be shipped as replaceable data, or an
    # upgrade would overwrite the operator's profiles through the back door.
    check(not (data / "profiles").exists(),
          "profiles are config, never shipped into the data root")

    _suite.section("AN EDITED PROFILE SURVIVES AN UPGRADE")
    edited = cfg / "profiles" / "64gb.yaml"
    edited.write_text(edited.read_text() + "\n# operator edit\n")
    keep = edited.read_text()
    untouched = cfg / "profiles" / "32gb.yaml"
    untouched.write_text("# shipped\n")          # diverges, then is restored below
    shutil.copy(REPO / "profiles" / "32gb.yaml", untouched)   # back to shipped bytes

    report = prov.provision(REPO, cfg, data, state)
    check("profiles/64gb.yaml" in report["preserved"],
          "an edited profile is reported as preserved")
    check(edited.read_text() == keep, "an edited profile is NOT overwritten")
    check(untouched.read_text() == (REPO / "profiles" / "32gb.yaml").read_text(),
          "an unedited profile still matches what was shipped")

    _suite.section("PROVENANCE, NOT LOCATION, DECIDES")
    manifest = prov.load_manifest(state)
    check(manifest.get("config", {}).get("profiles/64gb.yaml"),
          "the manifest keeps the digest of a preserved file")
    check("profiles/64gb.yaml" in prov.user_edited(cfg, manifest),
          "an edited file is detected by digest")
    check("profiles/32gb.yaml" not in prov.user_edited(cfg, manifest),
          "an unedited file is not reported as edited")
    # A corrupt manifest must not license overwriting policy.
    (state / prov.MANIFEST_NAME).write_text("{ not json")
    check(prov.load_manifest(state) == {},
          "a corrupt manifest reads as empty rather than raising")

    _suite.section("A NEW SHIPPED DEFAULT IS REPORTED, NEVER INJECTED")
    (cfg / "profiles" / "16gb.yaml").unlink()
    absent = prov.missing_defaults(REPO, cfg)
    check("profiles/16gb.yaml" in absent,
          "a shipped file absent from config is reported")

    _suite.section("DATA IS REPLACED WHOLESALE")
    stray = data / "lib" / "not-shipped.sh"
    stray.write_text("# left behind by an older version\n")
    prov.provision(REPO, cfg, data, state)
    check(not stray.exists(), "a file no longer shipped is gone after an upgrade")
    check((data / "lib" / "policy.py").is_file(), "shipped data is present again")

    shutil.rmtree(box, ignore_errors=True)
    sys.exit(_suite.report())


if __name__ == "__main__":
    main()
