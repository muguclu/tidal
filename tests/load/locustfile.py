# ruff: noqa: E501  # long prose lines in the usage docstring and comments
"""Locust load test for the Tidal API read endpoints and backtest submit (T-504).

Never collected by the unit suite: ``pytest`` only collects ``test_*.py`` and this file is a locustfile
(``tests/load`` is also excluded by ``-m "not integration"`` runs because nothing here is a test).

Run (see tests/load/README.md)::

    uv run --group load locust -f tests/load/locustfile.py --headless -u 5 -r 1 -t 90s \
        --host http://127.0.0.1:8000 --csv docs/finops/results/load/local-run

Environment:
    TIDAL_LOAD_TOKEN            bearer token; omitted = anonymous (works when TIDAL_AUTH__DISABLED=true)
    TIDAL_LOAD_BACKTEST_WEIGHT  weight of POST /v1/backtests (default 1; 0 disables, e.g. no Service Bus)
    TIDAL_LOAD_PACING_S         seconds between requests per user (default 1.3 = under the 60/min limit)
    TIDAL_LOAD_PROFILE          "read" (default mix) or "cold" (bars/spreads over multi-year ranges)

Requests are deterministic (fixed ranges and seed, no randomness): the same run issues the same URLs.
"""

import os
from typing import Any

from locust import HttpUser, constant_pacing, task

TOKEN = os.environ.get("TIDAL_LOAD_TOKEN", "")
BACKTEST_WEIGHT = int(os.environ.get("TIDAL_LOAD_BACKTEST_WEIGHT", "1"))
PACING_S = float(os.environ.get("TIDAL_LOAD_PACING_S", "1.3"))
PROFILE = os.environ.get("TIDAL_LOAD_PROFILE", "read")

BARS_START, BARS_END = "2025-01-01T00:00:00Z", "2026-10-22T00:00:00Z"
COLD_START = "2005-01-01T00:00:00Z"
TICKS_START, TICKS_END = "2025-01-01T00:00:00Z", "2025-01-03T00:00:00Z"

BACKTEST_BODY: dict[str, Any] = {
    "strategy": "ma_crossover",
    "instrument_ids": ["EIA.BRENT.SPOT"],
    "interval": "1d",
    "start": 1577836800000000000,  # 2020-01-01T00:00:00Z
    "end": 1735689600000000000,  # 2025-01-01T00:00:00Z
    "costs": {"fee_bps": 1.0, "slippage_bps": 1.0},
    "seed": 42,
    "params": {"fast": 10, "slow": 50},
}


class TidalUser(HttpUser):
    """One simulated quant/trader session against the public API."""

    # The API enforces 60 requests/minute per principal (SPEC-04 section 3, in-memory token bucket). Anonymous
    # local runs share one principal, so the default pacing keeps ONE user at ~46 req/min; more users on one
    # principal measure the limiter (429), not the endpoints. Use one token per user in the cloud.
    wait_time = constant_pacing(PACING_S)

    def on_start(self) -> None:
        """Attach the bearer token when one is provided."""
        if TOKEN:
            self.client.headers["Authorization"] = f"Bearer {TOKEN}"

    @task(5)
    def bars(self) -> None:
        """Daily bars over the hot/cold boundary."""
        start = COLD_START if PROFILE == "cold" else BARS_START
        self.client.get(
            "/v1/bars",
            params={
                "instrument_id": "EIA.BRENT.SPOT",
                "interval": "1d",
                "start": start,
                "end": BARS_END,
                "limit": 500,
            },
            name="GET /v1/bars",
        )

    @task(2)
    def ticks(self) -> None:
        """Two days of synthetic ticks, first JSON page."""
        self.client.get(
            "/v1/ticks",
            params={
                "instrument_id": "SYN.WTI.TICK",
                "start": TICKS_START,
                "end": TICKS_END,
                "limit": 200,
            },
            name="GET /v1/ticks",
        )

    @task(5)
    def latest(self) -> None:
        """Latest bar for two instruments."""
        self.client.get(
            "/v1/latest",
            params=[("instrument_id", "EIA.BRENT.SPOT"), ("instrument_id", "EIA.WTI.SPOT")],
            name="GET /v1/latest",
        )

    @task(2)
    def spreads(self) -> None:
        """Brent minus WTI spot spread."""
        start = COLD_START if PROFILE == "cold" else BARS_START
        self.client.get(
            "/v1/spreads",
            params={
                "leg_a": "EIA.BRENT.SPOT",
                "leg_b": "EIA.WTI.SPOT",
                "interval": "1d",
                "start": start,
                "end": BARS_END,
            },
            name="GET /v1/spreads",
        )

    @task(BACKTEST_WEIGHT)
    def submit_backtest(self) -> None:
        """Submit a single-run backtest; expects 202."""
        if BACKTEST_WEIGHT <= 0:
            return
        self.client.post("/v1/backtests", json=BACKTEST_BODY, name="POST /v1/backtests")
