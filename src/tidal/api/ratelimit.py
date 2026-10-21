"""Per-principal rate limit: 60 requests/minute (SPEC-04 §3), in-memory token bucket.

LIMITATION: state lives in the process. With N API replicas the effective limit is up to N x 60
per minute per principal; a shared limiter (e.g. at API Management or Redis) is future work. Time
comes from the injected clock, so tests advance a fixed clock instead of sleeping.
"""

import math
from dataclasses import dataclass

from tidal.common.errors import RateLimited
from tidal.contracts.models import datetime_to_ns
from tidal.query.protocols import Clock

CAPACITY = 60
REFILL_PER_SECOND = 1.0  # 60 tokens per minute
_NS = 1_000_000_000
_PRUNE_ABOVE = 10_000  # bound memory: drop idle (full) buckets beyond this many principals


@dataclass(slots=True)
class _Bucket:
    tokens: float
    updated_ns: int


class TokenBucketLimiter:
    """Token bucket per key; ``check`` takes one token or raises ``RateLimited``."""

    def __init__(self, clock: Clock, capacity: int = CAPACITY) -> None:
        """Create a limiter reading time from ``clock``."""
        self._clock = clock
        self._capacity = capacity
        self._buckets: dict[str, _Bucket] = {}

    def check(self, key: str) -> None:
        """Consume one token for ``key``; raise ``RateLimited`` (with retry-after) if none left."""
        now = datetime_to_ns(self._clock.now())
        bucket = self._buckets.get(key)
        if bucket is None:
            if len(self._buckets) >= _PRUNE_ABOVE:
                self._prune(now)
            bucket = self._buckets[key] = _Bucket(float(self._capacity), now)
        elapsed = max(now - bucket.updated_ns, 0) / _NS
        bucket.tokens = min(self._capacity, bucket.tokens + elapsed * REFILL_PER_SECOND)
        bucket.updated_ns = now
        if bucket.tokens < 1:
            wait = math.ceil((1 - bucket.tokens) / REFILL_PER_SECOND)
            raise RateLimited(wait, "Rate limit of 60 requests per minute exceeded.")
        bucket.tokens -= 1

    def _prune(self, now: int) -> None:
        full_after = self._capacity / REFILL_PER_SECOND * _NS
        self._buckets = {k: b for k, b in self._buckets.items() if now - b.updated_ns < full_after}
