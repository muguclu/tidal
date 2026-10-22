"""T-210: bounded JSON paging, truncation safety, as_of and fill over the streaming reader."""

import io
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import httpx
import pyarrow as pa
import pytest
from fastapi import FastAPI
from pyarrow import ipc

from fakes.reader import FakeReader, make_bar
from tidal.api.app import create_app
from tidal.api.streaming import _encode, stream_response
from tidal.common.config import Settings
from tidal.common.errors import DependencyUnavailable
from tidal.common.timeutil import FixedClock
from tidal.contracts.models import Tier
from tidal.contracts.schemas import TICK_V1, Interval
from tidal.query.protocols import RowStream
from unit.api.stream_probe import SPAN, SyntheticReader, serve
from unit.api.stream_probe import T0 as PROBE_T0

pytestmark = pytest.mark.anyio
TICKS = 1000
BATCH = 10
T0 = int(datetime(2020, 4, 1, tzinfo=UTC).timestamp()) * 10**9
EOS = bytes([0xFF] * 4 + [0] * 4)  # Arrow IPC end-of-stream marker
RANGE = {"start": str(T0), "end": str(T0 + 10**6)}


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class ManyTicks(FakeReader):
    """1000 ticks one ns apart, streamed in batches of 10; records consumption."""

    def __init__(self) -> None:
        super().__init__()
        self.batches_pulled = 0
        self.closed = 0
        self.starts: list[int] = []
        self.as_of: list[int | None] = []
        self.bar_starts: list[int] = []

    async def stream_ticks(self, instrument_id: str, start_ns: int, end_ns: int) -> RowStream:
        self._known(instrument_id)
        self.starts.append(start_ns)
        first = max(start_ns, T0)

        async def batches() -> AsyncIterator[pa.RecordBatch]:
            try:
                for lo in range(first, min(end_ns, T0 + TICKS), BATCH):
                    self.batches_pulled += 1
                    ts = range(lo, min(lo + BATCH, end_ns, T0 + TICKS))
                    yield pa.RecordBatch.from_pylist(
                        [
                            {"ts_ns": t, "instrument_id": instrument_id, "price": -float(t - T0),
                             "size": 1.0, "side": 0, "quality": 0, "ingested_at_ns": t,
                             "version": 1}
                            for t in ts
                        ],
                        schema=TICK_V1,
                    )  # fmt: skip
            finally:
                self.closed += 1

        return RowStream(Tier.HOT, batches())

    async def stream_bars(
        self, instrument_id: str, interval: Interval, start_ns: int, end_ns: int,
        *, as_of_ns: int | None = None,
    ) -> RowStream:  # fmt: skip
        self.as_of.append(as_of_ns)
        self.bar_starts.append(start_ns)
        return await super().stream_bars(
            instrument_id, interval, start_ns, end_ns, as_of_ns=as_of_ns
        )


@pytest.fixture
def reader() -> ManyTicks:
    return ManyTicks()


@pytest.fixture
async def client(reader: ManyTicks) -> AsyncIterator[httpx.AsyncClient]:
    settings = Settings.from_env({"TIDAL_ENV": "local", "TIDAL_AUTH__DISABLED": "true"})
    app: FastAPI = create_app(settings, reader=reader, clock=FixedClock(T0))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


def params(**extra: str) -> dict[str, str]:
    return {"instrument_id": "EIA.WTI.SPOT", **RANGE, **extra}


async def test_json_page_reads_only_what_the_page_needs(
    client: httpx.AsyncClient, reader: ManyTicks
) -> None:
    body = (await client.get("/v1/ticks", params=params(limit="5"))).json()
    assert [r["ts_ns"] for r in body["rows"]] == list(range(T0, T0 + 5))
    assert reader.batches_pulled == 1  # not 100: the range is not re-read
    assert reader.closed == 1  # and the source is closed right after the page
    assert body["rows"][1]["price"] == -1.0  # negative prices pass through


async def test_following_pages_start_at_the_cursor(
    client: httpx.AsyncClient, reader: ManyTicks
) -> None:
    seen: list[int] = []
    cursor: str | None = None
    while True:
        extra = {"limit": "25", **({"cursor": cursor} if cursor else {})}
        body = (await client.get("/v1/ticks", params=params(**extra))).json()
        seen += [r["ts_ns"] for r in body["rows"]]
        cursor = body["next_cursor"]
        if cursor is None:
            break
    assert seen == list(range(T0, T0 + TICKS))
    assert reader.starts[1:] == [T0 + 25 * k for k in range(1, 40)]
    # every page pulled at most the 3 batches it needs (25 rows + 1 peek)
    assert reader.batches_pulled <= 40 * 3
    assert reader.closed == 40


async def test_arrow_honours_as_of_end_to_end(client: httpx.AsyncClient, reader: ManyTicks) -> None:
    p = {
        "instrument_id": "EIA.WTI.SPOT", "interval": "1d", "start": "2020-04-01T00:00:00Z",
        "end": "2020-05-01T00:00:00Z", "format": "arrow", "as_of": "2020-04-19T00:00:00Z",
    }  # fmt: skip
    r = await client.get("/v1/bars", params=p)
    assert r.status_code == 200
    assert reader.as_of == [int(datetime(2020, 4, 19, tzinfo=UTC).timestamp()) * 10**9]
    assert ipc.open_stream(io.BytesIO(r.content)).read_all().num_rows == 3


async def test_failure_after_first_batch_leaves_body_truncated() -> None:
    async def batches() -> AsyncIterator[pa.RecordBatch]:
        yield pa.RecordBatch.from_pylist([], schema=TICK_V1)
        raise ConnectionError("store died")

    chunks: list[bytes] = []

    async def drain() -> None:
        async for c in _encode(RowStream(Tier.HOT, batches()), TICK_V1, arrow=True):
            chunks.append(c)

    with pytest.raises(ConnectionError):
        await drain()
    # No end-of-stream marker was written (pyarrow readers would still accept this body, so the
    # real signal to clients is the aborted connection: see the end-to-end test below).
    assert not b"".join(chunks).endswith(EOS)


def fail() -> None:
    raise DependencyUnavailable("store down")


async def test_failure_before_first_batch_is_a_proper_error() -> None:
    closed = False

    async def batches() -> AsyncIterator[pa.RecordBatch]:
        nonlocal closed
        try:
            fail()
            yield pa.RecordBatch.from_pylist([], schema=TICK_V1)  # pragma: no cover
        finally:
            closed = True

    with pytest.raises(DependencyUnavailable):
        await stream_response(RowStream(Tier.HOT, batches()), TICK_V1, arrow=True)
    assert closed


async def test_empty_result_still_has_schema_and_tier() -> None:
    async def batches() -> AsyncIterator[pa.RecordBatch]:
        for _ in ():
            yield pa.RecordBatch.from_pylist([], schema=TICK_V1)

    resp = await stream_response(RowStream(Tier.COLD, batches()), TICK_V1, arrow=True)
    assert resp.headers["X-Tidal-Tier"] == "cold"
    data = b"".join([c async for c in resp.body_iterator if isinstance(c, bytes)])
    assert ipc.open_stream(io.BytesIO(data)).read_all().num_rows == 0


# --- ffill paging: the cursor anchor ------------------------------------------------------------
def _day_ns(day: int) -> int:
    return int(datetime(2020, 4, day, tzinfo=UTC).timestamp()) * 10**9


@pytest.fixture
def gappy(reader: ManyTicks) -> ManyTicks:
    """Bars on Apr 1 (+5.0), Apr 3 (-3.5) and Apr 10 (+2.0): a 6-day negative fill in between."""
    reader.bars["EIA.WTI.SPOT"] = [
        make_bar(_day(d), p) for d, p in ((1, 5.0), (3, -3.5), (10, 2.0))
    ]
    return reader


def _day(d: int) -> datetime:
    return datetime(2020, 4, d, tzinfo=UTC)


def bar_params(**extra: str) -> dict[str, str]:
    return {
        "instrument_id": "EIA.WTI.SPOT", "interval": "1d", "start": "2020-04-01T00:00:00Z",
        "end": "2020-04-30T00:00:00Z", "fill": "ffill", "as_of": "2020-04-29T00:00:00Z", **extra,
    }  # fmt: skip


async def _pages(client: httpx.AsyncClient, limit: int) -> list[dict[str, object]]:
    pages: list[dict[str, object]] = []
    cursor: str | None = None
    while True:
        extra = {"limit": str(limit), **({"cursor": cursor} if cursor else {})}
        body = (await client.get("/v1/bars", params=bar_params(**extra))).json()
        pages.append(body)
        cursor = body["next_cursor"]
        if cursor is None:
            return pages


async def test_ffill_pages_equal_single_page_and_start_at_the_anchor(
    client: httpx.AsyncClient, gappy: ManyTicks
) -> None:
    whole = (await client.get("/v1/bars", params=bar_params(limit="100"))).json()["rows"]
    assert [b["ts_ns"] for b in whole] == [_day_ns(d) for d in range(1, 11)]
    assert [b["close"] for b in whole] == [5.0, 5.0, -3.5, *[-3.5] * 6, 2.0]
    assert whole[3]["quality"] & 1  # filled from a negative close: negative bit set
    gappy.bar_starts.clear()
    pages = await _pages(client, 3)
    merged = [row for p in pages for row in p["rows"]]  # type: ignore[attr-defined]
    assert merged == whole  # same rows, same order, same pinned as_of
    assert len(pages) == 4  # 10 rows, 3 per page: the fill crosses two page boundaries
    # page 1 reads from start; later pages resume at the last real bar before the position
    assert gappy.bar_starts == [_day_ns(1), _day_ns(3), _day_ns(3), _day_ns(3)]
    assert set(gappy.as_of) == {int(datetime(2020, 4, 29, tzinfo=UTC).timestamp()) * 10**9}


async def test_as_of_zero_is_a_real_as_of(client: httpx.AsyncClient, gappy: ManyTicks) -> None:
    r = await client.get("/v1/bars", params=bar_params(as_of="0", fill="none"))
    assert r.status_code == 200
    assert gappy.as_of == [0]  # not replaced by the clock


# --- truncation, end to end over a real socket --------------------------------------------------
class FailingAfterThree(SyntheticReader):
    async def stream_ticks(self, instrument_id: str, start_ns: int, end_ns: int) -> RowStream:
        inner = await super().stream_ticks(instrument_id, start_ns, end_ns)

        async def batches() -> AsyncIterator[pa.RecordBatch]:
            n = 0
            async for batch in inner.batches:
                yield batch
                n += 1
                if n == 3:
                    raise ConnectionError("store died")

        return RowStream(inner.tier, batches())


@pytest.mark.parametrize("fmt", ["arrow", "parquet"])
async def test_midstream_failure_aborts_the_connection(fmt: str) -> None:
    settings = Settings.from_env({"TIDAL_ENV": "local", "TIDAL_AUTH__DISABLED": "true"})
    port: list[int] = []
    server, thread = serve(create_app(settings, reader=FailingAfterThree(1_000_000)), port)  # type: ignore[arg-type]
    p = {"instrument_id": "EIA.WTI.SPOT", "start": str(PROBE_T0), "end": str(PROBE_T0 + SPAN),
         "format": fmt}  # fmt: skip
    got = bytearray()
    try:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port[0]}") as c:

            async def download() -> None:
                async with c.stream("GET", "/v1/ticks", params=p) as r:
                    assert r.status_code == 200  # status was already sent with the first batch
                    async for chunk in r.aiter_bytes():
                        got.extend(chunk)

            with pytest.raises(httpx.RemoteProtocolError):
                await download()
    finally:
        server.should_exit = True
        thread.join(10)
    assert got  # some data arrived before the abort
    assert not bytes(got).endswith(EOS if fmt == "arrow" else b"PAR1")  # and no end marker
