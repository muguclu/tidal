"""T-210: bounded JSON paging, truncation safety, as_of and fill over the streaming reader."""

import io
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import httpx
import pyarrow as pa
import pytest
from fastapi import FastAPI
from pyarrow import ipc

from fakes.reader import FakeReader
from tidal.api.app import create_app
from tidal.api.streaming import _encode, stream_response
from tidal.common.config import Settings
from tidal.common.errors import DependencyUnavailable
from tidal.common.timeutil import FixedClock
from tidal.contracts.models import Tier
from tidal.contracts.schemas import TICK_V1, Interval
from tidal.query.protocols import RowStream

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
    # No end-of-stream marker: a client that checks for it (a Parquet reader needs the footer)
    # can tell the body is incomplete.
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
