"""Backtest engine (SPEC-05 §2): bars + request -> trades, equity, metrics, result hash.

Pure: no I/O, no clock, no hidden randomness (v1 strategies are deterministic; ``seed`` is
part of the hashed request). Vectorised with polars/numpy.

Execution model
---------------
* The strategy position for bar ``t`` is decided at the close of ``t`` and filled at
  ``t+1``: ``position_held[t] = signal[t-1]``.
* Fill price is the *next close* for ``1d`` bars (daily bars are synthetic with
  open == close, ADR-0013) and the *next open* for intraday bars.
* Sizing (no look-ahead): a fixed contract count ``q = INITIAL_CAPITAL / notional`` set at the
  FIRST usable fill (``notional >= MIN_PRICE``; |p| for one leg, |p_a| + |p_b| for a spread).
  The position is flat before that fill; the run is flat if none exists. P&L is additive in
  price (``q x dPrice``), so zero and negative prices (WTI 2020-04-20) pass through without
  ratios or clamping.
* Costs: ``(fee_bps | slippage_bps) / 1e4 x |qty traded| x |fill price|`` (spread: over
  both legs' notionals), in currency units, deducted from equity at the fill bar.
* Window: bars with ``start <= ts_ns < end`` (half-open) are used.
* ``calendar_spread`` emits one trades row per LEG per fill (+q leg_a, -q leg_b for a long
  spread); legs are inner-joined on ``ts_ns``; equity P&L is q x d(spread).
* ``n_trades`` counts position segments entered (closed + one still open at the end);
  ``hit_rate`` uses closed segments only, net of costs.
"""

import hashlib
import json
from dataclasses import dataclass

import numpy as np
import polars as pl
from numpy.typing import NDArray

from tidal.backtest.metrics import PERIODS_PER_YEAR, compute_metrics
from tidal.backtest.strategies import (
    calendar_spread_signals,
    ma_crossover_signals,
    mean_reversion_signals,
)
from tidal.common.errors import ValidationFailed
from tidal.contracts.models import (
    BacktestMetrics,
    CalendarSpreadRequest,
    MaCrossoverRequest,
    MeanReversionRequest,
)

INITIAL_CAPITAL = 1_000_000.0
MIN_PRICE = 1e-9  # below this a fill price cannot size a position

_EQUITY_SCHEMA = {
    "ts_ns": pl.Int64,
    "equity": pl.Float64,
    "position": pl.Float64,
    "drawdown": pl.Float64,
}
_TRADES_SCHEMA = {
    "ts_ns": pl.Int64,
    "instrument_id": pl.String,
    "qty": pl.Float64,
    "price": pl.Float64,
    "fee": pl.Float64,
    "slippage": pl.Float64,
}
_REQUIRED = ("ts_ns", "open", "close")

Request = MaCrossoverRequest | MeanReversionRequest | CalendarSpreadRequest


@dataclass(frozen=True)
class BacktestOutput:
    """Engine result; frames match ``BT_EQUITY_V1`` / ``BT_TRADES_V1`` columns and dtypes."""

    trades: pl.DataFrame
    equity: pl.DataFrame
    metrics: BacktestMetrics
    result_sha256: str


@dataclass(frozen=True)
class _Sim:
    trades: pl.DataFrame
    equity_df: pl.DataFrame
    equity: NDArray[np.float64]
    position: NDArray[np.float64]
    closed_pnls: NDArray[np.float64]
    n_trades: int


def _prepare(bars: pl.DataFrame, start: int, end: int) -> pl.DataFrame:
    missing = [c for c in _REQUIRED if c not in bars.columns]
    if missing:
        msg = f"bars missing columns: {missing}"
        raise ValidationFailed(msg)
    out = (
        bars.select(
            pl.col("ts_ns").cast(pl.Int64),
            pl.col("open").cast(pl.Float64),
            pl.col("close").cast(pl.Float64),
        )
        .filter((pl.col("ts_ns") >= start) & (pl.col("ts_ns") < end))
        .sort("ts_ns")
    )
    if out["ts_ns"].n_unique() != out.height:
        msg = "bars contain duplicate ts_ns; pass one version per bar"
        raise ValidationFailed(msg)
    return out


def _spread(
    a: pl.DataFrame, b: pl.DataFrame, fill_col: str
) -> tuple[pl.DataFrame, NDArray[np.float64], NDArray[np.float64]]:
    """Inner-join legs on ts_ns; return the spread frame and each leg's fill prices."""
    j = a.join(b, on="ts_ns", how="inner", suffix="_b").sort("ts_ns")
    frame = j.select(
        "ts_ns",
        (pl.col("open") - pl.col("open_b")).alias("open"),
        (pl.col("close") - pl.col("close_b")).alias("close"),
    )
    return frame, j[fill_col].to_numpy(), j[f"{fill_col}_b"].to_numpy()


def _simulate(
    ts: NDArray[np.int64],
    signal: NDArray[np.float64],
    legs: list[tuple[str, NDArray[np.float64], float]],
    fee_bps: float,
    slip_bps: float,
) -> _Sim:
    """Simulate; ``legs`` is ``(instrument_id, fill prices, weight)``, traded value = sum w*p."""
    fill = sum((w * px for _, px, w in legs), np.zeros(len(ts)))
    notional = sum((np.abs(px) for _, px, _ in legs), np.zeros(len(ts)))  # per unit traded
    pos = np.concatenate(([0.0], signal[:-1]))  # held from the fill of each bar
    # Fixed size from the FIRST usable fill (no future data): flat before it.
    usable = np.flatnonzero(notional >= MIN_PRICE)
    first = int(usable[0]) if len(usable) else len(ts)
    pos[:first] = 0.0
    unit = INITIAL_CAPITAL / notional[first] if first < len(ts) else 0.0
    pos_prev = np.concatenate(([0.0], pos[:-1]))
    dprice = np.concatenate(([0.0], np.diff(fill)))
    gross = unit * pos_prev * dprice
    dq = unit * (pos - pos_prev)
    fee = fee_bps / 1e4 * np.abs(dq) * notional
    slip = slip_bps / 1e4 * np.abs(dq) * notional
    equity = INITIAL_CAPITAL + np.cumsum(gross - fee - slip)
    peak = np.maximum.accumulate(equity)
    with np.errstate(all="ignore"):
        dd = np.where(peak > 0, equity / peak - 1.0, 0.0)

    # Position segments split on sign changes; closing costs go to the segment being closed.
    sgn = np.sign(pos)
    sgn_prev = np.sign(pos_prev)
    flipped = sgn != sgn_prev
    seg = np.cumsum(flipped)
    seg_prev = np.concatenate(([0], seg[:-1]))
    cost = fee + slip
    rate = (fee_bps + slip_bps) / 1e4
    close_cost = np.where(flipped, rate * unit * np.abs(pos_prev) * notional, 0.0)
    k = int(seg[-1]) + 1
    seg_pnl = np.bincount(seg_prev, weights=gross - close_cost, minlength=k)
    seg_pnl = seg_pnl - np.bincount(seg, weights=cost - close_cost, minlength=k)
    counts = np.bincount(seg, minlength=k)
    seg_sign = np.bincount(seg, weights=sgn, minlength=k) / np.maximum(counts, 1)
    entered = seg_sign != 0
    closed = entered & (np.arange(k) < k - 1)

    m = dq != 0
    parts = [
        pl.DataFrame(
            {
                "ts_ns": ts[m],
                "instrument_id": [iid] * int(m.sum()),
                "qty": w * dq[m],
                "price": px[m],
                "fee": fee_bps / 1e4 * np.abs(dq[m]) * np.abs(px[m]),
                "slippage": slip_bps / 1e4 * np.abs(dq[m]) * np.abs(px[m]),
            },
            schema=_TRADES_SCHEMA,
        )
        for iid, px, w in legs
    ]
    trades = pl.concat(parts).sort("ts_ns", maintain_order=True)
    equity_df = pl.DataFrame(
        {"ts_ns": ts, "equity": equity, "position": pos, "drawdown": dd}, schema=_EQUITY_SCHEMA
    )
    return _Sim(trades, equity_df, equity, pos, seg_pnl[closed], int(entered.sum()))


def _metrics_json(metrics: BacktestMetrics) -> str:
    """Metrics rounded to 12 significant digits so libm differences cannot change the hash."""
    d = {
        k: (v if v is None or isinstance(v, int) else format(v, ".12g"))
        for k, v in metrics.model_dump().items()
    }
    return json.dumps(d, sort_keys=True)


def _hash(
    request_json: str, trades: pl.DataFrame, equity: pl.DataFrame, metrics: BacktestMetrics
) -> str:
    h = hashlib.sha256()
    h.update(request_json.encode())
    for df in (equity, trades):
        for col in df.columns:
            s = df[col]
            if s.dtype == pl.String:
                data = "\x00".join(s.to_list()).encode()
            else:
                data = s.to_numpy().astype("<i8" if s.dtype == pl.Int64 else "<f8").tobytes()
            h.update(col.encode() + b"\x00" + len(data).to_bytes(8, "little"))
            h.update(data)
    h.update(_metrics_json(metrics).encode())
    return h.hexdigest()


def run_backtest(request: Request, bars: dict[str, pl.DataFrame]) -> BacktestOutput:
    """Run one resolved (sweep-free) backtest.

    ``bars`` maps ``instrument_id`` to a frame with ``ts_ns``, ``open``, ``close`` for each id
    in ``request.instrument_ids``. The output is a pure function of ``(request, bars)``.
    Raises ``ValidationFailed`` for a sweep request, missing instruments or bad frames.
    """
    if request.sweep is not None:
        msg = "run_backtest takes a resolved request: expand the sweep first"
        raise ValidationFailed(msg)
    ids = request.instrument_ids
    absent = [i for i in ids if i not in bars]
    if absent:
        msg = f"bars missing for instruments: {absent}"
        raise ValidationFailed(msg)
    frames = [_prepare(bars[i], request.start, request.end) for i in ids]
    interval = request.interval.value
    ppy = PERIODS_PER_YEAR[interval]
    col = "close" if interval == "1d" else "open"
    if isinstance(request, CalendarSpreadRequest):
        frame, fa, fb = _spread(frames[0], frames[1], col)
        legs = [(ids[0], fa, 1.0), (ids[1], fb, -1.0)]
    else:
        frame = frames[0]
        legs = [(ids[0], frame[col].to_numpy(), 1.0)]
    request_json = request.model_dump_json(exclude_none=True)

    if frame.height == 0:
        eq = pl.DataFrame(schema=_EQUITY_SCHEMA)
        tr = pl.DataFrame(schema=_TRADES_SCHEMA)
        empty = np.empty(0)
        metrics = compute_metrics(empty, empty, empty, 0, ppy)
        return BacktestOutput(tr, eq, metrics, _hash(request_json, tr, eq, metrics))

    if isinstance(request, MaCrossoverRequest):
        sig = ma_crossover_signals(frame, request.params)
    elif isinstance(request, MeanReversionRequest):
        sig = mean_reversion_signals(frame, request.params)
    else:
        sig = calendar_spread_signals(frame, request.params)

    sim = _simulate(
        frame["ts_ns"].to_numpy(),
        sig.to_numpy(),
        legs,
        request.costs.fee_bps,
        request.costs.slippage_bps,
    )
    metrics = compute_metrics(sim.equity, sim.position, sim.closed_pnls, sim.n_trades, ppy)
    return BacktestOutput(
        sim.trades, sim.equity_df, metrics, _hash(request_json, sim.trades, sim.equity_df, metrics)
    )
