"""Connector contract suite. Every connector must pass it (fake: in-memory; hubspot: against an
HTTP mock of the HubSpot API that mimics the endpoints the connector uses)."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from framework_sanic.connectors.hubspot.connector import REFERENCE_PROPERTY, HubSpotConnector
from framework_sanic.core.domain.entities import EntityRef
from framework_sanic.core.ports.connector import (
    Connector,
    ConnectorCredential,
    ConnectorError,
    EntityQuery,
    NewEntity,
)
from framework_sanic.core.ports.platform import OAuthTokens
from framework_sanic.core.services.phone import phone_variants
from framework_sanic.features.call_logging.models import CallActivity, Note
from framework_sanic.features.call_logging.port import CallLogger
from framework_sanic.features.sms_logging.models import SmsActivity, SmsLog
from framework_sanic.features.sms_logging.port import SmsLogger
from framework_sanic.stubs.connector.connector import FakeConnector

CRED = ConnectorCredential(scope="company", tokens=OAuthTokens(access_token="tok", extra={"portal_id": "42"}))


class FakeHubSpot:
    """Tiny in-memory HubSpot API: objects, search by EQ filters, properties, default associations."""

    def __init__(self) -> None:
        self.objects: dict[str, dict[str, dict[str, Any]]] = {
            "contacts": {},
            "calls": {},
            "communications": {},
            "notes": {},
        }
        self.properties: set[str] = set()
        self.associations: set[tuple[str, str, str, str]] = set()
        self.next_id = 100
        self.fail: list[int] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if self.fail:
            status = self.fail.pop(0)
            return httpx.Response(status, headers={"Retry-After": "7"} if status == 429 else {})
        assert request.headers["Authorization"] == "Bearer tok"
        parts = request.url.path.strip("/").split("/")
        body = json.loads(request.content) if request.content else {}
        if parts[:3] == ["crm", "v3", "properties"]:
            obj = parts[3]
            if request.method == "GET":
                return httpx.Response(200 if f"{obj}:{parts[4]}" in self.properties else 404, json={})
            self.properties.add(f"{obj}:{body['name']}")
            return httpx.Response(201, json=body)
        if parts[:2] == ["crm", "v4"]:
            self.associations.add((parts[3], parts[4], parts[7], parts[8]))
            return httpx.Response(200, json={})
        obj = parts[3]
        store = self.objects[obj]
        if len(parts) == 5 and parts[4] == "search":
            results = []
            for oid, props in store.items():
                for group in body.get("filterGroups", []):
                    if all(props.get(f["propertyName"]) == f["value"] for f in group["filters"]):
                        results.append({"id": oid, "properties": props})
                        break
            return httpx.Response(200, json={"results": results[: body.get("limit", 10)]})
        if request.method == "POST":
            self.next_id += 1
            oid = str(self.next_id)
            store[oid] = dict(body["properties"])
            return httpx.Response(201, json={"id": oid, "properties": store[oid]})
        oid = parts[4]
        if oid not in store:
            return httpx.Response(404, json={"message": "not found"})
        if request.method == "PATCH":
            store[oid].update(body["properties"])
        return httpx.Response(200, json={"id": oid, "properties": store[oid]})


def make_fake() -> tuple[Connector, Callable[[str, str], Any]]:
    c = FakeConnector()

    def seed(name: str, phone: str) -> Any:
        return c.seed_contact(name, phone).ref

    return c, seed


def make_hubspot() -> tuple[Connector, Callable[[str, str], Any]]:
    api = FakeHubSpot()
    http = httpx.AsyncClient(transport=httpx.MockTransport(api.handler))
    c = HubSpotConnector(client_id="id", client_secret="s", api_base="https://api.test", http=http)
    c._api = api  # type: ignore[attr-defined]

    def seed(name: str, phone: str) -> Any:
        api.next_id += 1
        api.objects["contacts"][str(api.next_id)] = {"firstname": name, "phone": phone}
        return EntityRef(object_type="contact", id=str(api.next_id))

    return c, seed


@pytest.fixture(params=["fake", "hubspot"])
def connector(request: pytest.FixtureRequest) -> tuple[Connector, Callable[[str, str], Any]]:
    return make_fake() if request.param == "fake" else make_hubspot()


def call(call_id: str = "9", **kw: Any) -> CallActivity:
    return CallActivity(
        company_id="c1",
        call_id=call_id,
        state="hangup",
        terminal=True,
        direction="inbound",
        external_number="+14155550100",
        internal_number="+14155550199",
        duration_ms=61_000,
        started_at=1_700_000_000_000,
        **kw,
    )


async def test_manifest(connector: tuple[Connector, Any]) -> None:
    c, _ = connector
    m = c.ui_manifest()
    assert m.connector == c.name and m.fields


async def test_search_by_phone_variants(connector: tuple[Connector, Any]) -> None:
    c, seed = connector
    ref = seed("Ada", "+14155550100")
    found = await c.search_entity(CRED, EntityQuery(phone_variants=phone_variants("(415) 555-0100")))
    assert [e.ref.id for e in found] == [ref.id]
    entity = await c.get_entity(CRED, ref)
    assert entity.ref == ref


async def test_create_entity(connector: tuple[Connector, Any]) -> None:
    c, _ = connector
    e = await c.create_entity(
        CRED, NewEntity(properties={"firstname": "Bo", "name": "Bo", "phone": "+15550001"})
    )
    assert e.ref.id


# --- feature ports: call_logging's CallLogger, sms_logging's SmsLogger (both connectors support both)


async def test_call_idempotency_via_reference(connector: tuple[Connector, Any]) -> None:
    c, seed = connector
    assert isinstance(c, CallLogger)  # call_logging port
    contact = seed("Ada", "+14155550100")
    act = call(external_contact_ref=contact)
    assert await c.find_by_reference(CRED, act.activity_key) is None
    ref = await c.upsert_call(CRED, act)
    assert await c.find_by_reference(CRED, act.activity_key) == ref
    ref2 = await c.upsert_call(CRED, act.model_copy(update={"external_ref": ref, "duration_ms": 99_000}))
    assert ref2 == ref
    await c.attach_recording(CRED, ref, "https://dialpad.example/rec/1")
    await c.add_note(
        CRED,
        ref,
        Note(
            company_id="c1", note_id="note-00001", call_id="9", author_user_id="u", body="hi", created_at=1.0
        ),
    )
    await c.add_note(
        CRED,
        ref,
        Note(
            company_id="c1", note_id="note-00001", call_id="9", author_user_id="u", body="hi", created_at=1.0
        ),
    )
    if isinstance(c, HubSpotConnector):
        api: FakeHubSpot = c._api  # type: ignore[attr-defined]
        assert len(api.objects["calls"]) == 1 and len(api.objects["notes"]) == 1
        props = api.objects["calls"][ref.id]
        assert props[REFERENCE_PROPERTY] == act.activity_key
        assert props["hs_call_duration"] == 99_000 and props["hs_call_recording_url"].endswith("/rec/1")
        assert ("calls", ref.id, "contacts", contact.id) in api.associations


async def test_sms_reference(connector: tuple[Connector, Any]) -> None:
    c, _ = connector
    assert isinstance(c, SmsLogger)  # sms_logging port
    act = SmsActivity(company_id="c1", sms_id=77, direction="inbound", from_number="+1415")
    ref = await c.log_sms(CRED, SmsLog(activity=act, text="hello"))
    assert await c.find_by_reference(CRED, act.activity_key) == ref


async def test_map_error(connector: tuple[Connector, Any]) -> None:
    c, _ = connector
    err = c.map_error(RuntimeError("x"))
    assert isinstance(err, ConnectorError)
    if isinstance(c, HubSpotConnector):
        api: FakeHubSpot = c._api  # type: ignore[attr-defined]
        for status, kind in ((401, "auth"), (429, "rate_limited"), (503, "transient"), (400, "permanent")):
            api.fail.append(status)
            with pytest.raises(httpx.HTTPStatusError) as exc:
                await c.get_entity(CRED, EntityRef(object_type="contact", id="1"))
            mapped = c.map_error(exc.value)
            assert mapped.kind == kind
            if kind == "rate_limited":
                assert mapped.retry_after == 7.0
