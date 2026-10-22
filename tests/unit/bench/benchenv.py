"""Test helper: make the repo root importable (``benchmarks`` is not installed) + shared dataset."""

import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from benchmarks.storage.workloads import Dataset, DatasetSpec, build_dataset  # noqa: E402

TINY = DatasetSpec(
    scale="S", seed=7, start=date(2024, 11, 1), end=date(2024, 12, 31), n_instruments=2
)


def make_tiny(factory: pytest.TempPathFactory) -> Dataset:
    """A 2-instrument, 2-month dataset (about 0.7M ticks) in a fresh temp root."""
    return build_dataset(TINY, factory.mktemp("bench-data"))
