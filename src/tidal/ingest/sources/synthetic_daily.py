"""Synthetic daily history for the daily catalogue (T-117, ADR-0013).

Generates ``BAR_V1`` ``1d`` bars directly (no ticks) for the ``EIA.*`` / ``FRED.*`` catalogue ids:
Brent/WTI/Henry Hub spot, WTI and Henry Hub futures M1..M4 and EUR/USD. The ids are kept for
contract stability; the values are synthetic and every row carries ``SYNTHETIC``.

Model (parameters and the "stylised, not calibrated" caveat in ``synthetic_params.yaml``,
section ``daily_history``): spot is an OU deviation around a stylised anchor curve driven by
Student-t(5) shocks with stress-window vol multipliers; futures add a carry that is lower
(towards backwardation) when the price is above its anchor, plus seasonality and basis noise, so
M1-M2 calendar spreads change sign over the history. Episodes overwrite specific days, giving a
deterministic WTI negative print on 2020-04-20.

Determinism: all draws use ``Generator(PCG64(SeedSequence(seed, spawn_key=(crc32(key), 1))))``.
Paths are simulated once over the whole 2000-01-01..2060-12-31 business-day grid with fixed-size
draws and then sliced, so a bar never depends on the requested ``start``/``end``. Daily bars are
stamped at UTC midnight of the trade date (ADR-0009); only Monday-Friday exist (no holidays).
``end`` is exclusive of the clock's own date: a daily bar is complete only after its day.
"""

import zlib
from datetime import date
from functools import lru_cache
from typing import Any, Final, NamedTuple

import numpy as np
import polars as pl
import pyarrow as pa
import yaml
from pydantic import BaseModel, ConfigDict

from tidal.common.errors import ConfigError
from tidal.common.timeutil import Clock
from tidal.contracts.instruments import CATALOGUE_V1, Granularity, Source
from tidal.contracts.schemas import BAR_V1, Interval
from tidal.ingest.sources.synthetic import ANCHOR, LAST_YEAR, PARAMS_PATH
from tidal.ingest.validate import validate_and_flag

NS_DAY: Final = 86_400 * 10**9
N_FUTURES: Final = 4
_EPOCH: Final = date(1970, 1, 1).toordinal()
_T_DF: Final = 5.0


class _Anchors(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    anchors: list[tuple[date, float]]
    daily_vol: float
    mean_reversion: float


class Commodity(_Anchors):
    """Spot (and optionally futures) parameters of one commodity."""

    spot: str
    futures: str | None = None
    corr_with: str | None = None
    rho: float = 0.0
    carry_mean: float = 0.0
    carry_level_beta: float = 0.0
    carry_noise_vol: float = 0.0
    carry_reversion: float = 0.02
    seasonal_amp: float = 0.0
    basis_noise: float = 0.0
    fut_volume: float = 0.0


class Fx(_Anchors):
    """Spot FX parameters."""

    instrument_id: str


class StressWindow(BaseModel):
    """Inclusive date window with a daily-vol multiplier."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    start: date
    end: date
    vol_mult: float


class Episode(BaseModel):
    """Deterministic overwrite of one commodity's spot and FUT1 on ``date``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    commodity: str
    date: date
    spot: float
    fut1: float
    pre_factor: float
    post_factor: float


class DailyParams(BaseModel):
    """The ``daily_history`` section of ``synthetic_params.yaml``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    stress_windows: list[StressWindow]
    commodities: dict[str, Commodity]
    fx: dict[str, Fx]
    range_scale: float
    episodes: list[Episode]


@lru_cache(maxsize=1)
def load_daily_params() -> DailyParams:
    """Validated ``daily_history`` parameters."""
    loaded: Any = yaml.safe_load(PARAMS_PATH.read_text(encoding="utf-8"))  # untyped YAML document
    return DailyParams(**loaded["daily_history"])


def daily_instruments() -> list[str]:
    """Daily (``1d``) non-``SYN`` catalogue ids in contract order."""
    return [
        e.instrument_id
        for e in CATALOGUE_V1
        if e.source is not Source.SYN and e.granularity is Granularity.D1
    ]


class _Series(NamedTuple):
    close: np.ndarray
    sigma: np.ndarray  # daily vol proxy per day (range scale)
    volume: np.ndarray | None  # None = no volume (NaN)


def _rng(seed: int, key: str) -> np.random.Generator:
    child = np.random.SeedSequence(seed, spawn_key=(zlib.crc32(key.encode()), 1))
    return np.random.Generator(np.random.PCG64(child))


def _shocks(rng: np.random.Generator, *shape: int) -> np.ndarray:
    """Unit-variance Student-t(5) draws."""
    return np.asarray(rng.standard_t(_T_DF, size=shape) / np.sqrt(_T_DF / (_T_DF - 2.0)))


def _ou(shock: np.ndarray, kappa: float) -> np.ndarray:
    """``y_t = (1 - kappa) y_{t-1} + shock_t`` with ``y_{-1} = 0`` (vectorised EWM recurrence)."""
    scaled = shock / kappa
    scaled[0] = 0.0
    return pl.Series(scaled).ewm_mean(alpha=kappa, adjust=False).to_numpy()


@lru_cache(maxsize=1)
def _grid() -> np.ndarray:
    """Epoch-day numbers of every Monday-Friday from ``ANCHOR`` to the end of ``LAST_YEAR``."""
    first = ANCHOR.toordinal() - _EPOCH
    last = date(LAST_YEAR + 1, 1, 1).toordinal() - _EPOCH
    days = np.arange(first, last)
    return days[(days + 3) % 7 < 5]  # noqa: PLR2004 - epoch day 0 was a Thursday


def _anchor_curve(days: np.ndarray, anchors: list[tuple[date, float]]) -> np.ndarray:
    xs = np.array([a.toordinal() - _EPOCH for a, _ in anchors], dtype=float)
    ys = np.log(np.array([v for _, v in anchors]))
    return np.asarray(np.exp(np.interp(days, xs, ys)))


def _vol_mult(days: np.ndarray, p: DailyParams) -> np.ndarray:
    mult = np.ones(days.size)
    for w in p.stress_windows:
        lo, hi = w.start.toordinal() - _EPOCH, w.end.toordinal() - _EPOCH
        mult = np.where((days >= lo) & (days <= hi), mult * w.vol_mult, mult)
    return mult


def _apply_episodes(p: DailyParams, days: np.ndarray, name: str, out: dict[str, _Series]) -> None:
    """Overwrite spot and FUT1 of ``name`` around each of its episodes (new arrays, no aliasing)."""
    com = p.commodities[name]
    if com.futures is None:
        return
    ids = {"spot": com.spot, "fut1": f"{com.futures}1"}
    for ep in (e for e in p.episodes if e.commodity == name):
        i = int(np.searchsorted(days, ep.date.toordinal() - _EPOCH))
        if i == 0 or i >= days.size - 1 or days[i] != ep.date.toordinal() - _EPOCH:
            raise ConfigError(f"episode {ep.name} is not on a business day inside the grid")
        for key, iid in ids.items():
            close = out[iid].close.copy()
            close[i - 1] *= ep.pre_factor
            close[i] = ep.spot if key == "spot" else ep.fut1
            close[i + 1] *= ep.post_factor
            out[iid] = out[iid]._replace(close=close)


@lru_cache(maxsize=4)
def _build(seed: int) -> dict[str, _Series]:
    """Full-horizon close/vol/volume arrays of every daily instrument for ``seed``."""
    p = load_daily_params()
    days = _grid()
    n = days.size
    mult = _vol_mult(days, p)
    z_spot = {c: _shocks(_rng(seed, f"DAILY.{c}.SPOT"), n) for c in p.commodities}
    out: dict[str, _Series] = {}
    for name, com in p.commodities.items():
        z = z_spot[name]
        if com.corr_with is not None:
            z = com.rho * z_spot[com.corr_with] + np.sqrt(1.0 - com.rho**2) * z
        sigma = com.daily_vol * mult
        y = _ou(sigma * z, com.mean_reversion)
        spot = _anchor_curve(days, com.anchors) * np.exp(y)
        out[com.spot] = _Series(spot, sigma, None)
        if com.futures is None:
            continue
        rng = _rng(seed, f"DAILY.{name}.CURVE")
        noise = _ou(com.carry_noise_vol * _shocks(rng, n), com.carry_reversion)
        doy = (days - (date(2000, 1, 1).toordinal() - _EPOCH)) % 365.25
        season = -com.seasonal_amp * np.cos(2.0 * np.pi * (doy - 15.0) / 365.25)
        carry = com.carry_mean - com.carry_level_beta * y + season + noise
        eps = _shocks(rng, N_FUTURES, n)
        vol_noise = rng.standard_normal(N_FUTURES * n).reshape(N_FUTURES, n)
        for k in range(1, N_FUTURES + 1):  # four contracts, not market rows
            fut = spot * np.exp(k * carry + com.basis_noise * np.sqrt(k) * eps[k - 1])
            volume = com.fut_volume / k * np.exp(0.4 * vol_noise[k - 1])
            out[f"{com.futures}{k}"] = _Series(fut, sigma, volume)
    for name, fx in p.fx.items():
        y = _ou(fx.daily_vol * mult * _shocks(_rng(seed, f"DAILY.{name}.FX"), n), fx.mean_reversion)
        out[fx.instrument_id] = _Series(
            _anchor_curve(days, fx.anchors) * np.exp(y), fx.daily_vol * mult, None
        )
    for name in p.commodities:
        _apply_episodes(p, days, name, out)
    return out


def generate_daily(
    instrument_id: str, *, seed: int, start: date, end: date, clock: Clock
) -> pa.Table:
    """Validated ``BAR_V1`` ``1d`` table of ``instrument_id`` for trade dates in ``[start, end]``.

    ``open`` is the previous close (the first bar of the grid opens at its close); ``high``/``low``
    extend the open/close range by a seeded fraction of daily vol. Futures have a positive
    volume, spot and FX ``NaN`` (no volume, data-contracts §3.1). Negative prices are never
    clamped. ``ingested_at_ns`` is ``clock.now_ns()``; every row is flagged ``SYNTHETIC``.
    """
    if start.year < ANCHOR.year or end.year > LAST_YEAR:
        raise ConfigError(f"daily history range outside 2000..{LAST_YEAR}")
    series = _build(seed).get(instrument_id)
    if series is None:
        raise ConfigError(f"no synthetic daily series for {instrument_id}")
    days = _grid()
    lo = int(np.searchsorted(days, start.toordinal() - _EPOCH, side="left"))
    hi = int(np.searchsorted(days, end.toordinal() - _EPOCH, side="right"))
    rng = _rng(seed, f"DAILY.{instrument_id}.RANGE")
    z = np.abs(rng.standard_normal((2, days.size)))
    prev = np.concatenate([series.close[:1], series.close[:-1]])
    open_, close = prev[lo:hi], series.close[lo:hi]
    ext = load_daily_params().range_scale * series.sigma[lo:hi] * np.abs(close)
    high = np.maximum(open_, close) + z[0, lo:hi] * ext
    low = np.minimum(open_, close) - z[1, lo:hi] * ext
    n = hi - lo
    volume = np.full(n, np.nan) if series.volume is None else series.volume[lo:hi]
    table = pa.Table.from_arrays(
        [
            pa.array(days[lo:hi].astype(np.int64) * NS_DAY),
            pa.DictionaryArray.from_arrays(np.zeros(n, np.int16), pa.array([instrument_id])),
            pa.DictionaryArray.from_arrays(np.zeros(n, np.int8), pa.array([Interval.D1.value])),
            pa.array(open_),
            pa.array(high),
            pa.array(low),
            pa.array(close),
            pa.array(volume),
            pa.nulls(n, pa.int32()),
            pa.array(np.zeros(n, np.int8)),
            pa.array(np.full(n, clock.now_ns(), np.int64)),
            pa.array(np.ones(n, np.int16)),
        ],
        schema=BAR_V1,
    )
    return validate_and_flag(table, synthetic=True)
