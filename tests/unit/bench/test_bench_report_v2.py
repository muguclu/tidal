"""T-204: results schema v2, request-based EUR, F0 raw/adjusted, repro, sweeps, report render."""

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq
import pytest
from benchmarks.storage import report
from benchmarks.storage.formats.sql_base import SqlTarget
from benchmarks.storage.results_schema import (
    ResultRow,
    bytes_read_class,
    read_results,
    write_results,
)
from benchmarks.storage.run import (
    format_ids_from_arg,
    run_benchmark,
    sweep_stale_containers,
    sweep_stale_schemas,
)
from benchmarks.storage.workloads import Dataset
from hypothesis import given
from hypothesis import strategies as st

from tidal.common.errors import ConfigError
from tidal.finops.fetch_prices import Price, PriceBook


def _price(key: str, unit_price: float) -> Price:
    return Price(
        key=key, unit="x", unitPrice=unit_price, meterId="m", productName="p", skuName="s",
        meterName="n", effectiveStartDate="2026-01-01",
    )  # fmt: skip


BOOK = PriceBook(
    captured_at="2026-10-22",  # type: ignore[arg-type]  # pydantic parses the ISO date
    source="test",
    region="westeurope",
    currency="EUR",
    prices=(
        _price("blob_hot_stored_gb_month", 0.02),
        _price("sql_gp_storage_gb_month", 0.12),
        _price("blob_hot_read_10k", 0.004),
        _price("blob_hot_write_10k", 0.05),
        _price("blob_hot_other_10k", 0.004),
        _price("egress_first_tier_gb", 0.08),
        _price("sql_gp_serverless_vcore_hour", 0.36),
    ),
)
GB = 1024**3


def _row(**kw: Any) -> ResultRow:
    base: dict[str, Any] = {
        "dataset_key": "k", "scale": "S", "seed": 1, "format_id": "F5", "workload_id": "W2",
        "cache": "warm", "runs": 5, "wall_p50_s": 0.5, "wall_p95_s": 0.7, "cpu_s": 0.4,
        "rows": 100, "rows_per_s": 200.0, "bytes_at_rest": GB, "bytes_read": 8 * 1024**2,
        "bytes_read_source": "duckdb_profile", "storage_price_key": "blob_hot_stored_gb_month",
        "target": "local",
    }  # fmt: skip
    return ResultRow(**{**base, **kw})


def _f0(**kw: Any) -> ResultRow:
    return _row(
        format_id="F0", target="sql_server", bytes_at_rest=10 * GB,
        bytes_at_rest_adjusted=9 * GB, bytes_billed=12 * GB, rows=None, rows_per_s=None,
        bytes_read_source="sql_statistics_io", storage_price_key="sql_gp_storage_gb_month",
        cold_mode="dropcleanbuffers", **kw,
    )  # fmt: skip


def test_schema_v2_round_trip_keeps_nulls_and_new_columns(tmp_path: Path) -> None:
    rows = [
        _f0(),
        _row(
            target="azurite", requests_get=7, requests_head=1, requests_list=2, requests_other=0,
            egress_bytes=123, row_groups_files=3, row_groups_total=9, row_groups_max=4,
            cold_mode="fresh_connection",
        ),
    ]  # fmt: skip
    write_results(tmp_path / "r.parquet", rows, {"duckdb": "x"})
    back, _ = read_results(tmp_path / "r.parquet")
    assert back == rows
    assert back[0].rows is None
    assert back[0].requests_get is None
    schema = pq.read_schema(tmp_path / "r.parquet")
    assert schema.metadata[b"tidal.bench.schema"] == b"2"


def test_bytes_read_classes_do_not_mix() -> None:
    assert bytes_read_class("duckdb_profile") == bytes_read_class("file_size")
    assert bytes_read_class("sql_statistics_io") == "logical_pages"
    assert bytes_read_class("azurite_http_response_bytes") == "http_bytes"
    assert len({bytes_read_class(s) for s in ("duckdb_profile", "sql_statistics_io")}) == 2


def test_eur_from_real_requests_hand_checked() -> None:
    r = _row(requests_get=10, requests_head=2, requests_list=4, requests_other=1)
    # reads (10+2) x 0.004 + list 4 x 0.05 + other 1 x 0.004 = 0.048 + 0.2 + 0.004 = 0.252
    # per 10k ops -> x 1000 queries / 10_000 = 0.0252
    assert report.eur_per_1k_from_requests(r, BOOK) == pytest.approx(0.0252)
    assert report.eur_per_1k_from_requests(_row(), BOOK) is None


def test_sql_compute_is_vcore_seconds() -> None:
    # 1 s x 0.36 EUR / 3600 s per vCore-hour x 1000 queries = 0.1
    assert report.eur_per_1k_sql_compute(1.0, BOOK) == pytest.approx(0.1)


def test_f0_is_reported_raw_and_adjusted_with_hand_checked_ratios() -> None:
    rows = [_f0(), _row(bytes_at_rest=GB // 2, workload_id="W2")]
    labels = [ln.label for ln in report.size_lines(rows, "S")]
    assert labels == ["F0 (raw)", "F0 (adjusted, no ts_sub)", "F5"]
    h = report.headline_ratios(rows, BOOK, "S")
    assert h["size_f0_raw_over_f5"] == pytest.approx(20.0)  # 10 GiB / 0.5 GiB
    assert h["size_f0_adj_over_f5"] == pytest.approx(18.0)  # 9 / 0.5
    # SQL billed 12 GiB x 0.12 = 1.44 ; adjusted (12 - 1) x 0.12 = 1.32 ; F5 0.5 x 0.02 = 0.01
    assert h["eur_f0_raw_over_f5"] == pytest.approx(144.0)
    assert h["eur_f0_adj_over_f5"] == pytest.approx(132.0)
    assert h["size_f4_over_f5"] is None  # not run: never fabricated


def test_missing_sides_render_as_na_not_zero() -> None:
    md = report.render_size_table([_f0()], BOOK, "S", 1000)
    assert "n/a" in md  # no F5 to compare with
    io = report.render_io_table([_f0()], "S")
    assert "n/a" in io  # SQL rows scanned is None


def test_compare_runs_identical_is_zero_and_flags_slow_drift() -> None:
    a = [_row(), _row(workload_id="W3", wall_p50_s=0.01)]
    same = report.compare_runs(a, a)
    assert same["max_size_delta"] == 0
    assert same["n_time_fail"] == 0
    b = [_row(wall_p50_s=0.6, bytes_at_rest=GB + 1), _row(workload_id="W3", wall_p50_s=0.02)]
    cmp = report.compare_runs(a, b)
    assert cmp["n"] == 2
    assert cmp["n_time_fail"] == 2
    assert cmp["n_big"] == 1
    assert cmp["n_big_time_fail"] == 1
    assert cmp["max_size_delta"] == pytest.approx(1 / GB)
    text = report.render_repro(cmp)
    assert "PASS" in text
    assert "Worst timing deltas" in text


@given(st.integers(0, 10**6), st.integers(0, 10**6), st.integers(0, 10**6))
def test_request_cost_is_monotonic(g: int, h: int, extra: int) -> None:
    lo = report.eur_per_1k_from_requests(_row(requests_get=g, requests_head=h), BOOK)
    hi = report.eur_per_1k_from_requests(_row(requests_get=g + extra, requests_head=h), BOOK)
    assert lo is not None
    assert hi is not None
    assert hi >= lo >= 0


def test_sweep_ids_expand_and_sort() -> None:
    ids = format_ids_from_arg("F0..F2,F6-sweep")
    assert ids[:3] == ["F0", "F1", "F2"]
    assert len(ids) == 12
    assert (
        report.format_sort_key("F5")
        < report.format_sort_key("F6")
        < report.format_sort_key("F6-z3-rg128k")
    )
    assert sorted(ids[3:], key=report.format_sort_key) == ids[3:]  # z3 rg128k..4m, z6, z9


# --- sweeps -----------------------------------------------------------------------------------
@dataclass
class _Container:
    name: str
    last_modified: datetime


class _FakeService:
    def __init__(
        self, containers: list[_Container], url: str = "http://127.0.0.1:10000/devstoreaccount1"
    ) -> None:
        self.containers = containers
        self.deleted: list[str] = []
        self.url = url
        self.account_name = url.rstrip("/").rsplit("/", 1)[-1]

    def list_containers(self, name_starts_with: str = "") -> list[_Container]:
        return [c for c in self.containers if c.name.startswith(name_starts_with)]

    def delete_container(self, name: str) -> None:
        self.deleted.append(name)


def test_container_sweep_keeps_young_and_foreign() -> None:
    now = datetime(2026, 10, 22, 12, tzinfo=UTC)
    old = now - timedelta(hours=3)
    svc = _FakeService([
        _Container("bench-f5-abcdef12", old),
        _Container("bench-f5-young", now - timedelta(minutes=5)),
        _Container("bench-UPPER", old),  # does not match the generated pattern
    ])  # fmt: skip
    gone = sweep_stale_containers(svc, min_age_min=60, now=now)  # type: ignore[arg-type]
    assert gone == ["bench-f5-abcdef12"] == svc.deleted


def test_container_sweep_refuses_a_real_account() -> None:
    svc = _FakeService([], url="https://tidaldev.blob.core.windows.net/")
    with pytest.raises(ConfigError, match="only runs against Azurite"):
        sweep_stale_containers(svc)  # type: ignore[arg-type]


class _Cur:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self._rows = rows

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._rows

    def fetchone(self) -> tuple[Any, ...]:
        return self._rows[0]


class _FakeCon:
    def __init__(self, now: datetime, schemas: list[tuple[str, datetime | None]]) -> None:
        self.now, self.schemas = now, schemas
        self.sql: list[str] = []

    def execute(self, sql: str, *args: Any) -> _Cur:
        self.sql.append(sql)
        if "FROM sys.schemas" in sql:
            return _Cur(list(self.schemas))
        if "SYSDATETIME" in sql:
            return _Cur([(self.now,)])
        if "FROM sys.tables" in sql:
            return _Cur([("tick_f0",)])
        return _Cur([])

    def close(self) -> None:
        self.sql.append("closed")


def test_schema_sweep_drops_only_stale_generated_names(monkeypatch: pytest.MonkeyPatch) -> None:
    now = datetime(2026, 10, 22, 12)  # noqa: DTZ001 - SQL Server SYSDATETIME() is naive
    con = _FakeCon(now, [
        ("bench_0123456789", now - timedelta(hours=2)),  # stale
        ("bench_abcdef0123", now - timedelta(minutes=3)),  # a live run
        ("bench_empty0000", None),  # name does not match the pattern: untouched
        ("bench_fedcba9876", None),  # empty, generated name: dropped
    ])  # fmt: skip
    target = SqlTarget("127.0.0.1", "db", "sa", "x")
    monkeypatch.setattr(SqlTarget, "connect", lambda self, **_: con)
    dropped = sweep_stale_schemas(target, min_age_min=60)
    assert dropped == ["bench_0123456789", "bench_fedcba9876"]
    assert any("DROP TABLE [bench_0123456789].[tick_f0]" in s for s in con.sql)
    assert not any("bench_abcdef0123]" in s and "DROP" in s for s in con.sql)
    assert con.sql[-1] == "closed"


# --- runner end to end on files ----------------------------------------------------------------
def test_runner_records_v2_columns_for_files(tiny_dataset: Dataset, tmp_path: Path) -> None:
    rows = run_benchmark(
        tiny_dataset.spec, ["F5", "F4"], ["W1", "W3"], 2, tmp_path,
        data_root=tiny_dataset.root.parent, w1_runs=1,
    )  # fmt: skip
    assert {r.target for r in rows} == {"local"}
    w1 = next(r for r in rows if r.format_id == "F5" and r.workload_id == "W1")
    assert w1.runs == 1
    assert w1.cold_mode == "n/a"
    cold = next(r for r in rows if r.format_id == "F5" and r.cache == "cold")
    warm = next(r for r in rows if r.format_id == "F5" and r.cache == "warm")
    assert cold.cold_mode == "fresh_connection"
    assert warm.cold_mode == "n/a"
    assert cold.bytes_billed == cold.bytes_at_rest
    assert cold.bytes_at_rest_adjusted is None
    assert cold.requests_get is None
    assert cold.row_groups_files
    assert cold.row_groups_total
    assert cold.row_groups_max
    back, _ = read_results(tmp_path / "results.parquet")
    assert back == rows
    assert json.loads((tmp_path / "summary.json").read_text())["results"]


# --- the report on synthetic rows --------------------------------------------------------------
def _full_rows() -> list[ResultRow]:
    rows = [_f0(workload_id=w) for w in ("W2", "W3")]
    rows += [_f0(workload_id="W1", cache="n/a")]
    for fid, size in (("F4", GB), ("F5", GB // 4)):
        for w in ("W2", "W3"):
            for cache in ("cold", "warm"):
                rows.append(
                    _row(
                        format_id=fid, workload_id=w, cache=cache, bytes_at_rest=size,
                        row_groups_files=2, row_groups_total=3, row_groups_max=2,
                    )
                )  # fmt: skip
    rows.append(_row(target="azurite", workload_id="W3", requests_get=9, requests_head=1,
                     requests_list=2, requests_other=0, egress_bytes=5))  # fmt: skip
    rows += [_row(format_id="F6-z3-rg128k", bytes_at_rest=GB // 5),
             _row(format_id="F6-z9-rg1m", bytes_at_rest=GB // 6)]  # fmt: skip
    return rows


def test_report_renders_every_section_and_labels_estimates(tmp_path: Path) -> None:
    rs = report.RunSet(rows=_full_rows(), env={"python": "3.12"}, ds_rows={"S": 1_000_000})
    rs.not_run = ["SQL at M: NOT RUN (disk)"]
    md = report.render_report(
        rs, BOOK, date_str="2026-10-22", repro=report.compare_runs(rs.rows, rs.rows)
    )
    headings = (
        "## Executive summary", "## Methodology", "## Results, scale S", "## Why F5 wins",
        "## Reproducibility", "## Not run", "## Caveats", "NOT RUN (disk)",
    )  # fmt: skip
    for heading in headings:
        assert heading in md
    assert md.count("\n* **") == 3  # exactly three executive bullets
    assert "F0 (adjusted, no ts_sub)" in md
    assert "F0 (raw)" in md
    assert "MODELLED" in md
    assert "MEASURED" in md
    assert "logical_pages" in md
    assert "storage_bytes" in md
    assert "excluded (C5)" in md
    paths = report.render_scale_charts(rs.rows, BOOK, tmp_path, "S")
    names = {p.name for p in paths}
    want = {"size_eur_S.png", "p50_S.png", "bytes_read_S.png", "f6_sweep_S.png", "requests_S.png"}
    assert want <= names
    assert all(p.stat().st_size > 0 for p in paths)


def test_load_runs_rejects_duplicates_and_reads_dataset_rows(tmp_path: Path) -> None:
    d1, d2 = tmp_path / "a", tmp_path / "b"
    for d in (d1, d2):
        d.mkdir()
        write_results(d / "results.parquet", [_row()], {})
    (d1 / "summary.json").write_text(json.dumps({"dataset": {"rows": {"i": 5, "j": 7}}}))
    with pytest.raises(ValueError, match="duplicate"):
        report.load_runs([d1, d2])
    assert report.load_runs([d1]).ds_rows == {"S": 12}


def test_encoding_ablation_shows_delta_beats_plain(tiny_dataset: Dataset, tmp_path: Path) -> None:
    f = next(iter(tiny_dataset.base.root.rglob("*.parquet")))
    res = report.encoding_ablation(pq.read_table(f), tmp_path)
    by = {r["variant"]: r for r in res}
    plain = by["ts_ns without DELTA_BINARY_PACKED (PLAIN)"]
    assert by["F5 as written"]["ts_ns_bytes"] < plain["ts_ns_bytes"]
    assert by["F5 as written"]["file_bytes"] <= by["neither"]["file_bytes"]
    assert "ts_ns B/tick" in report.render_ablation(res)


def _key(r: ResultRow) -> tuple[str, str, str]:
    return (r.format_id, r.workload_id, r.cache)


def test_parallel_load_matches_sequential_and_flushes(
    tiny_dataset: Dataset, tmp_path: Path
) -> None:
    root = tiny_dataset.root.parent
    seq = run_benchmark(
        tiny_dataset.spec, ["F5", "F4"], ["W1", "W3"], 1, tmp_path / "s", data_root=root, w1_runs=1
    )
    par = run_benchmark(
        tiny_dataset.spec,
        ["F5", "F4"],
        ["W1", "W3"],
        1,
        tmp_path / "p",
        parallel_load=True,
        data_root=root,
        w1_runs=1,
    )
    assert sorted(map(_key, par)) == sorted(map(_key, seq))
    by_seq = {_key(r): r for r in seq}
    for r in par:  # sizes are deterministic, timings are not
        assert r.bytes_at_rest == by_seq[_key(r)].bytes_at_rest
        assert r.bytes_read == by_seq[_key(r)].bytes_read
    back, _ = read_results(tmp_path / "p" / "results.parquet")
    assert len(back) == len(par)


def test_bare_f6_is_the_grid_point_and_never_runs_twice() -> None:
    ids = format_ids_from_arg("F5,F6,F6-sweep")
    assert ids.count("F6-z9-rg1m") == 1
    assert "F6" not in ids
    assert len(ids) == 10


def test_same_run_duplicates_become_replicates(tmp_path: Path) -> None:
    write_results(
        tmp_path / "results.parquet",
        [_row(wall_p50_s=0.5), _row(wall_p50_s=0.55), _row(wall_p50_s=0.5, workload_id="W3")],
        {},
    )
    rs = report.load_runs([tmp_path])
    assert len(rs.rows) == 2
    assert [r.wall_p50_s for r in rs.replicates] == [0.55]
    text = report.render_replicates(rs)
    assert "10.0 %" in text
    assert "1 cells" in text
