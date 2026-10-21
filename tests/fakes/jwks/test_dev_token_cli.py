"""Smoke tests for scripts/dev-token.py via typer's CliRunner."""

import importlib.util
import json
from pathlib import Path
from types import ModuleType

import jwt
import pytest
from typer.testing import CliRunner

from fakes.jwks.core import DEFAULT_AUDIENCE, issuer_for

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "dev-token.py"


@pytest.fixture(scope="module")
def script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("dev_token_script", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_mints_token_and_caches_keys(
    script: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TIDAL_ENV", "local")
    keys = tmp_path / "keys"
    jwks_out = tmp_path / "out" / "jwks.json"
    runner = CliRunner()
    args = ["--role", "Tidal.Quant", "--keys-dir", str(keys), "--print-jwks", str(jwks_out)]
    first = runner.invoke(script.app, args)
    second = runner.invoke(script.app, args)
    assert first.exit_code == 0, first.output
    assert second.exit_code == 0

    published = json.loads(jwks_out.read_text(encoding="utf-8"))
    assert published == json.loads((keys / "jwks.json").read_text(encoding="utf-8"))
    key = jwt.PyJWKSet.from_dict(published).keys[0].key
    for out in (first.stdout, second.stdout):
        claims = jwt.decode(
            out.strip(),
            key,
            algorithms=["RS256"],
            audience=DEFAULT_AUDIENCE,
            issuer=issuer_for(),
        )
        assert claims["roles"] == ["Tidal.Quant"]
    assert "PRIVATE" not in first.output
    assert "PRIVATE" not in jwks_out.read_text(encoding="utf-8")


def test_unset_env_allowed(
    script: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("TIDAL_ENV", raising=False)
    result = CliRunner().invoke(
        script.app, ["--role", "Tidal.Reader", "--keys-dir", str(tmp_path), "--ttl", "60"]
    )
    assert result.exit_code == 0


@pytest.mark.parametrize("env", ["dev", "prod", ""])
def test_refuses_non_local_env(
    script: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, env: str
) -> None:
    monkeypatch.setenv("TIDAL_ENV", env)
    result = CliRunner().invoke(
        script.app, ["--role", "Tidal.Quant", "--keys-dir", str(tmp_path / "k")]
    )
    assert result.exit_code == 2
    assert not (tmp_path / "k").exists()


def test_rejects_unknown_role(
    script: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TIDAL_ENV", "local")
    result = CliRunner().invoke(
        script.app, ["--role", "Tidal.Nope", "--keys-dir", str(tmp_path / "k")]
    )
    assert result.exit_code == 2
