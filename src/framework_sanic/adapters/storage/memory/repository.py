"""In-memory Repository. Used for tests and single-process local development."""

from __future__ import annotations

import asyncio
import copy
from collections import defaultdict
from collections.abc import AsyncIterator
from types import TracebackType
from typing import Any, Self

from framework_sanic.core.domain.errors import ServiceUnavailable
from framework_sanic.core.domain.records import Key, Record, UpsertResult
from framework_sanic.core.ports.repository import (
    MergeFn,
    StorageSchema,
    check_record,
    classify,
    is_expired,
)
from framework_sanic.core.ports.runtime import Clock, SystemClock


def _norm(key: Key) -> Key:
    return tuple(key)


def _matches(record: Record, filters: dict[str, Any]) -> bool:
    return all(record.get(k) == v for k, v in filters.items())


class MemoryRepository:
    def __init__(self, clock: Clock | None = None) -> None:
        self._clock = clock or SystemClock()
        self._data: dict[str, dict[Key, Record]] = defaultdict(dict)
        self._lock = asyncio.Lock()
        self.down = False  # test switch: simulate the database being unavailable
        self.schema: StorageSchema | None = None  # what setup() received (for tests)

    def _check(self) -> None:
        if self.down:
            raise ServiceUnavailable("memory repository is down")

    def _read(self, kind: str, key: Key) -> Record | None:
        rec = self._data[kind].get(_norm(key))
        if rec is None or is_expired(rec, self._clock.now()):
            return None
        return copy.deepcopy(rec)

    async def get(self, kind: str, key: Key) -> Record | None:
        self._check()
        return self._read(kind, key)

    async def upsert(self, kind: str, key: Key, merge_fn: MergeFn) -> UpsertResult:
        self._check()
        async with self._lock:
            old = self._read(kind, key)
            new = merge_fn(copy.deepcopy(old))
            result = classify(old, new)
            if result in ("created", "updated"):
                assert new is not None
                check_record(new)
                self._data[kind][_norm(key)] = copy.deepcopy(new)
            return result

    async def delete(self, kind: str, key: Key) -> None:
        self._check()
        async with self._lock:
            self._data[kind].pop(_norm(key), None)

    async def query(self, kind: str, **filters: Any) -> AsyncIterator[Record]:
        self._check()
        now = self._clock.now()
        snapshot = [copy.deepcopy(r) for r in self._data[kind].values()]
        for rec in snapshot:
            if not is_expired(rec, now) and _matches(rec, filters):
                yield rec

    def unit_of_work(self) -> MemoryUnitOfWork:
        return MemoryUnitOfWork(self)

    async def ping(self) -> bool:
        return not self.down

    async def close(self) -> None:
        return None

    async def setup(self, schema: StorageSchema) -> None:
        self.schema = schema  # nothing to create; queries scan in memory

    async def purge_expired(self, *, limit: int = 1000) -> int:
        self._check()
        now = self._clock.now()
        removed = 0
        async with self._lock:
            for records in self._data.values():
                for k in [k for k, r in records.items() if is_expired(r, now)]:
                    if removed >= limit:
                        return removed
                    del records[k]
                    removed += 1
        return removed


class MemoryUnitOfWork:
    """Serializable: holds the repository lock for its whole duration and commits atomically."""

    def __init__(self, repo: MemoryRepository) -> None:
        self._repo = repo
        self._staged: dict[tuple[str, Key], Record | None] = {}

    async def __aenter__(self) -> Self:
        self._repo._check()
        await self._repo._lock.acquire()
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        try:
            if exc_type is None:
                self._repo._check()
                for (kind, key), rec in self._staged.items():
                    if rec is None:
                        self._repo._data[kind].pop(key, None)
                    else:
                        self._repo._data[kind][key] = rec
        finally:
            self._repo._lock.release()

    async def get(self, kind: str, key: Key) -> Record | None:
        k = (kind, _norm(key))
        if k in self._staged:
            return copy.deepcopy(self._staged[k])
        return self._repo._read(kind, key)

    async def upsert(self, kind: str, key: Key, merge_fn: MergeFn) -> UpsertResult:
        old = await self.get(kind, key)
        new = merge_fn(copy.deepcopy(old))
        result = classify(old, new)
        if result in ("created", "updated"):
            assert new is not None
            check_record(new)
            self._staged[(kind, _norm(key))] = copy.deepcopy(new)
        return result

    async def delete(self, kind: str, key: Key) -> None:
        self._staged[(kind, _norm(key))] = None

    async def add_outbox(self, key: Key, record: Record) -> None:
        check_record(record)
        self._staged[("Outbox", _norm(key))] = copy.deepcopy(record)
