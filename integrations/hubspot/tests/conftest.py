from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).parent))  # tests/ helpers (fake_hubspot_api) without a package

from fake_hubspot_api import FakeHubSpot  # noqa: E402
from framework_sanic.sdk import ConnectorCredential, OAuthTokens  # noqa: E402
from framework_sanic_hubspot import HubSpotConnector  # noqa: E402

CRED = ConnectorCredential(scope="company", tokens=OAuthTokens(access_token="tok", extra={"portal_id": "42"}))


@pytest.fixture
def api() -> FakeHubSpot:
    return FakeHubSpot()


@pytest.fixture
def hubspot(api: FakeHubSpot) -> HubSpotConnector:
    return HubSpotConnector(client_id="id", client_secret="s", api_base="https://api.test", http=api.client())


@pytest.fixture
def harness_connector(hubspot: HubSpotConnector) -> HubSpotConnector:
    """Run the framework harness with HubSpot (against the in-memory API) instead of the fake CRM."""
    return hubspot


@pytest.fixture
def harness_settings() -> dict[str, Any]:
    """... with HubSpot's listeners."""
    return {"FEATURES": "hubspot.calls,hubspot.sms,hubspot.notes"}
