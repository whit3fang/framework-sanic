"""Regression tests: external token refresh errors, dev secrets in production, one listener object
per framework instance, one contact per new caller."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from framework_sanic.bootstrap import build_services
from framework_sanic.core.domain.errors import ConfigurationError, NotConnected
from framework_sanic.core.ports.connector import ConnectorCredential, ConnectorError
from framework_sanic.core.ports.platform import OAuthTokens, TargetInfo
from framework_sanic.stubs.listeners import ReceivedCall
from framework_sanic.testing import Harness, call_event, make_settings, start_harness


async def expired_company_credential(h: Harness) -> None:
    await h.services.external_credentials.save(
        "c1", "company", "", OAuthTokens(access_token="old", refresh_token="rt", expires_in=1)
    )
    h.clock.advance(3600)


async def test_a_refresh_outage_keeps_the_credential() -> None:
    h = await start_harness(connect=False)
    await h.connect_company(external=False)
    await expired_company_credential(h)

    async def down(tokens: OAuthTokens) -> OAuthTokens:
        raise httpx.ConnectError("token endpoint unreachable")

    h.connector.auth.refresh = down  # type: ignore[method-assign]
    with pytest.raises(httpx.ConnectError):  # the job retries later
        await h.services.external_credentials.resolve("c1", None)
    stored = await h.services.token_store.get_external("c1", "company", "")
    assert stored is not None and stored.status == "ok"


async def test_a_rejected_refresh_marks_the_credential_bad() -> None:
    h = await start_harness(connect=False)
    await h.connect_company(external=False)
    await expired_company_credential(h)

    async def revoked(tokens: OAuthTokens) -> OAuthTokens:
        raise ConnectorError("auth", "invalid_grant")

    h.connector.auth.refresh = revoked  # type: ignore[method-assign]
    with pytest.raises(NotConnected):
        await h.services.external_credentials.resolve("c1", None)
    stored = await h.services.token_store.get_external("c1", "company", "")
    assert stored is not None and stored.status == "bad"


def test_real_dialpad_refuses_dev_secrets() -> None:
    with pytest.raises(ConfigurationError, match="SESSION_SECRET"):
        build_services(make_settings(PLATFORM_ADAPTER="dialpad"))
    real = {
        "DIALPAD_CLIENT_SECRET": "s" * 40,
        "WEBHOOK_SECRET_KEY": "w" * 40,
        "SESSION_SECRET": "x" * 40,
        "KMS_KEY": "projects/p/locations/l/keyRings/r/cryptoKeys/k",
    }
    assert make_settings(PLATFORM_ADAPTER="dialpad", **real).insecure_defaults() == []
    assert make_settings().insecure_defaults() == []  # the fake Dialpad (tests, dev) may use them


async def test_each_framework_instance_gets_its_own_listener() -> None:
    a = await start_harness(FEATURES="fake.calls")
    b = await start_harness(FEATURES="fake.calls")  # set up after a: must not take over a's listener
    await a.webhook("c1", call_event("5", "hangup"))
    await a.drain()
    assert await a.repo.get(ReceivedCall.KIND, ("c1", "5")) is not None
    assert await b.repo.get(ReceivedCall.KIND, ("c1", "5")) is None


async def test_one_contact_for_a_new_caller_reached_twice_at_once() -> None:
    h = await start_harness()
    cred = await h.services.external_credentials.resolve("c1", None)
    ref, number = "phone:+14155550999", "+14155550999"
    contacts = h.services.contacts
    original = h.connector.create_entity

    async def slow_create(*args, **kwargs):  # type: ignore[no-untyped-def]
        await asyncio.sleep(0.05)
        return await original(*args, **kwargs)

    h.connector.create_entity = slow_create  # type: ignore[method-assign]
    results = await asyncio.gather(
        contacts.resolve_or_create("c1", cred, ref, number, create_if_unknown=True),
        contacts.resolve_or_create("c1", cred, ref, number, create_if_unknown=True),
        return_exceptions=True,
    )
    created = [r for r in results if not isinstance(r, BaseException)]
    busy = [r for r in results if isinstance(r, ConnectorError)]
    assert len(created) == 1 and len(busy) == 1 and busy[0].kind == "transient"  # the loser retries
    retried = await contacts.resolve_or_create("c1", cred, ref, number, create_if_unknown=True)
    assert retried is not None and retried.external_id == created[0].external_id  # type: ignore[union-attr]
    assert len(h.connector.contacts) == 1


# --- second audit ------------------------------------------------------------------------------
async def test_rematch_cannot_point_a_logged_activity_elsewhere() -> None:
    h = await start_harness()
    token = json.loads((await h.request("GET", "/session", headers=id_token(h))).body)["session_token"]
    for ref in ("dialpad:c1:call:1001", "phone:+1:x", "anything"):
        body = {
            "dialpad_ref": ref,
            "external_id": "999",
            "external_type": "calls",
            "confirm_forward_only": True,
        }
        r = await h.request(
            "POST", "/contact/rematch", headers={"Authorization": f"Bearer {token}"}, body=json.dumps(body)
        )
        assert r.status == 422, ref
    assert await h.services.entity_maps.get("c1", "dialpad:c1:call:1001") is None


def id_token(h: Harness) -> dict[str, str]:
    return {"X-Dialpad-Id-Token": h.id_token(user_id="c1-u1", is_admin=False)}


async def test_a_session_cannot_be_renewed_past_its_max_age() -> None:
    h = await start_harness(SESSION_MAX_AGE_SECONDS=3600)

    async def renew(token: str) -> Any:
        return await h.request("GET", "/session", headers={"Authorization": f"Bearer {token}"})

    first = json.loads((await h.request("GET", "/session", headers=id_token(h))).body)
    h.clock.advance(600)
    second = await renew(first["session_token"])
    assert second.status == 200  # renewing within the limit works
    h.clock.advance(600)
    token = json.loads(second.body)["session_token"]
    for _ in range(4):  # keep renewing every 10 minutes...
        r = await renew(token)
        if r.status != 200:
            break
        token = json.loads(r.body)["session_token"]
        h.clock.advance(600)
    assert r.status == 401  # ...until an hour after the id_token login
    assert (await h.request("GET", "/session", headers=id_token(h))).status == 200  # a fresh login works


async def test_an_admin_change_survives_a_concurrent_recompute() -> None:
    h = await start_harness()
    targets = h.services.targets
    infos = [
        TargetInfo(target_type="office", target_id="o1"),
        TargetInfo(target_type="user", target_id="u1", parent_type="office", parent_id="o1"),
    ]
    await targets.sync_from_platform("c1", infos)
    real_all = targets.all
    calls = 0

    async def slow_first_read(company_id: str, **kw: Any) -> Any:
        nonlocal calls
        calls += 1
        result = await real_all(company_id, **kw)
        if calls == 1:
            await asyncio.sleep(0.1)  # a background recompute holding an old snapshot
        return result

    # Admin 1 turned the office off; its recompute read that snapshot (user -> off) and is slow.
    await h.repo.upsert(
        "Target", ("c1", "office", "o1"), lambda cur: {**cur, "override": False} if cur else None
    )
    targets.all = slow_first_read  # type: ignore[method-assign]
    background = asyncio.create_task(targets.recompute("c1"))
    await asyncio.sleep(0.01)
    await targets.set_override("c1", "office", "o1", None)  # admin 2 turns it back on meanwhile
    await background
    user = await targets.get("c1", "user", "u1")
    assert user is not None and user.effective_enabled is True  # not the stale "off"


async def test_creating_the_reference_property_twice_is_fine() -> None:
    from framework_sanic_hubspot import HubSpotConnector

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(404)  # "missing" for every worker
        return httpx.Response(409, json={"message": "Property already exists"})  # another one won

    connector = HubSpotConnector(
        client_id="id",
        client_secret="s",
        api_base="https://api.test",
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    cred = ConnectorCredential(scope="company", tokens=OAuthTokens(access_token="t"))
    await connector.references.ensure_property(cred, "calls")  # no error
