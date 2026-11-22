"""HubSpot connector: the framework's connector contract plus HubSpot-specific mapping checks."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from conftest import CRED
from fake_hubspot_api import FakeHubSpot
from framework_sanic.sdk import ConnectorCredential, OAuthTokens, Settings
from framework_sanic.testing.connector_contract import (
    ConnectorContract,
    contact_map,
)
from framework_sanic_hubspot import REFERENCE_PROPERTY, HubSpotConnector, make
from samples import sample_call, sample_note


class TestHubSpotContract(ConnectorContract):
    @pytest.fixture
    def connector(self, hubspot: HubSpotConnector) -> HubSpotConnector:
        return hubspot

    @pytest.fixture
    def credential(self) -> ConnectorCredential:
        return CRED

    @pytest.fixture
    def seed_contact(self, api: FakeHubSpot) -> Any:
        return api.add_contact

    @pytest.fixture
    def fail_with(self, api: FakeHubSpot) -> Any:
        return api.fail_with


async def test_call_properties_and_associations(hubspot: HubSpotConnector, api: FakeHubSpot) -> None:
    contact = api.add_contact("Ada", "+14155550100")
    act = sample_call(contact=contact_map(contact))
    call = await hubspot.upsert_call(CRED, act, None)
    assert (call.dialpad_id, call.external_type) == (act.activity_key, "calls")
    await hubspot.upsert_call(CRED, act.model_copy(update={"duration_ms": 99_000}), call)
    await hubspot.attach_recording(CRED, call, "https://dialpad.example/rec/1")
    note = await hubspot.add_note(CRED, call, sample_note())
    assert (note.dialpad_id, note.external_type) == ("dialpad:c1:note:note-00001", "notes")
    await hubspot.add_note(CRED, call, sample_note())
    assert len(api.objects["calls"]) == 1 and len(api.objects["notes"]) == 1
    props = api.objects["calls"][call.external_id]
    assert props[REFERENCE_PROPERTY] == act.activity_key
    assert props["hs_call_duration"] == 99_000 and props["hs_call_recording_url"].endswith("/rec/1")
    assert ("calls", call.external_id, "contacts", contact) in api.associations
    assert ("notes", note.external_id, "calls", call.external_id) in api.associations


async def test_entity_link_uses_portal(hubspot: HubSpotConnector, api: FakeHubSpot) -> None:
    contact_id = api.add_contact("Ada", "+14155550100")
    entity = await hubspot.get_entity(CRED, contact_map(contact_id))
    assert (entity.external_type, entity.external_id) == ("contact", contact_id)
    assert entity.url == f"https://app.hubspot.com/contacts/42/record/0-1/{contact_id}"
    assert entity.properties["name"] == "Ada"


async def test_oauth_exchange_identifies_portal() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth/v1/token":
            assert b"client_secret=s" in request.content
            if b"grant_type=authorization_code" in request.content:
                assert b"code_verifier=v" in request.content  # PKCE
                return httpx.Response(200, json={"access_token": "at", "refresh_token": "rt"})
            return httpx.Response(200, json={"access_token": "at2", "expires_in": 60})  # no rotation
        assert request.url.path == "/oauth/v1/access-tokens/at"
        return httpx.Response(200, json={"user_id": 7, "hub_id": 42})

    c = HubSpotConnector(
        client_id="id",
        client_secret="s",
        api_base="https://api.test",
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    url = c.auth.authorize_url(state="st", code_challenge="cc", redirect_uri="https://app/cb")
    assert url.startswith("https://app.hubspot.com/oauth/authorize?") and "code_challenge=cc" in url
    tokens = await c.auth.exchange_code(code="code", code_verifier="v", redirect_uri="https://app/cb")
    assert tokens.extra == {"external_user_id": "7", "portal_id": "42"}
    refreshed = await c.auth.refresh(OAuthTokens(access_token="old", refresh_token="rt", extra=tokens.extra))
    assert refreshed.access_token == "at2" and refreshed.refresh_token == "rt"  # kept when not rotated
    assert refreshed.extra == tokens.extra


def test_entry_point_factory_reads_own_settings() -> None:
    settings = Settings(_env_file=None, HUBSPOT_CLIENT_ID="cid", HUBSPOT_PORTAL_ID="99")
    c = make(settings)
    assert c.portal_id == "99" and "client_id=cid" in c.auth.authorize_url(
        state="s", code_challenge="c", redirect_uri="https://app/cb"
    )
