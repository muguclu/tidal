"""Smoke test: the package installs, imports and reports the version from pyproject."""

import re

import tidal


def test_package_version_is_semver() -> None:
    assert re.fullmatch(r"\d+\.\d+\.\d+", tidal.__version__)
