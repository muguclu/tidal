"""Read routers (T-111) on fakes: instruments, bars, ticks, latest, spreads, limits, paging."""

import io
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import httpx
import pyarrow.parquet as pq
import pytest
from fastapi import FastAPI
from pyarrow import ipc

from fakes.reader import SEEDED_IDS, FakeReader, make_bar
from tidal.api.app import create_app
from tidal.api.auth_roles import has_role
from tidal.api.deps import ALL_ROLES, Principal, get_principal
from tidal.api.ratelimit import TokenBucketLimiter
from tidal.common.config import Settings
from tidal.common.timeutil import FixedClock
from tidal.contracts.models import Bar, Tick, Tier
from tidal.contracts.schemas import Interval, TickSide
from tidal.query.protocols import Rows

pytestmark = pytest.mark.anyio
PROBLEM = "application/problem+json"
WTI = "EIA.WTI.SPOT"
BRENT = "EIA.BRENT.SPOT"
HH = "EIA.HH.SPOT"
RANGE = {"start": "2020-04-01T00:00:00Z", "end": "2020-05-01T00:00:00Z"}
DAY = 86_400 * 10**9


def _ns(day: int) -> int:
    return int(datetime(2020, 4, day, tzinfo=UTC).timestamp()) * 10**9


def _tick(ts_ns: int, price: float) -> Tick:
    return Tick(
        ts=datetime.fromtimestamp(ts_ns // 10**9, UTC), ts_ns=ts_ns, price=price, size=1.0,
        side=TickSide.TRADE, quality=0, version=1,
    )  # fmt: skip


class Reader(FakeReader):
    """FakeReader plus ticks (two share a timestamp), 1h bars, and recorded ``as_of``."""

    def __init__(self) -> None:
        super().__init__()
        self.as_of: list[int | None] = []
        self.starts: list[int] = []
        base = _ns(20)
        self.ticks = [_tick(base, 1.0), _tick(base, 2.0), _tick(base + 1, -3.0), _tick(base + 2, 4)]

    async def read_bars(
        self, instrument_id: str, interval: Interval, start_ns: int, end_ns: int,
        *, as_of_ns: int | None = None,
    ) -> Rows[Bar]:  # fmt: skip
        self.as_of.append(as_of_ns)
        self.starts.append(start_ns)
        return await super().read_bars(instrument_id, interval, start_ns, end_ns)

    async def read_ticks(self, instrument_id: str, start_ns: int, end_ns: int) -> Rows[Tick]:
        self._known(instrument_id)
        self.starts.append(start_ns)
        return Rows([t for t in self.ticks if start_ns <= t.ts_ns < end_ns], Tier.HOT)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def reader() -> Reader:
    return Reader()


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock(_ns(1))


@pytest.fixture
def app(reader: Reader, clock: FixedClock) -> FastAPI:
    settings = Settings.from_env({"TIDAL_AUTH__DISABLED": "true"})
    return create_app(settings, reader=reader, clock=clock)


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


def as_roles(app: FastAPI, *roles: str, oid: str = "u1") -> None:
    """Replace the principal with a stub holding only ``roles``."""
    app.dependency_overrides[get_principal] = lambda: Principal(oid, frozenset(roles))


# --- instruments -------------------------------------------------------------------------------


async def test_instruments_list_and_filters(client: httpx.AsyncClient) -> None:
    r = await client.get("/v1/instruments")
    assert r.status_code == 200
    assert {i["instrument_id"] for i in r.json()["items"]} == set(SEEDED_IDS)
    assert "X-Tidal-Tier" not in r.headers
    r = await client.get("/v1/instruments", params={"asset_class": "gas", "source": "EIA"})
    assert [i["instrument_id"] for i in r.json()["items"]] == [HH]
    assert (await client.get("/v1/instruments", params={"source": "XXX"})).status_code == 400


async def test_instrument_get_and_404(client: httpx.AsyncClient) -> None:
    r = await client.get(f"/v1/instruments/{WTI}")
    assert r.status_code == 200
    assert r.json()["instrument_id"] == WTI
    r = await client.get("/v1/instruments/EIA.NOPE.SPOT")
    assert r.status_code == 404
    assert r.headers["content-type"].startswith(PROBLEM)
    assert (await client.get("/v1/instruments/not-an-id")).status_code == 400


# --- bars --------------------------------------------------------------------------------------


def bars_params(instrument_id: str = WTI, **extra: str) -> dict[str, str]:
    return {"instrument_id": instrument_id, "interval": "1d", **RANGE, **extra}


async def test_bars_happy_path_preserves_negative_price(client: httpx.AsyncClient) -> None:
    r = await client.get("/v1/bars", params=bars_params())
    assert r.status_code == 200
    body = r.json()
    assert r.headers["X-Tidal-Tier"] == body["tier"] == "cold"
    assert body["next_cursor"] is None
    negative = next(b for b in body["rows"] if b["ts"] == "2020-04-20T00:00:00Z")
    assert negative["close"] == -37.63
    assert negative["ts_ns"] == _ns(20)


async def test_bars_accepts_epoch_ns_and_passes_as_of(
    client: httpx.AsyncClient, reader: Reader
) -> None:
    params = bars_params(start=str(_ns(17)), end=str(_ns(21)), as_of="2020-04-22T00:00:00Z")
    r = await client.get("/v1/bars", params=params)
    assert [b["ts_ns"] for b in r.json()["rows"]] == [_ns(17), _ns(20)]
    assert reader.as_of == [_ns(22)]


async def test_bars_unknown_instrument_404(client: httpx.AsyncClient) -> None:
    r = await client.get("/v1/bars", params=bars_params("EIA.NOPE.SPOT"))
    assert r.status_code == 404
    assert r.json()["correlation_id"] == r.headers["X-Correlation-Id"]


@pytest.mark.parametrize(
    "override",
    [
        {"interval": "5m"},
        {"start": "yesterday"},
        {"start": "2020-05-02T00:00:00Z"},  # start after end
        {"limit": "0"},
        {"limit": "100001"},
        {"fill": "bfill"},
        {"as_of": "2020-13-45T00:00:00Z"},  # matches the pattern, not a real time
        {"cursor": "garbage"},
    ],
)
async def test_bars_validation_400(client: httpx.AsyncClient, override: dict[str, str]) -> None:
    r = await client.get("/v1/bars", params=bars_params(**override))
    assert r.status_code == 400
    assert r.headers["content-type"].startswith(PROBLEM)


async def test_bars_1s_range_limit_413(client: httpx.AsyncClient) -> None:
    ok = {"start": "2020-04-01T00:00:00Z", "end": "2020-04-08T00:00:00Z"}
    r = await client.get("/v1/bars", params=bars_params(interval="1s", **ok))
    assert r.status_code == 200
    r = await client.get("/v1/bars", params=bars_params(interval="1s", **RANGE))
    assert r.status_code == 413
    too_long = {"start": "2000-01-01T00:00:00Z", "end": "2025-01-01T00:00:00Z"}
    assert (await client.get("/v1/bars", params=bars_params(**too_long))).status_code == 200


async def test_bars_ffill_only_when_requested(client: httpx.AsyncClient) -> None:
    plain = (await client.get("/v1/bars", params=bars_params())).json()["rows"]
    assert len(plain) == 3
    filled = (await client.get("/v1/bars", params=bars_params(fill="ffill"))).json()["rows"]
    assert [b["ts_ns"] for b in filled] == [_ns(d) for d in (17, 18, 19, 20, 21)]
    assert filled[1]["close"] == filled[2]["close"] == 18.27  # carried from the 17th
    assert filled[1]["volume"] is None
    assert filled[3]["close"] == -37.63


async def test_bars_pagination_round_trip(client: httpx.AsyncClient) -> None:
    seen: list[int] = []
    cursor: str | None = None
    pages = 0
    while True:
        extra = {"limit": "2", "fill": "ffill", **({"cursor": cursor} if cursor else {})}
        body = (await client.get("/v1/bars", params=bars_params(**extra))).json()
        seen += [b["ts_ns"] for b in body["rows"]]
        pages += 1
        cursor = body["next_cursor"]
        if cursor is None:
            break
    assert pages == 3
    assert seen == [_ns(d) for d in (17, 18, 19, 20, 21)]


async def test_bars_non_ffill_cursor_reads_from_cursor_position(
    client: httpx.AsyncClient, reader: Reader
) -> None:
    first = (await client.get("/v1/bars", params=bars_params(limit="1"))).json()
    second = (
        await client.get("/v1/bars", params=bars_params(limit="1", cursor=first["next_cursor"]))
    ).json()
    assert [b["ts_ns"] for b in second["rows"]] == [_ns(20)]
    assert reader.starts[-1] == _ns(20)


async def test_cursor_tampering_and_binding(client: httpx.AsyncClient, app: FastAPI) -> None:
    cursor = (await client.get("/v1/bars", params=bars_params(limit="1"))).json()["next_cursor"]
    payload, _, sig = cursor.partition(".")
    for bad in (f"{payload}.{sig[:-2]}AA", f"x{payload}.{sig}", payload, ""):
        r = await client.get("/v1/bars", params=bars_params(cursor=bad or "."))
        assert r.status_code == 400, bad
    # bound to the query ...
    r = await client.get("/v1/bars", params=bars_params(BRENT, cursor=cursor))
    assert r.status_code == 400
    # ... and to the principal
    as_roles(app, "Tidal.Reader", oid="someone-else")
    assert (await client.get("/v1/bars", params=bars_params(cursor=cursor))).status_code == 400
    # a differently signed but well-formed cursor is rejected too
    other = create_app(Settings.from_env({"TIDAL_AUTH__DISABLED": "true"}), reader=Reader())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=other), base_url="http://test"
    ) as c2:
        foreign = (await c2.get("/v1/bars", params=bars_params(limit="1"))).json()["next_cursor"]
    app.dependency_overrides.clear()
    assert (await client.get("/v1/bars", params=bars_params(cursor=foreign))).status_code == 400


async def test_bars_arrow_and_parquet_stream(client: httpx.AsyncClient) -> None:
    r = await client.get("/v1/bars", params=bars_params(format="arrow"))
    assert r.headers["content-type"] == "application/vnd.apache.arrow.stream"
    assert r.headers["X-Tidal-Tier"] == "cold"
    table = ipc.open_stream(io.BytesIO(r.content)).read_all()
    assert table.num_rows == 3
    assert -37.63 in table.column("close").to_pylist()
    r = await client.get("/v1/bars", params=bars_params(format="parquet"))
    assert r.headers["content-type"] == "application/vnd.apache.parquet"
    assert pq.read_table(io.BytesIO(r.content)).num_rows == 3
    r = await client.get("/v1/bars", params=bars_params(format="arrow", fill="ffill"))
    assert r.status_code == 400
    r = await client.get("/v1/bars", params=bars_params("EIA.NOPE.SPOT", format="arrow"))
    assert r.status_code == 404


async def test_empty_stream_still_has_schema(client: httpx.AsyncClient, app: FastAPI) -> None:
    r = await client.get("/v1/bars", params=bars_params(interval="1h", format="arrow"))
    assert ipc.open_stream(io.BytesIO(r.content)).read_all().num_rows == 0
    r = await client.get("/v1/ticks", params={"instrument_id": WTI, **RANGE, "format": "parquet"})
    assert pq.read_table(io.BytesIO(r.content)).schema.names[0] == "ts_ns"


# --- ticks -------------------------------------------------------------------------------------


def tick_params(**extra: str) -> dict[str, str]:
    return {
        "instrument_id": WTI,
        "start": "2020-04-20T00:00:00Z",
        "end": "2020-04-21T00:00:00Z",
        **extra,
    }


async def test_ticks_happy_path_and_tier(client: httpx.AsyncClient) -> None:
    r = await client.get("/v1/ticks", params=tick_params())
    assert r.status_code == 200
    assert r.headers["X-Tidal-Tier"] == "hot"
    assert [t["price"] for t in r.json()["rows"]] == [1.0, 2.0, -3.0, 4.0]


async def test_ticks_pagination_with_duplicate_timestamps(client: httpx.AsyncClient) -> None:
    prices: list[float] = []
    cursor: str | None = None
    while True:
        extra = {"limit": "1", **({"cursor": cursor} if cursor else {})}
        body = (await client.get("/v1/ticks", params=tick_params(**extra))).json()
        prices += [t["price"] for t in body["rows"]]
        cursor = body["next_cursor"]
        if cursor is None:
            break
    assert prices == [1.0, 2.0, -3.0, 4.0]


async def test_ticks_31_day_limit_and_errors(client: httpx.AsyncClient) -> None:
    ok = tick_params(start="2020-04-01T00:00:00Z", end="2020-05-02T00:00:00Z")  # exactly 31 d
    assert (await client.get("/v1/ticks", params=ok)).status_code == 200
    long = tick_params(start="2020-04-01T00:00:00Z", end="2020-05-02T00:00:01Z")
    assert (await client.get("/v1/ticks", params=long)).status_code == 413
    assert (await client.get("/v1/ticks", params=tick_params(limit="x"))).status_code == 400
    unknown = {**tick_params(), "instrument_id": "EIA.NOPE.SPOT"}
    assert (await client.get("/v1/ticks", params=unknown)).status_code == 404


# --- latest ------------------------------------------------------------------------------------


async def test_latest(client: httpx.AsyncClient) -> None:
    r = await client.get("/v1/latest", params=[("instrument_id", WTI), ("instrument_id", HH)])
    assert r.status_code == 200
    assert r.headers["X-Tidal-Tier"] == "hot"
    body = r.json()
    assert body["tier"] == "hot"
    assert {row["instrument_id"]: row["bar"]["close"] for row in body["rows"]}[WTI] == 8.91
    assert (await client.get("/v1/latest", params={"instrument_id": "EIA.NOPE.SPOT"})).json()[
        "rows"
    ] == []
    r = await client.get("/v1/latest", params={"instrument_id": WTI, "interval": "1h"})
    assert r.json()["rows"] == []


async def test_latest_validation(client: httpx.AsyncClient) -> None:
    assert (await client.get("/v1/latest")).status_code == 400
    many: list[tuple[str, str | int | float | bool | None]] = [("instrument_id", WTI)] * 101
    assert (await client.get("/v1/latest", params=many)).status_code == 400


# --- spreads -----------------------------------------------------------------------------------


def spread_params(**extra: str) -> dict[str, str]:
    return {"leg_a": BRENT, "leg_b": WTI, "interval": "1d", **RANGE, **extra}


async def test_spreads_inner_join(client: httpx.AsyncClient, reader: Reader) -> None:
    reader.bars[HH].append(make_bar(datetime(2020, 4, 22, tzinfo=UTC), 2.0))  # only in leg_a
    r = await client.get("/v1/spreads", params=spread_params(leg_a=HH))
    assert r.status_code == 200
    rows = r.json()["rows"]
    assert [x["ts_ns"] for x in rows] == [_ns(17), _ns(20), _ns(21)]  # the 22nd is dropped
    r = await client.get("/v1/spreads", params=spread_params())
    row = next(x for x in r.json()["rows"] if x["ts_ns"] == _ns(20))
    assert row["leg_b"] == -37.63
    assert row["spread"] == pytest.approx(45.0 + 37.63)
    assert r.headers["X-Tidal-Tier"] == "cold"


async def test_spreads_errors(client: httpx.AsyncClient) -> None:
    assert (await client.get("/v1/spreads", params=spread_params(leg_b=BRENT))).status_code == 400
    missing = {k: v for k, v in spread_params().items() if k != "leg_b"}
    assert (await client.get("/v1/spreads", params=missing)).status_code == 400
    assert (
        await client.get("/v1/spreads", params=spread_params(leg_b="EIA.X.SPOT"))
    ).status_code == 404
    r = await client.get("/v1/spreads", params=spread_params(interval="1s"))
    assert r.status_code == 413


async def test_spreads_mixed_tier(app: FastAPI, client: httpx.AsyncClient, reader: Reader) -> None:
    original = reader.read_bars

    async def hot_for_hh(instrument_id: str, *a: Any, **kw: Any) -> Rows[Bar]:
        got = await original(instrument_id, *a, **kw)
        return Rows(got.rows, Tier.HOT) if instrument_id == HH else got

    reader.read_bars = hot_for_hh  # type: ignore[method-assign]
    r = await client.get("/v1/spreads", params=spread_params(leg_b=HH))
    assert r.headers["X-Tidal-Tier"] == r.json()["tier"] == "mixed"


# --- roles -------------------------------------------------------------------------------------

READER, TRADER, QUANT, ADMIN = ("Tidal." + n for n in ("Reader", "Trader", "Quant", "Admin"))
PATHS: dict[str, tuple[str, dict[str, Any]]] = {
    "instruments": ("/v1/instruments", {}),
    "instrument": (f"/v1/instruments/{WTI}", {}),
    "bars": ("/v1/bars", bars_params()),
    "latest": ("/v1/latest", {"instrument_id": WTI}),
    "ticks": ("/v1/ticks", tick_params()),
    "spreads": ("/v1/spreads", spread_params()),
}
MIN_ROLE = {"ticks": TRADER, "spreads": TRADER}


@pytest.mark.parametrize("role", [READER, TRADER, QUANT, ADMIN])
@pytest.mark.parametrize("name", list(PATHS))
async def test_role_matrix(client: httpx.AsyncClient, app: FastAPI, name: str, role: str) -> None:
    as_roles(app, role)
    path, params = PATHS[name]
    allowed = {READER: 0, TRADER: 1, QUANT: 2}.get(role, -1) >= (1 if name in MIN_ROLE else 0)
    r = await client.get(path, params=params)
    assert r.status_code == (200 if allowed else 403), (name, role)
    if not allowed:
        assert r.headers["content-type"].startswith(PROBLEM)


def test_has_role_hierarchy() -> None:
    p = Principal("x", frozenset({QUANT}))
    assert has_role(p, READER)
    assert has_role(p, TRADER)
    assert has_role(p, QUANT)
    assert not has_role(p, ADMIN)
    assert has_role(Principal("a", ALL_ROLES), ADMIN)


async def test_no_token_is_401_when_auth_enabled(reader: Reader) -> None:
    app = create_app(Settings.from_env({}), reader=reader)  # local, auth not disabled
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        r = await c.get("/v1/instruments")
    assert r.status_code == 401


async def test_missing_reader_is_503() -> None:
    app = create_app(Settings.from_env({"TIDAL_AUTH__DISABLED": "true"}))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        assert (await c.get("/v1/instruments")).status_code == 503


# --- rate limit --------------------------------------------------------------------------------


async def test_rate_limit_per_principal_with_injected_clock(
    client: httpx.AsyncClient, app: FastAPI, clock: FixedClock
) -> None:
    as_roles(app, READER, oid="a")
    for _ in range(60):
        assert (await client.get("/v1/instruments")).status_code == 200
    r = await client.get("/v1/instruments")
    assert r.status_code == 429
    assert r.headers["Retry-After"] == "1"
    assert r.headers["content-type"].startswith(PROBLEM)
    as_roles(app, READER, oid="b")  # another principal has its own bucket
    assert (await client.get("/v1/instruments")).status_code == 200
    as_roles(app, READER, oid="a")
    clock.advance(1_000_000_000)  # one token refilled, no sleeping
    assert (await client.get("/v1/instruments")).status_code == 200
    assert (await client.get("/v1/instruments")).status_code == 429


def test_limiter_refill_is_capped_and_prunes(clock: FixedClock) -> None:
    limiter = TokenBucketLimiter(clock, capacity=2)
    limiter.check("k")
    clock.advance(3600 * 10**9)
    limiter.check("k")
    limiter.check("k")
    with pytest.raises(Exception, match="Rate limit"):
        limiter.check("k")
    import tidal.api.ratelimit as rl  # noqa: PLC0415

    rl_prune = rl._PRUNE_ABOVE
    rl._PRUNE_ABOVE = 1
    try:
        clock.advance(3600 * 10**9)
        limiter.check("other")
        assert list(limiter._buckets) == ["other"]
    finally:
        rl._PRUNE_ABOVE = rl_prune
