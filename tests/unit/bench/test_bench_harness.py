"""Registry, dataset determinism, W2 oracle, smoke run (T-201)."""

import os
import time
from datetime import date
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest
from benchmarks.storage.run import F5Format, OracleMismatchError, run_benchmark
from benchmarks.storage.workloads import (
    WORKLOADS,
    Dataset,
    DatasetSpec,
    WorkloadResult,
    build_dataset,
    default_range,
    parse_ids,
    values_match,
)

from tidal.contracts.schemas import TICK_V1
from tidal.ingest.sources import synthetic
from tidal.storage.parquet_writer import LocalStore, write_partition


def test_registry_has_w1_to_w6_with_definitions() -> None:
    assert list(WORKLOADS) == [f"W{i}" for i in range(1, 7)]
    assert all(w.definition and w.title for w in WORKLOADS.values())


def test_parse_ids() -> None:
    assert parse_ids("W1..W3,W6", "W") == ["W1", "W2", "W3", "W6"]
    assert parse_ids("F5", "F") == ["F5"]


def test_cache_key_depends_on_every_parameter() -> None:
    base = DatasetSpec()
    keys = {
        base.key(),
        DatasetSpec(seed=1).key(),
        DatasetSpec(scale="M").key(),
        DatasetSpec(n_instruments=1).key(),
        DatasetSpec(restate_rate=0.02).key(),
    }
    assert len(keys) == 5
    assert base.key() == DatasetSpec().key()


def test_dataset_is_deterministic_and_cached(tiny_dataset: Dataset, tmp_path: Path) -> None:
    again = build_dataset(tiny_dataset.spec, tmp_path)  # fresh root -> regenerated
    assert again.content_hash == tiny_dataset.content_hash
    assert again.rows == tiny_dataset.rows
    cached = build_dataset(
        tiny_dataset.spec, tiny_dataset.root.parent
    )  # same root -> read from cache
    assert cached.content_hash == tiny_dataset.content_hash
    other = build_dataset(DatasetSpec(**{**tiny_dataset.spec.__dict__, "seed": 8}), tmp_path)
    assert other.content_hash != tiny_dataset.content_hash


def test_restatements_are_injected(tiny_dataset: Dataset) -> None:
    w6 = WORKLOADS["W6"]
    v = w6.oracle(tiny_dataset, w6.params(tiny_dataset))
    assert v["n_restated_0"] == 0  # before the restatement was published
    assert v["n_restated_1"] > 0
    assert v["n_0"] == v["n_1"]  # restatements replace rows, never add


def test_w2_oracle_matches_f5_and_detects_corruption(tiny_dataset: Dataset, tmp_path: Path) -> None:
    w2 = WORKLOADS["W2"]
    p = w2.params(tiny_dataset)
    want = w2.oracle(tiny_dataset, p)
    fmt = F5Format(tmp_path / "f5")
    fmt.load(tiny_dataset)
    got = fmt.run("W2", p).value
    assert values_match(got, want)
    assert want["n_days"] > 20
    bad = dict(got)
    bad["sum_close"] += 1.0
    assert not values_match(bad, want)
    bad = dict(got)
    bad["n_trades"] += 1
    assert not values_match(bad, want)
    fmt.teardown()


def test_oracle_mismatch_is_raised(tiny_dataset: Dataset, tmp_path: Path) -> None:
    class Broken(F5Format):
        def run(self, workload: str, params: Any) -> WorkloadResult:
            res = super().run(workload, params)
            return WorkloadResult({k: v + 1 for k, v in res.value.items()}, res.rows_scanned)

    with pytest.raises(OracleMismatchError):
        run_benchmark(
            tiny_dataset.spec,
            ["F5"],
            ["W3"],
            1,
            tmp_path / "o",
            data_root=tiny_dataset.root.parent,
            factories={"F5": Broken},
        )


def test_smoke_f5_w1_to_w6(tiny_dataset: Dataset, tmp_path: Path) -> None:
    t0 = time.perf_counter()
    rows = run_benchmark(
        tiny_dataset.spec, ["F5"], [f"W{i}" for i in range(1, 7)], 2, tmp_path,
        data_root=tiny_dataset.root.parent,
    )  # fmt: skip
    if not os.environ.get("CI"):  # shared CI runners are too noisy for a wall-clock bound
        assert time.perf_counter() - t0 < 30
    assert {(r.workload_id, r.cache) for r in rows} == {("W1", "n/a")} | {
        (f"W{i}", c) for i in range(2, 7) for c in ("cold", "warm")
    }
    assert all(r.wall_p50_s > 0 and r.bytes_at_rest > 0 for r in rows)
    assert all(r.bytes_read for r in rows if r.workload_id != "W1")
    assert (tmp_path / "results.parquet").exists()
    assert (tmp_path / "summary.json").exists()


def test_default_range_covers_scale_years(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TIDAL_SYNTH__END", raising=False)
    for scale, years in (("S", 5), ("M", 10), ("L", 20)):
        start, end = default_range(scale)
        assert end == date(2025, 12, 31)
        assert start == date(2025 - years + 1, 1, 1)
        assert len({u[1] for u in plan_years(scale, start, end)}) == years
    monkeypatch.setenv("TIDAL_SYNTH__END", "2023-06-30")
    assert default_range("M") == (date(2014, 1, 1), date(2023, 6, 30))


def plan_years(scale: str, start: date, end: date) -> list[tuple[str, int, int]]:
    return [(u.instrument_id, u.year, u.month) for u in synthetic.plan(scale, start, end)]


def _tick_table(rows: list[tuple[int, int, float, int, int]]) -> Any:
    """(ts_ns, side, price, ingested_at_ns, version) -> TICK_V1 table for SYN.EURUSD.TICK."""

    n = len(rows)
    cols = list(zip(*rows, strict=True))
    return pa.table(
        {
            "ts_ns": pa.array(cols[0], pa.int64()),
            "instrument_id": pa.array(["SYN.EURUSD.TICK"] * n).dictionary_encode(),
            "price": pa.array(cols[2], pa.float64()),
            "size": pa.array([1.0] * n, pa.float32()),
            "side": pa.array(cols[1], pa.int8()),
            "quality": pa.array([0] * n, pa.int8()),
            "ingested_at_ns": pa.array(cols[3], pa.int64()),
            "version": pa.array(cols[4], pa.int16()),
        }
    ).cast(TICK_V1)


INST = "SYN.EURUSD.TICK"
T0 = 1_730_800_000_000_000_000  # 2024-11-05 UTC, inside partition 2024-11
BASE, RESTATE = 1_735_689_600_000_000_000, 1_738_368_000_000_000_000


def _mini(tmp_path: Path) -> Dataset:
    ds_root = tmp_path / "mini"
    kw: dict[str, Any] = {"dataset": "ticks", "instrument_id": INST, "year": 2024, "month": 11}
    base = [(T0, 0, 1.0, BASE, 1), (T0, 1, 2.0, BASE, 1), (T0 + 1, 0, 3.0, BASE, 1),
            (T0 + 1, 1, 9.0, BASE, 1)]  # fmt: skip
    write_partition(
        LocalStore(ds_root / "base"), batches=[_tick_table(base)], source_hash="t", **kw
    )
    rest = [(T0, 0, 1.5, RESTATE, 2)]
    write_partition(
        LocalStore(ds_root / "restate"), batches=[_tick_table(rest)], source_hash="t", **kw
    )
    part = (INST, 2024, 11)
    return Dataset(ds_root, DatasetSpec(), (part,), part, {INST: 4}, "x", (INST,))


def test_w6_and_w4_use_the_tick_key_ts_and_side(tmp_path: Path) -> None:
    ds = _mini(tmp_path)
    w6, w4 = WORKLOADS["W6"], WORKLOADS["W4"]
    p6 = w6.params(ds)
    p6["asof_ns"] = [BASE + 1, RESTATE + 1]  # before / after the restatement is published
    want = w6.oracle(ds, p6)
    # 4 distinct (ts_ns, side) keys: grouping by ts_ns alone would collapse them to 2
    assert want["n_0"] == want["n_1"] == 4
    assert want["n_restated_0"] == 0
    assert want["n_restated_1"] == 1
    assert want["sum_price_0"] == pytest.approx(15.0)
    assert want["sum_price_1"] == pytest.approx(15.5)
    fmt = F5Format(tmp_path / "f5")
    fmt.load(ds)
    assert values_match(fmt.run("W6", p6).value, want)
    p4 = w4.params(ds)
    last = w4.oracle(ds, p4)
    assert last == {"ts_ns": T0 + 1, "price": 3.0}  # the trade, not the side-1 quote at same ts
    assert values_match(fmt.run("W4", p4).value, last)
    fmt.teardown()


def test_profiling_pass_is_separate_from_timed_run(tmp_path: Path) -> None:
    ds = _mini(tmp_path)
    fmt = F5Format(tmp_path / "f5")
    fmt.load(ds)
    p = WORKLOADS["W3"].params(ds)
    plain = fmt.run("W3", p)
    assert plain.bytes_read is None or plain.bytes_read == 0
    prof = fmt.io_profile("W3", p)
    assert (prof.bytes_read or 0) > 0
    assert prof.value == plain.value
    fmt.teardown()


def test_w1_source_is_preloaded(tiny_dataset: Dataset) -> None:
    sub = tiny_dataset.select(list(tiny_dataset.instruments[:1])).preloaded()
    part = sub.partitions()[0]
    assert sub.read_partition(part) is sub.read_partition(part)  # served from memory


def test_dataset_cache_build_is_atomic(
    tmp_path: Path, tiny_dataset: Dataset, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = synthetic.generate_unit
    calls = {"n": 0}

    def flaky(*a: Any, **k: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("crash mid-build")
        return real(*a, **k)

    monkeypatch.setattr(synthetic, "generate_unit", flaky)
    with pytest.raises(RuntimeError, match="crash"):
        build_dataset(tiny_dataset.spec, tmp_path)
    assert list(tmp_path.iterdir()) == []  # neither a half-built cache nor temp leftovers
    monkeypatch.setattr(synthetic, "generate_unit", real)
    again = build_dataset(tiny_dataset.spec, tmp_path)
    assert again.content_hash == tiny_dataset.content_hash
