"""Every console script in pyproject points at an importable callable (images run them as-is)."""

import importlib
import importlib.util
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS: dict[str, str] = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))[
    "project"
]["scripts"]
# Entry points whose module lands in a later task; remove the entry when that task merges.
PENDING: dict[str, str] = {}


def _importable(module: str) -> bool:
    try:
        return importlib.util.find_spec(module) is not None
    except ModuleNotFoundError:  # a parent package is missing too
        return False


@pytest.mark.parametrize("name", sorted(SCRIPTS))
def test_console_script_target_is_callable(name: str) -> None:
    module, _, attr = SCRIPTS[name].partition(":")
    if name in PENDING and not _importable(module):
        pytest.skip(f"{module} arrives with {PENDING[name]}")
    assert callable(getattr(importlib.import_module(module), attr))
