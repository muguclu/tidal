"""T-302 determinism: same input => same result_sha256, across runs, processes and platforms."""

import subprocess
import sys
from pathlib import Path

import pytest

from tidal.backtest import run_backtest
from unit.backtest._bt_support import fixture_paths, load_fixture, request_and_bars

PATHS = fixture_paths()
IDS = [p.stem for p in PATHS]
RUNNER = Path(__file__).resolve().parent / "_hash_runner.py"


@pytest.mark.parametrize("path", PATHS, ids=IDS)
def test_repeated_runs_identical(path: Path) -> None:
    fx = load_fixture(path)
    hashes = {run_backtest(*request_and_bars(fx)).result_sha256 for _ in range(5)}
    assert len(hashes) == 1


@pytest.mark.parametrize("path", PATHS, ids=IDS)
def test_matches_committed_hash(path: Path) -> None:
    """Pinned hash: Linux CI and Windows dev must both reproduce it (cross-machine criterion)."""
    fx = load_fixture(path)
    got = run_backtest(*request_and_bars(fx)).result_sha256
    assert got == fx["result_sha256"], (
        f"{path.stem}: platform={sys.platform} produced {got}; a mismatch is a real "
        "cross-platform determinism finding, do not re-pin without investigating"
    )


@pytest.mark.parametrize("path", PATHS[:2], ids=IDS[:2])
def test_fresh_subprocess_same_hash(path: Path, tmp_path: Path) -> None:
    here = run_backtest(*request_and_bars(load_fixture(path))).result_sha256
    proc = subprocess.run(  # noqa: S603 - fixed argv, trusted local script
        [sys.executable, str(RUNNER), str(path)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == here


def test_hash_sensitive_to_inputs() -> None:
    fx = load_fixture(PATHS[0])
    base = run_backtest(*request_and_bars(fx)).result_sha256
    fx["bars"]["T.A.X"]["close"][-1] += 1.0
    assert run_backtest(*request_and_bars(fx)).result_sha256 != base
    fx2 = load_fixture(PATHS[0])
    fx2["costs"]["fee_bps"] = 1.0
    assert run_backtest(*request_and_bars(fx2)).result_sha256 != base
