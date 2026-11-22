"""The whole framework with HubSpot as the connector: Dialpad webhook in, HubSpot call out."""

from __future__ import annotations

from fake_hubspot_api import FakeHubSpot
from framework_sanic.testing import Harness, call_event
from framework_sanic_hubspot import REFERENCE_PROPERTY


async def test_call_reaches_hubspot(harness: Harness, api: FakeHubSpot) -> None:
    api.token = "ext-company"  # the company credential connect_company() stores
    contact = api.add_contact("Ada", "+14155550100")
    await harness.connect_company()
    for state in ("connected", "hangup"):
        resp = await harness.webhook("c1", call_event("4242", state))
        assert resp.status == 200
    await harness.drain()
    calls = list(api.objects["calls"].values())
    assert len(calls) == 1 and calls[0][REFERENCE_PROPERTY] == "dialpad:c1:call:4242"
    assert calls[0]["hs_call_direction"] == "INBOUND"
    call_id = next(iter(api.objects["calls"]))
    assert ("calls", call_id, "contacts", contact) in api.associations  # matched by phone
