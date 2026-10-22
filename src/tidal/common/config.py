"""Settings mirroring ``docs/contracts/config-contract.md`` v1.1.

Prefix ``TIDAL_``, nested delimiter ``__``. Use :func:`get_settings` in services and
:meth:`Settings.from_env` with an explicit mapping in tests.
Startup fails with :class:`~tidal.common.errors.ConfigError` when ``TIDAL_ENV`` is unset (fail
closed: no implicit ``local``), on unknown ``TIDAL_*`` keys and on the contract rules (local-only
keys outside ``TIDAL_ENV=local``). Error messages never echo values.
"""

import logging
import os
from collections.abc import Mapping
from datetime import date
from functools import lru_cache
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    SecretStr,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic_settings import (
    BaseSettings,
    EnvSettingsSource,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

from tidal.common.errors import ConfigError

ENV_PREFIX = "TIDAL_"
NESTED_DELIMITER = "__"
AZURITE_ACCOUNT_URL = "http://127.0.0.1:10000/devstoreaccount1"

_HTTP_URL: TypeAdapter[HttpUrl] = TypeAdapter(HttpUrl)


def _check_http_url(value: str) -> str:
    _HTTP_URL.validate_python(value)
    return value  # keep the original text: HttpUrl would append a trailing slash


Url = Annotated[str, AfterValidator(_check_http_url)]


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


class StorageSettings(_Section):
    """``TIDAL_STORAGE__*``."""

    account_url: Url = AZURITE_ACCOUNT_URL
    use_managed_identity: bool = False
    connection_string: SecretStr | None = None


class SqlSettings(_Section):
    """``TIDAL_SQL__*``."""

    server: str = "localhost"
    database: str = "tidal"
    auth: Literal["entra_mi", "sql_password"] = "sql_password"
    user: str = "sa"
    password: SecretStr | None = None


class ServiceBusSettings(_Section):
    """``TIDAL_SERVICEBUS__*``."""

    fqdn: str = "localhost"
    connection_string: SecretStr | None = None


class QuerySettings(_Section):
    """``TIDAL_QUERY__*``: how ``tidal-api`` reaches the query service."""

    base_url: Url = "http://query:8000"
    audience: str = "api://tidal-query"


class AuthSettings(_Section):
    """``TIDAL_AUTH__*``: Entra ID token validation."""

    tenant_id: UUID | None = None
    audience: str = "api://tidal"
    disabled: bool = False


class SourcesSettings(_Section):
    """``TIDAL_SOURCES__*``: upstream API keys."""

    eia_api_key: SecretStr | None = None
    fred_api_key: SecretStr | None = None


class SynthSettings(_Section):
    """``TIDAL_SYNTH__*``: synthetic tick generator (part of the determinism key)."""

    scale: Literal["S", "M", "L"] = "S"
    seed: int = 20261021
    start: date = date(2005, 1, 1)
    end: date = date(2025, 12, 31)

    @model_validator(mode="after")
    def _start_not_after_end(self) -> Self:
        if self.start > self.end:
            msg = "TIDAL_SYNTH__START must be <= TIDAL_SYNTH__END"
            raise ValueError(msg)
        return self


class ClockSettings(_Section):
    """``TIDAL_CLOCK__*``: local/test-only frozen "now"."""

    now: AwareDatetime | None = None


class OtelSettings(_Section):
    """``TIDAL_OTEL__*``: Azure Monitor exporter."""

    connection_string: SecretStr | None = None


def allowed_env_keys() -> frozenset[str]:
    """Every ``TIDAL_*`` environment variable name the contract defines (upper case)."""
    keys: set[str] = set()
    for name, field in Settings.model_fields.items():
        section = field.annotation
        if isinstance(section, type) and issubclass(section, _Section):
            keys.update(
                f"{ENV_PREFIX}{name}{NESTED_DELIMITER}{sub}".upper() for sub in section.model_fields
            )
        else:
            keys.add(f"{ENV_PREFIX}{name}".upper())
    return frozenset(keys)


class _StrictEnvSource(EnvSettingsSource):
    """Env source over an explicit mapping that rejects unknown ``TIDAL_*`` keys."""

    def __init__(self, settings_cls: type[BaseSettings], environ: Mapping[str, str]) -> None:
        unknown = sorted(
            k
            for k in environ
            if k.upper().startswith(ENV_PREFIX) and k.upper() not in allowed_env_keys()
        )
        if unknown:
            raise ConfigError("unknown configuration keys: " + ", ".join(unknown))
        self._environ = environ  # set before super().__init__, which calls _load_env_vars
        super().__init__(settings_cls)

    def _load_env_vars(self) -> Mapping[str, str | None]:
        return {k.lower(): v for k, v in self._environ.items()}


class Settings(BaseSettings):
    """Process configuration; secrets are ``SecretStr`` and never appear in repr/str/dumps."""

    model_config = SettingsConfigDict(
        env_prefix=ENV_PREFIX,
        env_nested_delimiter=NESTED_DELIMITER,
        case_sensitive=False,
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
    )

    env: Literal["local", "dev", "prod"]  # required (contract v1.1): no default, fail closed
    log_level: str = "INFO"
    service_name: Literal["api", "query", "worker", "ingest", "tiering", "legacy"] = "api"
    hot_window_days: int = Field(default=90, gt=0)
    storage: StorageSettings = StorageSettings()
    sql: SqlSettings = SqlSettings()
    servicebus: ServiceBusSettings = ServiceBusSettings()
    query: QuerySettings = QuerySettings()
    auth: AuthSettings = AuthSettings()
    sources: SourcesSettings = SourcesSettings()
    synth: SynthSettings = SynthSettings()
    clock: ClockSettings = ClockSettings()
    otel: OtelSettings = OtelSettings()

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,  # noqa: ARG003 - passed by keyword
        dotenv_settings: PydanticBaseSettingsSource,  # noqa: ARG003 - passed by keyword
        file_secret_settings: PydanticBaseSettingsSource,  # noqa: ARG003 - passed by keyword
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Init kwargs, then the process environment through the strict env source."""
        return (init_settings, _StrictEnvSource(settings_cls, os.environ))

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> Self:
        """Build settings from an explicit environment mapping; raise ``ConfigError`` if invalid."""
        values = _StrictEnvSource(cls, environ)()
        try:
            return cls.model_validate(values)
        except ValidationError as exc:
            problems = [
                f"{'.'.join(str(p) for p in err['loc']) or 'settings'}: {err['msg']}"
                for err in exc.errors(include_input=False, include_url=False)
            ]
            raise ConfigError("invalid configuration: " + "; ".join(problems)) from None

    @model_validator(mode="before")
    @classmethod
    def _env_required(cls, data: object) -> object:
        """Fail closed on a missing ``TIDAL_ENV`` with an error that names the variable."""
        if isinstance(data, Mapping) and data.get("env") is None:
            msg = "TIDAL_ENV is required (local|dev|prod); it has no default"
            raise ValueError(msg)
        return data

    @field_validator("log_level")
    @classmethod
    def _known_log_level(cls, value: str) -> str:
        level = value.upper()
        if level not in logging.getLevelNamesMapping():
            msg = "TIDAL_LOG_LEVEL must be a standard logging level name"
            raise ValueError(msg)
        return level

    @model_validator(mode="after")
    def _startup_rules(self) -> Self:
        problems: list[str] = []
        if self.env != "local":
            if self.auth.disabled:
                problems.append("TIDAL_AUTH__DISABLED=true is only allowed when TIDAL_ENV=local")
            elif self.auth.tenant_id is None:
                problems.append(f"TIDAL_AUTH__TENANT_ID is required when TIDAL_ENV={self.env}")
            if self.sql.auth != "entra_mi":
                problems.append(f"TIDAL_SQL__AUTH must be entra_mi when TIDAL_ENV={self.env}")
            if not self.storage.use_managed_identity:
                problems.append(
                    f"TIDAL_STORAGE__USE_MANAGED_IDENTITY must be true when TIDAL_ENV={self.env}"
                )
            local_only = {
                "TIDAL_STORAGE__CONNECTION_STRING": self.storage.connection_string,
                "TIDAL_SERVICEBUS__CONNECTION_STRING": self.servicebus.connection_string,
                "TIDAL_SQL__PASSWORD": self.sql.password,
                "TIDAL_CLOCK__NOW": self.clock.now,
            }
            leaked = [key for key, value in local_only.items() if value is not None]
            if leaked:
                problems.append(f"{', '.join(leaked)} must not be set when TIDAL_ENV={self.env}")
        if self.sql.auth == "entra_mi" and self.sql.server.strip() in {
            "",
            "localhost",
            "127.0.0.1",
        }:
            problems.append(
                "TIDAL_SQL__AUTH=entra_mi requires TIDAL_SQL__SERVER (an Azure SQL host)"
            )
        if problems:
            raise ValueError("; ".join(problems))
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings from ``os.environ``, validated once; ``cache_clear()`` in tests.

    Raises ``ConfigError`` naming ``TIDAL_ENV`` when it is unset (and on any other contract
    violation); a failed call is not cached, so the next call re-validates.
    """
    return Settings.from_env(os.environ)
