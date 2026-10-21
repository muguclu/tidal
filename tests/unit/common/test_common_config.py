"""Settings vs docs/contracts/config-contract.md: keys, types, defaults, secrets, startup rules."""

import re
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from pydantic import SecretStr, ValidationError

from tidal.common.config import AZURITE_ACCOUNT_URL, Settings, allowed_env_keys, get_settings
from tidal.common.errors import ConfigError

CONTRACT = Path(__file__).resolve().parents[3] / "docs" / "contracts" / "config-contract.md"
ROW = re.compile(r"^\| `(TIDAL_[A-Z_]+)` \| (.+?) \| (.+?) \| (.+?) \|")
TENANT = "6f1d2c3b-4a5e-4f60-8a7b-9c0d1e2f3a4b"


def _contract_rows() -> list[tuple[str, str, str, bool]]:
    rows = []
    for line in CONTRACT.read_text(encoding="utf-8").splitlines():
        if m := ROW.match(line):
            key, typ, default, secret = m.groups()
            typ = typ.replace("\\|", "|").strip("`")
            rows.append((key, typ, default.strip("`"), "yes" in secret))
    return rows


ROWS = _contract_rows()


def _get(settings: Settings, key: str) -> Any:
    value: Any = settings
    for part in key.removeprefix("TIDAL_").lower().split("__"):
        value = getattr(value, part)
    return value


def _sample(key: str, typ: str) -> tuple[str, Any]:
    """Env string for ``key`` plus the expected parsed value (non-default where possible)."""
    overrides = {"TIDAL_ENV": ("local", "local"), "TIDAL_LOG_LEVEL": ("debug", "DEBUG")}
    if key in overrides:
        return overrides[key]
    if "|" in typ:
        choice = typ.rsplit("|", maxsplit=1)[-1]
        return choice, choice
    samples: dict[str, tuple[str, Any]] = {
        "str": (f"v-{key.lower()}", f"v-{key.lower()}"),
        "url": ("https://example.test/x", "https://example.test/x"),
        "bool": ("true", True),
        "secret": (f"s3cr3t-{key}", f"s3cr3t-{key}"),
        "uuid": (TENANT, UUID(TENANT)),
        "int": ("7", 7),
        "date": ("2010-02-03", date(2010, 2, 3)),
        "datetime": ("2025-12-31T23:59:59Z", datetime(2025, 12, 31, 23, 59, 59, tzinfo=UTC)),
    }
    return samples[typ]


def test_contract_table_parsed() -> None:
    assert len(ROWS) == 27


def test_code_keys_equal_contract_keys() -> None:
    assert allowed_env_keys() == {key for key, *_ in ROWS}


@pytest.mark.parametrize(("key", "typ", "default", "secret"), ROWS, ids=[r[0] for r in ROWS])
def test_default_matches_contract(key: str, typ: str, default: str, secret: bool) -> None:
    value = _get(Settings.from_env({}), key)
    if default == "—":
        assert value is None
    elif default == "Azurite URL":
        assert value == AZURITE_ACCOUNT_URL
    else:
        assert str(value).lower() == default.lower()
    assert secret == (typ == "secret")


def test_every_contract_key_parsed_from_env() -> None:
    environ = {key: _sample(key, typ)[0] for key, typ, *_ in ROWS}
    settings = Settings.from_env(environ)
    for key, typ, _default, secret in ROWS:
        value = _get(settings, key)
        if secret:
            assert isinstance(value, SecretStr), key
            value = value.get_secret_value()
        assert value == _sample(key, typ)[1], key


def test_keys_are_case_insensitive() -> None:
    assert Settings.from_env({"tidal_sql__database": "lower"}).sql.database == "lower"


def test_bare_settings_reads_process_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TIDAL_SQL__DATABASE", "fromenv")
    assert Settings().sql.database == "fromenv"


def test_get_settings_is_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    get_settings.cache_clear()
    monkeypatch.setenv("TIDAL_HOT_WINDOW_DAYS", "30")
    try:
        assert get_settings() is get_settings()
        assert get_settings().hot_window_days == 30
    finally:
        get_settings.cache_clear()


def test_azure_client_id_is_ignored() -> None:
    environ = {"AZURE_CLIENT_ID": "abc", "PATH": "x"}
    assert Settings.from_env(environ).env == "local"


@pytest.mark.parametrize(
    "key", ["TIDAL_SQL_PASSWORD", "TIDAL_STORAGE__ACOUNT_URL", "tidal_typo", "TIDAL_SQL"]
)
def test_unknown_tidal_key_fails(key: str) -> None:
    with pytest.raises(ConfigError, match="unknown configuration keys"):
        Settings.from_env({key: "x"})


def test_unknown_key_fails_for_bare_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TIDAL_LOGLEVEL", "INFO")
    with pytest.raises(ConfigError):
        Settings()


def test_bare_settings_validation_hides_input(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TIDAL_ENV", "prod")
    monkeypatch.setenv("TIDAL_SQL__PASSWORD", "hunter2")
    with pytest.raises(ValidationError) as exc_info:
        Settings()
    assert "hunter2" not in str(exc_info.value)


@pytest.mark.parametrize("env", ["dev", "prod"])
def test_auth_disabled_outside_local_fails(env: str) -> None:
    with pytest.raises(ConfigError, match="TIDAL_AUTH__DISABLED"):
        Settings.from_env({"TIDAL_ENV": env, "TIDAL_AUTH__DISABLED": "true"})


def test_auth_disabled_allowed_locally() -> None:
    assert Settings.from_env({"TIDAL_AUTH__DISABLED": "true"}).auth.disabled is True


@pytest.mark.parametrize(
    "key",
    [
        "TIDAL_STORAGE__CONNECTION_STRING",
        "TIDAL_SERVICEBUS__CONNECTION_STRING",
        "TIDAL_SQL__PASSWORD",
        "TIDAL_CLOCK__NOW",
    ],
)
@pytest.mark.parametrize("env", ["dev", "prod"])
def test_local_only_keys_fail_outside_local(key: str, env: str) -> None:
    value = "2025-12-31T00:00:00Z" if key == "TIDAL_CLOCK__NOW" else "AccountKey=topsecret"
    with pytest.raises(ConfigError, match=key) as exc_info:
        Settings.from_env({"TIDAL_ENV": env, key: value})
    assert "topsecret" not in str(exc_info.value)


def test_local_only_keys_allowed_locally() -> None:
    environ = {
        "TIDAL_STORAGE__CONNECTION_STRING": "UseDevelopmentStorage=true",
        "TIDAL_SERVICEBUS__CONNECTION_STRING": "UseDevelopmentEmulator=true",
        "TIDAL_SQL__PASSWORD": "pw",
        "TIDAL_CLOCK__NOW": "2025-12-31T23:59:59Z",
    }
    assert Settings.from_env(environ).clock.now is not None


def test_otel_connection_string_allowed_in_azure() -> None:
    # The contract table sources it from a Key Vault reference in Azure (open question, T-005).
    environ = {"TIDAL_ENV": "prod", "TIDAL_OTEL__CONNECTION_STRING": "InstrumentationKey=k"}
    assert Settings.from_env(environ).otel.connection_string is not None


@pytest.mark.parametrize("server", [None, "", "localhost"])
def test_entra_mi_without_server_fails(server: str | None) -> None:
    environ = {"TIDAL_SQL__AUTH": "entra_mi"}
    if server is not None:
        environ["TIDAL_SQL__SERVER"] = server
    with pytest.raises(ConfigError, match="TIDAL_SQL__SERVER"):
        Settings.from_env(environ)


def test_entra_mi_with_server_ok() -> None:
    environ = {
        "TIDAL_ENV": "prod",
        "TIDAL_SQL__AUTH": "entra_mi",
        "TIDAL_SQL__SERVER": "sql-tidal.database.windows.net",
    }
    assert Settings.from_env(environ).sql.auth == "entra_mi"


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("TIDAL_ENV", "staging"),
        ("TIDAL_LOG_LEVEL", "LOUD"),
        ("TIDAL_STORAGE__ACCOUNT_URL", "not a url"),
        ("TIDAL_CLOCK__NOW", "2025-12-31T00:00:00"),  # naive datetimes are rejected
        ("TIDAL_SYNTH__SEED", "abc"),
    ],
)
def test_invalid_values_fail_without_echoing_input(key: str, value: str) -> None:
    with pytest.raises(ConfigError) as exc_info:
        Settings.from_env({key: value})
    assert value not in str(exc_info.value)


def test_secrets_never_revealed() -> None:
    secrets = [key for key, typ, *_ in ROWS if typ == "secret"]
    assert len(secrets) == 6
    settings = Settings.from_env({key: f"pw-{i}-hunter2" for i, key in enumerate(secrets)})
    rendered = [
        repr(settings),
        str(settings),
        str(settings.model_dump()),
        settings.model_dump_json(),
    ]
    for text in rendered:
        assert "hunter2" not in text
    assert settings.sql.password is not None
    assert settings.sql.password.get_secret_value().endswith("-hunter2")


def test_settings_are_frozen() -> None:
    settings = Settings.from_env({})
    with pytest.raises(ValidationError, match="frozen"):
        settings.env = "prod"  # type: ignore[misc]
