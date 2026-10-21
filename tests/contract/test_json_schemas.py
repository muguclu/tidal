"""Committed docs/contracts/schemas/*.json == a fresh ``python -m tidal.contracts.export``."""

import json
from pathlib import Path

import pytest

from tidal.contracts import export
from tidal.contracts.models import Strategy

SCHEMA_DIR = Path(__file__).resolve().parents[2] / "docs" / "contracts" / "schemas"


def test_committed_schemas_equal_fresh_export() -> None:
    rendered = export.render()
    committed = {p.name: p.read_bytes() for p in SCHEMA_DIR.glob("*.json")}
    assert sorted(committed) == sorted(rendered), "run: uv run python -m tidal.contracts.export"
    for name, text in rendered.items():
        assert committed[name] == text.encode("utf-8"), f"{name} is stale; re-run the export"


def test_export_is_deterministic_sorted_and_lf(tmp_path: Path) -> None:
    export.main([str(tmp_path / "a")])
    export.write(tmp_path / "b")
    for path in (tmp_path / "a").iterdir():
        raw = path.read_bytes()
        assert raw == (tmp_path / "b" / path.name).read_bytes()
        assert b"\r\n" not in raw
        assert raw.endswith(b"}\n")
        text = raw.decode("utf-8")
        assert (
            json.dumps(json.loads(text), indent=2, sort_keys=True, ensure_ascii=False) + "\n"
            == text
        )


def test_api_components_present() -> None:
    defs = json.loads(export.render()["api.v1.json"])["$defs"]
    expected = {
        "Problem", "Instrument", "InstrumentList", "Bar", "BarsResponse", "Tick", "TicksResponse",
        "LatestBar", "LatestResponse", "SpreadPoint", "SpreadResponse", "BacktestRequest",
        "BacktestCosts", "BacktestAccepted", "BacktestStatus", "BacktestChildren",
        "BacktestMetrics", "BacktestResult", "BacktestList", "TieringRunAccepted", "StorageStats",
    }  # fmt: skip
    assert expected <= set(defs)
    assert defs["BacktestRequest"]["discriminator"]["propertyName"] == "strategy"
    mapping = defs["BacktestRequest"]["discriminator"]["mapping"]
    assert sorted(mapping) == sorted(s.value for s in Strategy)


def test_message_schemas_use_schema_alias() -> None:
    for name in ("backtest.job.v1.json", "backtest.result.v1.json", "tiering.job.v1.json"):
        schema = json.loads(export.render()[name])
        assert "schema" in schema["required"]
        assert "schema_" not in schema["properties"]


def test_main_defaults_to_docs_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.argv", ["export"])
    export.main()
    written = sorted(p.name for p in (tmp_path / export.DEFAULT_OUT_DIR).iterdir())
    assert written == sorted(export.render())
