"""In-memory Cache for tests and local development."""

from __future__ import annotations

import time

from framework_sanic.core.ports.cache import CacheUnavailable
from framework_sanic.core.services.rate_limit import LocalBucket, TokenBucketMath


class MemoryCache:
    def __init__(self) -> None:
        self._data: dict[str, tuple[bytes, float | None]] = {}
        self._buckets: dict[str, LocalBucket] = {}
        self.down = False  # test switch: simulate Redis being unavailable

    def _check(self) -> None:
        if self.down:
            raise CacheUnavailable("memory cache is down")

    def _live(self, key: str) -> bytes | None:
        item = self._data.get(key)
        if item is None:
            return None
        value, exp = item
        if exp is not None and exp <= time.monotonic():
            del self._data[key]
            return None
        return value

    async def get(self, key: str) -> bytes | None:
        self._check()
        return self._live(key)

    async def set(self, key: str, value: bytes, ttl_seconds: float | None = None) -> None:
        self._check()
        self._data[key] = (value, time.monotonic() + ttl_seconds if ttl_seconds else None)

    async def delete(self, key: str) -> None:
        self._check()
        self._data.pop(key, None)

    async def add(self, key: str, value: bytes, ttl_seconds: float) -> bool:
        self._check()
        if self._live(key) is not None:
            return False
        self._data[key] = (value, time.monotonic() + ttl_seconds)
        return True

    async def take_tokens(
        self, key: str, *, capacity: float, refill_per_second: float, cost: float = 1.0, reserve: float = 0.0
    ) -> float:
        self._check()
        bucket, wait = TokenBucketMath.take(
            self._buckets.get(key),
            now=time.monotonic(),
            capacity=capacity,
            refill_per_second=refill_per_second,
            cost=cost,
            reserve=reserve,
        )
        self._buckets[key] = bucket
        return wait

    async def ping(self) -> bool:
        return not self.down

    async def close(self) -> None:
        return None
