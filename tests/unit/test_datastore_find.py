"""Datastore find/count: how the contract is translated to Datastore queries (stub client; the
real backend runs the contract suite against the emulator when DATASTORE_EMULATOR_HOST is set)."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

pytest.importorskip("google.cloud.datastore")
from framework_sanic.adapters.storage.datastore.repository import DatastoreRepository, index_yaml  # noqa: E402
from framework_sanic.core.ports.repository import IndexSpec, StorageSchema  # noqa: E402
from framework_sanic.testing import FakeClock  # noqa: E402


class StubQuery:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.filters: list[tuple[str, str, Any]] = []
        self.order: list[str] = []
        self.fetched: dict[str, Any] = {}
        self.rows = rows

    def add_filter(self, *, filter: Any) -> None:  # noqa: A002 - the client's keyword
        self.filters.append((filter.property_name, filter.operator, filter.value))

    def fetch(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.fetched = kwargs
        return [{"_json": json.dumps(r), "_v": 1} for r in self.rows]


class StubClient:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.query_made = StubQuery(rows)

    def query(self, **kwargs: Any) -> StubQuery:
        return self.query_made

    def aggregation_query(self, q: StubQuery) -> Any:
        return SimpleNamespace(
            count=lambda alias: SimpleNamespace(fetch=lambda: [[SimpleNamespace(value=7)]])
        )


async def test_find_translates_filters_order_and_paging() -> None:
    clock = FakeClock(1000.0)
    rows = [
        {"schema_version": 1, "company_id": "c1", "due": 5.0},
        {"schema_version": 1, "company_id": "c1", "due": 6.0, "expires_at": 10.0},  # expired
    ]
    client = StubClient(rows)
    repo = DatastoreRepository("p", client=client, clock=clock)
    got = await repo.find(
        "Task", where=[("company_id", "==", "c1"), ("due", ">=", 1.0)], order_by=["-due"], limit=20, offset=40
    )
    q = client.query_made
    assert q.filters == [("company_id", "=", "c1"), ("due", ">=", 1.0)]
    assert q.order == ["-due"] and q.fetched == {"limit": 20, "offset": 40}
    assert got == [rows[0]]  # the expired record is dropped
    assert await repo.count("Task", where=[("company_id", "==", "c1")]) == 7


def test_index_yaml_marks_descending_fields() -> None:
    schema = StorageSchema(kinds=("Task",), indexes=(IndexSpec("Task", ("company_id", "-due")),))
    assert index_yaml(schema) == (
        "indexes:\n- kind: Task\n  properties:\n  - name: company_id\n  - name: due\n    direction: desc\n"
    )
