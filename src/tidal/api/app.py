"""FastAPI app factory (Factory pattern) and the ``tidal-api`` entrypoint (SPEC-04 §2)."""

import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import uvicorn
from fastapi import FastAPI
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from tidal.api.deps import assert_auth_allowed
from tidal.api.errors import (
    CORRELATION_HEADER,
    internal_error_response,
    register_exception_handlers,
    rename_problem_media_types,
)
from tidal.api.openapi_contract import normalize
from tidal.api.ratelimit import TokenBucketLimiter
from tidal.api.routers import backtests, bars, health, instruments, latest, spreads, ticks
from tidal.common.config import Settings, get_settings
from tidal.common.logging import (
    bind_correlation_id,
    configure_logging,
    get_logger,
    new_correlation_id,
    reset_correlation_id,
)
from tidal.common.timeutil import SystemClock
from tidal.query.protocols import Bus, Clock, Reader

_VALID_CORRELATION_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_HEADER_KEY = CORRELATION_HEADER.lower().encode()

logger = get_logger(__name__)


class CorrelationMiddleware:
    """Pure ASGI middleware: bind/echo ``X-Correlation-Id`` and turn unhandled errors into 500s.

    The incoming id is used only if it is a short token of safe characters (it ends up in logs and
    headers); otherwise a ULID is generated. Unhandled exceptions are logged (never returned) and
    answered with a generic problem+json that still carries the correlation id.
    """

    def __init__(self, app: ASGIApp) -> None:
        """Wrap ``app``."""
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Handle one ASGI call."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        supplied = dict(scope["headers"]).get(_HEADER_KEY, b"").decode("latin-1")
        cid = supplied if _VALID_CORRELATION_ID.match(supplied) else new_correlation_id()
        token = bind_correlation_id(cid)
        started = False

        async def send_with_id(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                headers: list[tuple[bytes, bytes]] = list(message.get("headers", []))
                if not any(k.lower() == _HEADER_KEY for k, _ in headers):
                    headers.append((_HEADER_KEY, cid.encode()))
                message["headers"] = headers
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        except Exception:
            logger.exception("unhandled_error", path=scope["path"], method=scope["method"])
            if started:
                raise
            response = internal_error_response()
            await response(scope, receive, send_with_id)
        finally:
            reset_correlation_id(token)


def create_app(
    settings: Settings | None = None,
    *,
    reader: Reader | None = None,
    bus: Bus | None = None,
    clock: Clock | None = None,
) -> FastAPI:
    """Build the API (Factory pattern); every external client is injected.

    ``reader`` and ``bus`` default to ``None`` (real clients arrive with T-208); the ready probe
    then reports ``not_ready``. ``clock`` defaults to the system clock.
    """
    settings = settings or get_settings()
    assert_auth_allowed(settings)
    configure_logging(settings)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        logger.info("api_started", env=settings.env, auth_disabled=settings.auth.disabled)
        try:
            yield
        finally:
            aclose = getattr(reader, "aclose", None)
            if aclose is not None:  # RemoteReader owns an HTTP client; fakes have none
                await aclose()
            logger.info("api_stopped")

    app = FastAPI(
        title="Tidal API",
        version="1.0.0",
        lifespan=lifespan,
        docs_url="/docs" if settings.env == "local" else None,
        openapi_url="/openapi.json" if settings.env == "local" else None,
        redoc_url=None,
    )
    app.state.settings = settings
    app.state.reader = reader
    app.state.bus = bus
    app.state.clock = clock or SystemClock()
    app.state.rate_limiter = TokenBucketLimiter(app.state.clock)
    app.add_middleware(CorrelationMiddleware)
    register_exception_handlers(app)
    for module in (health, instruments, bars, ticks, latest, spreads, backtests):
        app.include_router(module.router)
    default_openapi = app.openapi

    def openapi() -> dict[str, Any]:
        if app.openapi_schema is None:
            normalize(rename_problem_media_types(default_openapi()))
        return app.openapi_schema or {}

    app.openapi = openapi  # type: ignore[method-assign]  # FastAPI documents this override
    return app


def run() -> None:
    """``tidal-api`` entrypoint: reads go through ``tidal-query`` (0.0.0.0:8000)."""
    from tidal.query.service import remote_reader_from_settings  # noqa: PLC0415 - avoids a cycle

    settings = get_settings()
    uvicorn.run(
        create_app(settings, reader=remote_reader_from_settings(settings)),
        host="0.0.0.0",  # noqa: S104 - container bind
        port=8000,
        log_config=None,
    )
