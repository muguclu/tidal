"""Arrow/Parquet streaming responses and API-layer forward-fill (SPEC-04 §2).

Batches are encoded one at a time, so memory stays at about one batch whatever the range.

KNOWN LIMITATION: the status line and headers are sent before the first batch. If the reader
fails mid-stream the connection ends early and the failure is only logged (with the correlation
id); a truncated Arrow IPC stream can look like a complete one to a client that does not check
for the end-of-stream marker. T-210 adds a trailer / row count so clients can detect truncation,
plus the 10M-row RSS test (SPEC-04 §4).
"""

import io
from collections.abc import AsyncIterator, Iterator, Sequence
from contextlib import aclosing
from typing import Any, cast

import pyarrow as pa
import pyarrow.parquet as pq
from fastapi.responses import StreamingResponse

from tidal.common.logging import get_correlation_id, get_logger
from tidal.common.timeutil import INTERVAL_NS, from_ts_ns
from tidal.contracts.models import Bar
from tidal.contracts.schemas import Interval
from tidal.query.protocols import RowStream

TIER_HEADER = "X-Tidal-Tier"
ARROW = "application/vnd.apache.arrow.stream"
PARQUET = "application/vnd.apache.parquet"
NEGATIVE = 1  # quality bit 0 (data-contracts §5)
RESTATED = 8  # quality bit 3: version > 1

logger = get_logger(__name__)


def ffill_bars(bars: Sequence[Bar], interval: Interval) -> Iterator[Bar]:
    """Lazily forward-fill gaps between consecutive bars with flat bars at the previous close."""
    step = INTERVAL_NS[interval]
    prev: Bar | None = None
    for bar in bars:
        if prev is not None:
            for ts_ns in range(prev.ts_ns + step, bar.ts_ns, step):
                c = prev.close
                quality = (NEGATIVE if c < 0 else 0) | (RESTATED if prev.version > 1 else 0)
                yield Bar(
                    ts=from_ts_ns(ts_ns), ts_ns=ts_ns, open=c, high=c, low=c, close=c,
                    volume=None, quality=quality, version=prev.version,
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


async def _encode(stream: RowStream, schema: pa.Schema, *, arrow: bool) -> AsyncIterator[bytes]:
    """Encode batches one at a time; always closes the batch source and the writer."""
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
    finally:
        writer.close()
    yield sink.drain()


def stream_response(stream: RowStream, schema: pa.Schema, *, arrow: bool) -> StreamingResponse:
    """Arrow IPC stream or Parquet body without materialising the result."""
    return StreamingResponse(
        _encode(stream, schema, arrow=arrow),
        media_type=ARROW if arrow else PARQUET,
        headers={TIER_HEADER: stream.tier.value},
    )
