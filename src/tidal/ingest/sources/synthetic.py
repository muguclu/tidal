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
  each run carries ``GAP_BEFORE``. Every row carries ``SYNTHETIC``; ``validate_and_flag`` then adds
  ``NEGATIVE``/``OUTLIER``. Real model jumps can also be flagged ``OUTLIER`` (intended).

Determinism and stream keys
---------------------------
All draws use ``numpy.random.Generator(PCG64(SeedSequence(seed, spawn_key=key)))``. Keys are
deterministic children of the root seed (the stateless equivalent of ``SeedSequence.spawn``, so the
order of generation never matters and years/instruments can run in parallel):

* ``(crc32(instrument_id), 0)``            -> daily path stream
* ``(crc32(instrument_id), year, month)``  -> tick stream of one instrument-month (child of the
  ``(instrument, year)`` node)

``crc32`` of the id is used instead of the catalogue position so keys never change when the
catalogue is extended. ``ingested_at_ns`` comes from the injected clock, never from wall time.
Output is one TICK_V1 table per instrument-month (about 0.6M rows at L rate, bounded memory).

Source protocol: ``Source`` lives in T-102's ``base.py``; until then this module exposes the plain
functions ``plan`` and ``generate``.
"""

import zlib
from collections.abc import Iterator
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Any, Final

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


def plan(scale: str, start: date, end: date) -> list[tuple[str, int, int]]:
    """Work units ``(instrument_id, year, month)`` for ``scale`` over ``[start, end]`` inclusive.

    Instruments: the first N ``SYN.*`` catalogue rows. Years: the last N years of the range
    (all of them if shorter). Months: those overlapping the range. Order: instrument, year, month.
    """
    cfg = _scale_cfg(scale)
    years = range(max(start.year, end.year - cfg["years"] + 1), end.year + 1)
    return [
        (inst, year, month)
        for inst in synthetic_instruments()[: cfg["instruments"]]
        for year in years
        for month in range(1, 13)
        if (year, month) >= (start.year, start.month) and (year, month) <= (end.year, end.month)
    ]


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


def _month_ticks(
    instrument_id: str,
    year: int,
    month: int,
    *,
    seed: int,
    scale: str,
    start: date | None,
    end: date | None,
) -> dict[str, np.ndarray]:
    p = load_params(instrument_id)
    path = daily_path(instrument_id, seed)
    cal = _day_selection(year, month, start, end)
    cal = cal[path.business[cal]]
    rng = _rng(seed, _crc(instrument_id), year, month)
    inj = _raw()["injection"]
    assert isinstance(inj, dict)  # noqa: S101
    regime = path.regime[cal].astype(np.intp)
    base_rate = expected_ticks(scale, instrument_id) / (
        BUSINESS_DAYS_PER_YEAR * float(p.stationary @ np.asarray(p.regime_activity))
    )
    counts = rng.poisson(base_rate * np.asarray(p.regime_activity)[regime])
    n = int(counts.sum())
    day = np.repeat(np.arange(cal.size), counts)
    # sorted uniforms inside each day = ordered arrival times in activity time
    u = np.sort(rng.random(n) + day) - day
    cdf, edges = _session_cdf(p.session_start_h, p.session_end_h)
    t_ns = (np.interp(u, cdf, edges) * _NS_HOUR).astype(np.int64)
    ts = (cal + _ANCHOR_DAY)[day] * _NS_DAY + t_ns
    ts = np.maximum.accumulate(ts - np.arange(n)) + np.arange(n)  # strictly increasing (PK)
    # Brownian bridge in activity time, per day, segmented cumsum
    prev_u = np.where(
        np.arange(n) == np.repeat(np.cumsum(counts) - counts, counts), 0.0, np.roll(u, 1)
    )
    dw = rng.standard_normal(n) * np.sqrt(u - prev_u)
    last_u = np.zeros(cal.size)
    np.maximum.at(last_u, day, u)
    w_end = np.bincount(day, weights=dw, minlength=cal.size) + rng.standard_normal(
        cal.size
    ) * np.sqrt(1.0 - last_u)
    starts = np.cumsum(counts) - counts
    cs = np.cumsum(dw)
    offset = np.where(starts > 0, cs[np.maximum(starts - 1, 0)] if n else 0.0, 0.0)
    bridge = cs - offset[day] - u * w_end[day]
    sigma = p.sigma_diffusive[regime]
    jump = path.jump[cal]
    jump_u = rng.random(cal.size)
    x_open = path.x_close[cal] - path.ret[cal]
    x = (
        x_open[day]
        + (path.ret[cal] - jump)[day] * u
        + jump[day] * (u >= jump_u[day])
        + sigma[day] * bridge
    )
    level = np.exp(x)
    mid = level - p.shift
    half = 0.5e-4 * p.spread_bps * np.asarray(p.spread_regime_mult)[regime][day] * level
    kind = rng.random(n)
    is_trade = kind < p.trade_share
    side = np.where(is_trade, 0, np.where(kind < (1 + p.trade_share) / 2, 1, 2)).astype(np.int8)
    sign = np.where(is_trade, rng.choice([-1.0, 1.0], size=n), np.where(side == 1, -1.0, 1.0))
    price = mid + sign * half
    out = injection_mask_outliers(rng, n, float(inj["outlier_rate"]))
    price = np.where(
        out,
        price
        + rng.choice([-1.0, 1.0], size=n) * rng.uniform(_OUTLIER_MIN, _OUTLIER_MAX, n) * level,
        price,
    )
    size = np.where(is_trade, p.size_median * np.exp(0.8 * rng.standard_normal(n)), 0.0)
    dropped = injection_mask_gaps(rng, n, float(inj["gap_rate"]), float(inj["gap_mean_len"]))
    after_gap = np.zeros(n, dtype=bool)
    after_gap[1:] = dropped[:-1] & ~dropped[1:]
    quality = np.where(after_gap, int(QualityFlag.GAP_BEFORE), 0)
    keep = ~dropped
    return {
        "ts_ns": ts[keep],
        "price": price[keep],
        "size": size[keep].astype(np.float32),
        "side": side[keep],
        "quality": (quality[keep] | int(QualityFlag.SYNTHETIC)).astype(np.int8),
    }


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
) -> Iterator[pa.Table]:
    """Yield the validated ``TICK_V1`` table(s) of one instrument-month (one table per call).

    ``start``/``end`` (inclusive, ``TIDAL_SYNTH__START/END``) clip the month; they change the number
    of days drawn, hence are part of the determinism key. ``ingested_at_ns`` is ``clock.now_ns()``.
    Output is sorted by (strictly increasing) ``ts_ns`` and already passed ``validate_and_flag``.
    """
    if not 2000 <= year <= LAST_YEAR:  # noqa: PLR2004
        msg = f"year {year} outside generator range 2000..{LAST_YEAR}"
        raise ConfigError(msg)
    cols = _month_ticks(instrument_id, year, month, seed=seed, scale=scale, start=start, end=end)
    n = cols["ts_ns"].size
    if n == 0:
        return
    inst = pa.DictionaryArray.from_arrays(np.zeros(n, dtype=np.int16), pa.array([instrument_id]))
    table = pa.Table.from_arrays(
        [
            pa.array(cols["ts_ns"]),
            inst,
            pa.array(cols["price"]),
            pa.array(cols["size"]),
            pa.array(cols["side"]),
            pa.array(cols["quality"]),
            pa.array(np.full(n, clock.now_ns(), dtype=np.int64)),
            pa.array(np.ones(n, dtype=np.int16)),
        ],
        schema=TICK_V1,
    )
    yield validate_and_flag(table, synthetic=True)
