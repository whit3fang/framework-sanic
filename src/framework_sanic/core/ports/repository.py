"""Repository and UnitOfWork ports.

Contract (verified by ``tests/contract/test_repository_contract.py`` against every adapter):

* ``upsert`` is an atomic read-modify-write. ``merge_fn`` receives the current record (or None)
  and returns the new record, or None to leave storage untouched ("ignored"). Returning a record
  equal to the current one yields "unchanged" and performs no write.
* Every record written must carry ``schema_version`` (ValueError otherwise).
* A record whose ``expires_at`` (epoch seconds) is in the past is treated as absent.
* ``query`` does equality matching on top-level fields.
* A UnitOfWork commits all of its upserts, deletes and outbox writes atomically, or none.
  Conflicting concurrent writes raise ``Conflict``; use ``run_in_uow`` to retry.

Lifecycle:

* ``setup(schema)`` is called once at startup, before any other call. It must be idempotent:
  create or verify whatever the backend needs (tables, indexes) for ``schema.kinds`` and the
  ``schema.indexes`` that enabled features declared (``ctx.declare_index``).
* ``purge_expired(limit=...)`` physically deletes up to ``limit`` expired records and returns how
  many it deleted. The platform feature calls it on a schedule. Adapters whose backend enforces
  TTL natively may still implement it as a fallback (or return 0).
* ``close()`` releases connections at shutdown.

Writing an adapter: implement ``Repository`` + ``UnitOfWork`` and run the reusable contract suite
in ``framework_sanic.testing.repository_contract`` against it.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from types import TracebackType
from typing import Any, Protocol, Self, runtime_checkable

from ..domain.errors import Conflict
from ..domain.records import Key, Record, UpsertResult

MergeFn = Callable[[Record | None], Record | None]


@dataclass(frozen=True)
class IndexSpec:
    """Equality-query index a feature declared: ``query(kind, **{f: ... for f in fields})``."""

    kind: str
    fields: tuple[str, ...]


@dataclass(frozen=True)
class StorageSchema:
    """What the enabled framework + features will store, handed to ``Repository.setup``."""

    kinds: tuple[str, ...] = ()
    indexes: tuple[IndexSpec, ...] = field(default=())


class UnitOfWork(Protocol):
    async def __aenter__(self) -> Self: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None: ...

    async def get(self, kind: str, key: Key) -> Record | None: ...

    async def upsert(self, kind: str, key: Key, merge_fn: MergeFn) -> UpsertResult: ...

    async def delete(self, kind: str, key: Key) -> None: ...

    async def add_outbox(self, key: Key, record: Record) -> None:
        """Write an outbox record (kind "Outbox") as part of this unit of work."""
        ...


@runtime_checkable
class Repository(Protocol):
    async def get(self, kind: str, key: Key) -> Record | None: ...

    async def upsert(self, kind: str, key: Key, merge_fn: MergeFn) -> UpsertResult: ...

    async def delete(self, kind: str, key: Key) -> None: ...

    def query(self, kind: str, **filters: Any) -> AsyncIterator[Record]: ...

    def unit_of_work(self) -> UnitOfWork: ...

    async def ping(self) -> bool: ...

    async def setup(self, schema: StorageSchema) -> None: ...

    async def purge_expired(self, *, limit: int = 1000) -> int: ...

    async def close(self) -> None: ...


def check_record(record: Record) -> None:
    if "schema_version" not in record:
        raise ValueError("record is missing schema_version")


def is_expired(record: Record, now: float) -> bool:
    exp = record.get("expires_at")
    return isinstance(exp, (int, float)) and exp <= now


def classify(old: Record | None, new: Record | None) -> UpsertResult:
    if new is None:
        return "ignored"
    if old is None:
        return "created"
    return "unchanged" if old == new else "updated"


async def run_in_uow[T](repo: Repository, fn: Callable[[UnitOfWork], Awaitable[T]], *, retries: int = 8) -> T:
    """Run ``fn`` in a unit of work, retrying on optimistic-concurrency conflicts."""
    last: Exception | None = None
    for _ in range(retries):
        try:
            async with repo.unit_of_work() as uow:
                return await fn(uow)
        except Conflict as exc:
            last = exc
    raise Conflict(f"unit of work failed after {retries} attempts") from last
