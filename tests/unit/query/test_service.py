"""tidal-query service + RemoteReader (T-209): parity with the in-process reader, tier, auth."""

import struct
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import httpx
import pyarrow as pa
import pytest
from fastapi import FastAPI

from fakes.reader import FakeReader
from tidal.api.app import create_app
from tidal.common.config import Settings
from tidal.common.errors import (
    ConfigError,
    DependencyUnavailable,
    NotFound,
    RangeTooLarge,
    Unauthenticated,
    ValidationFailed,
)
from tidal.common.logging import bind_correlation_id, reset_correlation_id
from tidal.contracts.instruments import AssetClass, Source
from tidal.contracts.models import Tick, Tier
from tidal.contracts.schemas import TICK_V1, Interval, TickSide
from tidal.query.protocols import Rows, RowStream
from tidal.query.service import (
    AnonymousTokenProvider,
    FailClosedValidator,
    ManagedIdentityTokenProvider,
    RemoteReader,
    create_query_app,
    remote_reader_from_settings,
    token_provider_for,
)

pytestmark = pytest.mark.anyio
WTI = "EIA.WTI.SPOT"
LO = int(datetime(2020, 4, 1, tzinfo=UTC).timestamp()) * 10**9
HI = int(datetime(2020, 5, 1, tzinfo=UTC).timestamp()) * 10**9
LOCAL = Settings.from_env({"TIDAL_ENV": "local", "TIDAL_AUTH__DISABLED": "true"})
DAY = 86_400 * 10**9
SECURED = Settings.from_env({"TIDAL_ENV": "local"})


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class TierReader(FakeReader):
    """FakeReader reporting a fixed tier (the labelling itself is the reader's job)."""

    def __init__(self, tier: Tier) -> None:
        super().__init__()
        self.tier = tier

    async def read_bars(self, *args: Any, **kwargs: Any) -> Rows[Any]:
        got = await super().read_bars(*args, **kwargs)
        return Rows(got.rows, self.tier)

    async def stream_bars(self, *args: Any, **kwargs: Any) -> RowStream:
        got = await super().stream_bars(*args, **kwargs)
        return RowStream(self.tier, got.batches)


class RecordingTokens:
    def __init__(self, token: str = "tok-123") -> None:  # noqa: S107 - fake token
        self.token_value = token
        self.audiences: list[str] = []

    async def token(self, audience: str) -> str:
        self.audiences.append(audience)
        return self.token_value


class AcceptToken:
    def __init__(self, expected: str) -> None:
        self.expected = expected

    async def validate(self, token: str) -> None:
        if token != self.expected:
            raise Unauthenticated("bad token")


def remote(query_app: FastAPI, tokens: RecordingTokens | None = None) -> RemoteReader:
    return remote_reader_from_settings(
        SECURED, tokens=tokens or RecordingTokens(), transport=httpx.ASGITransport(app=query_app)
    )


def secured_query(reader: FakeReader) -> FastAPI:
    return create_query_app(reader, AcceptToken("tok-123"), SECURED)


async def test_reads_match_in_process_reader() -> None:
    fake = FakeReader()
    rr = remote(secured_query(fake))
    want = await fake.read_bars(WTI, Interval.D1, LO, HI)
    got = await rr.read_bars(WTI, Interval.D1, LO, HI, as_of_ns=5)
    assert got.rows == want.rows
    assert got.tier is want.tier
    assert any(b.close == -37.63 for b in got.rows)  # negative prices survive the wire
    ids = [WTI, "EIA.HH.SPOT"]
    assert await rr.latest(ids) == await fake.latest(ids)
    assert await rr.instruments() == await fake.instruments()
    assert await rr.read_ticks(WTI, LO, HI) == await fake.read_ticks(WTI, LO, HI)
    await rr.ping()


async def test_not_found_maps_to_typed_error() -> None:
    rr = remote(secured_query(FakeReader()))
    with pytest.raises(NotFound):
        await rr.read_bars("EIA.NOPE.SPOT", Interval.D1, LO, HI)
    with pytest.raises(NotFound):
        await rr.stream_bars("EIA.NOPE.SPOT", Interval.D1, LO, HI)


async def test_stream_roundtrip_is_batchwise() -> None:
    fake = FakeReader()
    rr = remote(secured_query(fake))
    stream = await rr.stream_bars(WTI, Interval.D1, LO, HI)
    batches = [b async for b in stream.batches]
    want = [b async for b in (await fake.stream_bars(WTI, Interval.D1, LO, HI)).batches]
    assert len(batches) == len(want) == 3
    got_t, want_t = pa.Table.from_batches(batches), pa.Table.from_batches(want)
    assert got_t.schema == want_t.schema
    assert got_t.drop_columns(["volume"]).equals(want_t.drop_columns(["volume"]))  # NaN != NaN
    assert stream.tier is Tier.COLD


@pytest.mark.parametrize("tier", list(Tier))
async def test_tier_is_carried_through_the_public_api(tier: Tier) -> None:
    app = create_app(LOCAL, reader=remote(secured_query(TierReader(tier))))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        q = {
            "instrument_id": WTI, "interval": "1d",
            "start": "2020-04-01T00:00:00Z", "end": "2020-05-01T00:00:00Z",
        }  # fmt: skip
        r = await c.get("/v1/bars", params=q)
        assert r.status_code == 200
        assert r.headers["X-Tidal-Tier"] == tier.value
        assert r.json()["tier"] == tier.value
        s = await c.get("/v1/bars", params={**q, "format": "arrow"})
        assert s.headers["X-Tidal-Tier"] == tier.value
        assert pa.ipc.open_stream(s.content).read_all().num_rows == 3
        assert (await c.get("/v1/instruments")).status_code == 200
        miss = await c.get("/v1/bars", params={**q, "instrument_id": "EIA.NOPE.SPOT"})
        assert miss.status_code == 404
        assert miss.headers["content-type"].startswith("application/problem+json")


async def test_query_service_requires_the_token_and_sends_audience() -> None:
    fake = FakeReader()
    tokens = RecordingTokens()
    await remote(secured_query(fake), tokens).ping()
    assert tokens.audiences == [SECURED.query.audience]
    with pytest.raises(DependencyUnavailable):  # rejected credentials surface as 503
        await remote(secured_query(fake), RecordingTokens("other")).ping()
    with pytest.raises(DependencyUnavailable):
        await remote(secured_query(fake), RecordingTokens("")).ping()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=secured_query(fake)), base_url="http://q"
    ) as raw:
        assert (await raw.get("/internal/v1/ping")).status_code == 401
        assert (await raw.get("/health/live")).status_code == 200


async def test_default_validator_fails_closed_and_local_disabled_skips_auth() -> None:
    closed = create_query_app(FakeReader(), FailClosedValidator(), SECURED)
    with pytest.raises(DependencyUnavailable):
        await remote(closed, RecordingTokens("anything")).ping()
    open_local = create_query_app(FakeReader(), FailClosedValidator(), LOCAL)
    reader = remote_reader_from_settings(
        LOCAL, tokens=RecordingTokens(""), transport=httpx.ASGITransport(app=open_local)
    )
    await reader.ping()


def test_token_provider_choice_and_auth_guard() -> None:
    assert isinstance(token_provider_for(LOCAL), AnonymousTokenProvider)
    assert isinstance(token_provider_for(SECURED), ManagedIdentityTokenProvider)
    bad = LOCAL.model_copy(update={"env": "dev"})
    with pytest.raises(ConfigError):
        create_query_app(FakeReader(), FailClosedValidator(), bad)


async def test_unreachable_query_service_is_503() -> None:
    def boom(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    rr = remote_reader_from_settings(
        LOCAL, tokens=RecordingTokens(""), transport=httpx.MockTransport(boom)
    )
    with pytest.raises(DependencyUnavailable):
        await rr.read_bars(WTI, Interval.D1, LO, HI)
    with pytest.raises(DependencyUnavailable):
        await rr.stream_bars(WTI, Interval.D1, LO, HI)


async def test_truncated_stream_is_detected() -> None:
    def cut(_: httpx.Request) -> httpx.Response:
        # one valid frame header announcing more bytes than are sent, no end marker
        return httpx.Response(200, headers={"X-Tidal-Tier": "hot"}, content=b"\x00" * 7 + b"\x09ab")

    rr = remote_reader_from_settings(
        LOCAL, tokens=RecordingTokens(""), transport=httpx.MockTransport(cut)
    )
    stream = await rr.stream_bars(WTI, Interval.D1, LO, HI)
    with pytest.raises(DependencyUnavailable):
        _ = [b async for b in stream.batches]


async def test_midstream_failure_is_not_reported_as_complete() -> None:
    class Dying(FakeReader):
        async def stream_bars(self, *args: Any, **kwargs: Any) -> RowStream:
            got = await super().stream_bars(*args, **kwargs)

            async def batches() -> AsyncIterator[pa.RecordBatch]:
                async for b in got.batches:
                    yield b
                    raise RuntimeError("store died")

            return RowStream(Tier.HOT, batches())

    app = secured_query(Dying())
    rr = remote_reader_from_settings(
        SECURED,
        tokens=RecordingTokens(),
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
    )
    stream = await rr.stream_bars(WTI, Interval.D1, LO, HI)
    with pytest.raises(DependencyUnavailable):
        _ = [b async for b in stream.batches]


# -- review round -------------------------------------------------------------------------------
def _tick(ts_ns: int, price: float) -> Tick:
    return Tick(
        ts=datetime.fromtimestamp(ts_ns // 10**9, UTC), ts_ns=ts_ns, price=price, size=1.5,
        side=TickSide.TRADE, quality=0, version=1,
    )  # fmt: skip


class RecordingReader(FakeReader):
    """FakeReader with ticks, per-instrument tiers and recorded ``as_of_ns``."""

    def __init__(self) -> None:
        super().__init__()
        self.as_of: list[int | None] = []
        self.tiers: dict[str, Tier] = {}
        self.ticks = [_tick(LO, -1.0), _tick(LO, 2.0), _tick(LO + 1, 3.0)]

    async def read_bars(self, instrument_id: str, *args: Any, **kwargs: Any) -> Rows[Any]:
        self.as_of.append(kwargs.get("as_of_ns"))
        got = await super().read_bars(instrument_id, *args, **kwargs)
        return Rows(got.rows, self.tiers.get(instrument_id, got.tier))

    async def read_ticks(self, instrument_id: str, start_ns: int, end_ns: int) -> Rows[Any]:
        self._known(instrument_id)
        return Rows([t for t in self.ticks if start_ns <= t.ts_ns < end_ns], Tier.HOT)

    async def stream_ticks(self, instrument_id: str, start_ns: int, end_ns: int) -> RowStream:
        got = await self.read_ticks(instrument_id, start_ns, end_ns)

        async def batches() -> AsyncIterator[pa.RecordBatch]:
            for t in got.rows:
                yield pa.RecordBatch.from_pylist(
                    [{"ts_ns": t.ts_ns, "instrument_id": instrument_id, "price": t.price,
                      "size": t.size, "side": 0, "quality": 0, "ingested_at_ns": 0,
                      "version": 1}],
                    schema=TICK_V1,
                )  # fmt: skip

        return RowStream(Tier.HOT, batches())


async def test_as_of_arrives_and_ticks_roundtrip() -> None:
    fake = RecordingReader()
    rr = remote(secured_query(fake))
    await rr.read_bars(WTI, Interval.D1, LO, HI, as_of_ns=123)
    await rr.read_bars(WTI, Interval.D1, LO, HI)
    assert fake.as_of == [123, None]
    got = await rr.read_ticks(WTI, LO, HI)
    assert got.rows == fake.ticks
    assert got.tier is Tier.HOT
    stream = await rr.stream_ticks(WTI, LO, HI)
    table = pa.Table.from_batches([b async for b in stream.batches])
    assert table.column("ts_ns").to_pylist() == [t.ts_ns for t in fake.ticks]
    assert table.column("price").to_pylist() == [-1.0, 2.0, 3.0]


async def test_instrument_filters_roundtrip() -> None:
    fake = RecordingReader()
    rr = remote(secured_query(fake))
    crude = await rr.instruments(asset_class=AssetClass.CRUDE)
    assert crude == await fake.instruments(asset_class=AssetClass.CRUDE)
    assert 0 < len(crude) < len(await rr.instruments())
    assert await rr.instruments(source=Source.EIA) == await fake.instruments(source=Source.EIA)
    assert await rr.instruments(asset_class=AssetClass.CRUDE, source=Source.FRED) == []


async def test_spread_across_tiers_is_mixed() -> None:
    fake = RecordingReader()
    fake.tiers = {"EIA.WTI.SPOT": Tier.COLD, "EIA.BRENT.SPOT": Tier.HOT}
    app = create_app(LOCAL, reader=remote(secured_query(fake)))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get(
            "/v1/spreads",
            params={"leg_a": "EIA.WTI.SPOT", "leg_b": "EIA.BRENT.SPOT", "interval": "1d",
                    "start": "2020-04-01T00:00:00Z", "end": "2020-05-01T00:00:00Z"},
        )  # fmt: skip
    assert r.status_code == 200
    assert r.headers["X-Tidal-Tier"] == "mixed"
    assert r.json()["tier"] == "mixed"


def _stream_client(body: bytes) -> RemoteReader:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"X-Tidal-Tier": "hot"}, content=body)

    return remote_reader_from_settings(
        LOCAL, tokens=RecordingTokens(""), transport=httpx.MockTransport(handler)
    )


def _frame(batch: pa.RecordBatch) -> bytes:
    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, batch.schema) as w:
        w.write_batch(batch)
    data = sink.getvalue().to_pybytes()
    return struct.pack(">Q", len(data)) + data


async def test_framing_requires_terminator_and_rejects_corrupt_length() -> None:
    fake = FakeReader()
    batch = await anext((await fake.stream_bars(WTI, Interval.D1, LO, HI)).batches)
    whole = _frame(batch)
    ok = await _stream_client(whole + struct.pack(">Q", 0)).stream_bars(WTI, Interval.D1, LO, HI)
    assert [b.num_rows async for b in ok.batches] == [1]
    # ends exactly on a frame boundary, no terminator: truncated
    cut = await _stream_client(whole).stream_bars(WTI, Interval.D1, LO, HI)
    with pytest.raises(DependencyUnavailable, match="truncated"):
        _ = [b async for b in cut.batches]
    huge = await _stream_client(struct.pack(">Q", (64 << 20) + 1)).stream_bars(
        WTI, Interval.D1, LO, HI
    )
    with pytest.raises(DependencyUnavailable, match="corrupt"):
        _ = [b async for b in huge.batches]


async def test_correlation_id_is_propagated() -> None:
    seen: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("X-Correlation-Id"))
        return httpx.Response(204)

    rr = remote_reader_from_settings(
        LOCAL, tokens=RecordingTokens(""), transport=httpx.MockTransport(handler)
    )
    reset = bind_correlation_id("cid-abc-123")
    try:
        await rr.ping()
    finally:
        reset_correlation_id(reset)
    assert seen == ["cid-abc-123"]


async def test_token_failure_invalid_body_and_bad_request() -> None:
    class Boom:
        async def token(self, audience: str) -> str:
            raise RuntimeError("credential-detail")

    def ok_but_invalid(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"tier": "hot", "rows": [{"nope": 1}]})

    def bad_request(_: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"detail": "bad"})

    failing = remote_reader_from_settings(
        SECURED, tokens=Boom(), transport=httpx.ASGITransport(app=secured_query(FakeReader()))
    )
    with pytest.raises(DependencyUnavailable) as err:
        await failing.ping()
    assert "credential-detail" not in str(err.value)
    invalid = remote_reader_from_settings(
        LOCAL, tokens=RecordingTokens(""), transport=httpx.MockTransport(ok_but_invalid)
    )
    with pytest.raises(DependencyUnavailable, match="invalid response"):
        await invalid.read_bars(WTI, Interval.D1, LO, HI)
    drift = remote_reader_from_settings(
        LOCAL, tokens=RecordingTokens(""), transport=httpx.MockTransport(bad_request)
    )
    with pytest.raises(ValidationFailed):
        await drift.ping()


async def test_query_service_enforces_range_limits() -> None:
    rr = remote(secured_query(FakeReader()))
    with pytest.raises(RangeTooLarge):
        await rr.read_bars(WTI, Interval.S1, LO, LO + 8 * DAY)
    with pytest.raises(RangeTooLarge):
        await rr.stream_bars(WTI, Interval.M1, LO, LO + 367 * DAY)
    with pytest.raises(RangeTooLarge):
        await rr.read_ticks(WTI, LO, LO + 32 * DAY)
    with pytest.raises(RangeTooLarge):
        await rr.stream_ticks(WTI, LO, LO + 32 * DAY)


async def test_aclose_closes_the_token_provider() -> None:
    class Closing(RecordingTokens):
        closed = False

        async def close(self) -> None:
            self.closed = True

    tokens = Closing()
    await remote(secured_query(FakeReader()), tokens).aclose()
    assert tokens.closed
