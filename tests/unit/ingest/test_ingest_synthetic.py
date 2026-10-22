"""Synthetic generator: shape, determinism, planning, clipping (T-104)."""

import hashlib
from datetime import UTC, date, datetime

import numpy as np
import pyarrow as pa
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pyarrow import ipc

from tidal.common.errors import ConfigError
from tidal.common.timeutil import FixedClock, month_bounds, to_ts_ns
from tidal.contracts.schemas import TICK_V1, QualityFlag
from tidal.ingest.sources import synthetic as syn
from tidal.ingest.sources.synthetic import generate, plan
from tidal.ingest.validate import validate_and_flag

NOW = to_ts_ns(datetime(2026, 1, 1, tzinfo=UTC))
CLOCK = FixedClock(NOW)
SEED = 20261021
SYN, NEG, OUT = (int(QualityFlag.SYNTHETIC), int(QualityFlag.NEGATIVE), int(QualityFlag.OUTLIER))
GAP = int(QualityFlag.GAP_BEFORE)
GOLDEN = (170905, "f4a141f436268e2eda0d01839ba4e9951e8c9e7563a3894b6a32ece837d43037")
NS_DAY = 86_400_000_000_000


def month(inst: str, year: int, mon: int, *, seed: int = SEED, scale: str = "S") -> pa.Table:
    return pa.concat_tables(generate(inst, year, mon, seed=seed, scale=scale, clock=CLOCK))


def sha(table: pa.Table) -> str:
    sink = pa.BufferOutputStream()
    with ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return hashlib.sha256(sink.getvalue().to_pybytes()).hexdigest()


def test_shape_schema_flags_and_clock() -> None:
    table = month("SYN.BRENT.TICK", 2021, 3)
    assert [(f.name, f.type) for f in table.schema] == [(f.name, f.type) for f in TICK_V1]
    cols = table.to_pydict()
    ts = np.asarray(cols["ts_ns"])
    assert np.all(np.diff(ts) > 0), "strictly increasing, so (instrument, ts, side) is unique"
    lo, hi = month_bounds(2021, 3)
    assert lo <= ts[0]
    assert ts[-1] < hi
    assert set(cols["ingested_at_ns"]) == {NOW}
    assert set(cols["version"]) == {1}
    assert set(cols["instrument_id"]) == {"SYN.BRENT.TICK"}
    quality = np.asarray(cols["quality"])
    assert np.all(quality & SYN)
    assert np.all(quality & ~(SYN | NEG | OUT | GAP) == 0)
    side, size = np.asarray(cols["side"]), np.asarray(cols["size"])
    assert set(side.tolist()) == {0, 1, 2}
    assert np.all(size[side != 0] == 0)
    assert np.all(size[side == 0] > 0)
    assert np.all((ts // NS_DAY + 3) % 7 < 5), "weekdays only"


def test_bid_ask_spread_positive() -> None:
    cols = month("SYN.TTF.TICK", 2019, 6).to_pydict()
    side, price = np.asarray(cols["side"]), np.asarray(cols["price"])
    assert np.median(price[side == 2]) > np.median(price[side == 1])


def test_determinism_same_seed_and_generation_order() -> None:
    units = [("SYN.WTI.TICK", 2018, 5), ("SYN.HH.TICK", 2019, 1), ("SYN.WTI.TICK", 2017, 12)]
    forward = {u: sha(month(*u)) for u in units}
    backward = {u: sha(month(*u)) for u in reversed(units)}
    syn.daily_path.cache_clear()  # a cold cache must give the same bytes
    again = {u: sha(month(*u)) for u in units}
    assert forward == backward == again
    assert len(set(forward.values())) == len(units)


def test_seed_and_clock_change_output() -> None:
    a = month("SYN.HH.TICK", 2019, 1)
    assert sha(a) != sha(month("SYN.HH.TICK", 2019, 1, seed=SEED + 1))
    later = pa.concat_tables(
        generate("SYN.HH.TICK", 2019, 1, seed=SEED, scale="S", clock=FixedClock(NOW + 1))
    )
    assert sha(a) != sha(later)
    assert a.drop(["ingested_at_ns"]).equals(later.drop(["ingested_at_ns"]))


def test_plan_scales() -> None:
    start, end = date(2005, 1, 1), date(2025, 12, 31)
    s, m, big = plan("S", start, end), plan("M", start, end), plan("L", start, end)
    assert (len(s), len(m), len(big)) == (3 * 5 * 12, 10 * 10 * 12, 10 * 20 * 12)
    assert s[0][:3] == ("SYN.BRENT.TICK", 2021, 1)
    assert s[-1][:3] == ("SYN.HH.TICK", 2025, 12)
    assert all(u.start is None and u.end is None for u in s)
    assert {u[0] for u in s} == set(syn.synthetic_instruments()[:3])
    assert big[0][1] == 2006  # last 20 years of the range
    short = plan("L", date(2024, 11, 15), date(2025, 2, 3))
    assert {(u.year, u.month) for u in short} == {(2024, 11), (2024, 12), (2025, 1), (2025, 2)}
    edges = [u for u in short if u.instrument_id == "SYN.BRENT.TICK"]
    assert [(u.start, u.end) for u in edges] == [
        (date(2024, 11, 15), None),
        (None, None),
        (None, None),
        (None, date(2025, 2, 3)),
    ]


@pytest.mark.parametrize(("scale", "total"), [("S", 50e6), ("M", 300e6), ("L", 1.5e9)])
def test_expected_ticks_match_config_contract(scale: str, total: float) -> None:
    units = plan(scale, date(2005, 1, 1), date(2025, 12, 31))
    insts = {u[0] for u in units}
    years = {u[1] for u in units}
    got = sum(syn.expected_ticks(scale, i) for i in insts) * len(years)
    assert got == pytest.approx(total, rel=0.01)


def test_clipping_and_empty_range() -> None:
    start, end = date(2021, 3, 10), date(2021, 3, 12)
    table = pa.concat_tables(
        generate("SYN.BRENT.TICK", 2021, 3, seed=SEED, scale="S", clock=CLOCK, start=start, end=end)
    )
    ts = table["ts_ns"].to_numpy()
    assert ts[0] >= to_ts_ns(datetime(2021, 3, 10, tzinfo=UTC))
    assert ts[-1] < to_ts_ns(datetime(2021, 3, 13, tzinfo=UTC))
    saturday, sunday = date(2021, 3, 13), date(2021, 3, 14)
    empty = generate(
        "SYN.BRENT.TICK", 2021, 3, seed=SEED, scale="S", clock=CLOCK, start=saturday, end=sunday
    )
    assert not list(empty)


def test_errors() -> None:
    with pytest.raises(ConfigError):
        plan("XL", date(2020, 1, 1), date(2020, 2, 1))
    with pytest.raises(ConfigError):
        syn.load_params("SYN.NOPE.TICK")
    with pytest.raises(ConfigError):
        list(generate("SYN.HH.TICK", 1999, 1, seed=1, scale="S", clock=CLOCK))
    base = syn.load_params("SYN.HH.TICK").model_dump()
    with pytest.raises(ConfigError):
        _ = syn.InstrumentParams(**(base | {"jump_sd": 1.0})).sigma_diffusive
    bad_rows = ((1.0, 0.0, 0.0), (0.5, 0.2, 0.2), (0.0, 0.0, 1.0))
    with pytest.raises(ValueError, match="sum to 1"):
        syn.InstrumentParams(**(base | {"transition": bad_rows}))


def test_l_scale_month_row_count() -> None:
    table = month("SYN.BRENT.TICK", 2020, 3, scale="L")
    expected = syn.expected_ticks("L", "SYN.BRENT.TICK") / 12
    assert 0.7 * expected < table.num_rows < 1.3 * expected


def test_power_prices_go_negative_and_are_flagged() -> None:
    inst = "SYN.POWER_DE.TICK"
    path = syn.daily_path(inst, SEED)
    price = np.exp(path.x_close) - syn.load_params(inst).shift
    first = int(np.flatnonzero((price < -5) & path.business)[0])
    moment = datetime.fromtimestamp((first + syn._ANCHOR_DAY) * 86_400, tz=UTC)
    power = month(inst, moment.year, moment.month)
    negative = power["price"].to_numpy() < 0
    assert negative.any()
    flagged = (power["quality"].to_numpy() & NEG) != 0
    assert np.array_equal(negative, flagged)


@settings(max_examples=40, deadline=None)
@given(
    n=st.integers(0, 5000),
    rate=st.floats(0.0, 0.2),
    mean_len=st.floats(1.0, 50.0),
    seed=st.integers(0, 2**32),
)
def test_gap_mask_properties(n: int, rate: float, mean_len: float, seed: int) -> None:
    mask = syn.injection_mask_gaps(np.random.Generator(np.random.PCG64(seed)), n, rate, mean_len)
    assert mask.shape == (n,)
    assert mask.dtype == bool
    again = syn.injection_mask_gaps(np.random.Generator(np.random.PCG64(seed)), n, rate, mean_len)
    assert np.array_equal(mask, again)


@settings(max_examples=30, deadline=None)
@given(start=st.floats(0.0, 20.0), length=st.floats(1.0, 4.0))
def test_session_cdf_monotone(start: float, length: float) -> None:
    cdf, edges = syn._session_cdf(start, start + length)
    assert cdf[0] == 0.0
    assert cdf[-1] == pytest.approx(1.0)
    assert np.all(np.diff(cdf) >= 0)
    assert edges[-1] == 24.0


def test_chunking_does_not_change_ticks_and_boundaries_are_data_derived() -> None:
    args = ("SYN.WTI.TICK", 2020, 6)
    whole = pa.concat_tables(generate(*args, seed=SEED, scale="L", clock=CLOCK, chunk_rows=10**9))
    parts = list(generate(*args, seed=SEED, scale="L", clock=CLOCK, chunk_rows=60_000))
    assert len(parts) > 3
    assert all(t.num_rows < 130_000 for t in parts)
    chunked = pa.concat_tables(parts)
    keep = ["ts_ns", "price", "size", "side", "ingested_at_ns"]
    assert chunked.select(keep).equals(whole.select(keep))
    again = list(generate(*args, seed=SEED, scale="L", clock=CLOCK, chunk_rows=60_000))
    assert [t.num_rows for t in again] == [t.num_rows for t in parts]
    assert sha(pa.concat_tables(again)) == sha(chunked)


def test_golden_hash_of_integer_columns() -> None:
    """Pinned hash of ts_ns/side/quality for one small month.

    Float columns (price, size) are excluded: their bytes can differ across numpy versions or
    platforms, so full-table hash equality only holds within one environment. Integer columns are
    expected to be stable; a change here means the stream layout or model changed on purpose.
    """
    table = month("SYN.HH.TICK", 2019, 1)
    digest = hashlib.sha256()
    for col in ("ts_ns", "side", "quality"):
        digest.update(table[col].to_numpy().tobytes())
    assert (table.num_rows, digest.hexdigest()) == GOLDEN


def test_crc32_stream_keys_have_no_collisions() -> None:
    ids = syn.synthetic_instruments()
    assert len({syn._crc(i) for i in ids}) == len(ids)


def test_injected_outliers_are_flagged_even_in_validator_warmup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[pa.Table] = []
    real = validate_and_flag

    def spy(table: pa.Table, *, synthetic: bool = False) -> pa.Table:
        seen.append(table)
        return real(table, synthetic=synthetic)

    monkeypatch.setattr(syn, "validate_and_flag", spy)
    out = pa.concat_tables(generate("SYN.WTI.TICK", 2022, 4, seed=3, scale="L", clock=CLOCK))
    before = pa.concat_tables(seen)["quality"].to_numpy()
    after = out["quality"].to_numpy()
    injected = (before & OUT) != 0
    assert injected.sum() > 0
    assert np.all((after & OUT)[injected] != 0)
    assert np.all(before & SYN == 0), "SYNTHETIC is set only by validate_and_flag"
    assert np.all(after & SYN != 0)


def test_max_workers_for_memory() -> None:
    assert syn.max_workers_for_memory() == 2 * 1024**3 // syn.WORKER_PEAK_BYTES
    assert syn.max_workers_for_memory(1) == 1


def test_generate_unit_passes_clip_bounds() -> None:
    unit = plan("S", date(2025, 12, 29), date(2025, 12, 31))[0]
    table = pa.concat_tables(syn.generate_unit(unit, seed=SEED, scale="S", clock=CLOCK))
    assert table["ts_ns"].to_numpy()[0] >= to_ts_ns(datetime(2025, 12, 29, tzinfo=UTC))
