"""Short locks in the cache ("leases").

Each holder gets its own random token, and release deletes the key only while it still holds
that token. So a holder whose lease expired (a slow job) never frees the lease someone else took
after it. The cache is only a fast path: when it is down the lease is granted, and callers keep
their own correctness checks (idempotent writes, compare-and-set).
"""

from __future__ import annotations

import secrets

from ..ports.cache import Cache, CacheUnavailable


async def acquire_lease(cache: Cache, key: str, seconds: float) -> str | None:
    """The holder's token, or None when someone else holds the lease."""
    token = secrets.token_hex(8)
    try:
        return token if await cache.add(key, token.encode(), seconds) else None
    except CacheUnavailable:
        return token


async def release_lease(cache: Cache, key: str, token: str) -> None:
    try:
        await cache.delete_if(key, token.encode())
    except CacheUnavailable:
        pass
