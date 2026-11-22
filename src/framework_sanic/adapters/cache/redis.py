"""Redis Cache adapter. Connection errors surface as CacheUnavailable."""

from __future__ import annotations

import time
from typing import Any

import redis.asyncio as aioredis
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import RedisError
from redis.exceptions import TimeoutError as RedisTimeoutError

from framework_sanic.core.ports.cache import CacheUnavailable

# KEYS[1]=bucket  ARGV: capacity, refill_per_second, cost, reserve, now
_TOKEN_BUCKET = """
local capacity = tonumber(ARGV[1])
local refill = tonumber(ARGV[2])
local cost = tonumber(ARGV[3])
local reserve = tonumber(ARGV[4])
local now = tonumber(ARGV[5])
local state = redis.call('HMGET', KEYS[1], 'tokens', 'updated')
local tokens = tonumber(state[1])
local updated = tonumber(state[2])
if tokens == nil then tokens = capacity; updated = now end
tokens = math.min(capacity, tokens + math.max(0, now - updated) * refill)
local wait = 0
if tokens - cost >= reserve then
  tokens = tokens - cost
else
  if refill > 0 then wait = (reserve + cost - tokens) / refill else wait = 60 end
end
redis.call('HSET', KEYS[1], 'tokens', tokens, 'updated', now)
local ttl = 60
if refill > 0 then ttl = math.ceil(capacity / refill) + 60 end
redis.call('EXPIRE', KEYS[1], ttl)
return tostring(wait)
"""


class RedisCache:
    def __init__(self, url: str, *, prefix: str = "ifw:", client: Any = None) -> None:
        self._client: Any = client or aioredis.from_url(
            url, socket_timeout=0.5, socket_connect_timeout=0.5, health_check_interval=30
        )
        self._prefix = prefix
        self._bucket_script: Any = self._client.register_script(_TOKEN_BUCKET)

    def _k(self, key: str) -> str:
        return self._prefix + key

    async def _call(self, coro: Any) -> Any:
        try:
            return await coro
        except (RedisConnectionError, RedisTimeoutError, OSError) as exc:
            raise CacheUnavailable(str(exc)) from exc
        except RedisError as exc:
            raise CacheUnavailable(str(exc)) from exc

    async def get(self, key: str) -> bytes | None:
        value: bytes | None = await self._call(self._client.get(self._k(key)))
        return value

    async def set(self, key: str, value: bytes, ttl_seconds: float | None = None) -> None:
        px = int(ttl_seconds * 1000) if ttl_seconds else None
        await self._call(self._client.set(self._k(key), value, px=px))

    async def delete(self, key: str) -> None:
        await self._call(self._client.delete(self._k(key)))

    async def add(self, key: str, value: bytes, ttl_seconds: float) -> bool:
        return bool(
            await self._call(self._client.set(self._k(key), value, nx=True, px=int(ttl_seconds * 1000)))
        )

    async def take_tokens(
        self, key: str, *, capacity: float, refill_per_second: float, cost: float = 1.0, reserve: float = 0.0
    ) -> float:
        raw = await self._call(
            self._bucket_script(
                keys=[self._k(key)], args=[capacity, refill_per_second, cost, reserve, time.time()]
            )
        )
        return float(raw.decode() if isinstance(raw, bytes) else raw)

    async def ping(self) -> bool:
        try:
            return bool(await self._call(self._client.ping()))
        except CacheUnavailable:
            return False

    async def close(self) -> None:
        await self._client.aclose()
