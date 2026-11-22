"""SQLAlchemy (async) Repository.

One generic table holds every kind. Atomic upserts use optimistic concurrency on a ``version``
column (insert relies on the primary key, update on ``WHERE version = :old``), which works on
PostgreSQL, MySQL and SQLite. SQLite additionally serializes writers in-process because it
cannot upgrade concurrent read transactions to write transactions.

Fields: ``kind``, ``company_id`` and ``expires_at`` are real columns; every other field is read
from the JSON document with a typed expression: ``json_extract(data, '$.f')`` on SQLite (native
SQL types) and ``data -> 'f'`` on PostgreSQL (``data`` is JSONB there; JSONB compares numbers as
numbers and strings as strings). Conditions also check the JSON type, so values of different
types never match (the ``find`` contract).

Indexes: ``setup`` creates ``(kind, company_id)`` and ``(kind, expires_at)``, plus one expression
index per declared feature index (``ctx.declare_index``), e.g. ``(kind, company_id,
json_extract(data, '$.status'), json_extract(data, '$.due'))``. Queries build exactly the same
expressions, so the database uses those indexes for filtering and sorting. Field names are
checked to be plain identifiers before they reach any SQL.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import logging
from collections.abc import AsyncIterator, Sequence
from types import TracebackType
from typing import Any, Self

from sqlalchemy import (
    JSON,
    Column,
    ColumnElement,
    Float,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    and_,
    cast,
    delete,
    false,
    func,
    insert,
    literal,
    literal_column,
    or_,
    select,
    text,
    update,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from framework_sanic.core.domain.errors import Conflict
from framework_sanic.core.domain.records import Key, Record, UpsertResult, key_to_str, stable_hash
from framework_sanic.core.ports.repository import (
    Condition,
    IndexSpec,
    MergeFn,
    Scalar,
    StorageSchema,
    check_field,
    check_find,
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
    Column("data", JSON().with_variant(JSONB(), "postgresql"), nullable=False),
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
            for spec in dict.fromkeys(schema.indexes):
                await conn.execute(text(self._index_ddl(spec)))

    # --- typed field expressions (shared by queries and index DDL, so indexes match) ----------
    def _field_sql(self, name: str) -> str:
        check_field(name)
        if name == "company_id":
            return "company_id"
        return f"json_extract(data, '$.{name}')" if self._sqlite else f"(data -> '{name}')"

    def _index_ddl(self, spec: IndexSpec) -> str:
        parts = ["kind"]
        for f in spec.fields:
            name = f.removeprefix("-")
            parts.append(self._field_sql(name) + (" DESC" if f.startswith("-") else ""))
        name = f"ix_rec_{stable_hash(spec.kind, spec.fields, length=20)}"
        return f"CREATE INDEX IF NOT EXISTS {name} ON records ({', '.join(parts)})"

    def _field(self, name: str) -> ColumnElement[Any]:
        check_field(name)
        if name == "company_id":
            return records.c.company_id
        if self._sqlite:
            return func.json_extract(records.c.data, literal_column(f"'$.{name}'"))
        return records.c.data.op("->")(literal_column(f"'{name}'"))

    def _type_is(self, name: str, kinds: tuple[str, ...]) -> ColumnElement[bool]:
        """The field holds one of ``kinds`` ("str", "num", "bool")."""
        if name == "company_id":
            return literal(True) if "str" in kinds else false()
        if self._sqlite:
            names = {"str": ("text",), "num": ("integer", "real"), "bool": ("true", "false")}
            json_type = func.json_type(records.c.data, literal_column(f"'$.{name}'"))
        else:
            names = {"str": ("string",), "num": ("number",), "bool": ("boolean",)}
            json_type = func.jsonb_typeof(self._field(name))
        return json_type.in_([t for k in kinds for t in names[k]])

    def _value(self, name: str, value: Scalar) -> Any:
        if name == "company_id" or self._sqlite:
            return int(value) if isinstance(value, bool) else value  # SQLite stores JSON true as 1
        import json

        return cast(literal(json.dumps(value)), JSONB)

    def _cond(self, condition: Condition) -> ColumnElement[bool]:
        name, op, value = condition
        kind = "bool" if isinstance(value, bool) else "num" if isinstance(value, (int, float)) else "str"
        expr, rhs = self._field(name), self._value(name, value)
        compare = {
            "==": expr == rhs,
            "<": expr < rhs,
            "<=": expr <= rhs,
            ">": expr > rhs,
            ">=": expr >= rhs,
        }[op]
        return and_(self._type_is(name, (kind,)), compare)

    def _live(self, kind: str, where: Sequence[Condition]) -> list[ColumnElement[bool]]:
        now = self._clock.now()
        return [
            records.c.kind == kind,
            or_(records.c.expires_at.is_(None), records.c.expires_at > now),
            *(self._cond(c) for c in where),
        ]

    async def find(
        self,
        kind: str,
        *,
        where: Sequence[Condition] = (),
        order_by: Sequence[str] = (),
        limit: int = 100,
        offset: int = 0,
    ) -> list[Record]:
        check_find(where, order_by, limit, offset)
        conds = self._live(kind, where)
        order: list[Any] = []
        for o in order_by:
            name = o.removeprefix("-")
            conds.append(self._type_is(name, ("str", "num", "bool")))  # no value: left out
            order.append(self._field(name).desc() if o.startswith("-") else self._field(name).asc())
        stmt = (
            select(records.c.data)
            .where(and_(*conds))
            .order_by(*order, records.c.key.asc())
            .limit(limit)
            .offset(offset)
        )
        async with self._engine.connect() as conn:
            return [row[0] for row in (await conn.execute(stmt)).all()]

    async def count(self, kind: str, *, where: Sequence[Condition] = ()) -> int:
        check_find(where, (), 1, 0)
        stmt = select(func.count()).select_from(records).where(and_(*self._live(kind, where)))
        async with self._engine.connect() as conn:
            return int((await conn.execute(stmt)).scalar_one())

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
            removed = 0
            for kind, key in rows:
                res = await conn.execute(
                    delete(records).where(
                        and_(
                            records.c.kind == kind,
                            records.c.key == key,
                            records.c.expires_at <= now,  # unless its expiry was pushed back meanwhile
                        )
                    )
                )
                removed += res.rowcount
            return removed

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
