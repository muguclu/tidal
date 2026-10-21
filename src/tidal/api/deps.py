"""Dependency providers (SPEC-04 §2): every external client is injected, so tests use fakes.

Clients live on ``app.state`` (set by ``create_app``); routers depend only on these providers.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated

from fastapi import Depends, Request

from tidal.common.config import Settings
from tidal.common.errors import ConfigError, DependencyUnavailable, Unauthenticated
from tidal.query.protocols import Bus, Clock, Reader

ALL_ROLES = frozenset({"Tidal.Reader", "Tidal.Trader", "Tidal.Quant", "Tidal.Admin"})


@dataclass(frozen=True, slots=True)
class Principal:
    """Authenticated caller; ``oid`` is logged only as a hash."""

    oid: str
    roles: frozenset[str]


LOCAL_ANONYMOUS = Principal(oid="local-anonymous", roles=ALL_ROLES)


class SystemClock:
    """Wall clock (UTC)."""

    def now(self) -> datetime:
        """Return the current UTC time."""
        return datetime.now(UTC)


def get_settings(request: Request) -> Settings:
    """Settings the app was created with."""
    settings: Settings = request.app.state.settings
    return settings


def get_reader(request: Request) -> Reader:
    """Injected reader; 503 when none is configured."""
    reader: Reader | None = request.app.state.reader
    if reader is None:
        raise DependencyUnavailable("The data reader is not configured.")
    return reader


def get_bus(request: Request) -> Bus:
    """Injected Service Bus producer; 503 when none is configured."""
    bus: Bus | None = request.app.state.bus
    if bus is None:
        raise DependencyUnavailable("The message bus is not configured.")
    return bus


def get_clock(request: Request) -> Clock:
    """Injected clock."""
    clock: Clock = request.app.state.clock
    return clock


def assert_auth_allowed(settings: Settings) -> None:
    """Startup assertion: auth may be disabled only when ``TIDAL_ENV=local``."""
    if settings.auth.disabled and settings.env != "local":
        raise ConfigError("TIDAL_AUTH__DISABLED=true is only allowed when TIDAL_ENV=local")


async def validate_bearer_token(_token: str, _settings: Settings) -> Principal:
    """SEAM (T-402): validate an Entra ID JWT and return its principal.

    Until T-402 replaces this with issuer/audience/signature/exp/nbf checks over cached JWKS,
    every token is rejected (fail closed). The token is never logged.
    """
    raise Unauthenticated("Token validation is not available.")


async def get_principal(
    request: Request, settings: Annotated[Settings, Depends(get_settings)]
) -> Principal:
    """Return the caller: anonymous when auth is disabled in local, else the validated token."""
    if settings.auth.disabled and settings.env == "local":
        return LOCAL_ANONYMOUS
    scheme, _, token = request.headers.get("Authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise Unauthenticated("A bearer token is required.")
    return await validate_bearer_token(token.strip(), settings)
