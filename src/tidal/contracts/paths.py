"""Backtest output prefix builder (messaging-contract §3); the bottom layer of path building.

``tidal.storage.layout`` re-exports :func:`backtest_prefix` so it stays the one public path API;
it lives here so that ``contracts`` never depends on ``storage``.
"""

import re
from typing import Final

from tidal.contracts.models import ULID_PATTERN

CONTAINER_BACKTESTS: Final = "backtests"
MAX_CHILD_INDEX: Final = 9999
_JOB = re.compile(rf"bt_{ULID_PATTERN}")


def backtest_prefix(job_id: str, child_index: int | None = None) -> str:
    """``backtests/bt_<ULID>/`` or ``.../child-NNNN/``; raises ``ValueError`` on bad input."""
    if not _JOB.fullmatch(job_id):
        raise ValueError(f"bad job id {job_id!r}")
    if child_index is None:
        return f"{CONTAINER_BACKTESTS}/{job_id}/"
    if not 0 <= child_index <= MAX_CHILD_INDEX:
        raise ValueError(f"child index {child_index} out of range")
    return f"{CONTAINER_BACKTESTS}/{job_id}/child-{child_index:04d}/"
