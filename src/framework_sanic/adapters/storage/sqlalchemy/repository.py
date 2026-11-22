"""SQLAlchemy (async) Repository.

One generic table holds every kind. Atomic upserts use optimistic concurrency on a ``version``
column (insert relies on the primary key, update on ``WHERE version = :old``), which works on
PostgreSQL, MySQL and SQLite. SQLite additionally serializes writers in-process because it
cannot upgrade concurrent read transactions to write transactions.

Indexes: ``kind``, ``company_id`` and ``expires_at`` are real columns. ``setup`` creates the
``(kind, company_id)`` and ``(kind, expires_at)`` indexes; declared feature indexes are served by
``(kind, company_id)`` (every framework query is company-scoped or kind-wide) with the remaining
fields filtered on the JSON document. Declared indexes that do not lead with ``company_id`` are
logged, since they cannot be served natively by this generic layout.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import logging
from collections.abc import AsyncIterator
from types import TracebackType
from typing import Any, Self

from sqlalchemy import (
    JSON,
    Column,
    Float,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    and_,
    delete,
    insert,
    select,
    update,
)
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from framework_sanic.core.domain.errors import Conflict
from framework_sanic.core.domain.records import Key, Record, UpsertResult, key_to_str
from framework_sanic.core.ports.repository import (
    MergeFn,
    StorageSchema,
    check_record,
    classify,
    is_expired,
)
from framework_sanic.core.ports.runtime import Clock, SystemClock

metadata = MetaData()
records = Table(
    "records",
    metadata,
    Column("kind", String(64), primary_key=True),
    Column("key", String(512), primary_key=True),
    Column("company_id", String(128), index=True),
    Column("data", JSON, nullable=False),
    Column("version", Integer, nullable=False),
    Column("expires_at", Float, nullable=True),
    Index("ix_records_kind_company", "kind", "company_id"),
    Index("ix_records_kind_expires", "kind", "expires_at"),
)

log = logging.getLogger(__name__)

_PAGE = 500


def _json_filter(field: str, value: Any) -> Any:
    col = records.c.data[field]
    if isinstance(value, bool):
        return col.as_boolean() == value
    if isinstance(value, int):
        return col.as_integer() == value
    if isinstance(value, float):
        return col.as_float() == value
    return col.as_string() == str(value)


class SqlAlchemyRepository:
    def __init__(self, url: str, *, clock: Clock | None = None, engine: AsyncEngine | None = None) -> None:
        self._engine = engine or create_async_engine(url)
        self._clock = clock or SystemClock()
        self._sqlite = self._engine.dialect.name == "sqlite"
        self._write_lock = asyncio.Lock()

    async def setup(self, schema: StorageSchema) -> None:
        """Create the table and indexes (idempotent)."""
        async with self._engine.begin() as conn:
            await conn.run_sync(metadata.create_all)
        for spec in schema.indexes:
            if spec.fields and spec.fields[0] != "company_id":
                log.info(
                    "index %s(%s) is served by (kind) with in-row filtering in the generic SQL layout",
                    spec.kind,
                    ", ".join(spec.fields),
                )

    @contextlib.asynccontextmanager
    async def _write(self) -> AsyncIterator[AsyncConnection]:
        if self._sqlite:
            async with self._write_lock, self._engine.begin() as conn:
                yield conn
        else:
            async with self._engine.begin() as conn:
                yield conn

    async def _get_row(self, conn: AsyncConnection, kind: str, key: Key) -> tuple[Record | None, int | None]:
        row = (
            await conn.execute(
                select(records.c.data, records.c.version).where(
                    and_(records.c.kind == kind, records.c.key == key_to_str(key))
                )
            )
        ).first()
        if row is None:
            return None, None
        data: Record = row[0]
        version: int = row[1]
        if is_expired(data, self._clock.now()):
            return None, version
        return data, version

    async def get(self, kind: str, key: Key) -> Record | None:
        async with self._engine.connect() as conn:
            data, _ = await self._get_row(conn, kind, key)
            return data

    async def _apply(self, conn: AsyncConnection, kind: str, key: Key, merge_fn: MergeFn) -> UpsertResult:
        old, version = await self._get_row(conn, kind, key)
        new = merge_fn(copy.deepcopy(old))
        result = classify(old, new)
        if result not in ("created", "updated"):
            return result
        assert new is not None
        check_record(new)
        await self._write_row(conn, kind, key, new, version)
        return result

    async def _write_row(
        self, conn: AsyncConnection, kind: str, key: Key, new: Record, version: int | None
    ) -> None:
        values = {
            "data": new,
            "company_id": str(new.get("company_id", "")),
            "expires_at": new.get("expires_at"),
        }
        if version is None:
            await conn.execute(insert(records).values(kind=kind, key=key_to_str(key), version=1, **values))
        else:
            res = await conn.execute(
                update(records)
                .where(
                    and_(
                        records.c.kind == kind, records.c.key == key_to_str(key), records.c.version == version
                    )
                )
                .values(version=version + 1, **values)
            )
            if res.rowcount != 1:
                raise Conflict(f"concurrent update of {kind} {key}")

    async def upsert(self, kind: str, key: Key, merge_fn: MergeFn) -> UpsertResult:
        for attempt in range(20):
            try:
                async with self._write() as conn:
                    return await self._apply(conn, kind, key, merge_fn)
            except (Conflict, IntegrityError):
                await asyncio.sleep(0.005 * attempt)
            except OperationalError as exc:  # SQLite "database is locked"
                if "locked" not in str(exc):
                    raise
                await asyncio.sleep(0.01 * (attempt + 1))
        raise Conflict(f"upsert of {kind} {key} kept conflicting")

    async def delete(self, kind: str, key: Key) -> None:
        async with self._write() as conn:
            await conn.execute(
                delete(records).where(and_(records.c.kind == kind, records.c.key == key_to_str(key)))
            )

    async def query(self, kind: str, **filters: Any) -> AsyncIterator[Record]:
        conds = [records.c.kind == kind]
        python_filters: dict[str, Any] = {}
        for field, value in filters.items():
            if field == "company_id":
                conds.append(records.c.company_id == str(value))
            elif value is None:
                python_filters[field] = None
            else:
                conds.append(_json_filter(field, value))
        last_key = ""
        while True:
            async with self._engine.connect() as conn:
                rows = (
                    await conn.execute(
                        select(records.c.key, records.c.data)
                        .where(and_(*conds, records.c.key > last_key))
                        .order_by(records.c.key)
                        .limit(_PAGE)
                    )
                ).all()
            now = self._clock.now()
            for _k, data in rows:
                if is_expired(data, now):
                    continue
                if all(data.get(f) == v for f, v in python_filters.items()):
                    yield data
            if len(rows) < _PAGE:
                return
            last_key = rows[-1][0]

    def unit_of_work(self) -> SqlAlchemyUnitOfWork:
        return SqlAlchemyUnitOfWork(self)

    async def ping(self) -> bool:
        try:
            async with self._engine.connect() as conn:
                await conn.execute(select(1))
            return True
        except Exception:  # noqa: BLE001
            return False

    async def purge_expired(self, *, limit: int = 1000) -> int:
        now = self._clock.now()
        async with self._write() as conn:
            rows = (
                await conn.execute(
                    select(records.c.kind, records.c.key)
                    .where(and_(records.c.expires_at.is_not(None), records.c.expires_at <= now))
                    .limit(limit)
                )
            ).all()
            for kind, key in rows:
                await conn.execute(delete(records).where(and_(records.c.kind == kind, records.c.key == key)))
            return len(rows)

    async def close(self) -> None:
        await self._engine.dispose()


class SqlAlchemyUnitOfWork:
    """One database transaction; optimistic conflicts raise Conflict and roll everything back."""

    def __init__(self, repo: SqlAlchemyRepository) -> None:
        self._repo = repo
        self._cm: contextlib.AbstractAsyncContextManager[AsyncConnection] | None = None
        self._conn: AsyncConnection | None = None

    async def __aenter__(self) -> Self:
        self._cm = self._repo._write()
        self._conn = await self._cm.__aenter__()
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        assert self._cm is not None
        try:
            await self._cm.__aexit__(exc_type, exc, tb)
        except IntegrityError as err:
            raise Conflict("unit of work conflicted on commit") from err
        if isinstance(exc, IntegrityError):
            raise Conflict("unit of work conflicted") from exc

    @property
    def conn(self) -> AsyncConnection:
        assert self._conn is not None
        return self._conn

    async def get(self, kind: str, key: Key) -> Record | None:
        data, _ = await self._repo._get_row(self.conn, kind, key)
        return data

    async def upsert(self, kind: str, key: Key, merge_fn: MergeFn) -> UpsertResult:
        try:
            return await self._repo._apply(self.conn, kind, key, merge_fn)
        except IntegrityError as exc:
            raise Conflict(str(exc)) from exc

    async def delete(self, kind: str, key: Key) -> None:
        await self.conn.execute(
            delete(records).where(and_(records.c.kind == kind, records.c.key == key_to_str(key)))
        )

    async def add_outbox(self, key: Key, record: Record) -> None:
        check_record(record)
        _, version = await self._repo._get_row(self.conn, "Outbox", key)
        try:
            await self._repo._write_row(self.conn, "Outbox", key, record, version)
        except IntegrityError as exc:
            raise Conflict(str(exc)) from exc
