"""Results schema round trip and the MODELLED EUR computation against hand-checked inputs."""

from pathlib import Path
from typing import Any

import pytest
from benchmarks.storage import report
from benchmarks.storage.results_schema import ResultRow, read_results, write_results

from tidal.finops.fetch_prices import PrecisionLimitedPrice, Price, PriceBook


def _price(key: str, unit_price: float, **kw: Any) -> Price:
    return Price(
        key=key, unit="x", unitPrice=unit_price, meterId="m", productName="p", skuName="s",
        meterName="n", effectiveStartDate="2026-01-01", **kw,
    )  # fmt: skip


BOOK = PriceBook(
    captured_at="2026-10-22",  # type: ignore[arg-type]  # pydantic parses the ISO date
    source="test",
    region="westeurope",
    currency="EUR",
    prices=(
        _price("blob_hot_stored_gb_month", 0.02),
        _price("blob_hot_read_10k", 0.004),
        _price("egress_first_tier_gb", 0.08),
        _price("tiny", 0.0, precision_limited=True),
    ),
)


def _row(**kw: Any) -> ResultRow:
    base: dict[str, Any] = {
        "dataset_key": "k", "scale": "S", "seed": 1, "format_id": "F5", "workload_id": "W2",
        "cache": "warm", "runs": 5, "wall_p50_s": 0.5, "wall_p95_s": 0.7, "cpu_s": 0.4,
        "rows": 100, "rows_per_s": 200.0, "bytes_at_rest": 2 * 1024**3,
        "bytes_read": 8 * 1024**2, "bytes_read_source": "duckdb_profile",
        "storage_price_key": "blob_hot_stored_gb_month",
    }  # fmt: skip
    return ResultRow(**{**base, **kw})


def test_results_round_trip(tmp_path: Path) -> None:
    rows = [
        _row(),
        _row(workload_id="W4", bytes_read=None, rows_per_s=None, bytes_read_source="n/a"),
    ]
    write_results(tmp_path / "r.parquet", rows, {"duckdb": "2.0.0"})
    back, env = read_results(tmp_path / "r.parquet")
    assert back == rows
    assert env == {"duckdb": "2.0.0"}


def test_eur_at_rest_hand_checked() -> None:
    # 2 GiB x 0.02 EUR/GiB-month = 0.04
    got = report.eur_per_month_at_rest(2 * 1024**3, BOOK, "blob_hot_stored_gb_month")
    assert got == pytest.approx(0.04)


def test_eur_per_1k_queries_hand_checked() -> None:
    # 8 MiB -> 2 read ops: 2/10000 x 0.004 = 8e-7 ; egress 8/1024 GiB x 0.08 = 6.25e-4
    # default (in-region, egress 0): 1000 x 8e-7 = 0.0008 ; with internet egress 0.6258
    assert report.eur_per_1k_queries(8 * 1024**2, BOOK) == pytest.approx(0.0008)
    got = report.eur_per_1k_queries(8 * 1024**2, BOOK, internet_egress=True)
    assert got == pytest.approx(0.6258)
    # 1 byte still costs one read operation: 1/10000 x 0.004 x 1000 = 0.0004
    assert report.eur_per_1k_queries(1, BOOK) == pytest.approx(0.0004)
    assert report.eur_per_1k_queries(None, BOOK) is None


def test_precision_limited_price_is_refused() -> None:
    with pytest.raises(PrecisionLimitedPrice):
        BOOK.unit_price("tiny")


def test_markdown_labels_every_eur_cell_modelled(tmp_path: Path) -> None:
    rows = [_row(), _row(workload_id="W1", cache="n/a")]
    md = report.render_markdown(rows, BOOK, {"python": "3.12"})
    body = [ln for ln in md.splitlines() if ln.startswith("| F5")]
    assert len(body) == 2
    assert all(ln.count("(MODELLED)") >= 1 for ln in body)
    assert "0.040000 (MODELLED)" in body[0] or "0.040000 (MODELLED)" in body[1]
    assert "[MODELLED] EUR/1k queries =" in md
    assert "lower bound" in md
    assert "page cache is NOT dropped" in md
    assert "client-process CPU only" in md
    assert "(C5)" in md
    assert "internet egress" not in md.split("Environment")[0].split("\n", 1)[0]
    md2 = report.render_markdown(rows, BOOK, {}, internet_egress=True)
    assert "incl. internet egress" in md2
    assert "python=3.12" in md
    paths = report.render_charts(rows, tmp_path)
    assert all(p.exists() and p.stat().st_size > 0 for p in paths)
