"""``GET /v1/bars`` plus helpers shared by ticks/spreads (time parsing, limits, streaming)."""

from collections.abc import Iterable, Iterator, Sequence
from dataclasses import replace
from enum import StrEnum
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import StreamingResponse

from tidal.api.auth_roles import READER, ReaderPrincipal
from tidal.api.deps import get_clock, get_reader
from tidal.api.errors import CORRELATION_HEADER, correlation_header_doc, problem_docs
from tidal.api.pagination import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    binding,
    cursor_key,
    decode_cursor,
    encode_cursor,
    page_of,
)
from tidal.api.streaming import ARROW, PARQUET, TIER_HEADER, ffill_bars, stream_response
from tidal.common.errors import ValidationFailed
from tidal.common.timeutil import to_ts_ns
from tidal.contracts.instruments import InstrumentId
from tidal.contracts.models import Bar, BarsResponse, Tier, datetime_to_ns
from tidal.contracts.schemas import BAR_V1, Interval
from tidal.query.limits import check_bars_range
from tidal.query.protocols import Clock, Reader

router = APIRouter(prefix="/v1", tags=["market-data"])

TimeParam = Annotated[
    str, Query(max_length=35, pattern=r"^([0-9]{1,19}|[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:.]+Z)$")
]


class Format(StrEnum):
    """Response format."""

    JSON = "json"
    ARROW = "arrow"
    PARQUET = "parquet"


class Fill(StrEnum):
    """Gap handling; ``ffill`` is applied in the API layer only."""

    NONE = "none"
    FFILL = "ffill"


def parse_time(value: str) -> int:
    """Parse ISO-8601 Z or epoch-ns string to ``ts_ns``; 400 when unparseable."""
    try:
        return to_ts_ns(value)
    except ValueError:
        raise ValidationFailed("Invalid time parameter.") from None


def parse_range(start: str, end: str) -> tuple[int, int]:
    """Parse ``[start, end)``; 400 unless ``start < end``."""
    start_ns, end_ns = parse_time(start), parse_time(end)
    if end_ns <= start_ns:
        raise ValidationFailed("end must be after start.")
    return start_ns, end_ns


def market_data_ok(*, binary: bool = True, tier: bool = True) -> dict[str, Any]:
    """OpenAPI doc of the 200 response (correlation + tier headers, binary media types)."""
    doc: dict[str, Any] = {"headers": {CORRELATION_HEADER: correlation_header_doc()}}
    if tier:
        doc["headers"][TIER_HEADER] = {
            "description": "Storage tier(s) that served the query (FinOps attribution).",
            "required": True,
            "schema": {"type": "string", "enum": ["hot", "cold", "mixed"]},
        }
    if binary:
        stream = {"schema": {"type": "string", "format": "binary"}}
        doc["content"] = {ARROW: stream, PARQUET: stream}
    return doc


def tier_of(tiers: Iterable[Tier]) -> Tier:
    """The tier of a response built from several reads."""
    unique = set(tiers)
    return unique.pop() if len(unique) == 1 else Tier.MIXED


@router.get(
    "/bars",
    operation_id="getBars",
    summary="OHLCV bars for one instrument",
    response_model=BarsResponse,
    openapi_extra={"x-tidal-min-role": READER},
    responses={200: market_data_ok(), **problem_docs(400, 401, 403, 404, 413, 429, 503)},
)
async def get_bars(  # noqa: PLR0917 - contract query parameters
    request: Request,
    response: Response,
    principal: ReaderPrincipal,
    reader: Annotated[Reader, Depends(get_reader)],
    clock: Annotated[Clock, Depends(get_clock)],
    instrument_id: Annotated[InstrumentId, Query()],
    interval: Annotated[Interval, Query()],
    start: TimeParam,
    end: TimeParam,
    fill: Fill = Fill.NONE,
    as_of: TimeParam | None = None,
    format: Format = Format.JSON,
    cursor: Annotated[str | None, Query(max_length=512)] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
) -> BarsResponse | StreamingResponse:
    """Bars in ``[start, end)``; ``fill=ffill`` and cursor paging are JSON-only API-layer logic."""
    start_ns, end_ns = parse_range(start, end)
    check_bars_range(interval, start_ns, end_ns)
    given_as_of = parse_time(as_of) if as_of is not None else None
    if format is not Format.JSON:
        if fill is Fill.FFILL:
            raise ValidationFailed("fill=ffill is supported for format=json only.")
        stream = await reader.stream_bars(
            instrument_id, interval, start_ns, end_ns, as_of_ns=given_as_of
        )
        return stream_response(stream, BAR_V1, arrow=format is Format.ARROW)
    bound = binding(
        "bars", principal.oid, instrument_id, interval, start_ns, end_ns, fill, given_as_of
    )
    key = cursor_key(request)
    pos = decode_cursor(key, bound, cursor) if cursor else None
    # Without an explicit as_of, pin the first page's request time into the cursor so row
    # versions do not change between pages.
    as_of_ns = given_as_of or (pos.as_of_ns if pos else None) or datetime_to_ns(clock.now())
    # ffill needs the bar before the page start, so it always reads from the original start.
    read_from = pos.ts_ns if pos and fill is Fill.NONE else start_ns
    got = await reader.read_bars(instrument_id, interval, read_from, end_ns, as_of_ns=as_of_ns)
    rows: Sequence[Bar] | Iterator[Bar] = (
        ffill_bars(got.rows, interval) if fill is Fill.FFILL else got.rows
    )
    page, nxt = page_of(rows, lambda b: b.ts_ns, pos, limit)
    response.headers[TIER_HEADER] = got.tier.value
    return BarsResponse(
        instrument_id=instrument_id,
        interval=interval,
        tier=got.tier,
        rows=page,
        next_cursor=encode_cursor(key, bound, replace(nxt, as_of_ns=as_of_ns)) if nxt else None,
    )
