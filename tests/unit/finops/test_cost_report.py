"""Tests for the weekly cost report (T-502); fixtures in tests/fixtures/finops."""

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from tidal.finops import cost_report as cr

REPO = Path(__file__).resolve().parents[3]
FIX = REPO / "tests" / "fixtures" / "finops"
A = FIX / "actual-mtd-2026-10-22.csv"
B = FIX / "actual-mtd-2026-10-24.csv"
CLASSIC = FIX / "classic-payg.csv"
PRICES = REPO / "docs" / "finops" / "prices.yaml"
MODEL = REPO / "docs" / "finops" / "cost-model.md"


def _gen(tmp_path: Path, files: list[Path], week: str | None = "2026-W43") -> Path:
    return cr.generate(files, out_dir=tmp_path, prices=PRICES, cost_model=MODEL, week=week)


def test_parse_modern_schema() -> None:
    rows = cr.parse_export(B)
    assert rows[0].day == date(2026, 10, 20)
    assert rows[0].component == "registry"
    assert rows[0].park == "false"
    assert rows[0].cost == Decimal("0.146600")


def test_parse_classic_schema_and_brace_less_tags() -> None:
    rows = cr.parse_export(CLASSIC)
    assert [r.day for r in rows] == [date(2026, 10, 19), date(2026, 10, 20)]
    assert [r.component for r in rows] == ["registry", "registry"]
    assert rows[0].cost == Decimal("0.1466")


def test_missing_columns_raises(tmp_path: Path) -> None:
    bad = tmp_path / "bad.csv"
    bad.write_text("Foo,Bar\n1,2\n", encoding="utf-8")
    with pytest.raises(cr.CostReportError, match="missing required columns"):
        cr.parse_export(bad)


def test_last_file_wins_per_day() -> None:
    merged = cr.merge_exports([A, B])
    oct22 = [r for r in merged if r.day == date(2026, 10, 22)]
    from_b = [r for r in cr.parse_export(B) if r.day == date(2026, 10, 22)]
    assert sum(r.cost for r in oct22) == sum(r.cost for r in from_b)
    assert len(oct22) == len(from_b)
    assert any(r.day == date(2026, 10, 16) for r in merged)  # days only in A are kept


def test_week_labels() -> None:
    assert cr.iso_week_label(date(2026, 10, 24)) == "2026-W43"
    assert cr.parse_week("2026-W43") == (date(2026, 10, 19), date(2026, 10, 25))
    with pytest.raises(cr.CostReportError):
        cr.parse_week("2026-43")


def test_report_is_byte_identical(tmp_path: Path) -> None:
    one = _gen(tmp_path / "a", [A, B]).read_bytes()
    two = _gen(tmp_path / "b", [A, B]).read_bytes()
    assert one == two
    assert b"\r" not in one


def test_report_sections_and_name(tmp_path: Path) -> None:
    target = _gen(tmp_path, [A, B], week=None)
    assert target.name == "cost-weekly-2026-W43.md"
    text = target.read_text(encoding="utf-8")
    for heading in (
        "## 1. Totals",
        "## 2. Cost by day (week)",
        "## 3. Cost by service / meter category (week)",
        "## 4. Cost by `component` tag (week)",
        "## 5. Comparison with `docs/finops/cost-model.md`",
        "## 6. Evidence columns for C3 to C9 (week)",
        "## 7. C10 parking",
        "## 8. Billed vs list price x quantity",
        "## 9. What this export cannot show",
    ):
        assert heading in text
    assert "MEASURED" in text
    assert "MODELLED" in text
    assert "(untagged)" in text  # the Bandwidth row has no tags
    assert "NEEDS-CLOUD" in text


def test_totals_match_independent_sum(tmp_path: Path) -> None:
    rows = cr.merge_exports([A, B])
    week = [r for r in rows if date(2026, 10, 19) <= r.day <= date(2026, 10, 25)]
    expected = cr._money(sum((r.cost for r in week), Decimal(0)))
    text = _gen(tmp_path, [A, B]).read_text(encoding="utf-8")
    assert f"| Week 2026-W43 (6 days) | {expected} | MEASURED |" in text


def test_parked_days_cost_less_on_parkable_meters() -> None:
    rows = cr.merge_exports([A, B])
    by_day: dict[date, Decimal] = {}
    for r in rows:
        if r.park == "true" and r.category != "Azure App Service":
            by_day[r.day] = by_day.get(r.day, Decimal(0)) + r.cost
    assert by_day[date(2026, 10, 22)] < by_day[date(2026, 10, 21)]
    assert by_day[date(2026, 10, 23)] < by_day[date(2026, 10, 24)]


def test_reconciliation_uses_meter_id_and_skips_tiered(tmp_path: Path) -> None:
    text = _gen(tmp_path, [A, B]).read_text(encoding="utf-8")
    assert "| acr_basic_registry_day |" in text
    assert "loganalytics_ingestion_gb" not in text  # tiered: never modelled
    assert "containerapps_vcpu_active_second" not in text  # precision-limited: never modelled


def test_parse_cost_model_sums_components() -> None:
    est = cr.parse_cost_model(MODEL.read_text(encoding="utf-8"))
    assert est["registry"].parked == Decimal("4.40")
    assert est["compute"].active_high > est["compute"].active_low
    assert "AKS system pool" in est["compute"].rows


def test_non_euro_export_skips_comparison(tmp_path: Path) -> None:
    usd = tmp_path / "usd.csv"
    usd.write_text(CLASSIC.read_text(encoding="utf-8").replace(",EUR,", ",USD,"), encoding="utf-8")
    text = _gen(tmp_path / "out", [usd], week="2026-W43").read_text(encoding="utf-8")
    assert "WARNING: export currency is USD" in text
    assert "Skipped: currency mismatch." in text


def test_empty_week_raises(tmp_path: Path) -> None:
    with pytest.raises(cr.CostReportError, match="no export rows fall in"):
        _gen(tmp_path, [B], week="2026-W10")


def test_cli_returns_error_code(tmp_path: Path) -> None:
    assert (
        cr.main(["--export", str(tmp_path / "missing-dir-with-no-csv"), "--out-dir", str(tmp_path)])
        == 2
    )


def test_directory_runs_are_snapshots_and_partitions_are_kept(tmp_path: Path) -> None:
    rows = cr.parse_export(B)
    header = B.read_text(encoding="utf-8-sig").splitlines()[0]
    lines = B.read_text(encoding="utf-8-sig").splitlines()[1:]
    run = tmp_path / "exp" / "20261001-20261031" / "run1"
    run.mkdir(parents=True)
    half = len(lines) // 2
    (run / "part_1_0001.csv").write_text(
        "\n".join([header, *lines[:half]]) + "\n", encoding="utf-8"
    )
    (run / "part_1_0002.csv").write_text(
        "\n".join([header, *lines[half:]]) + "\n", encoding="utf-8"
    )
    snaps = cr.expand_snapshots([tmp_path])
    assert len(snaps) == 1
    assert len(snaps[0]) == 2
    assert len(cr.merge_snapshots(snaps)) == len(rows)
