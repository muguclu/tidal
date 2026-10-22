"""Synthetic daily history (T-117, ADR-0013): determinism, schema, episode, term structure."""

import hashlib
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl
import pyarrow as pa
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from sqlalchemy import Engine, create_engine, event
from sqlalchemy.pool import StaticPool
from typer.testing import CliRunner

from tidal.common.config import Settings
from tidal.common.errors import ConfigError
from tidal.common.timeutil import FixedClock, to_ts_ns
from tidal.contracts.schemas import BAR_V1, QualityFlag
from tidal.ingest import cli
from tidal.ingest.cli import Context
from tidal.ingest.sources import synthetic_daily as sd
from tidal.storage import sql_repo as sr
from tidal.storage.parquet_writer import LocalStore

NOW = datetime(2025, 12, 31, tzinfo=UTC)
CLOCK = FixedClock(to_ts_ns(NOW))
START, END = date(2005, 1, 1), date(2025, 12, 30)
WTI = "EIA.WTI.SPOT"
IDS = [
    "EIA.BRENT.SPOT",
    "EIA.WTI.SPOT",
    *(f"EIA.WTI.FUT{n}" for n in range(1, 5)),
    "EIA.HH.SPOT",
    *(f"EIA.HH.FUT{n}" for n in range(1, 5)),
    "FRED.EURUSD.FX",
]


def gen(iid: str, seed: int = 1, start: date = START, end: date = END) -> pa.Table:
    return sd.generate_daily(iid, seed=seed, start=start, end=end, clock=CLOCK)


def frame(iid: str, **kw: Any) -> pl.DataFrame:
    df = pl.from_arrow(gen(iid, **kw))
    assert isinstance(df, pl.DataFrame)
    return df


def test_catalogue_is_the_twelve_daily_series() -> None:
    assert sd.daily_instruments() == IDS


@pytest.mark.parametrize("iid", IDS)
def test_schema_flags_and_volume_conventions(iid: str) -> None:
    table = gen(iid)
    assert table.schema.equals(BAR_V1)
    df = pl.from_arrow(table)
    assert isinstance(df, pl.DataFrame)
    assert set(df["interval"].cast(pl.String)) == {"1d"}
    assert (df["instrument_id"].cast(pl.String) == iid).all()
    assert (df["quality"] & int(QualityFlag.SYNTHETIC) > 0).all()
    assert (df["version"] == 1).all()
    assert (df["ingested_at_ns"] == to_ts_ns(NOW)).all()
    if ".FUT" in iid:
        assert (df["volume"] > 0).all()
    else:
        assert df["volume"].is_nan().all()
    assert df["trade_count"].null_count() == df.height


def ipc_bytes(table: pa.Table) -> bytes:
    """Serialised table (``Table.equals`` treats NaN volumes as unequal)."""
    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return bytes(sink.getvalue().to_pybytes())


def test_same_seed_is_identical_other_seed_differs() -> None:
    first = ipc_bytes(gen(WTI, 5))
    sd._build.cache_clear()  # recompute from scratch, not from the cache
    assert first == ipc_bytes(gen(WTI, 5))
    assert first != ipc_bytes(gen(WTI, 6))


def test_date_range_is_utc_midnight_business_days() -> None:
    df = frame(WTI)
    days = df["ts_ns"].to_numpy() // sd.NS_DAY
    assert (df["ts_ns"].to_numpy() % sd.NS_DAY == 0).all()
    assert (np.diff(days) > 0).all()
    first = date(1970, 1, 1) + timedelta(days=int(days[0]))
    last = date(1970, 1, 1) + timedelta(days=int(days[-1]))
    assert first == date(2005, 1, 3)  # 2005-01-01 is a Saturday
    assert last == END
    assert ((days + 3) % 7 < 5).all()  # Monday-Friday only
    assert df.height == np.busday_count(first, END + timedelta(days=1))


def test_a_bar_does_not_depend_on_the_requested_range() -> None:
    whole = frame(WTI)
    part = frame(WTI, start=date(2019, 3, 1), end=date(2021, 6, 30))
    joined = whole.join(part, on="ts_ns", how="inner", suffix="_p")
    assert joined.height == part.height
    for col in ("open", "high", "low", "close", "volume"):
        assert joined[col].equals(joined[f"{col}_p"])


def test_wti_negative_price_episode_on_2020_04_20() -> None:
    day = to_ts_ns(datetime(2020, 4, 20, tzinfo=UTC))
    neg = int(QualityFlag.NEGATIVE)

    def on_day(iid: str) -> dict[str, Any]:
        return frame(iid).filter(pl.col("ts_ns") == day).row(0, named=True)

    spot, fut1, fut2 = on_day(WTI), on_day("EIA.WTI.FUT1"), on_day("EIA.WTI.FUT2")
    assert spot["close"] == -37.0
    assert fut1["close"] == -37.6
    assert fut2["close"] > 0  # extreme contango at the front
    assert spot["quality"] & neg
    assert fut1["quality"] & neg
    df = frame(WTI)
    prev = df.filter(pl.col("ts_ns") == day - sd.NS_DAY * 3).row(0, named=True)  # Friday
    assert prev["close"] > 0
    assert df.filter(pl.col("close") < 0).height == 1
    # episode day + next day (its open is the negative close, so its low is negative too)
    assert df.filter(pl.col("quality") & neg > 0).height == 2
    assert frame("EIA.BRENT.SPOT").filter(pl.col("low") <= 0).height == 0


@pytest.mark.parametrize("prefix", ["EIA.WTI.FUT", "EIA.HH.FUT"])
def test_term_structure_has_contango_and_backwardation(prefix: str) -> None:
    closes = [frame(f"{prefix}{n}")["close"].to_numpy() for n in range(1, 5)]
    spread = closes[1] - closes[0]
    assert 0.15 < float((spread > 0).mean()) < 0.95
    # longer tenors extend the same sign: M4-M1 co-moves with M2-M1
    assert np.corrcoef(closes[3] - closes[0], spread)[0, 1] > 0.8


def test_high_prices_are_more_backwardated_than_low_prices() -> None:
    spot = frame(WTI)["close"].to_numpy()
    spread = frame("EIA.WTI.FUT2")["close"].to_numpy() - frame("EIA.WTI.FUT1")["close"].to_numpy()
    keep = spot > 0
    hi = spread[keep & (spot > np.quantile(spot, 0.8))].mean()
    lo = spread[keep & (spot < np.quantile(spot, 0.2))].mean()
    assert hi < lo


def test_unknown_instrument_and_out_of_range_dates_are_rejected() -> None:
    with pytest.raises(ConfigError):
        gen("SYN.WTI.TICK")
    with pytest.raises(ConfigError):
        gen(WTI, start=date(1999, 1, 1))


@settings(max_examples=8, deadline=None)
@given(
    seed=st.integers(0, 2**31),
    iid=st.sampled_from(IDS),
    start=st.dates(date(2005, 1, 1), date(2024, 12, 31)),
    span=st.integers(0, 400),
)
def test_bar_invariants_hold_for_any_seed_and_window(
    seed: int, iid: str, start: date, span: int
) -> None:
    end = min(start + timedelta(days=span), END)
    df = frame(iid, seed=seed, start=start, end=end)
    assert df.height == np.busday_count(start, end + timedelta(days=1))
    ordered = (pl.col("high") >= pl.max_horizontal("open", "close")) & (
        pl.col("low") <= pl.min_horizontal("open", "close")
    )
    assert df.select(ordered.all()).item()
    assert df["ts_ns"].is_sorted()
    finite = df.select(pl.all_horizontal(pl.col("open", "high", "low", "close").is_finite()))
    assert finite.to_series().all()


# -- end to end through the router ---------------------------------------------------------------
def make_engine() -> Engine:
    eng = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )

    @event.listens_for(eng, "connect")
    def _attach(dbapi: Any, _rec: object) -> None:
        for schema in ("ref", "md", "ops"):
            dbapi.execute(f"ATTACH DATABASE ':memory:' AS {schema}")

    sr.metadata.create_all(eng)
    return eng


def ctx_for(engine: Engine, root: Path) -> Context:
    settings = Settings.from_env({"TIDAL_ENV": "local", "TIDAL_HOT_WINDOW_DAYS": "90"})
    return Context(settings, LocalStore(root), sr.SqlRepository(engine, CLOCK), CLOCK)


def tree_hashes(root: Path) -> dict[str, str]:
    return {
        p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*.parquet"))
    }


def test_run_daily_history_routes_hot_cold_is_idempotent_and_byte_stable(tmp_path: Path) -> None:
    start = date(2019, 1, 1)
    engine = make_engine()
    first = cli.run_daily_history(ctx_for(engine, tmp_path / "a"), 1, start)
    assert first.end == END
    assert first.instruments == len(IDS)
    assert first.routed.hot > 0
    assert first.routed.cold > 0
    per_series = int(np.busday_count(start, END + timedelta(days=1)))
    assert first.routed.hot + first.routed.cold == per_series * len(IDS)
    again = cli.run_daily_history(ctx_for(engine, tmp_path / "a"), 1, start)
    assert (again.routed.hot, again.routed.cold) == (0, 0)  # unchanged bars are skipped

    cli.run_daily_history(ctx_for(make_engine(), tmp_path / "b"), 1, start)
    assert tree_hashes(tmp_path / "a")
    assert tree_hashes(tmp_path / "a") == tree_hashes(tmp_path / "b")


def test_run_daily_history_rejects_a_start_after_the_last_complete_day(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="after the last complete day"):
        cli.run_daily_history(ctx_for(make_engine(), tmp_path), 1, date(2025, 12, 31))


def test_synth_group_options_with_a_subcommand_are_rejected() -> None:
    result = CliRunner().invoke(cli.synth_app, ["--seed", "3", "daily-history"])
    assert result.exit_code == 2
    assert "do not apply" in result.output
