"""Registry, dataset determinism, W2 oracle, smoke run (T-201)."""

import time
from pathlib import Path
from typing import Any

import pytest
from benchmarks.storage.run import F5Format, OracleMismatchError, run_benchmark
from benchmarks.storage.workloads import (
    WORKLOADS,
    Dataset,
    DatasetSpec,
    WorkloadResult,
    build_dataset,
    parse_ids,
    values_match,
)


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
    assert time.perf_counter() - t0 < 30
    assert {(r.workload_id, r.cache) for r in rows} == {("W1", "cold")} | {
        (f"W{i}", c) for i in range(2, 7) for c in ("cold", "warm")
    }
    assert all(r.wall_p50_s > 0 and r.bytes_at_rest > 0 for r in rows)
    assert all(r.bytes_read for r in rows if r.workload_id != "W1")
    assert (tmp_path / "results.parquet").exists()
    assert (tmp_path / "summary.json").exists()
