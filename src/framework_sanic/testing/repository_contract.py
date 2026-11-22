"""Reusable Repository contract suite.

Every storage adapter must pass it. To test your own adapter, subclass ``RepositoryContract`` and
override the ``repository`` fixture (it receives the controllable ``clock`` and must return a
fresh, isolated repository); ``setup`` is called for you before each test::

    from framework_sanic.testing.repository_contract import ContractClock, RepositoryContract

    class TestMyAdapter(RepositoryContract):
        @pytest_asyncio.fixture
        async def repository(self, clock: ContractClock) -> AsyncIterator[Repository]:
            repo = MyRepository(dsn, clock=clock)
            yield repo
            await repo.close()

Requires ``pytest`` and ``pytest-asyncio`` (``pip install framework-sanic[testing]``).
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio

from framework_sanic.core.domain.records import Record
from framework_sanic.core.ports.repository import (
    IndexSpec,
    MergeFn,
    Repository,
    StorageSchema,
    UnitOfWork,
    run_in_uow,
)

# Every kind the suite writes, so adapters that need the kinds up front (setup, purge) see them.
KINDS = ("Thing", "Sms", "Other", "Act", "T", "A", "Ctr", "Many", "Outbox")
SCHEMA = StorageSchema(
    kinds=KINDS,
    indexes=(IndexSpec("T", ("company_id", "enabled")), IndexSpec("Outbox", ("created_at",))),
)


class ContractClock:
    """Controllable clock passed to the adapter under test (expiry depends on it)."""

    def __init__(self, start: float = 1_700_000_000.0) -> None:
        self.t = start

    def now(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def rec(**fields: Any) -> Record:
    return {"schema_version": 1, "company_id": "c1", **fields}


def setter(value: Record | None) -> MergeFn:
    return lambda cur: value


@pytest.mark.asyncio
class RepositoryContract:
    """Subclass and override ``repository``. Test methods receive ``(repo, clock)`` as ``contract``."""

    @pytest.fixture
    def clock(self) -> ContractClock:
        return ContractClock()

    @pytest_asyncio.fixture
    async def repository(self, clock: ContractClock) -> AsyncIterator[Repository]:
        raise NotImplementedError("override the `repository` fixture in your subclass")
        yield  # pragma: no cover

    @pytest_asyncio.fixture
    async def contract(
        self, repository: Repository, clock: ContractClock
    ) -> AsyncIterator[tuple[Repository, ContractClock]]:
        assert isinstance(repository, Repository), "adapter does not implement the Repository port"
        await repository.setup(SCHEMA)
        yield repository, clock

    # --- lifecycle ------------------------------------------------------------------------------

    async def test_setup_is_idempotent(self, contract: tuple[Repository, ContractClock]) -> None:
        r, _ = contract
        await r.upsert("Thing", ("c1", "s"), setter(rec(n=1)))
        await r.setup(SCHEMA)  # a second startup must not lose data
        assert await r.get("Thing", ("c1", "s")) == rec(n=1)
        assert await r.ping()

    async def test_purge_expired(self, contract: tuple[Repository, ContractClock]) -> None:
        r, clock = contract
        for i in range(5):
            await r.upsert("Act", ("c1", f"old{i}"), setter(rec(expires_at=clock.now() + 10)))
        await r.upsert("Act", ("c1", "live"), setter(rec(expires_at=clock.now() + 1000)))
        await r.upsert("Thing", ("c1", "forever"), setter(rec()))
        assert await r.purge_expired() == 0  # nothing expired yet
        clock.advance(11)
        first = await r.purge_expired(limit=2)
        assert first == 2  # honours the limit
        rest = 0
        for _ in range(10):
            n = await r.purge_expired(limit=2)
            rest += n
            if n == 0:
                break
        assert first + rest == 5
        assert await r.get("Act", ("c1", "live")) is not None
        assert await r.get("Thing", ("c1", "forever")) is not None

    # --- behaviour ------------------------------------------------------------------------------

    async def test_get_missing(self, contract: tuple[Repository, ContractClock]) -> None:
        r, _ = contract
        assert await r.get("Thing", ("c1", "x")) is None

    async def test_upsert_lifecycle(self, contract: tuple[Repository, ContractClock]) -> None:
        r, _ = contract
        key = ("c1", "a")
        assert await r.upsert("Thing", key, setter(rec(n=1))) == "created"
        assert await r.get("Thing", key) == rec(n=1)
        assert await r.upsert("Thing", key, lambda cur: {**(cur or {}), "n": 2}) == "updated"
        assert await r.upsert("Thing", key, lambda cur: cur) == "unchanged"
        assert await r.upsert("Thing", key, lambda cur: None) == "ignored"
        assert (await r.get("Thing", key)) == rec(n=2)
        assert await r.upsert("Thing", ("c1", "never"), lambda cur: None) == "ignored"
        assert await r.get("Thing", ("c1", "never")) is None

    async def test_merge_fn_receives_current(self, contract: tuple[Repository, ContractClock]) -> None:
        r, _ = contract
        seen: list[Record | None] = []

        def merge(cur: Record | None) -> Record | None:
            seen.append(cur)
            return rec(n=(cur or {}).get("n", 0) + 1)

        await r.upsert("Thing", ("c1", "m"), merge)
        await r.upsert("Thing", ("c1", "m"), merge)
        assert seen[0] is None and seen[1] == rec(n=1)

    async def test_schema_version_required(self, contract: tuple[Repository, ContractClock]) -> None:
        r, _ = contract
        with pytest.raises(ValueError):
            await r.upsert("Thing", ("c1", "bad"), setter({"company_id": "c1"}))

    async def test_int_keys_and_kinds_are_separate(self, contract: tuple[Repository, ContractClock]) -> None:
        r, _ = contract
        await r.upsert("Sms", ("c1", 123), setter(rec(v="int")))
        await r.upsert("Sms", ("c1", "123"), setter(rec(v="str")))
        await r.upsert("Other", ("c1", 123), setter(rec(v="other")))
        assert (await r.get("Sms", ("c1", 123)))["v"] == "int"  # type: ignore[index]
        assert (await r.get("Other", ("c1", 123)))["v"] == "other"  # type: ignore[index]

    async def test_ttl_expiry(self, contract: tuple[Repository, ContractClock]) -> None:
        r, clock = contract
        await r.upsert("Act", ("c1", "t"), setter(rec(expires_at=clock.now() + 10)))
        assert await r.get("Act", ("c1", "t")) is not None
        clock.advance(11)
        assert await r.get("Act", ("c1", "t")) is None
        assert [x async for x in r.query("Act", company_id="c1")] == []
        # an expired record behaves as absent for upsert
        assert await r.upsert("Act", ("c1", "t"), setter(rec(expires_at=clock.now() + 10))) == "created"

    async def test_query_filters(self, contract: tuple[Repository, ContractClock]) -> None:
        r, _ = contract
        await r.upsert("T", ("c1", "1"), setter(rec(name="a", enabled=True, n=1)))
        await r.upsert("T", ("c1", "2"), setter(rec(name="b", enabled=False, n=2)))
        await r.upsert("T", ("c2", "3"), setter({**rec(name="a", enabled=True, n=1), "company_id": "c2"}))
        names = sorted([x["name"] async for x in r.query("T", company_id="c1")])
        assert names == ["a", "b"]
        assert [x["n"] async for x in r.query("T", company_id="c1", enabled=True)] == [1]
        assert len([x async for x in r.query("T", name="a")]) == 2
        assert [x["name"] async for x in r.query("T", company_id="c1", n=2)] == ["b"]
        assert [x async for x in r.query("T", company_id="nope")] == []

    async def test_delete(self, contract: tuple[Repository, ContractClock]) -> None:
        r, _ = contract
        await r.upsert("T", ("c1", "d"), setter(rec()))
        await r.delete("T", ("c1", "d"))
        assert await r.get("T", ("c1", "d")) is None
        await r.delete("T", ("c1", "d"))  # idempotent

    async def test_concurrent_upserts_are_atomic(self, contract: tuple[Repository, ContractClock]) -> None:
        r, _ = contract
        key = ("c1", "counter")

        def inc(cur: Record | None) -> Record:
            return rec(n=(cur or {}).get("n", 0) + 1)

        await asyncio.gather(*(r.upsert("Ctr", key, inc) for _ in range(30)))
        assert (await r.get("Ctr", key))["n"] == 30  # type: ignore[index]

    async def test_unit_of_work_commits_atomically(self, contract: tuple[Repository, ContractClock]) -> None:
        r, _ = contract
        async with r.unit_of_work() as uow:
            assert await uow.upsert("A", ("c1", "1"), setter(rec(v=1))) == "created"
            assert await uow.get("A", ("c1", "1")) == rec(v=1)  # reads its own writes
            await uow.add_outbox(("c1", "o1"), rec(topic="sync"))
        assert await r.get("A", ("c1", "1")) == rec(v=1)
        assert await r.get("Outbox", ("c1", "o1")) == rec(topic="sync")

    async def test_unit_of_work_rolls_back(self, contract: tuple[Repository, ContractClock]) -> None:
        r, _ = contract
        with pytest.raises(RuntimeError):
            async with r.unit_of_work() as uow:
                await uow.upsert("A", ("c1", "rb"), setter(rec(v=1)))
                await uow.add_outbox(("c1", "rb-o"), rec())
                raise RuntimeError("boom")
        assert await r.get("A", ("c1", "rb")) is None
        assert await r.get("Outbox", ("c1", "rb-o")) is None

    async def test_unit_of_work_delete(self, contract: tuple[Repository, ContractClock]) -> None:
        r, _ = contract
        await r.upsert("A", ("c1", "del"), setter(rec()))
        async with r.unit_of_work() as uow:
            await uow.delete("A", ("c1", "del"))
        assert await r.get("A", ("c1", "del")) is None

    async def test_concurrent_units_of_work_with_retry(
        self, contract: tuple[Repository, ContractClock]
    ) -> None:
        r, _ = contract
        key = ("c1", "uow-counter")

        async def work(uow: UnitOfWork) -> None:
            await uow.upsert("Ctr", key, lambda cur: rec(n=(cur or {}).get("n", 0) + 1))
            await uow.add_outbox(("c1", f"o-{uuid.uuid4().hex}"), rec())

        await asyncio.gather(*(run_in_uow(r, work, retries=50) for _ in range(10)))
        assert (await r.get("Ctr", key))["n"] == 10  # type: ignore[index]
        assert len([x async for x in r.query("Outbox", company_id="c1")]) == 10

    async def test_query_pagination(self, contract: tuple[Repository, ContractClock]) -> None:
        r, _ = contract
        for i in range(620):
            await r.upsert("Many", ("c1", f"{i:04d}"), setter(rec(i=i)))
        assert len([x async for x in r.query("Many", company_id="c1")]) == 620
