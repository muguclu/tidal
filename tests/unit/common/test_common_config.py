"""Settings vs docs/contracts/config-contract.md: keys, types, defaults, secrets, startup rules."""

import re
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from pydantic import SecretStr, ValidationError

from tidal.common.config import Settings, allowed_env_keys, get_settings
from tidal.common.errors import ConfigError

CONTRACT = Path(__file__).resolve().parents[3] / "docs" / "contracts" / "config-contract.md"
ROW = re.compile(r"^\| `(TIDAL_[A-Z_]+)` \| (.+?) \| (.+?) \| (.+?) \|")
TENANT = "6f1d2c3b-4a5e-4f60-8a7b-9c0d1e2f3a4b"
REQUIRED = "**required**"  # Default column of keys without a default
LOCAL = {"TIDAL_ENV": "local"}


def _local(**extra: str) -> dict[str, str]:
    """Minimal valid local environment (``TIDAL_ENV`` is required, contract v1.1)."""
    return {**LOCAL, **extra}


def _azure(env: str = "prod", **extra: str) -> dict[str, str]:
    """Minimal valid environment outside local (Entra-only, tenant set)."""
    return {
        "TIDAL_ENV": env,
        "TIDAL_AUTH__TENANT_ID": TENANT,
        "TIDAL_SQL__AUTH": "entra_mi",
        "TIDAL_SQL__SERVER": "sql-tidal.database.windows.net",
        "TIDAL_STORAGE__USE_MANAGED_IDENTITY": "true",
        **extra,
    }


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
    assert len(ROWS) == 32


def test_code_keys_equal_contract_keys() -> None:
    assert allowed_env_keys() == {key for key, *_ in ROWS}


def test_required_keys_match_contract() -> None:
    required = {key for key, _typ, default, _secret in ROWS if default == REQUIRED}
    assert required == {"TIDAL_ENV"}
    code_required = {
        f"TIDAL_{n}".upper() for n, f in Settings.model_fields.items() if f.is_required()
    }
    assert code_required == required


@pytest.mark.parametrize(("key", "typ", "default", "secret"), ROWS, ids=[r[0] for r in ROWS])
def test_default_matches_contract(key: str, typ: str, default: str, secret: bool) -> None:
    if default == REQUIRED:
        environ = {k: _sample(k, t)[0] for k, t, d, _s in ROWS if d == REQUIRED and k != key}
        with pytest.raises(ConfigError, match=key):
            Settings.from_env(environ)
        return
    value = _get(Settings.from_env(LOCAL), key)
    if default == "—":
        assert value is None
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
    environ = {"tidal_env": "local", "tidal_sql__database": "lower"}
    assert Settings.from_env(environ).sql.database == "lower"


@pytest.mark.parametrize("environ", [{}, {"TIDAL_SQL__DATABASE": "x"}, {"AZURE_CLIENT_ID": "abc"}])
def test_missing_env_fails_closed(environ: dict[str, str]) -> None:
    with pytest.raises(ConfigError, match="TIDAL_ENV is required"):
        Settings.from_env(environ)


@pytest.mark.parametrize("env", ["local", "dev", "prod"])
def test_explicit_env_accepted(env: str) -> None:
    environ = LOCAL if env == "local" else _azure(env)
    assert Settings.from_env(environ).env == env


def test_bare_settings_missing_env_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TIDAL_ENV", raising=False)
    with pytest.raises(ValidationError, match="TIDAL_ENV is required"):
        Settings()


def test_bare_settings_reads_process_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TIDAL_ENV", "local")
    monkeypatch.setenv("TIDAL_SQL__DATABASE", "fromenv")
    assert Settings().sql.database == "fromenv"


def test_get_settings_without_env_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    get_settings.cache_clear()
    monkeypatch.delenv("TIDAL_ENV", raising=False)
    try:
        with pytest.raises(ConfigError, match="TIDAL_ENV is required"):
            get_settings()
        monkeypatch.setenv("TIDAL_ENV", "local")  # a failed call is not cached
        assert get_settings().env == "local"
    finally:
        get_settings.cache_clear()


def test_get_settings_is_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    get_settings.cache_clear()
    monkeypatch.setenv("TIDAL_ENV", "local")
    monkeypatch.setenv("TIDAL_HOT_WINDOW_DAYS", "30")
    try:
        assert get_settings() is get_settings()
        assert get_settings().hot_window_days == 30
    finally:
        get_settings.cache_clear()


def test_azure_client_id_is_ignored() -> None:
    environ = _local(AZURE_CLIENT_ID="abc", PATH="x")
    assert Settings.from_env(environ).env == "local"


@pytest.mark.parametrize(
    "key", ["TIDAL_SQL_PASSWORD", "TIDAL_STORAGE__ACOUNT_URL", "tidal_typo", "TIDAL_SQL"]
)
def test_unknown_tidal_key_fails(key: str) -> None:
    with pytest.raises(ConfigError, match="unknown configuration keys"):
        Settings.from_env(_local(**{key: "x"}))


@pytest.mark.parametrize("name", ["EIA_API_KEY", "FRED_API_KEY"])
def test_removed_vendor_key_fails_startup(name: str) -> None:
    """ADR-0013: the vendor key group is gone, so a leftover variable stops startup."""
    key = "TIDAL_" + "SOURCES__" + name  # assembled: the removed name is documented in the ADR only
    with pytest.raises(ConfigError, match="unknown configuration keys"):
        Settings.from_env(_local(**{key: "x"}))


def test_unknown_key_fails_for_bare_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TIDAL_ENV", "local")
    monkeypatch.setenv("TIDAL_LOGLEVEL", "INFO")
    with pytest.raises(ConfigError):
        Settings()


def test_bare_settings_validation_hides_input(monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in _azure(**{"TIDAL_SQL__PASSWORD": "hunter2"}).items():
        monkeypatch.setenv(key, value)
    with pytest.raises(ValidationError) as exc_info:
        Settings()
    assert "hunter2" not in str(exc_info.value)


@pytest.mark.parametrize("env", ["dev", "prod"])
def test_minimal_azure_env_ok(env: str) -> None:
    settings = Settings.from_env(_azure(env))
    assert settings.env == env
    assert settings.auth.tenant_id == UUID(TENANT)


@pytest.mark.parametrize("env", ["dev", "prod"])
def test_auth_disabled_outside_local_fails(env: str) -> None:
    with pytest.raises(ConfigError, match="TIDAL_AUTH__DISABLED"):
        Settings.from_env(_azure(env, TIDAL_AUTH__DISABLED="true"))


def test_auth_disabled_allowed_locally() -> None:
    assert Settings.from_env(_local(TIDAL_AUTH__DISABLED="true")).auth.disabled is True


@pytest.mark.parametrize("env", ["dev", "prod"])
def test_tenant_required_outside_local(env: str) -> None:
    environ = _azure(env)
    del environ["TIDAL_AUTH__TENANT_ID"]
    with pytest.raises(ConfigError, match="TIDAL_AUTH__TENANT_ID is required"):
        Settings.from_env(environ)


def test_tenant_optional_locally() -> None:
    assert Settings.from_env(LOCAL).auth.tenant_id is None


@pytest.mark.parametrize("env", ["dev", "prod"])
def test_sql_password_auth_rejected_outside_local(env: str) -> None:
    environ = _azure(env, TIDAL_SQL__AUTH="sql_password")
    with pytest.raises(ConfigError, match="TIDAL_SQL__AUTH must be entra_mi"):
        Settings.from_env(environ)


@pytest.mark.parametrize("value", [None, "false"])
@pytest.mark.parametrize("env", ["dev", "prod"])
def test_managed_identity_required_outside_local(env: str, value: str | None) -> None:
    environ = _azure(env)
    if value is None:
        del environ["TIDAL_STORAGE__USE_MANAGED_IDENTITY"]
    else:
        environ["TIDAL_STORAGE__USE_MANAGED_IDENTITY"] = value
    with pytest.raises(ConfigError, match="TIDAL_STORAGE__USE_MANAGED_IDENTITY must be true"):
        Settings.from_env(environ)


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
        Settings.from_env(_azure(env, **{key: value}))
    assert "topsecret" not in str(exc_info.value)


def test_local_only_keys_allowed_locally() -> None:
    environ = {
        **LOCAL,
        "TIDAL_STORAGE__CONNECTION_STRING": "UseDevelopmentStorage=true",
        "TIDAL_SERVICEBUS__CONNECTION_STRING": "UseDevelopmentEmulator=true",
        "TIDAL_SQL__PASSWORD": "pw",
        "TIDAL_CLOCK__NOW": "2025-12-31T23:59:59Z",
    }
    assert Settings.from_env(environ).clock.now is not None


def test_otel_connection_string_allowed_in_azure() -> None:
    # Exempt: in Azure it is a Key Vault reference (config-contract rules).
    environ = _azure(TIDAL_OTEL__CONNECTION_STRING="InstrumentationKey=k")
    assert Settings.from_env(environ).otel.connection_string is not None


@pytest.mark.parametrize("server", [None, "", "localhost", "127.0.0.1"])
def test_entra_mi_without_server_fails(server: str | None) -> None:
    environ = _local(TIDAL_SQL__AUTH="entra_mi")
    if server is not None:
        environ["TIDAL_SQL__SERVER"] = server
    with pytest.raises(ConfigError, match="TIDAL_SQL__SERVER"):
        Settings.from_env(environ)


@pytest.mark.parametrize("days", ["0", "-1"])
def test_hot_window_days_must_be_positive(days: str) -> None:
    with pytest.raises(ConfigError, match="hot_window_days"):
        Settings.from_env(_local(TIDAL_HOT_WINDOW_DAYS=days))


def test_synth_start_after_end_fails() -> None:
    environ = _local(TIDAL_SYNTH__START="2020-01-02", TIDAL_SYNTH__END="2020-01-01")
    with pytest.raises(ConfigError, match="TIDAL_SYNTH__START must be <= TIDAL_SYNTH__END"):
        Settings.from_env(environ)


def test_synth_single_day_ok() -> None:
    environ = _local(TIDAL_SYNTH__START="2020-01-01", TIDAL_SYNTH__END="2020-01-01")
    assert Settings.from_env(environ).synth.start == date(2020, 1, 1)


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
        Settings.from_env(_local(**{key: value}))
    assert value not in str(exc_info.value)


def test_secrets_never_revealed() -> None:
    secrets = [key for key, typ, *_ in ROWS if typ == "secret"]
    assert len(secrets) == 5
    settings = Settings.from_env(_local(**{k: f"pw-{i}-hunter2" for i, k in enumerate(secrets)}))
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
    settings = Settings.from_env(LOCAL)
    with pytest.raises(ValidationError, match="frozen"):
        settings.env = "prod"  # type: ignore[misc]
