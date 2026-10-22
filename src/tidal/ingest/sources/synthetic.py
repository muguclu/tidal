"""Seeded regime-switching synthetic tick generator (SPEC-01 section 4, T-104).

Model (per instrument, parameters in ``synthetic_params.yaml``)
---------------------------------------------------------------
* **Daily layer** (``daily_path``): Markov chain over 3 regimes (calm/stressed/crisis) on weekdays,
  log-level ``x`` follows GBM with weak mean reversion (OU pull to ``log(base + shift)``, so 20-year
  paths stay plausible), regime-dependent diffusive vol and Merton jumps (Poisson count, normal
  size). ``daily_vol`` in the YAML is the *total* daily vol, so the diffusive part is solved as
  ``sqrt(vol^2 - lambda_r (mu^2 + s^2))``. Power uses a shifted process ``price = exp(x) - shift``
  and can go negative. The path depends only on ``(seed, instrument)`` and is anchored at
  2000-01-01, so it does not depend on ``scale`` or the requested range.
* **Tick layer** (``generate``): per weekday a Poisson number of ticks with mean
  ``base_rate * regime_activity``; arrival times follow a U-shaped intensity inside the UTC session
  window (inverse-CDF sampling of sorted uniforms in activity time = exact inhomogeneous Poisson
  given the count). Within a day, ``x`` is a Brownian bridge in activity time from the day's
  open to its close (so daily returns are exactly the daily-layer returns) plus the day's jump at
  a random time.
  Each tick is a trade (``side`` 0, at mid +- half spread) or a bid/ask quote (``side`` 1/2,
  size 0).
* **Fault injection**: ``outlier_rate`` of ticks get a fat-finger displacement of 5-25 % of the
  level; ``gap_rate`` of the nominal ticks are dropped in geometric runs and the first row after
  each run carries ``GAP_BEFORE``; injected outliers are flagged ``OUTLIER`` directly. Every row
  gets ``SYNTHETIC`` from ``validate_and_flag`` (the only place), which also adds ``NEGATIVE`` and
  its own ``OUTLIER`` findings. Real model jumps can also be flagged ``OUTLIER`` (intended).

Determinism and stream keys
---------------------------
All draws use ``numpy.random.Generator(PCG64(SeedSequence(seed, spawn_key=key)))``. Keys are
deterministic children of the root seed (the stateless equivalent of ``SeedSequence.spawn``, so the
order of generation never matters and years/instruments can run in parallel):

* ``(crc32(instrument_id), 0)``            -> daily path stream
* ``(crc32(instrument_id), year, month)``  -> month stream: per-day tick counts and jump times
  (child of the ``(instrument, year)`` node)
* ``(crc32(instrument_id), year, month, calendar_day_index)`` -> one stream per business day for
  all tick-level draws. Because each day owns its stream, chunking cannot change any value.

``crc32`` of the id is used instead of the catalogue position so keys never change when the
catalogue is extended (a test asserts no collisions across ``SYN.*``). ``ingested_at_ns`` comes
from the injected clock, never from wall time. Floats may differ in the last bits across numpy
versions or platforms, so byte-equal output is guaranteed within one environment only; a pinned
golden hash covers the integer columns.

Chunking: a month is yielded as several tables of whole business days, cut greedily at
``CHUNK_ROWS`` nominal ticks from the drawn tick counts (data-derived, never timing-derived).
Concatenating the chunks gives the same ticks for any ``chunk_rows``; only the validator's rolling
OUTLIER window restarts per chunk. Gap runs end at the day boundary.

Performance (measured on Windows, 6 logical cores, numpy 2.x, polars validator included, T-104 fix)
---------------------------------------------------------------------------------------------
* Worst month: ``SYN.EURUSD.TICK`` 2022-04 at L rate, default seed (81 % crisis days): 2.46M rows
  in 21 tables of at most 126k rows; 1.1-1.4M rows/s single process.
* Peak memory of that month: 250 MB Windows peak working set (whole process incl. interpreter,
  polars, pyarrow); tracemalloc peak (numpy/python allocations only) 23 MB. A process that
  generates 48 sampled L months in a row peaked at 276 MB. ``WORKER_PEAK_BYTES`` = 300 MiB.
* L scale (1.5B rows, 2400 units): single process 1.2-1.4M rows/s, 2 workers 1.83M rows/s
  (13.6 min measured for 29M sampled rows, extrapolated), so about 14 min with 2 workers.
  Extrapolation to 8 workers at that efficiency is about 4 min; it was NOT measured, because
  pools of 3+ workers died with ``BrokenProcessPool`` on this machine (cause not diagnosed; serial
  runs of the same units are fine).
* Whole run under 2 GB: ``max_workers_for_memory()`` = 6 workers (2 GiB / 300 MiB, parent excluded).

Source protocol: ``Source`` lives in T-102's ``base.py``; until then this module exposes the plain
functions ``plan``, ``generate`` and ``generate_unit``.
"""

import zlib
from collections.abc import Iterator
from datetime import date, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any, Final, NamedTuple

import numpy as np
import pyarrow as pa
import yaml
from pydantic import BaseModel, ConfigDict, field_validator

from tidal.common.errors import ConfigError
from tidal.common.timeutil import Clock, month_bounds
from tidal.contracts.instruments import CATALOGUE_V1, Source
from tidal.contracts.schemas import TICK_V1, QualityFlag
from tidal.ingest.validate import validate_and_flag

PARAMS_PATH: Final = Path(__file__).with_name("synthetic_params.yaml")
ANCHOR: Final = date(2000, 1, 1)
LAST_YEAR: Final = 2060
BUSINESS_DAYS_PER_YEAR: Final = 261
_EPOCH_DAY: Final = date(1970, 1, 1).toordinal()
_ANCHOR_DAY: Final = ANCHOR.toordinal() - _EPOCH_DAY
_NS_DAY: Final = 86_400 * 10**9
_NS_HOUR: Final = 3_600 * 10**9
_SESSION_GRID: Final = 1440  # minutes
CHUNK_ROWS: Final = 100_000  # nominal ticks per yielded table (whole business days)
#: Measured peak working set of one worker process (see module docstring), rounded up.
WORKER_PEAK_BYTES: Final = 300 * 1024**2
_OUTLIER_MIN, _OUTLIER_MAX = 0.05, 0.25
_Triple = tuple[float, float, float]


class InstrumentParams(BaseModel):
    """Resolved (defaults + override) parameters of one instrument; see the YAML header."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    base_level: float
    shift: float
    daily_vol: _Triple
    transition: tuple[_Triple, _Triple, _Triple]
    regime_activity: _Triple
    jump_intensity: float
    jump_regime_mult: _Triple
    jump_mean: float
    jump_sd: float
    mean_reversion: float
    ticks_weight: float
    session_start_h: float
    session_end_h: float
    trade_share: float
    spread_bps: float
    spread_regime_mult: _Triple
    size_median: float

    @field_validator("transition")
    @classmethod
    def _rows_sum_to_one(cls, value: tuple[_Triple, ...]) -> tuple[_Triple, ...]:
        if any(abs(sum(row) - 1.0) > 1e-9 for row in value):  # noqa: PLR2004
            msg = "transition rows must sum to 1"
            raise ValueError(msg)
        return value

    @property
    def sigma_diffusive(self) -> np.ndarray:
        """Diffusive daily vol per regime so that total vol (with jumps) equals ``daily_vol``."""
        jump_var = (
            self.jump_intensity
            * np.asarray(self.jump_regime_mult)
            * (self.jump_mean**2 + self.jump_sd**2)
        )
        var = np.asarray(self.daily_vol) ** 2 - jump_var
        if np.any(var <= 0):
            msg = "jump variance exceeds target daily variance in some regime"
            raise ConfigError(msg)
        return np.sqrt(var)

    @property
    def stationary(self) -> np.ndarray:
        """Stationary regime distribution of the transition matrix."""
        return np.asarray(np.linalg.matrix_power(np.asarray(self.transition), 10_000)[0])


@lru_cache(maxsize=1)
def _raw() -> dict[str, Any]:  # Any: untyped YAML document, validated on use
    loaded = yaml.safe_load(PARAMS_PATH.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)  # noqa: S101 - file is part of the package
    return loaded


def load_params(instrument_id: str) -> InstrumentParams:
    """Parameters of ``instrument_id`` (defaults merged with the per-instrument override)."""
    raw = _raw()
    instruments = raw["instruments"]
    assert isinstance(instruments, dict)  # noqa: S101
    if instrument_id not in instruments:
        msg = f"no synthetic parameters for {instrument_id}"
        raise ConfigError(msg)
    return InstrumentParams(**{**raw["defaults"], **instruments[instrument_id]})


def synthetic_instruments() -> list[str]:
    """``SYN.*`` instrument ids in contract (catalogue) order."""
    return [e.instrument_id for e in CATALOGUE_V1 if e.source is Source.SYN]


def _scale_cfg(scale: str) -> dict[str, int]:
    scales = _raw()["scales"]
    assert isinstance(scales, dict)  # noqa: S101
    if scale not in scales:
        msg = f"unknown synthetic scale {scale!r}; expected one of {sorted(scales)}"
        raise ConfigError(msg)
    return scales[scale]  # type: ignore[no-any-return]


class WorkUnit(NamedTuple):
    """One instrument-month; ``start``/``end`` are the clip bounds of a partial edge month."""

    instrument_id: str
    year: int
    month: int
    start: date | None = None  # inclusive; None = whole month from day 1
    end: date | None = None  # inclusive; None = whole month to its last day


def plan(scale: str, start: date, end: date) -> list[WorkUnit]:
    """Work units for ``scale`` over ``[start, end]`` inclusive.

    Instruments: the first N ``SYN.*`` catalogue rows. Years: the last N years of the range
    (all of them if shorter). Months: those overlapping the range; the first/last month carry
    clip bounds when partial (feed them to ``generate_unit``). Order: instrument, year, month.
    """
    cfg = _scale_cfg(scale)
    years = range(max(start.year, end.year - cfg["years"] + 1), end.year + 1)
    months = [
        (year, month)
        for year in years
        for month in range(1, 13)
        if (start.year, start.month) <= (year, month) <= (end.year, end.month)
    ]
    first = start if start.day > 1 else None
    last = end if (end + timedelta(days=1)).month == end.month else None
    units = []
    for inst in synthetic_instruments()[: cfg["instruments"]]:
        for year, month in months:
            clip_start = first if (year, month) == (start.year, start.month) else None
            clip_end = last if (year, month) == (end.year, end.month) else None
            units.append(WorkUnit(inst, year, month, clip_start, clip_end))
    return units


def expected_ticks(scale: str, instrument_id: str) -> float:
    """Expected ticks per instrument-year at ``scale`` (before 0.1 % gap removal)."""
    return _scale_cfg(scale)["ticks_per_instrument_year"] * load_params(instrument_id).ticks_weight


def _rng(seed: int, *key: int) -> np.random.Generator:
    return np.random.Generator(np.random.PCG64(np.random.SeedSequence(seed, spawn_key=key)))


def _crc(instrument_id: str) -> int:
    return zlib.crc32(instrument_id.encode())


class DailyPath:
    """Calendar-day arrays from ``ANCHOR``: regime, returns, jump and log-level (read-only)."""

    def __init__(self, n_days: int) -> None:
        """Allocate zeroed arrays for ``n_days`` calendar days."""
        self.regime = np.zeros(n_days, dtype=np.int8)
        self.ret = np.zeros(n_days)  # total log-return of the day (0 on weekends)
        self.jump = np.zeros(n_days)  # summed jump size inside ``ret``
        self.x_close = np.zeros(n_days)  # log of the shifted level at the end of the day
        self.business = np.zeros(n_days, dtype=bool)


@lru_cache(maxsize=64)
def daily_path(instrument_id: str, seed: int) -> DailyPath:
    """Regime/return/level path for the whole anchor range (cheap: one scalar loop over days)."""
    p = load_params(instrument_id)
    rng = _rng(seed, _crc(instrument_id), 0)
    n_days = date(LAST_YEAR + 1, 1, 1).toordinal() - ANCHOR.toordinal()
    path = DailyPath(n_days)
    days = _ANCHOR_DAY + np.arange(n_days)
    path.business = (days + 3) % 7 < 5  # noqa: PLR2004 - epoch day 0 was a Thursday
    n_bus = int(path.business.sum())
    cum = np.cumsum(np.asarray(p.transition), axis=1)
    draws = rng.random(n_bus)
    regime = np.empty(n_bus, dtype=np.int8)
    state = int(np.searchsorted(np.cumsum(p.stationary), rng.random()))
    for i in range(n_bus):  # per business day (not per tick): a 3-state Markov chain
        state = min(int(np.searchsorted(cum[state], draws[i], side="right")), 2)
        regime[i] = state
    lam = p.jump_intensity * np.asarray(p.jump_regime_mult)[regime]
    n_jumps = rng.poisson(lam)
    jump = n_jumps * p.jump_mean + np.sqrt(n_jumps) * p.jump_sd * rng.standard_normal(n_bus)
    eps = p.sigma_diffusive[regime] * rng.standard_normal(n_bus) + jump
    mu = float(np.log(p.base_level + p.shift))
    kappa, x = p.mean_reversion, mu
    ret = np.empty(n_bus)
    for i, e in enumerate(eps.tolist()):  # AR(1) recurrence, per business day
        step = kappa * (mu - x) + e
        ret[i] = step
        x += step
    path.regime[path.business] = regime
    path.ret[path.business] = ret
    path.jump[path.business] = jump
    path.x_close = np.log(p.base_level + p.shift) + np.cumsum(path.ret)
    return path


@lru_cache(maxsize=8)
def _session_cdf(start_h: float, end_h: float) -> tuple[np.ndarray, np.ndarray]:
    """Cumulative activity over the day on a minute grid: U-shaped inside the session, 0 outside."""
    edges = np.linspace(0.0, 24.0, _SESSION_GRID + 1)
    mid = (edges[:-1] + edges[1:]) / 2
    u = np.clip((mid - start_h) / (end_h - start_h), 0.0, 1.0)
    weight = np.where((mid >= start_h) & (mid < end_h), 1.0 + 3.0 * (2.0 * u - 1.0) ** 2, 0.0)
    return np.concatenate([[0.0], np.cumsum(weight) / weight.sum()]), edges


def injection_mask_outliers(rng: np.random.Generator, n: int, rate: float) -> np.ndarray:
    """Boolean mask of ticks to turn into fat-finger outliers (Bernoulli(rate) per tick)."""
    return rng.random(n) < rate


def injection_mask_gaps(
    rng: np.random.Generator, n: int, rate: float, mean_len: float
) -> np.ndarray:
    """Boolean mask of ticks to drop: geometric runs of mean ``mean_len`` covering ``rate`` of n."""
    starts = np.flatnonzero(rng.random(n) < rate / mean_len)
    lengths = rng.geometric(1.0 / mean_len, size=starts.size)
    delta = np.zeros(n + 1, dtype=np.int64)
    np.add.at(delta, starts, 1)
    np.add.at(delta, np.minimum(starts + lengths, n), -1)
    return np.cumsum(delta[:n]) > 0


def _day_selection(year: int, month: int, start: date | None, end: date | None) -> np.ndarray:
    """Calendar-day indices (into the daily path) of business days of the month within the range."""
    lo, hi = month_bounds(year, month)
    first, last = lo // _NS_DAY, hi // _NS_DAY
    if start is not None:
        first = max(first, start.toordinal() - _EPOCH_DAY)
    if end is not None:
        last = min(last, end.toordinal() - _EPOCH_DAY + 1)
    return np.arange(first, last) - _ANCHOR_DAY


class _Day(NamedTuple):
    """Daily-layer inputs of one business day."""

    cal: int  # calendar-day index into the daily path
    n: int  # nominal tick count
    regime: int
    jump_u: float  # activity-time position of the day's jump


def _day_ticks(
    rng: np.random.Generator, p: InstrumentParams, path: DailyPath, day: _Day
) -> dict[str, np.ndarray]:
    """All ticks of one business day (about 120k rows at most at L rate); fully vectorised."""
    n = day.n
    inj = _raw()["injection"]
    u = np.sort(rng.random(n))  # ordered arrival times in activity time
    cdf, edges = _session_cdf(p.session_start_h, p.session_end_h)
    ts = (day.cal + _ANCHOR_DAY) * _NS_DAY + (np.interp(u, cdf, edges) * _NS_HOUR).astype(np.int64)
    # Brownian bridge in activity time from the day's open to its close
    dw = rng.standard_normal(n) * np.sqrt(np.diff(u, prepend=0.0))
    cs = np.cumsum(dw)
    w_end = cs[-1] + rng.standard_normal() * np.sqrt(1.0 - u[-1])
    bridge = cs - u * w_end
    jump, ret = float(path.jump[day.cal]), float(path.ret[day.cal])
    x = (
        (path.x_close[day.cal] - ret)
        + (ret - jump) * u
        + jump * (u >= day.jump_u)
        + p.sigma_diffusive[day.regime] * bridge
    )
    level = np.exp(x)
    half = 0.5e-4 * p.spread_bps * p.spread_regime_mult[day.regime] * level
    kind = rng.random(n)
    is_trade = kind < p.trade_share
    side = np.where(is_trade, 0, np.where(kind < (1 + p.trade_share) / 2, 1, 2)).astype(np.int8)
    sign = np.where(is_trade, rng.choice([-1.0, 1.0], size=n), np.where(side == 1, -1.0, 1.0))
    price = level - p.shift + sign * half
    out = injection_mask_outliers(rng, n, float(inj["outlier_rate"]))
    shock = rng.choice([-1.0, 1.0], size=n) * rng.uniform(_OUTLIER_MIN, _OUTLIER_MAX, n)
    price = np.where(out, price + shock * level, price)
    size = np.where(is_trade, p.size_median * np.exp(0.8 * rng.standard_normal(n)), 0.0)
    dropped = injection_mask_gaps(rng, n, float(inj["gap_rate"]), float(inj["gap_mean_len"]))
    after_gap = np.zeros(n, dtype=bool)
    after_gap[1:] = dropped[:-1] & ~dropped[1:]
    # injected outliers are flagged here (also inside the validator's warm-up window); the
    # validator ORs its own findings on top. SYNTHETIC is set only by validate_and_flag.
    quality = after_gap * int(QualityFlag.GAP_BEFORE) + out * int(QualityFlag.OUTLIER)
    keep = ~dropped
    return {
        "ts_ns": ts[keep],
        "price": price[keep],
        "size": size[keep].astype(np.float32),
        "side": side[keep],
        "quality": quality[keep].astype(np.int8),
    }


def _chunks(counts: np.ndarray, max_rows: int) -> list[range]:
    """Greedy day blocks of at most ``max_rows`` nominal ticks (a single day may exceed it).

    Boundaries depend only on the drawn tick counts, never on timing or memory.
    """
    blocks, first, total = [], 0, 0
    for i, c in enumerate(counts.tolist()):
        if total and total + c > max_rows:
            blocks.append(range(first, i))
            first, total = i, 0
        total += c
    if first < counts.size:
        blocks.append(range(first, counts.size))
    return blocks


def _month_chunks(
    instrument_id: str,
    year: int,
    month: int,
    *,
    seed: int,
    scale: str,
    start: date | None,
    end: date | None,
    max_rows: int,
) -> Iterator[dict[str, np.ndarray]]:
    p = load_params(instrument_id)
    path = daily_path(instrument_id, seed)
    cal = _day_selection(year, month, start, end)
    cal = cal[path.business[cal]]
    rng = _rng(seed, _crc(instrument_id), year, month)  # month stream: counts and jump positions
    regime = path.regime[cal].astype(np.intp)
    base_rate = expected_ticks(scale, instrument_id) / (
        BUSINESS_DAYS_PER_YEAR * float(p.stationary @ np.asarray(p.regime_activity))
    )
    counts = rng.poisson(base_rate * np.asarray(p.regime_activity)[regime])
    jump_u = rng.random(cal.size)
    carry = -(2**62)
    for block in _chunks(counts, max_rows):
        parts = []
        for i in block:
            if counts[i] == 0:
                continue
            day = _Day(int(cal[i]), int(counts[i]), int(regime[i]), float(jump_u[i]))
            day_rng = _rng(seed, _crc(instrument_id), year, month, day.cal)  # one stream per day
            parts.append(_day_ticks(day_rng, p, path, day))
        if not parts:
            continue
        cols = {k: np.concatenate([part[k] for part in parts]) for k in parts[0]}
        del parts
        idx = np.arange(cols["ts_ns"].size)  # strictly increasing ts (PK), carried across chunks
        cols["ts_ns"] = np.maximum(np.maximum.accumulate(cols["ts_ns"] - idx), carry + 1) + idx
        carry = int(cols["ts_ns"][-1])
        yield cols


def generate(
    instrument_id: str,
    year: int,
    month: int,
    *,
    seed: int,
    scale: str,
    clock: Clock,
    start: date | None = None,
    end: date | None = None,
    chunk_rows: int = CHUNK_ROWS,
) -> Iterator[pa.Table]:
    """Yield validated ``TICK_V1`` tables of one instrument-month, each of about ``chunk_rows``.

    The month is cut into blocks of whole business days (see ``_chunks``), so memory is bounded
    by the block, and the cut points are a pure function of the seed. Tables are consecutive and
    strictly increasing in ``ts_ns`` across chunks. ``start``/``end`` (inclusive,
    ``TIDAL_SYNTH__START/END``) clip the month and change which days exist, hence are part of the
    determinism key. ``ingested_at_ns`` is ``clock.now_ns()``. Every table already passed
    ``validate_and_flag(synthetic=True)``; its rolling OUTLIER window restarts per chunk.
    """
    if not 2000 <= year <= LAST_YEAR:  # noqa: PLR2004
        msg = f"year {year} outside generator range 2000..{LAST_YEAR}"
        raise ConfigError(msg)
    now = clock.now_ns()
    chunks = _month_chunks(
        instrument_id,
        year,
        month,
        seed=seed,
        scale=scale,
        start=start,
        end=end,
        max_rows=chunk_rows,
    )
    for cols in chunks:
        n = cols["ts_ns"].size
        inst = pa.DictionaryArray.from_arrays(
            np.zeros(n, dtype=np.int16), pa.array([instrument_id])
        )
        table = pa.Table.from_arrays(
            [
                pa.array(cols["ts_ns"]),
                inst,
                pa.array(cols["price"]),
                pa.array(cols["size"]),
                pa.array(cols["side"]),
                pa.array(cols["quality"]),
                pa.array(np.full(n, now, dtype=np.int64)),
                pa.array(np.ones(n, dtype=np.int16)),
            ],
            schema=TICK_V1,
        )
        yield validate_and_flag(table, synthetic=True)


def generate_unit(
    unit: WorkUnit, *, seed: int, scale: str, clock: Clock, chunk_rows: int = CHUNK_ROWS
) -> Iterator[pa.Table]:
    """``generate`` for one ``plan`` entry (passes the clip bounds of partial edge months)."""
    return generate(
        unit.instrument_id,
        unit.year,
        unit.month,
        seed=seed,
        scale=scale,
        clock=clock,
        start=unit.start,
        end=unit.end,
        chunk_rows=chunk_rows,
    )


def max_workers_for_memory(budget_bytes: int = 2 * 1024**3) -> int:
    """Parallel workers (at least 1) whose measured peaks fit ``budget_bytes`` for the whole run.

    Uses ``WORKER_PEAK_BYTES`` (measured Windows peak working set of a worker incl. interpreter,
    polars and pyarrow, on the worst L-rate month; see the module docstring).
    """
    return max(1, budget_bytes // WORKER_PEAK_BYTES)
