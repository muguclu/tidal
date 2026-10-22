"""SPEC-04 §3 range limits, shared by the public routers and the ``tidal-query`` service."""

from typing import Final

from tidal.common.errors import RangeTooLarge
from tidal.contracts.schemas import Interval

DAY_NS: Final = 86_400 * 1_000_000_000
BARS_1S_MAX_NS: Final = 7 * DAY_NS  # SPEC-04 §3
BARS_1M_MAX_NS: Final = 366 * DAY_NS  # SPEC-04 §3
TICKS_MAX_NS: Final = 31 * DAY_NS  # SPEC-04 §3


def check_bars_range(interval: Interval, start_ns: int, end_ns: int) -> None:
    """413 when 1s bars span more than 7 days or 1m bars more than 366 days."""
    span = end_ns - start_ns
    if interval is Interval.S1 and span > BARS_1S_MAX_NS:
        raise RangeTooLarge("Bars with interval 1s are limited to 7 days per call.")
    if interval is Interval.M1 and span > BARS_1M_MAX_NS:
        raise RangeTooLarge("Bars with interval 1m are limited to 366 days per call.")


def check_ticks_range(start_ns: int, end_ns: int) -> None:
    """413 when ticks span more than 31 days."""
    if end_ns - start_ns > TICKS_MAX_NS:
        raise RangeTooLarge("Ticks are limited to 31 days per call.")
