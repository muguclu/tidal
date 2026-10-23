"""Post-deploy smoke checks (T-311). Selected with ``-m smoke``; skip without TIDAL_SMOKE_*_URL."""

import os

import httpx
import pytest

pytestmark = pytest.mark.smoke

TIMEOUT = httpx.Timeout(10.0)


def test_api_live(api_url: str) -> None:
    """GET /v1/health/live answers 200 with status ok."""
    r = httpx.get(f"{api_url}/v1/health/live", timeout=TIMEOUT)
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_api_ready(api_url: str) -> None:
    """GET /v1/health/ready answers 200 (dependencies reachable). The body is not inspected."""
    r = httpx.get(f"{api_url}/v1/health/ready", timeout=TIMEOUT)
    assert r.status_code == 200


def test_query_live(query_url: str) -> None:
    """tidal-query /health/live answers 200 (no auth on this route)."""
    r = httpx.get(f"{query_url}/health/live", timeout=TIMEOUT)
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_backtest_submit_accepted(api_url: str) -> None:
    """One tiny backtest submit returns 202.

    Skipped until Entra auth exists (T-402) and a smoke token is provided in TIDAL_SMOKE_TOKEN.
    The body comes from TIDAL_SMOKE_BACKTEST_JSON (a contract-valid BacktestRequest) so no strategy
    schema is duplicated here.
    """
    token = os.environ.get("TIDAL_SMOKE_TOKEN", "")
    body = os.environ.get("TIDAL_SMOKE_BACKTEST_JSON", "")
    if not token or not body:
        pytest.skip("backtest submit needs TIDAL_SMOKE_TOKEN and TIDAL_SMOKE_BACKTEST_JSON (T-402)")
    r = httpx.post(
        f"{api_url}/v1/backtests",
        content=body,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        timeout=TIMEOUT,
    )
    assert r.status_code == 202
