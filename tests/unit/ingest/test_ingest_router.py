import io
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from sqlalchemy import Engine, create_engine, event
from sqlalchemy.pool import StaticPool

from tidal.common.timeutil import FixedClock
from tidal.contracts.schemas import BAR_V1, TICK_V1, Interval, QualityFlag
from tidal.ingest.router import DAY_NS, Routed, Router, read_part
from tidal.storage import sql_repo as sr
from tidal.storage.parquet_writer import LocalStore, ObjectStore

WTI = "EIA.WTI.SPOT"
NOW = 1_700_000_000 * 10**9  # 2023-11-14
OLD = (NOW // DAY_NS - 400) * DAY_NS  # ~400 days before now: cold with a 90 day window
RECENT = (NOW // DAY_NS - 3) * DAY_NS  # hot


@pytest.fixture
def engine() -> Engine:
    eng = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )

    @event.listens_for(eng, "connect")
    def _attach(dbapi: Any, _rec: object) -> None:
        for schema in ("ref", "md", "ops"):
            dbapi.execute(f"ATTACH DATABASE ':memory:' AS {schema}")

    sr.metadata.create_all(eng)
    return eng


@pytest.fixture
def router(engine: Engine, tmp_path: Path) -> Router:
    clock = FixedClock(NOW)
    r = Router(sr.SqlRepository(engine, clock), LocalStore(tmp_path), clock, 90, dedupe_cold=True)
    r.ensure_instruments([WTI, "EIA.BRENT.SPOT"])
    return r


def bars(ts: list[int], prices: list[float], *, ingested: int = NOW, iid: str = WTI) -> pa.Table:
    n = len(ts)
    px = np.array(prices)
    return pa.table(
        {
            "ts_ns": np.array(ts, np.int64),
            "instrument_id": pa.array([iid] * n).dictionary_encode().cast(BAR_V1.field(1).type),
            "interval": pa.array(["1d"] * n).dictionary_encode().cast(BAR_V1.field(2).type),
            "open": px, "high": px, "low": px, "close": px,
            "volume": np.full(n, np.nan),
            "trade_count": pa.nulls(n, pa.int32()),
            "quality": np.zeros(n, np.int8),
            "ingested_at_ns": np.full(n, ingested, np.int64),
            "version": np.ones(n, np.int16),
        },
        schema=BAR_V1,
    )  # fmt: skip


def parts(store: ObjectStore) -> list[str]:
    return [p for p in store.list_paths("marketdata/") if p.endswith(".parquet")]


def test_split_hot_and_cold_by_window(router: Router) -> None:
    t = bars([OLD, RECENT, NOW], [1.0, 2.0, 3.0])
    hot, cold = router.split(t)
    assert hot.column("ts_ns").to_pylist() == [RECENT, NOW]
    assert cold.column("ts_ns").to_pylist() == [OLD]
    assert router.cutoff_ns == NOW - 90 * DAY_NS


def test_routes_daily_bars_to_sql_and_year_partition(router: Router) -> None:
    out = router.route_bars(bars([OLD, RECENT], [1.0, 2.0]), "h")
    assert out == Routed(hot=1, cold=1)
    year = int(np.datetime64(OLD, "ns").astype("datetime64[Y]").astype(int)) + 1970
    assert parts(router.store) == [
        f"marketdata/v1/bars_1d/instrument={WTI}/year={year}/month=00/part-00000.parquet"
    ]
    assert router.repo.read_bars(WTI, Interval.D1, 0, NOW + 1).column("close").to_pylist() == [2.0]
    assert isinstance(router.store, LocalStore)
    meta = pq.read_metadata(router.store.root / parts(router.store)[0]).metadata or {}
    assert meta[b"tidal.source_hash"] == b"h"


def test_unchanged_cold_rows_write_nothing_and_restatement_appends_v2(router: Router) -> None:
    router.route_bars(bars([OLD], [1.0]), "h")
    assert router.route_bars(bars([OLD], [1.0]), "h") == Routed()
    assert len(parts(router.store)) == 1
    assert router.route_bars(bars([OLD], [2.0], ingested=NOW + 1), "h2") == Routed(0, 1)
    p1 = read_part(router.store, parts(router.store)[1])
    assert p1.column("version").to_pylist() == [2]
    assert p1.column("quality").to_pylist() == [int(QualityFlag.RESTATED)]
    assert p1.column("close").to_pylist() == [2.0]
    # a later identical delivery of the restated value is a no-op as well
    assert router.route_bars(bars([OLD], [2.0], ingested=NOW + 2), "h3") == Routed()


def test_new_row_next_to_existing_cold_rows_gets_version_one(router: Router) -> None:
    router.route_bars(bars([OLD], [1.0]), "h")
    assert router.route_bars(bars([OLD, OLD + DAY_NS], [1.0, 5.0]), "h") == Routed(0, 1)
    p1 = read_part(router.store, parts(router.store)[1])
    assert (p1.column("ts_ns").to_pylist(), p1.column("version").to_pylist()) == (
        [OLD + DAY_NS],
        [1],
    )


def test_bounds_follow_all_routed_rows_and_never_narrow(router: Router, engine: Engine) -> None:
    router.route_bars(bars([OLD, RECENT], [1.0, 2.0]), "h")
    router.flush_bounds()
    router.bounds.clear()
    router.route_bars(bars([RECENT], [2.0]), "h")
    router.flush_bounds()
    router.ensure_instruments([WTI])  # must keep stored bounds
    with engine.connect() as c:
        row = c.execute(sr.instrument.select().where(sr.instrument.c.instrument_id == WTI)).one()
    assert (row.first_ts_ns, row.last_ts_ns) == (OLD, RECENT)


def test_write_stream_splits_ticks_between_tiers(router: Router) -> None:
    ts = np.array([OLD, OLD + 1, RECENT, RECENT + 1], np.int64)
    n = ts.size
    t = pa.table(
        {
            "ts_ns": ts,
            "instrument_id": pa.array([WTI] * n).dictionary_encode().cast(TICK_V1.field(1).type),
            "price": np.arange(n, dtype=np.float64),
            "size": np.ones(n, np.float32),
            "side": np.zeros(n, np.int8),
            "quality": np.zeros(n, np.int8),
            "ingested_at_ns": np.full(n, NOW, np.int64),
            "version": np.ones(n, np.int16),
        },
        schema=TICK_V1,
    )
    year = int(np.datetime64(OLD, "ns").astype("datetime64[Y]").astype(int)) + 1970
    month = int(np.datetime64(OLD, "ns").astype("datetime64[M]").astype(int) % 12) + 1
    out = router.write_stream(
        "ticks", instrument_id=WTI, year=year, month=month, batches=[t.slice(0, 2), t.slice(2)],
        source_hash="s",
    )  # fmt: skip
    assert out == Routed(hot=2, cold=2)
    assert router.repo.read_ticks(WTI, 0, NOW + 1).num_rows == 2


def test_read_part_uses_store_read_only() -> None:
    buf = io.BytesIO()
    pq.write_table(pa.table({"a": [1, 2], "b": [3, 4]}), buf)

    class Mem:  # any ObjectStore works: read_part must not special-case the type
        def read(self, path: str) -> bytes:
            return buf.getvalue()

    assert read_part(Mem(), "x", ["b"]).to_pydict() == {"b": [3, 4]}  # type: ignore[arg-type]
