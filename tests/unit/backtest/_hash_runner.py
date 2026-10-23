"""Subprocess entry point: print the result_sha256 of a golden fixture (argv[1])."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # tests/ (namespace pkg "unit")

from tidal.backtest import run_backtest
from unit.backtest._bt_support import load_fixture, request_and_bars

if __name__ == "__main__":
    req, bars = request_and_bars(load_fixture(Path(sys.argv[1])))
    sys.stdout.write(run_backtest(req, bars).result_sha256 + "\n")
