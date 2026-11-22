"""Repository and UnitOfWork ports.

Contract (verified by ``tests/contract/test_repository_contract.py`` against every adapter):

* ``upsert`` is an atomic read-modify-write. ``merge_fn`` receives the current record (or None)
  and returns the new record, or None to leave storage untouched ("ignored"). Returning a record
  equal to the current one yields "unchanged" and performs no write.
* Every record written must carry ``schema_version`` (ValueError otherwise).
* A record whose ``expires_at`` (epoch seconds) is in the past is treated as absent.
* ``query`` does equality matching on top-level fields and yields every match (no order).
* ``find`` adds ranges, sorting and paging; ``count`` counts. Their rules, the same on every
  backend:

  - ``where`` is a list of ``(field, op, value)`` with op ``==``, ``<``, ``<=``, ``>`` or ``>=``,
    on top-level fields holding a str, int, float or bool. A condition only matches values of
    the same type as its value (numbers: int and float together), so a missing field or None
    never matches.
  - ``order_by`` lists fields, ``"-field"`` for descending. Records without a value for an order
    field are left out (as Datastore does). Ties are broken by key, ascending.
  - ``limit`` (1..``MAX_FIND``) and ``offset`` page the result.
  - Expired records are left out of ``find``. ``count`` may include records that expired but
    were not purged yet on backends that cannot filter them cheaply (Datastore).
  - Declare the query with ``ctx.declare_index(kind, *equality_fields, *range_or_order_fields)``
    (``"-field"`` for descending order). Datastore needs it for any query on two or more
    fields; SQL builds an index from it so the query does not scan.
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

import re
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from types import TracebackType
from typing import Any, Literal, Protocol, Self, runtime_checkable

from ..domain.errors import Conflict
from ..domain.records import Key, Record, UpsertResult, key_to_str

MergeFn = Callable[[Record | None], Record | None]

Op = Literal["==", "<", "<=", ">", ">="]
Scalar = str | int | float | bool
Condition = tuple[str, Op, Scalar]
OPS: tuple[str, ...] = ("==", "<", "<=", ">", ">=")
MAX_FIND = 1000
_FIELD = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")


def check_field(name: str) -> str:
    """Field names end up in SQL index definitions: plain identifiers only."""
    if not _FIELD.match(name):
        raise ValueError(f"invalid field name {name!r}")
    return name


def check_find(where: Sequence[Condition], order_by: Sequence[str], limit: int, offset: int) -> None:
    for field_name, op, value in where:
        check_field(field_name)
        if op not in OPS:
            raise ValueError(f"invalid operator {op!r}; expected one of {OPS}")
        if not isinstance(value, (str, int, float, bool)):
            raise ValueError(f"condition on {field_name!r} needs a str, int, float or bool value")
    for o in order_by:
        check_field(o.removeprefix("-"))
    if not 1 <= limit <= MAX_FIND:
        raise ValueError(f"limit must be between 1 and {MAX_FIND}")
    if offset < 0:
        raise ValueError("offset must be >= 0")


_COMPARE: dict[str, Callable[[Any, Any], bool]] = {
    "==": lambda a, b: a == b,
    "<": lambda a, b: a < b,
    "<=": lambda a, b: a <= b,
    ">": lambda a, b: a > b,
    ">=": lambda a, b: a >= b,
}


def find_in(
    items: Sequence[tuple[Key, Record]],
    where: Sequence[Condition],
    order_by: Sequence[str],
    limit: int,
    offset: int,
) -> list[Record]:
    """``find`` over records in memory: the reference behaviour of the contract."""
    hits = [(k, r) for k, r in items if matches_where(r, where)]
    hits = [(k, r) for k, r in hits if all(_sortable(r.get(o.removeprefix("-"))) for o in order_by)]
    hits.sort(key=lambda kr: key_to_str(kr[0]))  # final tie-break: key ascending
    for o in reversed(order_by):  # stable sorts, last order field first
        name = o.removeprefix("-")

        def by_field(kr: tuple[Key, Record], n: str = name) -> tuple[int, Any]:
            return _sort_value(kr[1][n])

        hits.sort(key=by_field, reverse=o.startswith("-"))
    return [r for _, r in hits[offset : offset + limit]]


def matches_where(record: Record, where: Sequence[Condition]) -> bool:
    for field_name, op, value in where:
        current = record.get(field_name)
        if current is None or not same_type(current, value) or not _COMPARE[op](current, value):
            return False
    return True


def _sortable(value: Any) -> bool:
    return isinstance(value, (str, int, float, bool))


def _sort_value(value: Any) -> tuple[int, Any]:
    # Mixed types in one field are undefined; keep them apart instead of raising.
    if isinstance(value, bool):
        return (0, value)
    if isinstance(value, (int, float)):
        return (1, value)
    return (2, value)


def same_type(a: Any, b: Any) -> bool:
    """The type rule of ``find``: bools only match bools, numbers match numbers, str str."""
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool)
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return True
    return isinstance(a, str) and isinstance(b, str)


@dataclass(frozen=True)
class IndexSpec:
    """A query a feature declared (``ctx.declare_index``): equality fields first, then the range
    or order fields; ``"-field"`` means descending."""

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

    async def find(
        self,
        kind: str,
        *,
        where: Sequence[Condition] = (),
        order_by: Sequence[str] = (),
        limit: int = 100,
        offset: int = 0,
    ) -> list[Record]: ...

    async def count(self, kind: str, *, where: Sequence[Condition] = ()) -> int: ...

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
