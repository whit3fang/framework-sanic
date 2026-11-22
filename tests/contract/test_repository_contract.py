"""Repository contract suite (``framework_sanic.testing.repository_contract``) on every adapter.

* memory: always
* sqlalchemy: SQLite file (set TEST_DATABASE_URL to run against PostgreSQL)
* datastore: only when DATASTORE_EMULATOR_HOST is set (docker compose provides the emulator)
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio

from framework_sanic.adapters.storage.memory.repository import MemoryRepository
from framework_sanic.adapters.storage.sqlalchemy.repository import SqlAlchemyRepository
from framework_sanic.core.ports.repository import Repository
from framework_sanic.testing.repository_contract import ContractClock, RepositoryContract


class TestMemoryRepository(RepositoryContract):
    @pytest_asyncio.fixture
    async def repository(self, clock: ContractClock) -> AsyncIterator[Repository]:
        yield MemoryRepository(clock)


class TestSqlAlchemyRepository(RepositoryContract):
    @pytest_asyncio.fixture
    async def repository(self, clock: ContractClock, tmp_path: Path) -> AsyncIterator[Repository]:
        url = os.environ.get("TEST_DATABASE_URL") or f"sqlite+aiosqlite:///{tmp_path / 'contract.db'}"
        repo = SqlAlchemyRepository(url, clock=clock)
        yield repo
        await repo.close()


@pytest.mark.datastore
class TestDatastoreRepository(RepositoryContract):
    @pytest_asyncio.fixture
    async def repository(self, clock: ContractClock) -> AsyncIterator[Repository]:
        if not os.environ.get("DATASTORE_EMULATOR_HOST"):
            pytest.skip("DATASTORE_EMULATOR_HOST not set")
        from framework_sanic.adapters.storage.datastore.repository import DatastoreRepository

        yield DatastoreRepository("contract-test", namespace=f"t{uuid.uuid4().hex[:8]}", clock=clock)
