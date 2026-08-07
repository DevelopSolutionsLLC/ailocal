"""provision.py — install authored assets into the config and data roots.

ADR 009. The checkout is a development input; a running installation reads its
assets from user-owned XDG locations. This module is what puts them there.

Two rules give the split its meaning:

  data root   shipped assets with no supported edit surface. Replaced wholesale
              on upgrade, so nothing user-authored may live here.
  config root user-editable policy. NEVER replaced automatically unless its
              digest still matches what was shipped, which proves the file has
              not been touched.

Provenance decides, not location: `ailocal install` records the SHA-256 of every
file it wrote, and an upgrade compares against that record. A file that has
diverged is left alone and reported.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path

#: Installed assets. Order is irrelevant; each component swaps independently.
#: `benchmarks/` is deliberately absent: it is a developer utility that is not
#: part of install or update (ADR 009), so it keeps its checkout dependency.
DATA_COMPONENTS = ("lib", "deploy", "clients")
CONFIG_COMPONENTS = ("profiles",)

MANIFEST_NAME = "install-manifest.json"


def digest(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def _tree_digests(root: Path, component: str) -> dict:
    base = root / component
    if not base.is_dir():
        return {}
    return {str(p.relative_to(root)): digest(p)
            for p in sorted(base.rglob("*"))
            if p.is_file() and "__pycache__" not in p.parts}


def load_manifest(state: Path) -> dict:
    p = state / MANIFEST_NAME
    if not p.is_file():
        return {}
    try:
        return json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        # A corrupt manifest must not license overwriting user policy: with no
        # provenance, every config file is treated as edited.
        return {}


def user_edited(config: Path, manifest: dict) -> list[str]:
    """Config files that no longer match what was installed."""
    out = []
    for rel, want in sorted(manifest.get("config", {}).items()):
        p = config / rel
        if p.is_file() and digest(p) != want:
            out.append(rel)
    return out


def _stage(source: Path, dest_root: Path, components) -> Path:
    staging = dest_root / f".staging-{os.getpid()}"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    for c in components:
        src = source / c
        if src.is_dir():
            shutil.copytree(src, staging / c,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    return staging


def _swap(staging: Path, dest_root: Path, components) -> None:
    """Replace each component atomically, keeping the previous tree for rollback.

    Per component rather than per file: a half-replaced deploy/ tree is a new
    compose file against old hooks, which is the failure this exists to prevent.
    """
    done = []
    try:
        for c in components:
            new = staging / c
            if not new.is_dir():
                continue
            live, old = dest_root / c, dest_root / f".rollback-{c}"
            if old.exists():
                shutil.rmtree(old)
            if live.exists():
                live.rename(old)
            new.rename(live)
            done.append((live, old))
    except OSError:
        for live, old in reversed(done):
            if live.exists():
                shutil.rmtree(live)
            if old.exists():
                old.rename(live)
        raise
    for _, old in done:
        if old.exists():
            shutil.rmtree(old)


def provision(source: Path, config: Path, data: Path, state: Path) -> dict:
    """Install assets. Returns a report; raises rather than half-applying.

    Data components are replaced wholesale. Config components are installed only
    where absent or still byte-identical to what was shipped.
    """
    source, config, data, state = (Path(source), Path(config),
                                   Path(data), Path(state))
    for p in (config, data, state):
        p.mkdir(parents=True, exist_ok=True)

    manifest = load_manifest(state)
    preserved = user_edited(config, manifest)

    staging = _stage(source, data, DATA_COMPONENTS)
    missing = [c for c in DATA_COMPONENTS
               if (source / c).is_dir() and not (staging / c).is_dir()]
    if missing:
        shutil.rmtree(staging, ignore_errors=True)
        raise RuntimeError(f"staging incomplete: {missing}")
    _swap(staging, data, DATA_COMPONENTS)
    shutil.rmtree(staging, ignore_errors=True)

    # Config: never clobber an edited file. A new shipped file still arrives;
    # only divergence is protected, so a fresh install is a plain copy.
    installed = []
    for c in CONFIG_COMPONENTS:
        src = source / c
        if not src.is_dir():
            continue
        for p in sorted(src.rglob("*")):
            if not p.is_file() or "__pycache__" in p.parts:
                continue
            rel = p.relative_to(source)
            if str(rel) in preserved:
                continue
            target = config / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.is_file():
                backup = state / "backups" / str(rel)
                backup.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(target, backup)
            shutil.copy2(p, target)
            installed.append(str(rel))

    record = {
        "source": str(source),
        "data": {c: _tree_digests(data, c) for c in DATA_COMPONENTS},
        "config": {r: digest(config / r) for r in installed}
        | {r: manifest.get("config", {})[r] for r in preserved
           if r in manifest.get("config", {})},
    }
    (state / MANIFEST_NAME).write_text(json.dumps(record, indent=2, sort_keys=True))
    return {"installed": installed, "preserved": preserved,
            "data_components": [c for c in DATA_COMPONENTS if (data / c).is_dir()]}


def missing_defaults(source: Path, config: Path) -> list[str]:
    """Shipped config keys absent from an installed (possibly edited) file.

    New defaults never arrive by silent replacement -- an edited policy file is
    the operator's, and injecting keys into it is how a machine ends up running
    something nobody wrote. They are reported instead.
    """
    out = []
    for c in CONFIG_COMPONENTS:
        src = source / c
        if not src.is_dir():
            continue
        for p in sorted(src.rglob("*")):
            if p.is_file() and not (config / p.relative_to(source)).exists():
                out.append(str(p.relative_to(source)))
    return out
