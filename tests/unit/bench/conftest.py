"""Shared fixtures for the benchmark harness tests (T-201)."""

from datetime import date

import pytest
from benchmarks.storage.workloads import Dataset, DatasetSpec, build_dataset

TINY = DatasetSpec(
    scale="S", seed=7, start=date(2024, 11, 1), end=date(2024, 12, 31), n_instruments=2
)


@pytest.fixture(scope="session")
def tiny_dataset(tmp_path_factory: pytest.TempPathFactory) -> Dataset:
    """A 2-instrument, 2-month dataset (about 0.7M ticks) in a fresh temp root."""
    return build_dataset(TINY, tmp_path_factory.mktemp("bench-data"))
