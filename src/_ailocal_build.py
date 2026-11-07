"""PEP 517 backend shim: never package a stale build tree.

setuptools copies src/ into build/lib and never prunes deleted files, so a
stale tree un-deletes them in the wheel; clearing it makes the wheel a function
of the source tree alone.
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
