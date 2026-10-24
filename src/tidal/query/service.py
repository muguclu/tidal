"""``tidal-query`` service and the ``tidal-api`` client for it (SPEC-04 §1, ADR-0002).

``create_query_app`` hosts a :class:`~tidal.query.protocols.Reader` (the unified reader in
production) behind a small internal HTTP API; ``RemoteReader`` implements the same ``Reader``
protocol over that API, so the public routers are unchanged whether they run on fakes, in-process
on the unified reader or against ``tidal-query``.

Wire format (internal, not part of ``openapi.yaml``)
- JSON reads (``bars``, ``ticks``, ``latest``, ``instruments``): the contract row models; the
  reader's tier travels in the body and in ``X-Tidal-Tier`` (SPEC-03 §5 watermark labelling is
  done by the reader; this layer only carries it).
- Streams: ``X-Tidal-Tier`` header (the up-front range label), then frames ``[u64 big-endian
  length][u8 tier code][Arrow IPC stream with one batch]`` and a final zero-length frame (8 zero
  bytes, no tier byte). The tier code is the tier observed so far (``observed_tier``); the client
  exposes the latest one through ``ObservedRowStream`` so a JSON page can report ``mixed`` when a
  late restatement turned up in a slice, even though the stream was closed early. A body that
  ends without the zero frame is a truncated stream and raises ``DependencyUnavailable`` instead
  of looking complete. A frame is capped at 64 MiB (both sides); the client holds the partial
  buffer plus one copy of the frame, so memory
  is bounded by a few frames, never by the range.
- Errors: RFC 7807 problem+json with the shared handlers; the client maps them back to typed
  errors. 401/403 from the query service are an API-to-query misconfiguration and surface to the
  caller as 503, never as the caller's own auth failure.

Authentication (api -> query)
- ``tidal-api`` obtains an Entra token for ``settings.query.audience`` from a
  :class:`TokenProvider` (Managed Identity ``id-api`` in Azure; an anonymous provider in
  ``TIDAL_ENV=local`` with auth disabled; a fake in tests) and sends it as a bearer token.
- ``tidal-query`` validates it with an injected :class:`TokenValidator`: in Azure the
  ``tidal.api.auth.EntraTokenValidator`` (audience, ``Tidal.Query.Read`` role, azp/oid == id-api).
  ``FailClosedValidator`` is the default when none is configured. With ``TIDAL_ENV=local`` and
  ``TIDAL_AUTH__DISABLED=true`` validation is skipped. Tokens are never logged.
"""

import asyncio
import json
import struct
from collections.abc import AsyncIterator, Sequence
from contextlib import aclosing
from typing import Annotated, Any, Final, Protocol, cast, runtime_checkable

import httpx
import pyarrow as pa
import uvicorn
from fastapi import APIRouter, Depends, FastAPI, Query, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, ValidationError

from tidal.api.app import CorrelationMiddleware
from tidal.api.auth import query_validator_from_settings
from tidal.api.deps import assert_auth_allowed
from tidal.api.errors import CORRELATION_HEADER, register_exception_handlers
from tidal.api.streaming import TIER_HEADER
from tidal.common.config import Settings, get_settings
from tidal.common.errors import (
    DependencyUnavailable,
    NotFound,
    RangeTooLarge,
    TidalError,
    Unauthenticated,
    ValidationFailed,
)
from tidal.common.logging import configure_logging, get_correlation_id, get_logger
from tidal.common.timeutil import clock_from_settings
from tidal.contracts.instruments import AssetClass, Instrument, Source
from tidal.contracts.models import Bar, LatestBar, Tick, Tier
from tidal.contracts.schemas import Interval
from tidal.query.limits import check_bars_range, check_ticks_range
from tidal.query.protocols import ObservedRowStream, Reader, Rows, RowStream, observed_tier

FRAMES_MEDIA_TYPE: Final = "application/vnd.tidal.batches"
_PREFIX: Final = "/internal/v1"
_LEN: Final = struct.Struct(">Q")
_TIERS: Final = tuple(Tier)  # wire tier codes are indexes into this
_MAX_FRAME: Final = 64 << 20  # 64 MiB: bound on one frame, checked by server and client
_TIMEOUT: Final = httpx.Timeout(60.0, connect=5.0)

logger = get_logger(__name__)


# -- auth seams ---------------------------------------------------------------------------------
@runtime_checkable
class TokenProvider(Protocol):
    """Source of bearer tokens for outbound calls (client side)."""

    async def token(self, audience: str) -> str:
        """Return an access token for ``audience`` (Entra application id URI)."""
        ...


@runtime_checkable
class TokenValidator(Protocol):
    """Validates an inbound bearer token (server side); raises ``Unauthenticated``."""

    async def validate(self, token: str) -> None:
        """Return normally when ``token`` is acceptable for this service."""
        ...


class ManagedIdentityTokenProvider:
    """Entra tokens from the app's Managed Identity (``id-api``); no secret is involved.

    The azure-identity credential caches and refreshes tokens itself.
    """

    def __init__(
        self,
        credential: Any = None,  # Any: azure-identity AsyncTokenCredential (untyped import)
    ) -> None:
        """Use ``credential`` or a lazily created ``ManagedIdentityCredential``."""
        self._credential = credential

    async def token(self, audience: str) -> str:
        """Token for ``<audience>/.default``."""
        if self._credential is None:
            from azure.identity.aio import ManagedIdentityCredential  # noqa: PLC0415

            self._credential = ManagedIdentityCredential()
        access = await self._credential.get_token(f"{audience}/.default")
        return str(access.token)

    async def close(self) -> None:
        """Release the credential's transport."""
        if self._credential is not None:
            await self._credential.close()


class AnonymousTokenProvider:
    """No token (local development with auth disabled only)."""

    async def token(self, audience: str) -> str:  # noqa: ARG002 - protocol signature
        """Empty token: the client then sends no ``Authorization`` header."""
        return ""


class FailClosedValidator:
    """Rejects every token; used where no Entra validator is configured."""

    async def validate(self, token: str) -> None:  # noqa: ARG002 - protocol signature
        """Always reject."""
        raise Unauthenticated("Token validation is not available.")


def token_provider_for(settings: Settings) -> TokenProvider:
    """Managed Identity everywhere except local with auth disabled."""
    if settings.env == "local" and settings.auth.disabled:
        return AnonymousTokenProvider()
    return ManagedIdentityTokenProvider()


# -- wire models --------------------------------------------------------------------------------
class _Wire(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class BarRows(_Wire):
    """JSON body of an internal bars read."""

    tier: Tier
    rows: list[Bar]


class TickRows(_Wire):
    """JSON body of an internal ticks read."""

    tier: Tier
    rows: list[Tick]


class LatestRows(_Wire):
    """JSON body of an internal latest read."""

    rows: list[LatestBar]


class InstrumentRows(_Wire):
    """JSON body of an internal instruments read."""

    items: list[Instrument]


# -- server -------------------------------------------------------------------------------------
async def _frames(stream: RowStream) -> AsyncIterator[bytes]:
    # Any: aclosing wants an object with aclose(); AsyncIterator does not declare it
    async with aclosing(cast("Any", stream.batches)) as batches:
        async for batch in batches:
            sink = pa.BufferOutputStream()
            with pa.ipc.new_stream(sink, batch.schema) as writer:
                writer.write_batch(batch)
            data = sink.getvalue().to_pybytes()
            if len(data) > _MAX_FRAME:
                raise DependencyUnavailable("A result batch exceeds the stream frame limit.")
            code = _TIERS.index(observed_tier(stream))
            yield _LEN.pack(len(data)) + bytes([code]) + data
    yield _LEN.pack(0)


def _json(model: _Wire, tier: Tier | None = None) -> Response:
    headers = {TIER_HEADER: tier.value} if tier else {}
    return Response(model.model_dump_json(), media_type="application/json", headers=headers)


def _stream(stream: RowStream) -> StreamingResponse:
    return StreamingResponse(
        _frames(stream), media_type=FRAMES_MEDIA_TYPE, headers={TIER_HEADER: stream.tier.value}
    )


def create_query_app(
    reader: Reader, validator: TokenValidator, settings: Settings | None = None
) -> FastAPI:
    """Build the ``tidal-query`` app; the reader and token validator are injected."""
    settings = settings or get_settings()
    assert_auth_allowed(settings)
    skip_auth = settings.auth.disabled and settings.env == "local"

    async def authenticate(request: Request) -> None:
        if skip_auth:
            return
        scheme, _, token = request.headers.get("Authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            raise Unauthenticated("A bearer token is required.")
        await validator.validate(token.strip())

    router = APIRouter(prefix=_PREFIX, dependencies=[Depends(authenticate)])
    iid = Annotated[str, Query(min_length=1, max_length=64)]
    ns = Annotated[int, Query()]
    as_of = Annotated[int | None, Query()]

    @router.get("/bars")
    async def bars(
        instrument_id: iid, interval: Interval, start_ns: ns, end_ns: ns, as_of_ns: as_of = None
    ) -> Response:
        check_bars_range(interval, start_ns, end_ns)
        got = await reader.read_bars(instrument_id, interval, start_ns, end_ns, as_of_ns=as_of_ns)
        return _json(BarRows.model_construct(tier=got.tier, rows=got.rows), got.tier)

    @router.get("/ticks")
    async def ticks(instrument_id: iid, start_ns: ns, end_ns: ns) -> Response:
        check_ticks_range(start_ns, end_ns)
        got = await reader.read_ticks(instrument_id, start_ns, end_ns)
        return _json(TickRows.model_construct(tier=got.tier, rows=got.rows), got.tier)

    @router.get("/stream/bars")
    async def stream_bars(
        instrument_id: iid, interval: Interval, start_ns: ns, end_ns: ns, as_of_ns: as_of = None
    ) -> StreamingResponse:
        check_bars_range(interval, start_ns, end_ns)
        return _stream(
            await reader.stream_bars(instrument_id, interval, start_ns, end_ns, as_of_ns=as_of_ns)
        )

    @router.get("/stream/ticks")
    async def stream_ticks(instrument_id: iid, start_ns: ns, end_ns: ns) -> StreamingResponse:
        check_ticks_range(start_ns, end_ns)
        return _stream(await reader.stream_ticks(instrument_id, start_ns, end_ns))

    @router.get("/latest")
    async def latest(
        instrument_id: Annotated[list[str], Query(min_length=1, max_length=100)],
        interval: Interval = Interval.D1,
    ) -> Response:
        rows = await reader.latest(instrument_id, interval)
        return _json(LatestRows(rows=rows), Tier.HOT)

    @router.get("/instruments")
    async def instruments(
        asset_class: AssetClass | None = None, source: Source | None = None
    ) -> Response:
        items = await reader.instruments(asset_class=asset_class, source=source)
        return _json(InstrumentRows(items=items))

    @router.get("/ping")
    async def ping() -> Response:
        await reader.ping()
        return Response(status_code=204)

    app = FastAPI(title="Tidal Query", docs_url=None, openapi_url=None, redoc_url=None)
    app.state.settings = settings
    app.add_middleware(CorrelationMiddleware)
    register_exception_handlers(app)

    @app.get("/health/live")
    async def live() -> dict[str, str]:
        return {"status": "ok"}

    app.include_router(router)
    return app


# -- client -------------------------------------------------------------------------------------
def _raise_for_problem(status: int, body: bytes) -> None:
    """Map an internal problem response to a typed error (never echoing unvetted bodies)."""
    detail: str | None = None
    if status == 400:  # noqa: PLR2004
        logger.error("query_service_rejected_request", status=status, probable="wire drift")
    try:
        parsed = json.loads(body)
        if isinstance(parsed, dict) and isinstance(parsed.get("detail"), str):
            detail = parsed["detail"]
    except ValueError:
        pass
    mapped: dict[int, type[TidalError]] = {
        400: ValidationFailed, 404: NotFound, 413: RangeTooLarge,
    }  # fmt: skip
    if status in mapped:
        raise mapped[status](detail)
    if status in (401, 403):
        logger.error("query_service_rejected_api_credentials", status=status)
        raise DependencyUnavailable("The query service rejected the API's credentials.")
    raise DependencyUnavailable("The query service failed.")


async def _parse[M: _Wire](model: type[M], content: bytes) -> M:
    """Validate a response body off the event loop; a bad body is a dependency failure."""
    try:
        return await asyncio.to_thread(model.model_validate_json, content)
    except ValidationError:
        logger.error("query_service_invalid_response", model=model.__name__)
        raise DependencyUnavailable("The query service sent an invalid response.") from None


class RemoteReader:
    """``Reader`` that calls ``tidal-query`` with a Managed Identity bearer token."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        tokens: TokenProvider,
        audience: str,
    ) -> None:
        """Inject the HTTP client (base URL set), token provider and token audience."""
        self._client = client
        self._tokens = tokens
        self._audience = audience

    async def _headers(self) -> dict[str, str]:
        headers: dict[str, str] = {}
        if cid := get_correlation_id():
            headers[CORRELATION_HEADER] = cid
        try:
            token = await self._tokens.token(self._audience)
        except Exception as exc:  # any credential failure; only the type is logged
            logger.error("query_token_acquisition_failed", error_type=type(exc).__name__)
            raise DependencyUnavailable("Could not obtain a token for the query service.") from None
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    async def _get(
        self,
        path: str,
        params: dict[str, Any],  # Any: heterogeneous query values (str, int, list, None)
    ) -> httpx.Response:
        clean = {k: v for k, v in params.items() if v is not None}
        try:
            resp = await self._client.get(
                _PREFIX + path, params=clean, headers=await self._headers()
            )
        except httpx.HTTPError:
            raise DependencyUnavailable("The query service is unreachable.") from None
        if resp.status_code >= 400:  # noqa: PLR2004
            _raise_for_problem(resp.status_code, resp.content)
        return resp

    async def read_bars(
        self,
        instrument_id: str,
        interval: Interval,
        start_ns: int,
        end_ns: int,
        *,
        as_of_ns: int | None = None,
    ) -> Rows[Bar]:
        """Bars in ``[start_ns, end_ns)`` with the tier the query service reported."""
        resp = await self._get(
            "/bars",
            {"instrument_id": instrument_id, "interval": interval.value, "start_ns": start_ns,
             "end_ns": end_ns, "as_of_ns": as_of_ns},
        )  # fmt: skip
        body = await _parse(BarRows, resp.content)
        return Rows(body.rows, body.tier)

    async def read_ticks(self, instrument_id: str, start_ns: int, end_ns: int) -> Rows[Tick]:
        """Ticks in ``[start_ns, end_ns)``."""
        resp = await self._get(
            "/ticks", {"instrument_id": instrument_id, "start_ns": start_ns, "end_ns": end_ns}
        )
        body = await _parse(TickRows, resp.content)
        return Rows(body.rows, body.tier)

    async def _open_stream(
        self,
        path: str,
        params: dict[str, Any],  # Any: heterogeneous query values (str, int, None)
    ) -> RowStream:
        clean = {k: v for k, v in params.items() if v is not None}
        request = self._client.build_request(
            "GET", _PREFIX + path, params=clean, headers=await self._headers()
        )
        try:
            resp = await self._client.send(request, stream=True)
        except httpx.HTTPError:
            raise DependencyUnavailable("The query service is unreachable.") from None
        if resp.status_code >= 400:  # noqa: PLR2004
            body = await resp.aread()
            await resp.aclose()
            _raise_for_problem(resp.status_code, body)
        try:
            tier = Tier(resp.headers[TIER_HEADER])
        except (KeyError, ValueError):
            await resp.aclose()
            raise DependencyUnavailable("The query service sent no valid tier.") from None
        latest = [tier]
        return ObservedRowStream(tier, self._decode(resp, latest), lambda: latest[0])

    @staticmethod
    async def _decode(resp: httpx.Response, latest: list[Tier]) -> AsyncIterator[pa.RecordBatch]:
        buf = bytearray()
        done = False
        try:
            async for chunk in resp.aiter_bytes():
                buf += chunk
                while len(buf) >= _LEN.size and not done:
                    (n,) = _LEN.unpack_from(buf)
                    if n == 0:
                        done = True
                        break
                    if n > _MAX_FRAME:
                        raise DependencyUnavailable("The query service sent a corrupt stream.")
                    head = _LEN.size + 1  # length + tier code
                    if len(buf) < head + n:
                        break
                    if buf[_LEN.size] >= len(_TIERS):
                        raise DependencyUnavailable("The query service sent a corrupt stream.")
                    latest[0] = _TIERS[buf[_LEN.size]]
                    # One copy on purpose: Arrow may alias the buffer, which must stay immutable
                    # while the bytearray is compacted below.
                    frame = bytes(buf[head : head + n])
                    del buf[: head + n]
                    with pa.ipc.open_stream(frame) as rdr:
                        for batch in rdr:
                            yield batch
                if done:
                    break
        except httpx.HTTPError:
            raise DependencyUnavailable("The query service stream was interrupted.") from None
        finally:
            await resp.aclose()
        if not done:
            raise DependencyUnavailable("The query service stream was truncated.")

    async def stream_bars(
        self,
        instrument_id: str,
        interval: Interval,
        start_ns: int,
        end_ns: int,
        *,
        as_of_ns: int | None = None,
    ) -> RowStream:
        """``BAR_V1`` batches streamed from the query service."""
        return await self._open_stream(
            "/stream/bars",
            {"instrument_id": instrument_id, "interval": interval.value, "start_ns": start_ns,
             "end_ns": end_ns, "as_of_ns": as_of_ns},
        )  # fmt: skip

    async def stream_ticks(self, instrument_id: str, start_ns: int, end_ns: int) -> RowStream:
        """``TICK_V1`` batches streamed from the query service."""
        return await self._open_stream(
            "/stream/ticks",
            {"instrument_id": instrument_id, "start_ns": start_ns, "end_ns": end_ns},
        )

    async def latest(
        self, instrument_ids: Sequence[str], interval: Interval = Interval.D1
    ) -> list[LatestBar]:
        """Last bar per instrument (hot store only)."""
        resp = await self._get(
            "/latest", {"instrument_id": list(instrument_ids), "interval": interval.value}
        )
        return (await _parse(LatestRows, resp.content)).rows

    async def instruments(
        self, *, asset_class: AssetClass | None = None, source: Source | None = None
    ) -> list[Instrument]:
        """Reference data, optionally filtered."""
        resp = await self._get(
            "/instruments",
            {"asset_class": asset_class.value if asset_class else None,
             "source": source.value if source else None},
        )  # fmt: skip
        return (await _parse(InstrumentRows, resp.content)).items

    async def ping(self) -> None:
        """Raise when the query service (or its stores) is down."""
        await self._get("/ping", {})

    async def aclose(self) -> None:
        """Close the HTTP client and the token provider (when it has ``close``)."""
        await self._client.aclose()
        close = getattr(self._tokens, "close", None)
        if close is not None:
            await close()


def remote_reader_from_settings(
    settings: Settings,
    *,
    tokens: TokenProvider | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> RemoteReader:
    """Client for ``settings.query.base_url``; ``tokens``/``transport`` are injectable."""
    client = httpx.AsyncClient(
        base_url=settings.query.base_url, timeout=_TIMEOUT, transport=transport
    )
    return RemoteReader(client, tokens or token_provider_for(settings), settings.query.audience)


# -- entrypoint ---------------------------------------------------------------------------------
def build_reader(settings: Settings) -> Reader:
    """The production unified reader (SQL hot + Parquet cold) for ``settings``."""
    from tidal.query.duck import session_from_settings  # noqa: PLC0415
    from tidal.query.reader import UnifiedReader  # noqa: PLC0415
    from tidal.storage.sql_repo import create_engine_from_settings  # noqa: PLC0415

    return UnifiedReader(
        create_engine_from_settings(settings),
        session_from_settings(settings),
        clock_from_settings(settings),
        settings,
    )


def run() -> None:
    """``tidal-query`` entrypoint: serve the unified reader on ``0.0.0.0:8000``."""
    settings = get_settings()
    configure_logging(settings)
    validator: TokenValidator = FailClosedValidator()
    if not (settings.auth.disabled and settings.env == "local"):
        validator = query_validator_from_settings(settings)  # ConfigError without caller ids
    app = create_query_app(build_reader(settings), validator, settings)
    uvicorn.run(app, host="0.0.0.0", port=8000, log_config=None)  # noqa: S104 - container bind
