"""Unit tests for the legacy VM exporter (T-310). Pure logic only; no Azure, no network."""

from __future__ import annotations

import importlib.util
import json
import sys
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType

import pytest

_SCRIPT = Path(__file__).resolve().parents[3] / "legacy" / "exporter" / "export_daily_bars.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("export_daily_bars", _SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


exp = _load()

NOW = datetime(2026, 1, 3, 21, 55, tzinfo=UTC)
DAY_NS = 86_400 * 1_000_000_000


def _ns(y: int, m: int, d: int) -> int:
    return int(datetime(y, m, d, tzinfo=UTC).timestamp()) * 1_000_000_000


def _env(**over: str) -> dict[str, str]:
    base = {
        "TIDAL_ENV": "dev",
        "TIDAL_STORAGE__ACCOUNT_URL": "https://sttidaldev.blob.core.windows.net/",
    }
    base.update(over)
    return base


def test_config_requires_explicit_env() -> None:
    env = _env()
    del env["TIDAL_ENV"]
    with pytest.raises(exp.ConfigError, match="TIDAL_ENV"):
        exp.Config.from_env(env)
    with pytest.raises(exp.ConfigError, match="TIDAL_ENV"):
        exp.Config.from_env(_env(TIDAL_ENV="local"))


def test_config_defaults_and_validation() -> None:
    cfg = exp.Config.from_env(_env(AZURE_CLIENT_ID="abc"))
    assert cfg.env == "dev"
    assert cfg.account_url == "https://sttidaldev.blob.core.windows.net"
    assert cfg.client_id == "abc"
    assert cfg.lookback_days == 7
    assert cfg.deallocate is True
    assert exp.Config.from_env(_env(EXPORT_DEALLOCATE="false")).deallocate is False
    for bad in ("0", "400", "x"):
        with pytest.raises(exp.ConfigError):
            exp.Config.from_env(_env(EXPORT_LOOKBACK_DAYS=bad))
    with pytest.raises(exp.ConfigError):
        exp.Config.from_env(_env(TIDAL_STORAGE__ACCOUNT_URL="http://x"))


def test_window_start_is_utc_midnight() -> None:
    assert exp.window_start_ns(NOW, 7) == _ns(2025, 12, 27)
    with pytest.raises(ValueError, match="timezone"):
        exp.window_start_ns(datetime(2026, 1, 1, tzinfo=None), 7)  # noqa: DTZ001 - naive on purpose


def test_select_parts_only_daily_parts_in_window_years() -> None:
    base = "v1/bars_1d/instrument=EIA.BRENT.SPOT"
    names = [
        f"{base}/year=2024/month=00/part-00000.parquet",
        f"{base}/year=2025/month=00/part-00000.parquet",
        f"{base}/year=2026/month=00/part-00001.parquet",
        f"{base}/year=2026/month=00/_manifest.json",
        "v1/ticks/instrument=SYN.A/year=2026/month=01/part-00000.parquet",
        f"{base}/year=2026/month=01/part-00000.parquet",
    ]
    assert exp.select_parts(names, NOW, 7) == [
        f"{base}/year=2025/month=00/part-00000.parquet",
        f"{base}/year=2026/month=00/part-00001.parquet",
    ]


def test_latest_versions_keeps_restatement_and_window() -> None:
    old, a, b = _ns(2025, 12, 1), _ns(2025, 12, 30), _ns(2026, 1, 2)
    rows = [
        {"ts_ns": old, "instrument_id": "X", "close": 1.0, "version": 1},
        {"ts_ns": a, "instrument_id": "X", "close": 10.0, "version": 1},
        {"ts_ns": a, "instrument_id": "X", "close": 11.0, "version": 2},
        {"ts_ns": b, "instrument_id": "A", "close": 5.0, "version": 1},
    ]
    out = exp.latest_versions(rows, exp.window_start_ns(NOW, 7))
    assert [(r["instrument_id"], r["close"]) for r in out] == [("A", 5.0), ("X", 11.0)]


def test_csv_keeps_negative_prices_and_blanks_nan() -> None:
    rows = [
        {
            "ts_ns": _ns(2020, 4, 20),
            "instrument_id": "EIA.WTI.SPOT",
            "open": -36.98,
            "high": -36.98,
            "low": -36.98,
            "close": -36.98,
            "volume": float("nan"),
        }
    ]
    assert exp.to_csv(rows) == (
        "date,instrument_id,open,high,low,close,volume\n2020-04-20,EIA.WTI.SPOT,-36.98,-36.98,-36.98,-36.98,\n"
    )


def test_csv_is_deterministic() -> None:
    rows = [
        {
            "ts_ns": _ns(2026, 1, 2) + 5 * DAY_NS // 24,
            "instrument_id": "A",
            "open": 1,
            "high": 2,
            "low": 0.5,
            "close": 1.5,
        }
    ]
    assert exp.to_csv(rows) == exp.to_csv(list(rows))
    assert "2026-01-02,A,1.0,2.0,0.5,1.5," in exp.to_csv(rows)


def test_export_blob_name() -> None:
    assert exp.export_blob_name(NOW) == "risk-feed/daily_bars_2026-01-03.csv"


def test_deallocate_self_calls_arm_with_imds_token() -> None:
    seen: list[urllib.request.Request] = []

    def fetch(req: urllib.request.Request) -> tuple[int, bytes]:
        seen.append(req)
        if "identity/oauth2/token" in req.full_url:
            assert "client_id=cid" in req.full_url
            assert req.get_header("Metadata") == "true"
            return 200, json.dumps({"access_token": "tok"}).encode()
        if "instance/compute" in req.full_url:
            return 200, json.dumps(
                {
                    "subscriptionId": "sub",
                    "resourceGroupName": "rg-tidal-dev",
                    "name": "vm-tidal-legacy-dev",
                }
            ).encode()
        return 202, b""

    exp.deallocate_self("cid", fetch)
    last = seen[-1]
    assert last.get_method() == "POST"
    assert last.full_url == (
        "https://management.azure.com/subscriptions/sub/resourceGroups/rg-tidal-dev/providers/"
        "Microsoft.Compute/virtualMachines/vm-tidal-legacy-dev/deallocate?api-version=2024-07-01"
    )
    assert last.get_header("Authorization") == "Bearer tok"


def test_deallocate_failure_raises() -> None:
    def fetch(req: urllib.request.Request) -> tuple[int, bytes]:
        if "oauth2/token" in req.full_url:
            return 200, b'{"access_token": "t"}'
        if "instance/compute" in req.full_url:
            return 200, b'{"subscriptionId": "s", "resourceGroupName": "r", "name": "n"}'
        return 403, b""

    with pytest.raises(RuntimeError, match="403"):
        exp.deallocate_self(None, fetch)


def test_main_deallocates_even_when_export_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str | None] = []
    monkeypatch.setattr(
        exp, "deallocate_self", lambda client_id, fetch=None: calls.append(client_id)
    )
    # No azure packages are needed: a missing or failing import is caught like any export failure.
    monkeypatch.setattr(
        exp, "run_export", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    assert exp.main(_env(AZURE_CLIENT_ID="cid")) == 1
    assert calls == ["cid"]
