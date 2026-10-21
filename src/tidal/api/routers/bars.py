"""``GET /v1/bars`` plus helpers shared by ticks/spreads (time parsing, limits, streaming)."""

import io
from collections.abc import AsyncIterator, Iterable, Iterator, Sequence
from enum import StrEnum
from typing import Annotated, Any, cast

import pyarrow as pa
import pyarrow.parquet as pq
from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import StreamingResponse

from tidal.api.auth_roles import READER, ReaderPrincipal
from tidal.api.deps import get_reader
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
from tidal.common.errors import RangeTooLarge, ValidationFailed
from tidal.common.timeutil import INTERVAL_NS, from_ts_ns, to_ts_ns
from tidal.contracts.instruments import InstrumentId
from tidal.contracts.models import Bar, BarsResponse, Tier
from tidal.contracts.schemas import BAR_V1, Interval
from tidal.query.protocols import Reader, RowStream

TIER_HEADER = "X-Tidal-Tier"
ARROW = "application/vnd.apache.arrow.stream"
PARQUET = "application/vnd.apache.parquet"
DAY_NS = 86_400 * 1_000_000_000
BARS_1S_MAX_NS = 7 * DAY_NS  # SPEC-04 §3
_NEGATIVE = 1  # quality bit 0

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


def check_bars_range(interval: Interval, start_ns: int, end_ns: int) -> None:
    """413 when 1s bars span more than 7 days (1m/1h/1d are unlimited)."""
    if interval is Interval.S1 and end_ns - start_ns > BARS_1S_MAX_NS:
        raise RangeTooLarge("Bars with interval 1s are limited to 7 days per call.")


def ffill_bars(bars: Sequence[Bar], interval: Interval) -> Iterator[Bar]:
    """Lazily forward-fill gaps between consecutive bars with flat bars at the previous close."""
    step = INTERVAL_NS[interval]
    prev: Bar | None = None
    for bar in bars:
        if prev is not None:
            for ts_ns in range(prev.ts_ns + step, bar.ts_ns, step):
                c = prev.close
                yield Bar(
                    ts=from_ts_ns(ts_ns), ts_ns=ts_ns, open=c, high=c, low=c, close=c,
                    volume=None, quality=_NEGATIVE if c < 0 else 0, version=prev.version,
                )  # fmt: skip
        yield bar
        prev = bar


class _Sink(io.RawIOBase):
    """Write-only file that tracks its position and hands out what was written so far."""

    def __init__(self) -> None:
        super().__init__()
        self._buf = bytearray()
        self._pos = 0

    def writable(self) -> bool:
        return True

    def write(self, data: Any) -> int:  # Any: RawIOBase.write takes any bytes-like buffer
        raw = bytes(data)
        self._buf += raw
        self._pos += len(raw)
        return len(raw)

    def tell(self) -> int:
        return self._pos

    def drain(self) -> bytes:
        out, self._buf = bytes(self._buf), bytearray()
        return out


async def _encode(stream: RowStream, schema: pa.Schema, fmt: Format) -> AsyncIterator[bytes]:
    """Encode batches one at a time; memory stays at about one batch."""
    sink = _Sink()
    writer: Any  # Any: the two writers share only write_batch/close
    if fmt is Format.ARROW:
        writer = pa.ipc.new_stream(sink, schema)
    else:
        writer = pq.ParquetWriter(cast("Any", sink), schema)
    try:
        async for batch in stream.batches:
            writer.write_batch(batch)
            if data := sink.drain():
                yield data
    finally:
        writer.close()
    yield sink.drain()


def stream_response(stream: RowStream, schema: pa.Schema, fmt: Format) -> StreamingResponse:
    """Arrow IPC stream or Parquet body without materialising the result."""
    return StreamingResponse(
        _encode(stream, schema, fmt),
        media_type=ARROW if fmt is Format.ARROW else PARQUET,
        headers={TIER_HEADER: stream.tier.value},
    )


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
    as_of_ns = parse_time(as_of) if as_of is not None else None
    if format is not Format.JSON:
        if fill is Fill.FFILL:
            raise ValidationFailed("fill=ffill is supported for format=json only.")
        stream = await reader.stream_bars(
            instrument_id, interval, start_ns, end_ns, as_of_ns=as_of_ns
        )
        return stream_response(stream, BAR_V1, format)
    bound = binding(
        "bars", principal.oid, instrument_id, interval, start_ns, end_ns, fill, as_of_ns
    )
    key = cursor_key(request)
    pos = decode_cursor(key, bound, cursor) if cursor else None
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
        next_cursor=encode_cursor(key, bound, nxt) if nxt else None,
    )
