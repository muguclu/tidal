"""SPEC-04 §4: a 10M-row stream through tidal-api -> RemoteReader -> tidal-query stays < 500 MB.

Measured in a fresh interpreter (``stream_probe.py``) over real loopback sockets, so the number is
the peak working set of the whole serving path (both services plus the client), not of pytest.
Peak RSS comes from ``GetProcessMemoryInfo`` on Windows and ``ru_maxrss`` on POSIX (no psutil, no
/proc). Arrow's own pool high-water mark is asserted too, because RSS alone could hide a buffered
body behind the Python baseline.
"""

import json
import subprocess
import sys
from pathlib import Path

PROBE = Path(__file__).with_name("stream_probe.py")
RSS_LIMIT_MB = 500
ARROW_POOL_LIMIT_MB = 64  # one 64Ki-row batch is ~2 MB; a buffered 10M-row body would be 300+


def probe(rows: int, fmt: str) -> dict[str, float]:
    done = subprocess.run(  # noqa: S603 - fixed argv, our own script
        [sys.executable, str(PROBE), str(rows), fmt],
        capture_output=True, text=True, timeout=600, check=False,
    )  # fmt: skip
    assert done.returncode == 0, done.stderr[-2000:]
    result: dict[str, float] = json.loads(done.stdout.strip().splitlines()[-1])
    return result


def test_arrow_10m_rows_peak_rss_below_500mb() -> None:
    r = probe(10_000_000, "arrow")
    assert r["rows"] == 10_000_000
    assert r["bytes"] > 300 * 2**20  # it really moved the whole result
    assert r["rss_peak_mb"] < RSS_LIMIT_MB
    assert r["arrow_max_mb"] < ARROW_POOL_LIMIT_MB


def test_parquet_stream_memory_is_bounded() -> None:
    r = probe(2_000_000, "parquet")
    assert r["rows"] == 2_000_000
    assert r["rss_peak_mb"] < RSS_LIMIT_MB
    assert r["arrow_max_mb"] < ARROW_POOL_LIMIT_MB
