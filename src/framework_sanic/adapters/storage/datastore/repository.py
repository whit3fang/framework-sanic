"""Google Cloud Datastore Repository (official ``google-cloud-datastore`` client, not Cloud NDB).

The client is synchronous; every operation runs in a worker thread via ``asyncio.to_thread``.
VERIFY: benchmark this thread-pool wrapper against an async client at target load.

Layout: entity kind = record kind, key name = canonical key string. The full record is stored
as an unindexed JSON blob (``_json``); top-level scalar fields are also stored as indexed
properties so ``query`` filters work; ``_expire_at`` (datetime) backs a Datastore TTL policy.

Indexes and TTL are deployed out of band (Datastore cannot create them at runtime):

* ``index_yaml(schema)`` renders the composite indexes for the declared multi-field indexes
  (``framework-sanic export-indexes`` writes it for ``gcloud datastore indexes create``).
  Single-field equality filters use Datastore's built-in indexes.
* Native TTL: ``gcloud firestore fields ttls update _expire_at --collection-group=<Kind>
  --enable-ttl`` per kind (``ttl_commands(schema)``). ``purge_expired`` deletes by ``_expire_at``
  as well, so expiry works with or without the policy (e.g. on the emulator).
"""

from __future__ import annotations

import asyncio
import copy
import datetime as dt
import functools
import json
from collections.abc import AsyncIterator, Callable, Sequence
from types import TracebackType
from typing import Any, Self, TypeVar

from framework_sanic.core.domain.errors import Conflict
from framework_sanic.core.domain.records import Key, Record, UpsertResult, key_to_str
from framework_sanic.core.ports.repository import (
    Condition,
    MergeFn,
    StorageSchema,
    check_find,
    check_record,
    classify,
    is_expired,
)
from framework_sanic.core.ports.runtime import Clock, SystemClock

T = TypeVar("T")
_SCALARS = (str, int, float, bool)
_MAX_INDEXED_STR = 1500


def _to_entity(client: Any, kind: str, key: Key, record: Record, version: int, namespace: str | None) -> Any:
    from google.cloud import datastore

    ds_key = client.key(kind, key_to_str(key), namespace=namespace)
    exclude = ["_json"]
    entity = datastore.Entity(key=ds_key, exclude_from_indexes=tuple(exclude))  # type: ignore[no-untyped-call]
    entity["_json"] = json.dumps(record, separators=(",", ":"))
    entity["_v"] = version
    for field, value in record.items():
        if field.startswith("_") or field.endswith("_enc"):  # encrypted blobs are never queried
            continue
        if isinstance(value, _SCALARS) and not (isinstance(value, str) and len(value) > _MAX_INDEXED_STR):
            entity[field] = value
    exp = record.get("expires_at")
    if isinstance(exp, (int, float)):
        entity["_expire_at"] = dt.datetime.fromtimestamp(exp, tz=dt.UTC)
    return entity


def _from_entity(entity: Any) -> tuple[Record, int]:
    return json.loads(entity["_json"]), int(entity.get("_v", 0))


def index_yaml(schema: StorageSchema) -> str:
    """Composite indexes (``index.yaml``) for declared indexes with more than one field."""
    lines = ["indexes:"]
    seen: set[tuple[str, tuple[str, ...]]] = set()
    for spec in schema.indexes:
        if len(spec.fields) < 2 or (spec.kind, spec.fields) in seen:
            continue
        seen.add((spec.kind, spec.fields))
        lines += [f"- kind: {spec.kind}", "  properties:"]
        for f in spec.fields:
            lines.append(f"  - name: {f.removeprefix('-')}")
            if f.startswith("-"):
                lines.append("    direction: desc")
    return "\n".join(lines if len(lines) > 1 else ["indexes: []"]) + "\n"


def ttl_commands(schema: StorageSchema) -> list[str]:
    """``gcloud`` commands enabling native TTL on ``_expire_at`` for every kind."""
    return [
        f"gcloud firestore fields ttls update _expire_at --collection-group={kind} --enable-ttl"
        for kind in schema.kinds
    ]


class DatastoreRepository:
    def __init__(
        self, project: str, *, namespace: str | None = None, clock: Clock | None = None, client: Any = None
    ) -> None:
        if client is None:
            from google.cloud import datastore

            client = datastore.Client(project=project, namespace=namespace)  # type: ignore[no-untyped-call]
        self._client = client
        self._ns = namespace
        self._clock = clock or SystemClock()
        self._schema = StorageSchema()

    async def _run(self, fn: Callable[[], T]) -> T:
        return await asyncio.to_thread(fn)

    def _key(self, kind: str, key: Key) -> Any:
        return self._client.key(kind, key_to_str(key), namespace=self._ns)

    def _read_sync(self, kind: str, key: Key) -> tuple[Record | None, int | None]:
        entity = self._client.get(self._key(kind, key))
        if entity is None:
            return None, None
        data, version = _from_entity(entity)
        if is_expired(data, self._clock.now()):
            return None, version
        return data, version

    async def get(self, kind: str, key: Key) -> Record | None:
        data, _ = await self._run(lambda: self._read_sync(kind, key))
        return data

    def _upsert_sync(self, kind: str, key: Key, merge_fn: MergeFn) -> UpsertResult:
        from google.api_core import exceptions as gexc

        for _ in range(10):
            try:
                with self._client.transaction():
                    old, version = self._read_sync(kind, key)
                    new = merge_fn(copy.deepcopy(old))
                    result = classify(old, new)
                    if result in ("created", "updated"):
                        assert new is not None
                        check_record(new)
                        self._client.put(
                            _to_entity(self._client, kind, key, new, (version or 0) + 1, self._ns)
                        )
                    return result
            except (gexc.Aborted, gexc.Conflict):
                continue
        raise Conflict(f"datastore upsert of {kind} {key} kept conflicting")

    async def upsert(self, kind: str, key: Key, merge_fn: MergeFn) -> UpsertResult:
        return await self._run(lambda: self._upsert_sync(kind, key, merge_fn))

    async def delete(self, kind: str, key: Key) -> None:
        await self._run(lambda: self._client.delete(self._key(kind, key)))

    async def query(self, kind: str, **filters: Any) -> AsyncIterator[Record]:
        from google.cloud.datastore.query import PropertyFilter

        def page(cursor: Any) -> tuple[list[Any], Any]:
            q = self._client.query(kind=kind, namespace=self._ns)
            for field, value in filters.items():
                q.add_filter(filter=PropertyFilter(field, "=", value))  # type: ignore[no-untyped-call]
            it = q.fetch(start_cursor=cursor, limit=500)
            pg = next(it.pages)
            return list(pg), it.next_page_token

        cursor = None
        while True:
            entities, cursor = await self._run(functools.partial(page, cursor))
            now = self._clock.now()
            for e in entities:
                data, _ = _from_entity(e)
                if not is_expired(data, now):
                    yield data
            if not cursor or not entities:
                return

    def _filtered(self, kind: str, where: Sequence[Condition]) -> Any:
        from google.cloud.datastore.query import PropertyFilter

        q = self._client.query(kind=kind, namespace=self._ns)
        for name, op, value in where:
            # Datastore compares values of the same type only, as the find contract requires
            # (it orders int and float together as numbers).
            q.add_filter(filter=PropertyFilter(name, "=" if op == "==" else op, value))  # type: ignore[no-untyped-call]
        return q

    async def find(
        self,
        kind: str,
        *,
        where: Sequence[Condition] = (),
        order_by: Sequence[str] = (),
        limit: int = 100,
        offset: int = 0,
    ) -> list[Record]:
        """Native query. Needs the composite index from ``ctx.declare_index`` for two or more
        fields (``framework-sanic export-indexes``). Entities without an order property are
        skipped by Datastore itself. Expired records not purged yet are dropped after the fetch,
        so a page can be shorter than ``limit``.
        VERIFY: range filters on several fields need Firestore-in-Datastore-mode multi-inequality
        support; on older rules the first order must be the range field."""
        check_find(where, order_by, limit, offset)

        def run() -> list[Record]:
            q = self._filtered(kind, where)
            q.order = list(order_by)  # "-field" is descending, as in our contract
            now = self._clock.now()
            out = []
            for entity in q.fetch(limit=limit, offset=offset):
                data, _ = _from_entity(entity)
                if not is_expired(data, now):
                    out.append(data)
            return out

        return await self._run(run)

    async def count(self, kind: str, *, where: Sequence[Condition] = ()) -> int:
        """Native count aggregation. May include expired records that the TTL policy has not
        deleted yet (Datastore removes them within about a day)."""
        check_find(where, (), 1, 0)

        def run() -> int:
            agg = self._client.aggregation_query(self._filtered(kind, where)).count(alias="n")
            for batch in agg.fetch():
                for result in batch:
                    return int(result.value)
            return 0

        return await self._run(run)

    def unit_of_work(self) -> DatastoreUnitOfWork:
        return DatastoreUnitOfWork(self)

    async def ping(self) -> bool:
        try:
            await self._run(lambda: self._client.get(self._key("_Ping", ("ping",))))
            return True
        except Exception:  # noqa: BLE001
            return False

    async def setup(self, schema: StorageSchema) -> None:
        """Remember the schema (``purge_expired`` needs the kinds) and check the connection.
        Indexes and TTL policies are deployed out of band; see the module docstring."""
        self._schema = schema
        if not await self.ping():
            raise ConnectionError("Datastore is not reachable")

    async def purge_expired(self, *, limit: int = 1000) -> int:
        now = dt.datetime.fromtimestamp(self._clock.now(), tz=dt.UTC)

        def purge_kind(kind: str, budget: int) -> int:
            from google.cloud.datastore.query import PropertyFilter

            q = self._client.query(kind=kind, namespace=self._ns)
            q.add_filter(filter=PropertyFilter("_expire_at", "<=", now))  # type: ignore[no-untyped-call]
            q.keys_only()
            keys = [e.key for e in q.fetch(limit=budget)]
            for i in range(0, len(keys), 500):  # Datastore caps a batch at 500 keys
                self._client.delete_multi(keys[i : i + 500])
            return len(keys)

        removed = 0
        for kind in self._schema.kinds:
            if removed >= limit:
                break
            removed += await self._run(functools.partial(purge_kind, kind, limit - removed))
        return removed

    async def close(self) -> None:
        return None


class DatastoreUnitOfWork:
    """Optimistic unit of work.

    Reads record the version they saw; writes are buffered. On exit a single transaction (in one
    thread, because the client's transaction stack is thread-local) re-reads every touched key,
    checks versions are unchanged, and applies all writes. A mismatch raises Conflict.
    """

    def __init__(self, repo: DatastoreRepository) -> None:
        self._repo = repo
        self._seen: dict[tuple[str, Key], int | None] = {}
        self._staged: dict[tuple[str, Key], Record | None] = {}

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        if exc_type is not None or not self._staged:
            return
        await self._repo._run(self._commit_sync)

    def _commit_sync(self) -> None:
        from google.api_core import exceptions as gexc

        client = self._repo._client
        try:
            with client.transaction():
                for (kind, key), seen_version in self._seen.items():
                    _, current = self._repo._read_sync(kind, key)
                    if current != seen_version:
                        raise Conflict(f"{kind} {key} changed during unit of work")
                for (kind, key), rec in self._staged.items():
                    if rec is None:
                        client.delete(self._repo._key(kind, key))
                    else:
                        version = (self._seen.get((kind, key)) or 0) + 1
                        client.put(_to_entity(client, kind, key, rec, version, self._repo._ns))
        except (gexc.Aborted, gexc.Conflict) as exc:
            raise Conflict(str(exc)) from exc

    async def get(self, kind: str, key: Key) -> Record | None:
        k = (kind, tuple(key))
        if k in self._staged:
            return copy.deepcopy(self._staged[k])
        data, version = await self._repo._run(lambda: self._repo._read_sync(kind, key))
        self._seen.setdefault(k, version)
        return data

    async def upsert(self, kind: str, key: Key, merge_fn: MergeFn) -> UpsertResult:
        old = await self.get(kind, key)
        new = merge_fn(copy.deepcopy(old))
        result = classify(old, new)
        if result in ("created", "updated"):
            assert new is not None
            check_record(new)
            self._staged[(kind, tuple(key))] = copy.deepcopy(new)
        return result

    async def delete(self, kind: str, key: Key) -> None:
        await self.get(kind, key)
        self._staged[(kind, tuple(key))] = None

    async def add_outbox(self, key: Key, record: Record) -> None:
        check_record(record)
        await self.get("Outbox", key)
        self._staged[("Outbox", tuple(key))] = copy.deepcopy(record)
