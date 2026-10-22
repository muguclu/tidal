"""Per-principal rate limit: 60 requests/minute (SPEC-04 §3), in-memory token bucket.

LIMITATION: state lives in the process. With N API replicas the effective limit is up to N x 60
per minute per principal; a shared limiter (e.g. at API Management or Redis) is future work.
Memory is bounded by an LRU cap: when more than ``max_keys`` principals are tracked the least
recently used bucket is evicted (that principal then starts again with a full bucket). Time comes
from the injected clock, so tests advance a fixed clock instead of sleeping.
"""

import math
from collections import OrderedDict
from dataclasses import dataclass

from tidal.common.errors import RateLimited
from tidal.contracts.models import datetime_to_ns
from tidal.query.protocols import Clock

CAPACITY = 60
REFILL_PER_SECOND = 1.0  # 60 tokens per minute
MAX_KEYS = 10_000
_NS = 1_000_000_000


@dataclass(slots=True)
class _Bucket:
    tokens: float
    updated_ns: int


class TokenBucketLimiter:
    """Token bucket per key; ``check`` takes one token or raises ``RateLimited``."""

    def __init__(self, clock: Clock, capacity: int = CAPACITY, max_keys: int = MAX_KEYS) -> None:
        """Create a limiter reading time from ``clock``."""
        self._clock = clock
        self._capacity = capacity
        self._max_keys = max_keys
        self._buckets: OrderedDict[str, _Bucket] = OrderedDict()

    def check(self, key: str) -> None:
        """Consume one token for ``key``; raise ``RateLimited`` (with retry-after) if none left."""
        now = datetime_to_ns(self._clock.now())
        bucket = self._buckets.get(key)
        if bucket is None:
            bucket = self._buckets[key] = _Bucket(float(self._capacity), now)
            if len(self._buckets) > self._max_keys:
                self._buckets.popitem(last=False)  # evict the least recently used
        else:
            self._buckets.move_to_end(key)
        elapsed = max(now - bucket.updated_ns, 0) / _NS
        bucket.tokens = min(self._capacity, bucket.tokens + elapsed * REFILL_PER_SECOND)
        bucket.updated_ns = now
        if bucket.tokens < 1:
            wait = math.ceil((1 - bucket.tokens) / REFILL_PER_SECOND)
            raise RateLimited(wait, "Rate limit of 60 requests per minute exceeded.")
        bucket.tokens -= 1
