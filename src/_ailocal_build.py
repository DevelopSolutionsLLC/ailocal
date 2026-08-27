"""PEP 517 backend shim: never package a stale build tree.

setuptools' build_py COPIES src/ into build/lib and never removes a file that
has since been deleted from source, so the next wheel keeps shipping it. That is
not theoretical: it was reproduced in the sibling Cadence checkout, where a
deleted hook still arrived in site-packages through `pipx install --force .`
because the wheel was built from a build/lib that still held it. ailocal builds
the same way, so it carries the same hazard. A stale build tree silently
un-deletes files, and the installed package stops matching the checkout.

Clearing build/lib before each build makes the wheel a function of the source
tree alone. It costs one directory copy per build and removes a whole class of
release-correctness bug, which is a trade worth making. This is a lifecycle fix,
not a documented cleanup ritual: nobody has to remember it.
"""
from __future__ import annotations

import shutil
from pathlib import Path

from setuptools.build_meta import *  # noqa: F401,F403  (the hooks we do not override)
from setuptools import build_meta as _setuptools

_BUILD_LIB = Path(__file__).resolve().parent.parent / "build" / "lib"


def _drop_stale_tree() -> None:
    shutil.rmtree(_BUILD_LIB, ignore_errors=True)


def build_wheel(wheel_directory, config_settings=None, metadata_directory=None):
    _drop_stale_tree()
    return _setuptools.build_wheel(wheel_directory, config_settings,
                                   metadata_directory)


def build_editable(wheel_directory, config_settings=None, metadata_directory=None):
    _drop_stale_tree()
    return _setuptools.build_editable(wheel_directory, config_settings,
                                      metadata_directory)


def build_sdist(sdist_directory, config_settings=None):
    _drop_stale_tree()
    return _setuptools.build_sdist(sdist_directory, config_settings)
