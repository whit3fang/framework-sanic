"""The connector contract suite (``framework_sanic.testing.connector_contract``) on the fake CRM.

Real connectors run the same suite in their own package (e.g. ``integrations/hubspot/tests``)."""

from __future__ import annotations

from typing import Any

import pytest

from framework_sanic.stubs.connector.connector import FakeConnector, FakeHTTPError
from framework_sanic.testing.connector_contract import ConnectorContract


class TestFakeConnector(ConnectorContract):
    @pytest.fixture
    def connector(self) -> FakeConnector:
        return FakeConnector()

    @pytest.fixture
    def seed_contact(self, connector: FakeConnector) -> Any:
        def seed(name: str, phone: str) -> str:
            return connector.seed_contact(name, phone).external_id

        return seed

    @pytest.fixture
    def record_count(self, connector: FakeConnector) -> Any:
        def count(kind: str) -> int:
            if kind == "note":
                return max((len(r.notes) for r in connector.records.values()), default=0)
            return len(connector.records_for(kind))

        return count

    @pytest.fixture
    def fail_with(self, connector: FakeConnector) -> Any:
        def fail(status: int, retry_after: float | None) -> None:
            connector.fail_next[:] = [FakeHTTPError(status, retry_after)] * 3

        return fail
