"""pytest plugin (auto-loaded through the ``pytest11`` entry point): the ``harness`` fixture.

Integrations customise it by overriding two fixtures in their ``conftest.py``::

    @pytest.fixture
    def harness_connector() -> Connector:
        return AcmeConnector(base_url="https://api.test", http=mock_client)

    @pytest.fixture
    def harness_settings() -> dict[str, Any]:
        return {"FEATURES": "call_logging,acme.tickets"}
"""

from __future__ import annotations

from typing import Any

import pytest
import pytest_asyncio

from framework_sanic.bootstrap import startup

from .harness import Harness, build_harness


@pytest.fixture
def harness_connector() -> Any:
    """The connector the harness runs with; None means the fake CRM."""
    return None


@pytest.fixture
def harness_settings() -> dict[str, Any]:
    """Settings overrides for the harness (see ``make_settings`` for the defaults)."""
    return {}


@pytest_asyncio.fixture
async def harness(harness_connector: Any, harness_settings: dict[str, Any]) -> Harness:
    h = build_harness(connector=harness_connector, **harness_settings)
    await startup(h.services, schedules=False)
    return h
