"""Cache port (Redis in production).

Redis is never the source of truth. It holds OAuth state/PKCE, leases, caches, rate-limit
buckets, the dedupe fast path and the session revocation list. Callers must tolerate
``CacheUnavailable`` wherever correctness does not depend on the cache.
"""

from __future__ import annotations

from typing import Protocol


class CacheUnavailable(Exception):
    pass


class Cache(Protocol):
    async def get(self, key: str) -> bytes | None: ...

    async def set(self, key: str, value: bytes, ttl_seconds: float | None = None) -> None: ...

    async def delete(self, key: str) -> None: ...

    async def add(self, key: str, value: bytes, ttl_seconds: float) -> bool:
        """Set if absent. Returns True if the key was set (lease acquired / first seen)."""
        ...

    async def delete_if(self, key: str, value: bytes) -> None:
        """Delete ``key`` only while it still holds ``value`` (atomic; releases a lease)."""
        ...

    async def take_tokens(
        self,
        key: str,
        *,
        capacity: float,
        refill_per_second: float,
        cost: float = 1.0,
        reserve: float = 0.0,
    ) -> float:
        """Token bucket. Returns 0 if granted, else seconds to wait.

        ``reserve`` tokens are kept back so that higher-priority lanes can still be served.
        """
        ...

    async def ping(self) -> bool: ...

    async def close(self) -> None: ...
