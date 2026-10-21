from datetime import UTC, datetime

import numpy as np
import pyarrow as pa
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from tidal.common.timeutil import to_ts_ns
from tidal.contracts.schemas import BAR_V1, TICK_V1, QualityFlag
from tidal.ingest.validate import SchemaViolation, validate_and_flag

DAY = 86_400 * 1_000_000_000
NEG, OUT, GAP = int(QualityFlag.NEGATIVE), int(QualityFlag.OUTLIER), int(QualityFlag.GAP_BEFORE)
RES, SYN = int(QualityFlag.RESTATED), int(QualityFlag.SYNTHETIC)


def d(y: int, m: int, day: int) -> int:
    return to_ts_ns(datetime(y, m, day, tzinfo=UTC))


def ticks(
    ts: list[int],
    price: list[float],
    *,
    inst: str = "SYN.WTI.TICK",
    side: int = 0,
    version: int = 1,
    quality: int = 0,
    size: float = 1.0,
) -> pa.Table:
    n = len(ts)
    return pa.table(
        {
            "ts_ns": pa.array(ts, pa.int64()),
            "instrument_id": pa.array([inst] * n).dictionary_encode().cast(TICK_V1.field(1).type),
            "price": pa.array(price, pa.float64()),
            "size": pa.array([size] * n, pa.float32()),
            "side": pa.array([side] * n, pa.int8()),
            "quality": pa.array([quality] * n, pa.int8()),
            "ingested_at_ns": pa.array([0] * n, pa.int64()),
            "version": pa.array([version] * n, pa.int16()),
        },
        schema=TICK_V1,
    )


def bars(
    ts: list[int],
    close: list[float],
    *,
    versions: list[int] | None = None,
    inst: str = "EIA.WTI.SPOT",
    interval: str = "1d",
    high: list[float] | None = None,
    low: list[float] | None = None,
    volume: float = float("nan"),
) -> pa.Table:
    n = len(ts)
    return pa.table(
        {
            "ts_ns": pa.array(ts, pa.int64()),
            "instrument_id": pa.array([inst] * n).dictionary_encode().cast(BAR_V1.field(1).type),
            "interval": pa.array([interval] * n).dictionary_encode().cast(BAR_V1.field(2).type),
            "open": pa.array(close, pa.float64()),
            "high": pa.array(high or close, pa.float64()),
            "low": pa.array(low or close, pa.float64()),
            "close": pa.array(close, pa.float64()),
            "volume": pa.array([volume] * n, pa.float64()),
            "trade_count": pa.array([None] * n, pa.int32()),
            "quality": pa.array([0] * n, pa.int8()),
            "ingested_at_ns": pa.array([0] * n, pa.int64()),
            "version": pa.array(versions or [1] * n, pa.int16()),
        },
        schema=BAR_V1,
    )


def q(table: pa.Table) -> list[int]:
    return [int(x or 0) for x in table.column("quality").to_pylist()]


# ---- flags -------------------------------------------------------------------------------------


def test_wti_negative_price_flagged_and_kept() -> None:
    days = [d(2020, 4, 17), d(2020, 4, 20), d(2020, 4, 21)]
    out = validate_and_flag(bars(days, [18.27, -37.63, 10.01]))
    assert out.column("close").to_pylist() == [18.27, -37.63, 10.01]
    assert out.column("low").to_pylist()[1] == -37.63
    # Fri -> Mon is not a gap; negative flagged on the 20th only.
    assert q(out) == [0, NEG, 0]
    assert out.schema.equals(BAR_V1)


def test_negative_tick() -> None:
    out = validate_and_flag(ticks([1, 2], [5.0, -0.5]))
    assert q(out) == [0, NEG]
    assert out.column("price").to_pylist() == [5.0, -0.5]


def test_synthetic_and_restated_and_existing_bits_kept() -> None:
    table = bars([d(2021, 1, 4), d(2021, 1, 4)], [1.0, 1.5], versions=[1, 2])
    assert q(validate_and_flag(table)) == [0, RES]
    assert q(validate_and_flag(table, synthetic=True)) == [SYN, RES | SYN]
    assert q(validate_and_flag(ticks([1], [1.0], quality=NEG | OUT))) == [NEG | OUT]


def _noisy_ticks(n: int = 3000, spike_at: int | None = None) -> pa.Table:
    rng = np.random.Generator(np.random.PCG64(7))
    price = 100.0 + np.cumsum(rng.normal(0.0, 0.05, n))
    if spike_at is not None:
        price[spike_at:] += 5.0  # a >> 8 sigma jump
    return ticks(list(range(1, n + 1)), price.tolist())


def test_outlier_injected() -> None:
    flags = np.array(q(validate_and_flag(_noisy_ticks(spike_at=2000))))
    assert flags[2000] & OUT
    assert not (flags[:2000] & OUT).any()
    assert not (flags[2001:] & OUT).any()  # level shift: later diffs are normal again


def test_outlier_warmup_and_flat_series_not_flagged() -> None:
    price = [100.0 + 0.01 * i for i in range(20)] + [500.0]  # < 30 prior diffs -> warm-up
    assert not any(q(validate_and_flag(ticks(list(range(1, 22)), price))))
    flat = [50.0] * 200 + [51.0]  # sigma == 0 never flags
    assert not any(q(validate_and_flag(ticks(list(range(1, 202)), flat))))
    ramp = [100.0 + 0.01 * i for i in range(2000)]  # float-noise sigma must not flag
    assert not any(q(validate_and_flag(ticks(list(range(1, 2001)), ramp))))


def test_outlier_independent_per_side() -> None:
    n = 100
    rng = np.random.Generator(np.random.PCG64(1))
    trades = 100 + np.cumsum(rng.normal(0, 0.05, n))
    quotes = trades + 1000  # different level; interleaving must not create diffs
    ts = list(range(1, 2 * n + 1))
    price = np.empty(2 * n)
    price[0::2], price[1::2] = trades, quotes
    table = ticks(ts, price.tolist())
    table = table.set_column(4, TICK_V1.field("side"), pa.array([0, 1] * n, pa.int8()))
    assert not any(q(validate_and_flag(table)))


def test_gap_weekend_is_not_a_gap_but_missing_wednesday_is() -> None:
    # Mon 4, Tue 5, Thu 7 (Wed missing), Fri 8, Mon 11 (weekend only)
    days = [d(2021, 1, 4), d(2021, 1, 5), d(2021, 1, 7), d(2021, 1, 8), d(2021, 1, 11)]
    assert q(validate_and_flag(bars(days, [1.0, 2.0, 3.0, 4.0, 5.0]))) == [0, 0, GAP, 0, 0]


def test_gap_uses_exchange_local_date() -> None:
    # London midnight in summer = 23:00 UTC the previous day. Sun 23:00 UTC is Mon local.
    h = 3_600 * 1_000_000_000
    fri, mon = d(2021, 7, 8) + 23 * h, d(2021, 7, 11) + 23 * h  # Fri 9th local, Mon 12th local
    table = bars([fri, mon], [1.0, 2.0], inst="EIA.BRENT.SPOT")
    assert q(validate_and_flag(table, exchange_tz={"EIA.BRENT.SPOT": "Europe/London"})) == [0, 0]
    with pytest.raises(ValueError, match="time zone"):
        validate_and_flag(table, exchange_tz={"EIA.BRENT.SPOT": "Mars/Base"})


def test_gaps_not_flagged_for_ticks_or_intraday_bars() -> None:
    assert not any(q(validate_and_flag(ticks([1, 10 * DAY], [1.0, 1.0]))))
    assert not any(q(validate_and_flag(bars([0, 10 * DAY], [1.0, 2.0], interval="1h"))))


def test_restated_versions_do_not_create_gaps_or_dupe_flags() -> None:
    days = [d(2021, 1, 4), d(2021, 1, 4), d(2021, 1, 5)]
    out = validate_and_flag(bars(days, [1.0, 1.1, 1.2], versions=[1, 2, 1]))
    assert q(out) == [0, RES, 0]


# ---- schema violations -------------------------------------------------------------------------


def _replace(table: pa.Table, column: str, values: list[object]) -> pa.Table:
    i = table.schema.get_field_index(column)
    return table.set_column(i, table.schema.field(i), pa.array(values, table.schema.field(i).type))


@pytest.mark.parametrize(
    ("table", "match"),
    [
        (_replace(ticks([1, 2], [1.0, 1.0]), "price", [1.0, float("nan")]), "non-finite"),
        (_replace(ticks([1, 2], [1.0, 1.0]), "price", [1.0, float("inf")]), "non-finite"),
        (_replace(ticks([1, 2], [1.0, 1.0]), "size", [1.0, -1.0]), "size"),
        (_replace(ticks([1, 2], [1.0, 1.0]), "side", [0, 3]), "side"),
        (_replace(ticks([1, 2], [1.0, 1.0]), "version", [1, 0]), "version"),
        (_replace(ticks([1, 2], [1.0, 1.0]), "quality", [0, 32]), "reserved"),
        (_replace(ticks([1, 2], [1.0, 1.0]), "quality", [0, -1]), "reserved"),
        (ticks([2, 1], [1.0, 1.0]), "decreasing"),
        (_replace(ticks([1, 2], [1.0, 1.0]), "price", [1.0, None]), "nulls"),
        (bars([1, 2], [1.0, 1.0], high=[1.0, 0.5], low=[1.0, 1.0]), "high < low"),
        (bars([1, 2], [1.0, 1.0], high=[1.0, 1.0], low=[1.0, 2.0]), "high < low|low above"),
        (bars([1, 2], [1.0, 1.0], high=[0.9, 1.0]), "high below"),
        (bars([1, 2], [1.0, float("nan")]), "non-finite"),
        (bars([1, 2], [1.0, 1.0], volume=-1.0), "volume"),
        (bars([1, 2], [1.0, 1.0], interval="5m"), "interval"),
        (pa.table({"ts_ns": [1]}), "TICK_V1 or BAR_V1"),
    ],
)
def test_schema_violations_rejected(table: pa.Table, match: str) -> None:
    with pytest.raises(SchemaViolation, match=match):
        validate_and_flag(table)


def test_unsorted_is_per_instrument_not_global() -> None:
    a = ticks([5, 6], [1.0, 1.0], inst="SYN.WTI.TICK")
    b = ticks([1, 2], [1.0, 1.0], inst="SYN.HH.TICK")
    assert q(validate_and_flag(pa.concat_tables([a, b]))) == [0, 0, 0, 0]


def test_schema_violation_is_typed_error() -> None:
    from tidal.common.errors import TidalError  # noqa: PLC0415

    with pytest.raises(TidalError):
        validate_and_flag(pa.table({"x": [1]}))


# ---- properties --------------------------------------------------------------------------------


@settings(max_examples=40, deadline=None)
@given(st.lists(st.floats(-1e6, 1e6, allow_nan=False), min_size=1, max_size=120))
def test_values_never_modified_and_idempotent(prices: list[float]) -> None:
    table = ticks(list(range(1, len(prices) + 1)), prices)
    once = validate_and_flag(table)
    assert once.drop_columns(["quality"]).equals(table.drop_columns(["quality"]))
    assert validate_and_flag(once).equals(once)
    assert [bool(x & NEG) for x in q(once)] == [p < 0 for p in prices]


def test_deterministic() -> None:
    table = _noisy_ticks(spike_at=1500)
    assert validate_and_flag(table).equals(validate_and_flag(table))
