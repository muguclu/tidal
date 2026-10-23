"""Shared helpers for the T-302 suites: fixture loading and request building (not a test file)."""

import json
from pathlib import Path
from typing import Any

import polars as pl

from tidal.backtest.engine import Request
from tidal.contracts.models import BacktestRequest

FIXTURE_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "backtest"


def fixture_paths() -> list[Path]:
    """All golden fixture files, sorted for stable parametrisation."""
    return sorted(FIXTURE_DIR.glob("*.json"))


def load_fixture(path: Path) -> dict[str, Any]:
    """Parse one golden fixture."""
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data


def make_request(  # noqa: PLR0917
    strategy: str,
    params: dict[str, Any],
    ids: list[str],
    interval: str,
    t0: int,
    step: int,
    n: int,
    fee_bps: float = 0.0,
    slippage_bps: float = 0.0,
) -> Request:
    """Build a validated request whose half-open window covers ``n`` bars from ``t0``."""
    return BacktestRequest.model_validate(
        {
            "strategy": strategy,
            "instrument_ids": ids,
            "interval": interval,
            "start": t0,
            "end": t0 + n * step,
            "params": params,
            "costs": {"fee_bps": fee_bps, "slippage_bps": slippage_bps},
            "seed": 1,
        }
    ).root


def make_bars(t0: int, step: int, open_: list[float], close: list[float]) -> pl.DataFrame:
    """Bar frame with ``ts_ns`` spaced by ``step``."""
    return pl.DataFrame(
        {
            "ts_ns": [t0 + i * step for i in range(len(close))],
            "open": [float(x) for x in open_],
            "close": [float(x) for x in close],
        }
    )


def request_and_bars(fx: dict[str, Any]) -> tuple[Request, dict[str, pl.DataFrame]]:
    """Turn a fixture dict into ``(request, bars)`` for ``run_backtest``."""
    ids = list(fx["bars"])
    n = len(fx["bars"][ids[0]]["close"])
    req = make_request(
        fx["strategy"],
        fx["params"],
        ids,
        fx["interval"],
        fx["t0_ns"],
        fx["step_ns"],
        n,
        fx["costs"]["fee_bps"],
        fx["costs"]["slippage_bps"],
    )
    bars = {
        i: make_bars(fx["t0_ns"], fx["step_ns"], b["open"], b["close"])
        for i, b in fx["bars"].items()
    }
    return req, bars
