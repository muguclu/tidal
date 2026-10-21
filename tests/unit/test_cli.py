"""Root CLI registers available groups and skips groups whose task has not landed yet."""

import sys
import types

import pytest
import typer
from typer.testing import CliRunner

from tidal.cli import build_app


@pytest.fixture
def fake_group(monkeypatch: pytest.MonkeyPatch) -> str:
    module = types.ModuleType("tidal_fake_group")
    group = typer.Typer()

    @group.command()
    def hello() -> None:
        typer.echo("hi")

    module.app = group  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "tidal_fake_group", module)
    return "tidal_fake_group:app"


def test_available_group_is_registered_and_missing_one_skipped(fake_group: str) -> None:
    app = build_app({"fake": fake_group, "later": "tidal.not_landed_yet:app"})
    result = CliRunner().invoke(app, ["fake", "hello"])
    assert result.exit_code == 0
    assert result.output.strip() == "hi"
    assert CliRunner().invoke(app, ["later"]).exit_code != 0


def test_missing_dependency_inside_existing_module_is_not_hidden(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken_import(name: str) -> types.ModuleType:
        raise ModuleNotFoundError("No module named 'polarz'", name="polarz")

    monkeypatch.setattr("tidal.cli.importlib.import_module", broken_import)
    with pytest.raises(ModuleNotFoundError):
        build_app({"x": "tidal.exists:app"})


def test_non_typer_attribute_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    module = types.ModuleType("tidal_not_typer")
    module.app = object()  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "tidal_not_typer", module)
    with pytest.raises(TypeError):
        build_app({"x": "tidal_not_typer:app"})
