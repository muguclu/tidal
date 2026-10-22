"""Arrow/Parquet streaming responses, bounded JSON paging and API-layer forward-fill (SPEC-04).

Streaming. Batches are encoded one at a time, so memory stays at about one batch whatever the
range. The first batch is pulled before the response starts, so a failure there is still a proper
problem+json (404/503) rather than a broken 200. Later failures cannot change the status line:
the writer is then NOT closed (no Arrow end-of-stream marker, no Parquet footer) and the error
propagates, so the server aborts the connection and a client sees a truncated, invalid body
instead of a complete-looking one. ``X-Tidal-Tier`` is the only tier signal (api-contract §1);
there is no trailer in the contract.

Paging. JSON pages are read from the reader's *stream* from the cursor position and the stream is
closed after ``limit + 1`` rows, so a page costs at most one reader slice, not the whole range.
``fill=ffill`` is JSON-only (SPEC-04 §3): it is applied here, only between real bars, with flat
bars at the previous close (negative prices pass through) and ``volume=None``.
"""

import io
import math
from collections.abc import AsyncIterator, Callable
from contextlib import aclosing
from typing import Any, cast

import pyarrow as pa
import pyarrow.parquet as pq
from fastapi.responses import StreamingResponse

from tidal.api.pagination import Position
from tidal.common.logging import get_correlation_id, get_logger
from tidal.common.timeutil import INTERVAL_NS, from_ts_ns
from tidal.contracts.models import Bar, Tick
from tidal.contracts.schemas import Interval, TickSide
from tidal.query.protocols import RowStream

TIER_HEADER = "X-Tidal-Tier"
ARROW = "application/vnd.apache.arrow.stream"
PARQUET = "application/vnd.apache.parquet"
NEGATIVE = 1  # quality bit 0 (data-contracts §5)
RESTATED = 8  # quality bit 3: version > 1

logger = get_logger(__name__)


# -- row conversion, fill and paging -----------------------------------------------------------
def _fill_bar(prev: Bar, ts_ns: int) -> Bar:
    c = prev.close
    quality = (NEGATIVE if c < 0 else 0) | (RESTATED if prev.version > 1 else 0)
    return Bar(
        ts=from_ts_ns(ts_ns), ts_ns=ts_ns, open=c, high=c, low=c, close=c,
        volume=None, quality=quality, version=prev.version,
    )  # fmt: skip


def bar_from_row(r: dict[str, Any]) -> Bar:  # Any: pyarrow row values of mixed column types
    """Contract ``Bar`` from a ``BAR_V1`` row (NaN volume means none)."""
    vol = r["volume"]
    return Bar.model_construct(
        ts=from_ts_ns(r["ts_ns"]), ts_ns=r["ts_ns"], open=r["open"], high=r["high"],
        low=r["low"], close=r["close"], volume=None if math.isnan(vol) else vol,
        quality=r["quality"], version=r["version"],
    )  # fmt: skip


def tick_from_row(r: dict[str, Any]) -> Tick:  # Any: pyarrow row values of mixed column types
    """Contract ``Tick`` from a ``TICK_V1`` row."""
    return Tick.model_construct(
        ts=from_ts_ns(r["ts_ns"]), ts_ns=r["ts_ns"], price=r["price"], size=r["size"],
        side=TickSide(r["side"]), quality=r["quality"], version=r["version"],
    )  # fmt: skip


async def _rows[T](
    batches: AsyncIterator[pa.RecordBatch], make: Callable[[dict[str, Any]], T]
) -> AsyncIterator[tuple[T, bool]]:
    async for batch in batches:
        for row in batch.to_pylist():
            yield make(row), True


async def _ffill(
    rows: AsyncIterator[tuple[Bar, bool]], interval: Interval
) -> AsyncIterator[tuple[Bar, bool]]:
    """Insert flat bars between consecutive real bars; the flag marks real bars."""
    step = INTERVAL_NS[interval]
    prev: Bar | None = None
    async for bar, _ in rows:
        if prev is not None:
            for ts_ns in range(prev.ts_ns + step, bar.ts_ns, step):
                yield _fill_bar(prev, ts_ns), False
        yield bar, True
        prev = bar


async def page_from_stream[T](
    stream: RowStream,
    make: Callable[[dict[str, Any]], T],
    ts_of: Callable[[T], int],
    start: Position | None,
    limit: int,
    *,
    ffill: Interval | None = None,
) -> tuple[list[T], Position | None]:
    """Up to ``limit`` rows at/after ``start`` plus the next position; closes the stream early.

    The stream must begin at ``start.ts_ns`` (or, with ``ffill``, at ``start.anchor_ns``: the last
    real bar before the position, which the fill needs). Only ``limit + 1`` rows are consumed
    (plus whatever the reader already sliced), however long the range is.
    """
    out: list[T] = []
    prev_ts: int | None = None
    idx = 0
    last_real: int | None = None
    async with aclosing(cast("Any", stream.batches)) as batches:
        rows: AsyncIterator[tuple[Any, bool]] = _rows(batches, make)  # Any: T or Bar when filled
        if ffill is not None:
            rows = _ffill(rows, ffill)
        async with aclosing(cast("Any", rows)):
            async for row, real in rows:
                ts = ts_of(row)
                idx = idx + 1 if ts == prev_ts else 0
                prev_ts = ts
                if start is None or (ts, idx) >= (start.ts_ns, start.idx):
                    if len(out) == limit:
                        return out, Position(ts, idx, anchor_ns=last_real)
                    out.append(row)
                if real:
                    last_real = ts
    return out, None


# -- encoding ----------------------------------------------------------------------------------
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


async def _encode(stream: RowStream, schema: pa.Schema, *, arrow: bool) -> AsyncIterator[bytes]:
    """Encode batches one at a time; always closes the batch source.

    The writer is closed only after the last batch: closing it on failure would append the
    Arrow end-of-stream marker / Parquet footer and make a truncated body look complete.
    """
    sink = _Sink()
    writer: Any  # Any: the two writers share only write_batch/close
    if arrow:
        writer = pa.ipc.new_stream(sink, schema)
    else:
        writer = pq.ParquetWriter(cast("Any", sink), schema)
    try:
        async with aclosing(cast("Any", stream.batches)) as batches:
            async for batch in batches:
                writer.write_batch(batch)
                if data := sink.drain():
                    yield data
    except Exception:
        logger.exception("stream_failed", correlation_id=get_correlation_id())
        raise
    writer.close()
    yield sink.drain()


async def _no_batches() -> AsyncIterator[pa.RecordBatch]:
    return
    yield  # pragma: no cover - makes this an async generator


async def _prefetched(
    first: pa.RecordBatch, rest: AsyncIterator[pa.RecordBatch]
) -> AsyncIterator[pa.RecordBatch]:
    try:
        yield first
        async for batch in rest:
            yield batch
    finally:
        await _aclose(rest)


async def _aclose(it: AsyncIterator[pa.RecordBatch]) -> None:
    close = getattr(it, "aclose", None)
    if close is not None:
        await close()


async def stream_response(
    stream: RowStream, schema: pa.Schema, *, arrow: bool
) -> StreamingResponse:
    """Arrow IPC stream or Parquet body without materialising the result.

    The first batch is awaited here so that errors before any data are normal error responses.
    """
    rest = aiter(stream.batches)
    try:
        first = await anext(rest)
    except StopAsyncIteration:
        source = RowStream(stream.tier, _no_batches())
    except BaseException:
        await _aclose(rest)
        raise
    else:
        source = RowStream(stream.tier, _prefetched(first, rest))
    return StreamingResponse(
        _encode(source, schema, arrow=arrow),
        media_type=ARROW if arrow else PARQUET,
        headers={TIER_HEADER: stream.tier.value},
    )
