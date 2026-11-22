"""Cache contract suite: memory adapter and the Redis adapter (fakeredis, or real Redis via TEST_REDIS_URL)."""

from __future__ import annotations

import asyncio
import os

import pytest

from framework_sanic.adapters.cache.memory import MemoryCache
from framework_sanic.adapters.cache.redis import RedisCache
from framework_sanic.core.ports.cache import Cache, CacheUnavailable


@pytest.fixture(params=["memory", "redis"])
async def cache(request: pytest.FixtureRequest) -> Cache:
    if request.param == "memory":
        return MemoryCache()
    url = os.environ.get("TEST_REDIS_URL")
    if url:
        return RedisCache(url, prefix=f"test{os.getpid()}:")
    import fakeredis

    return RedisCache("redis://fake", client=fakeredis.FakeAsyncRedis())


async def test_get_set_delete(cache: Cache) -> None:
    assert await cache.get("k") is None
    await cache.set("k", b"v")
    assert await cache.get("k") == b"v"
    await cache.delete("k")
    assert await cache.get("k") is None


async def test_ttl(cache: Cache) -> None:
    await cache.set("t", b"v", 0.05)
    assert await cache.get("t") == b"v"
    await asyncio.sleep(0.08)
    assert await cache.get("t") is None


async def test_add_is_set_if_absent(cache: Cache) -> None:
    assert await cache.add("lease", b"1", 10) is True
    assert await cache.add("lease", b"2", 10) is False
    assert await cache.get("lease") == b"1"


async def test_token_bucket(cache: Cache) -> None:
    granted = [await cache.take_tokens("b", capacity=5, refill_per_second=1) for _ in range(5)]
    assert granted == [0.0] * 5
    wait = await cache.take_tokens("b", capacity=5, refill_per_second=1)
    assert 0 < wait <= 1.01


async def test_token_bucket_reserve(cache: Cache) -> None:
    # Low-priority lane must leave 3 tokens for higher lanes.
    grants = [await cache.take_tokens("r", capacity=5, refill_per_second=0.01, reserve=3) for _ in range(4)]
    assert grants[:2] == [0.0, 0.0] and grants[2] > 0
    assert await cache.take_tokens("r", capacity=5, refill_per_second=0.01, reserve=0) == 0.0


async def test_unavailable_raises() -> None:
    c = MemoryCache()
    c.down = True
    with pytest.raises(CacheUnavailable):
        await c.get("x")
    r = RedisCache("redis://127.0.0.1:1/0")
    with pytest.raises(CacheUnavailable):
        await r.get("x")
    assert await r.ping() is False
