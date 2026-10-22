"""Repository logic on an in-memory SQLite engine.

SQLite proves: version rules (§7), NaN<->NULL mapping, as-of reads, ops upserts and the Core
statements' portability. It does NOT prove: T-SQL DDL, PAGE compression, column type ranges
(TINYINT/REAL), IDENTITY, pyodbc fast_executemany, Entra auth or locking; those are covered only by
tests/integration/storage against SQL Server.
"""

# ruff: noqa: PT018, PLC0415

import math
from datetime import UTC, datetime
from typing import Any

import numpy as np
import pyarrow as pa
import pytest
from pydantic import SecretStr
from sqlalchemy import Engine, create_engine, event, select
from sqlalchemy.pool import StaticPool

from tidal.common.config import Settings
from tidal.common.errors import NotFound, ValidationFailed
from tidal.contracts.schemas import BAR_V1, TICK_V1, Interval
from tidal.storage import sql_repo as sr

WTI = "EIA.WTI.SPOT"
T0 = 1_587_340_800 * 10**9  # 2020-04-20 (the negative-price day)
DAY = 86_400 * 10**9


def instrument_row(iid: str = WTI, **over: object) -> dict[str, object]:
    row: dict[str, object] = {
        "instrument_id": iid, "source": "EIA", "asset_class": "crude", "kind": "spot",
        "unit": "USD/bbl", "currency": "USD", "exchange_tz": "America/Chicago",
        "granularity": "1d", "roll_method": None, "first_ts_ns": None, "last_ts_ns": None,
        "description": "EIA RWTC, spot, USD/bbl",
    }  # fmt: skip
    return row | over


class FixedClock:
    def now(self) -> datetime:
        return datetime(2026, 1, 2, 3, 4, 5, 678000, tzinfo=UTC)

    def now_ns(self) -> int:
        return int(self.now().timestamp()) * 10**9


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
def repo(engine: Engine) -> sr.SqlRepository:
    r = sr.SqlRepository(engine, clock=FixedClock())
    r.upsert_instruments([instrument_row(), instrument_row("EIA.BRENT.SPOT")])
    return r


def bars(prices: list[float], *, volume: float = math.nan, ingested: int = T0, iid: str = WTI,
         start: int = T0, trade_count: list[int | None] | None = None) -> pa.Table:  # fmt: skip
    n = len(prices)
    px = np.array(prices)
    return pa.table(
        {
            "ts_ns": start + np.arange(n, dtype=np.int64) * DAY,
            "instrument_id": pa.array([iid] * n).dictionary_encode().cast(BAR_V1.field(1).type),
            "interval": pa.array(["1d"] * n).dictionary_encode().cast(BAR_V1.field(2).type),
            "open": px, "high": px, "low": px, "close": px,
            "volume": np.full(n, volume),
            "trade_count": pa.array(trade_count or [None] * n, pa.int32()),
            "quality": np.zeros(n, np.int8),
            "ingested_at_ns": np.full(n, ingested, np.int64),
            "version": np.ones(n, np.int16),
        },
        schema=BAR_V1,
    )  # fmt: skip


def ticks(prices: list[float], ts: list[int] | None = None) -> pa.Table:
    n = len(prices)
    return pa.table(
        {
            "ts_ns": np.array(ts or [T0 + i for i in range(n)], np.int64),
            "instrument_id": pa.array([WTI] * n).dictionary_encode().cast(TICK_V1.field(1).type),
            "price": np.array(prices),
            "size": np.ones(n, np.float32),
            "side": np.zeros(n, np.int8),
            "quality": np.zeros(n, np.int8),
            "ingested_at_ns": np.full(n, T0, np.int64),
            "version": np.ones(n, np.int16),
        },
        schema=TICK_V1,
    )


def test_instrument_upsert_is_idempotent_and_updates(repo: sr.SqlRepository) -> None:
    keys = repo.instrument_keys()
    assert set(keys) == {WTI, "EIA.BRENT.SPOT"} and len(set(keys.values())) == 2
    again = repo.upsert_instruments([instrument_row(description="changed")])
    assert again == keys  # surrogate keys are stable
    with repo.engine.connect() as c:
        desc = c.execute(
            select(sr.instrument.c.description).where(sr.instrument.c.instrument_id == WTI)
        ).scalar_one()
    assert desc == "changed"


def test_negative_price_round_trips_unclamped(repo: sr.SqlRepository) -> None:
    repo.upsert_bars(bars([-37.63, 18.27]))
    out = repo.read_bars(WTI, Interval.D1, T0, T0 + 10 * DAY)
    assert out.schema.equals(BAR_V1, check_metadata=False)
    assert out["close"].to_pylist() == [-37.63, 18.27]


def test_nan_volume_is_null_in_sql_and_nan_on_read(repo: sr.SqlRepository) -> None:
    repo.upsert_bars(bars([1.0, 2.0], trade_count=[None, 5]))
    with repo.engine.connect() as c:
        stored = c.execute(
            select(sr.bar_hot.c.volume, sr.bar_hot.c.trade_count).order_by(sr.bar_hot.c.ts_ns)
        ).all()
    assert [tuple(r) for r in stored] == [(None, None), (None, 5)]
    out = repo.read_bars(WTI, Interval.D1, T0, T0 + 5 * DAY)
    assert all(math.isnan(v or 0.0) for v in out["volume"].to_pylist())
    assert out["trade_count"].to_pylist() == [None, 5]


def test_reingest_same_data_is_noop_changed_data_appends_version(repo: sr.SqlRepository) -> None:
    assert repo.upsert_bars(bars([10.0, 11.0])) == 2
    assert repo.upsert_bars(bars([10.0, 11.0], ingested=T0 + 1)) == 0  # unchanged
    assert repo.upsert_bars(bars([10.0, 12.5], ingested=T0 + 100)) == 1  # one restated row
    with repo.engine.connect() as c:
        rows = c.execute(
            select(
                sr.bar_hot.c.ts_ns, sr.bar_hot.c.version, sr.bar_hot.c.quality, sr.bar_hot.c.close
            ).order_by("ts_ns", "version")
        ).all()
    assert [(r.version, r.quality, r.close) for r in rows] == [
        (1, 0, 10.0),
        (1, 0, 11.0),
        (2, 8, 12.5),
    ]
    latest = repo.read_bars(WTI, Interval.D1, T0, T0 + 5 * DAY)
    assert latest["close"].to_pylist() == [10.0, 12.5]
    assert latest["version"].to_pylist() == [1, 2]


def test_as_of_returns_knowledge_at_that_time(repo: sr.SqlRepository) -> None:
    repo.upsert_bars(bars([10.0, 11.0], ingested=T0))
    repo.upsert_bars(bars([10.0, 12.5], ingested=T0 + 100))
    old = repo.read_bars(WTI, Interval.D1, T0, T0 + 5 * DAY, as_of_ns=T0 + 50)
    new = repo.read_bars(WTI, Interval.D1, T0, T0 + 5 * DAY, as_of_ns=T0 + 100)
    assert old["close"].to_pylist() == [10.0, 11.0]
    assert new["close"].to_pylist() == [10.0, 12.5]
    assert repo.read_bars(WTI, Interval.D1, T0, T0 + 5 * DAY, as_of_ns=T0 - 1).num_rows == 0


def test_range_is_half_open_and_instrument_scoped(repo: sr.SqlRepository) -> None:
    repo.upsert_bars(bars([1.0, 2.0, 3.0]))
    repo.upsert_bars(bars([9.0], iid="EIA.BRENT.SPOT"))
    out = repo.read_bars(WTI, Interval.D1, T0 + DAY, T0 + 2 * DAY)
    assert out["close"].to_pylist() == [2.0]
    assert repo.read_bars(WTI, Interval.H1, T0, T0 + 9 * DAY).num_rows == 0


def test_ticks_versioning_and_read(repo: sr.SqlRepository) -> None:
    assert repo.upsert_ticks(ticks([1.0, -2.0, 3.0])) == 3
    assert repo.upsert_ticks(ticks([1.0, -2.0, 3.5])) == 1
    out = repo.read_ticks(WTI, T0, T0 + 10)
    assert out.schema.equals(TICK_V1, check_metadata=False)
    assert out["price"].to_pylist() == [1.0, -2.0, 3.5]
    assert out["version"].to_pylist() == [1, 1, 2]


def test_validation(repo: sr.SqlRepository) -> None:
    with pytest.raises(ValidationFailed):
        repo.upsert_bars(bars([math.nan]))  # price NaN is never allowed
    with pytest.raises(ValidationFailed):
        repo.upsert_ticks(bars([1.0]))  # wrong schema
    with pytest.raises(ValidationFailed):
        repo.upsert_bars(bars([1.0, 2.0], start=T0).take([0, 0]))  # duplicate key in batch
    with pytest.raises(NotFound):
        repo.upsert_bars(bars([1.0], iid="NOPE.NOPE.NOPE"))
    with pytest.raises(NotFound):
        repo.read_ticks("NOPE.NOPE.NOPE", 0, 1)


def test_watermark_and_checkpoint(repo: sr.SqlRepository) -> None:
    assert repo.get_watermark("md.bar_hot") is None
    repo.set_watermark("md.bar_hot", 5)
    repo.set_watermark("md.bar_hot", 9)
    assert repo.get_watermark("md.bar_hot") == 9
    assert repo.get_checkpoint("EIA", WTI) is None
    repo.set_checkpoint("EIA", WTI, 1)
    repo.set_checkpoint("EIA", WTI, 2)
    assert repo.get_checkpoint("EIA", WTI) == 2
    with repo.engine.connect() as c:
        assert (
            c.execute(select(sr.watermark.c.updated_at)).scalar_one() == "2026-01-02 03:04:05.678"
        )


def test_plan_versions_is_pure() -> None:
    import polars as pl

    key = ["interval_code", "ts_ns"]
    inc = pl.DataFrame({"interval_code": [4, 4], "ts_ns": [1, 2], "open": [1.0, 2.0],
                        "high": [1.0, 2.0], "low": [1.0, 2.0], "close": [1.0, 2.0],
                        "volume": [None, None], "trade_count": [None, None],
                        "quality": [0, 0], "version": [1, 1]})  # fmt: skip
    old = inc.head(1).with_columns(pl.lit(None, pl.Float64).alias("volume"))
    assert sr.plan_versions(old, inc, sr._BARS).select(key).to_dicts() == [
        {"interval_code": 4, "ts_ns": 2}
    ]


def test_access_token_struct() -> None:
    blob = sr.access_token_struct("ab")
    assert blob == (4).to_bytes(4, "little") + "ab".encode("utf-16-le")


def test_engine_from_settings_password_and_entra() -> None:
    local = Settings.from_env({"TIDAL_ENV": "local", "TIDAL_SQL__PASSWORD": "p@ss;word"})
    eng = sr.create_engine_from_settings(local)
    odbc = eng.url.query["odbc_connect"]
    assert "UID=sa" in odbc and "PWD={p@ss;word}" in odbc and "TrustServerCertificate=yes" in odbc
    assert "p@ss" not in repr(local)

    class Cred:
        def get_token(self, scope: str) -> Any:
            assert scope == sr.TOKEN_SCOPE
            return type("T", (), {"token": "tok"})()

    mi = Settings.from_env(
        {
            "TIDAL_ENV": "local",
            "TIDAL_SQL__AUTH": "entra_mi",
            "TIDAL_SQL__SERVER": "x.database.windows.net",
        }
    )
    eng = sr.create_engine_from_settings(mi, credential=Cred())  # type: ignore[arg-type]
    odbc = eng.url.query["odbc_connect"]
    assert "PWD" not in odbc and "UID" not in odbc and "Encrypt=yes" in odbc
    cparams: dict[str, Any] = {}
    for fn in eng.dialect.dispatch.do_connect:
        fn(eng.dialect, None, (), cparams)
    assert cparams["attrs_before"] == {1256: sr.access_token_struct("tok")}
    assert SecretStr("x")  # keep import used


def test_password_braces_are_escaped_in_odbc_string() -> None:
    s = Settings.from_env({"TIDAL_ENV": "local", "TIDAL_SQL__PASSWORD": "a}b;c}}d"})
    odbc = sr.create_engine_from_settings(s).url.query["odbc_connect"]
    assert "PWD={a}}b;c}}}}d};" in odbc
