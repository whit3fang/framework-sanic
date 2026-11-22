"""Token-bucket rate limiting with priority lanes.

Buckets live in the Cache (Redis). Lower-priority lanes must leave a reserve in the bucket, so
UI calls keep working while the reconciler or a backfill is consuming the shared budget. When the
cache is unavailable we fall back to a per-process bucket (fail-soft rather than fail-open).
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from ..domain.records import Lane
from ..ports.cache import Cache, CacheUnavailable

LANE_RESERVE_FRACTION: dict[Lane, float] = {Lane.UI: 0.0, Lane.LIVE: 0.1, Lane.BACKFILL: 0.3}


@dataclass
class LocalBucket:
    tokens: float
    updated: float


class TokenBucketMath:
    """Pure token-bucket arithmetic, shared by the memory cache and the local fallback."""

    @staticmethod
    def take(
        bucket: LocalBucket | None,
        *,
        now: float,
        capacity: float,
        refill_per_second: float,
        cost: float,
        reserve: float,
    ) -> tuple[LocalBucket, float]:
        if bucket is None:
            bucket = LocalBucket(tokens=capacity, updated=now)
        tokens = min(capacity, bucket.tokens + (now - bucket.updated) * refill_per_second)
        bucket = LocalBucket(tokens=tokens, updated=now)
        if tokens - cost >= reserve:
            bucket.tokens = tokens - cost
            return bucket, 0.0
        missing = reserve + cost - tokens
        return bucket, missing / refill_per_second if refill_per_second > 0 else 60.0


class RateLimiter:
    def __init__(self, cache: Cache) -> None:
        self._cache = cache
        self._local: dict[str, LocalBucket] = {}

    async def acquire(
        self, bucket: str, *, per_minute: float, lane: Lane = Lane.LIVE, cost: float = 1.0
    ) -> float:
        """Returns 0 when granted, otherwise the number of seconds to wait before retrying."""
        capacity = max(per_minute, 1.0)
        refill = per_minute / 60.0
        reserve = capacity * LANE_RESERVE_FRACTION[lane]
        try:
            return await self._cache.take_tokens(
                f"rl:{bucket}", capacity=capacity, refill_per_second=refill, cost=cost, reserve=reserve
            )
        except CacheUnavailable:
            state, wait = TokenBucketMath.take(
                self._local.get(bucket),
                now=time.monotonic(),
                capacity=capacity,
                refill_per_second=refill,
                cost=cost,
                reserve=reserve,
            )
            self._local[bucket] = state
            return wait
