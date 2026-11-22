"""Gate 4: Dialpad adapter and auth (admin OAuth, CAS refresh, fallback credentials, IdTokenVerifier)."""

from __future__ import annotations

import asyncio
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from framework_sanic.adapters.dialpad.id_token import DialpadIdTokenVerifier
from framework_sanic.adapters.dialpad.platform import DialpadPlatform
from framework_sanic.core.domain.entities import DialpadCredential
from framework_sanic.core.domain.errors import NotConnected, Unauthorized
from framework_sanic.core.ports.platform import PlatformIdentity
from framework_sanic.core.ports.queue import TOPIC_CONTROL
from framework_sanic.stubs.dialpad_tokens import make_dev_id_token
from framework_sanic.testing import Harness


async def test_admin_connect_flow(harness: Harness) -> None:
    h = harness
    r = await h.request("GET", "/oauth/dialpad/start")
    assert r.status == 302
    location = urlparse(r.headers["Location"])
    assert location.path == "/oauth/dialpad/callback"
    q = {k: v[0] for k, v in parse_qs(location.query).items()}

    # state is single use and validated
    bad = await h.request("GET", "/oauth/dialpad/callback", query={"code": q["code"], "state": "forged"})
    assert bad.status == 401

    r = await h.request("GET", "/oauth/dialpad/callback", query=q)
    assert r.status == 200 and b"connected" in r.body
    replay = await h.request("GET", "/oauth/dialpad/callback", query=q)
    assert replay.status == 401

    creds = await h.services.dialpad_credentials.ordered("c1")
    assert len(creds) == 1 and creds[0].status == "ok"
    rec = await h.repo.get(DialpadCredential.KIND, ("c1", "c1-u0"))
    assert rec is not None and not rec["access_token_enc"].startswith("at-")  # encrypted at rest
    assert any(m.topic == TOPIC_CONTROL and m.job == "reconcile" for m in h.queue.published)

    session = await h.request("GET", "/session", headers={"X-Dialpad-Id-Token": h.id_token()})
    assert b'"state":"needs_external_connect"' in session.body

    # external connect (user scope) -> ready
    token = __import__("json").loads(session.body)["session_token"]
    r = await h.request("GET", "/oauth/external/start", headers={"Authorization": f"Bearer {token}"})
    url = urlparse(__import__("json").loads(r.body)["authorize_url"])
    q2 = {k: v[0] for k, v in parse_qs(url.query).items()}
    r = await h.request("GET", "/oauth/external/callback", query=q2)
    assert r.status == 200
    session = await h.request("GET", "/session", headers={"Authorization": f"Bearer {token}"})
    assert b'"state":"ready"' in session.body


async def test_non_admin_cannot_connect(harness: Harness) -> None:
    h = harness
    h.platform.next_identity = PlatformIdentity(company_id="c9", user_id="c9-u5", is_admin=False)
    r = await h.request("GET", "/oauth/dialpad/start")
    q = {k: v[0] for k, v in parse_qs(urlparse(r.headers["Location"]).query).items()}
    assert (await h.request("GET", "/oauth/dialpad/callback", query=q)).status == 403


async def test_company_credential_requires_admin(harness: Harness) -> None:
    h = harness
    await h.connect_company()
    r = await h.request(
        "GET",
        "/oauth/external/start",
        query={"scope": "company"},
        headers={"X-Dialpad-Id-Token": h.id_token(user_id="c1-u2", is_admin=False)},
    )
    assert r.status == 403


async def test_refresh_uses_compare_and_set(harness: Harness) -> None:
    h = harness
    await h.connect_company()
    h.clock.advance(4000)  # access token expired
    h.cache.down = True  # no lease fast path: correctness must come from CAS
    results = await asyncio.gather(*(h.services.dialpad_credentials.access_token("c1") for _ in range(5)))
    tokens = {tok for _, tok in results}
    creds = await h.services.dialpad_credentials.ordered("c1")
    assert creds[0].version >= 2
    # every caller got a token the platform accepts
    for t in tokens:
        assert await h.platform.probe(t)


async def test_refresh_single_with_lease(harness: Harness) -> None:
    h = harness
    await h.connect_company()
    h.clock.advance(4000)
    await asyncio.gather(*(h.services.dialpad_credentials.access_token("c1") for _ in range(5)))
    assert h.platform.refresh_count == 1


async def test_fallback_to_next_credential(harness: Harness) -> None:
    h = harness
    await h.connect_company()
    h.clock.advance(1)
    second = PlatformIdentity(company_id="c1", user_id="c1-u1", is_admin=True)
    await h.services.dialpad_credentials.connect(second, h.platform.issue_tokens(second))
    ordered = await h.services.dialpad_credentials.ordered("c1")
    assert [c.credential_id for c in ordered] == ["c1-u0", "c1-u1"]  # primary then fallback

    # revoke the primary: gateway probes it, marks it bad and falls through
    primary_token = (await h.services.token_store.dialpad_tokens(ordered[0])).access_token
    h.platform.revoked.add(primary_token)
    targets = await h.services.gateway.call("c1", lambda t: h.platform.list_targets(t, "c1"))
    assert targets
    ordered = await h.services.dialpad_credentials.ordered("c1")
    assert {c.credential_id: c.status for c in ordered} == {"c1-u0": "bad", "c1-u1": "ok"}

    # all bad -> NotConnected
    fallback = next(c for c in ordered if c.credential_id == "c1-u1")
    h.platform.revoked.add((await h.services.token_store.dialpad_tokens(fallback)).access_token)
    with pytest.raises(NotConnected):
        await h.services.gateway.call("c1", lambda t: h.platform.list_targets(t, "c1"))
    with pytest.raises(NotConnected):
        await h.services.dialpad_credentials.access_token("c1")


async def test_id_token_verifier() -> None:
    v = DialpadIdTokenVerifier("s" * 32)
    claims = await v.verify(make_dev_id_token("s" * 32, company_id="c1", user_id="u1", is_admin=False))
    assert (claims.company_id, claims.user_id, claims.is_admin) == ("c1", "u1", False)
    with pytest.raises(Unauthorized):
        await v.verify(make_dev_id_token("x" * 32, company_id="c1", user_id="u1", is_admin=False))
    with pytest.raises(Unauthorized):
        await v.verify(make_dev_id_token("s" * 32, company_id="c1", user_id="u1", is_admin=False, ttl=-10))


async def test_real_dialpad_adapter_against_mock_api() -> None:
    """The httpx adapter speaks the documented endpoints (mocked transport)."""
    seen: list[tuple[str, str]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append((req.method, req.url.path))
        path = req.url.path
        if path == "/oauth2/token":
            return httpx.Response(200, json={"access_token": "a", "refresh_token": "r", "expires_in": 3600})
        if path == "/api/v2/users/me":
            return httpx.Response(200, json={"id": 5, "company_id": 9, "is_admin": True, "emails": ["a@x"]})
        if path == "/api/v2/company":
            return httpx.Response(200, json={"id": 9, "name": "Co"})
        if path == "/api/v2/offices":
            return httpx.Response(200, json={"items": [{"id": 1, "name": "HQ"}]})
        if path == "/api/v2/users":
            if req.url.params.get("cursor"):
                return httpx.Response(200, json={"items": [{"id": 6, "office_id": 1}]})
            return httpx.Response(200, json={"items": [{"id": 5, "office_id": 1}], "cursor": "n"})
        if path in ("/api/v2/departments", "/api/v2/callcenters"):
            return httpx.Response(404)
        if path == "/api/v2/webhooks":
            if req.method == "GET":
                return httpx.Response(200, json={"items": []})
            return httpx.Response(200, json={"id": 77, "hook_url": "h"})
        if path == "/api/v2/subscriptions/call":
            if req.method == "GET":
                return httpx.Response(
                    200,
                    json={
                        "items": [
                            {
                                "id": 3,
                                "target_type": "user",
                                "target_id": 5,
                                "webhook": {"id": 77},
                                "call_states": ["hangup"],
                                "enabled": True,
                            }
                        ]
                    },
                )
            return httpx.Response(200, json={"id": 4, **__import__("json").loads(req.content)})
        if path == "/api/v2/subscriptions/call/3" and req.method == "DELETE":
            return httpx.Response(200)
        return httpx.Response(429, headers={"Retry-After": "3"})

    p = DialpadPlatform(
        base_url="https://dp.test",
        client_id="cid",
        client_secret="sec",
        scopes=["calls:list"],
        client=httpx.AsyncClient(base_url="https://dp.test", transport=httpx.MockTransport(handler)),
    )
    url = p.authorize_url(state="s", code_challenge="c", redirect_uri="https://app/cb")
    assert "code_challenge_method=S256" in url and "client_id=cid" in url
    tokens = await p.exchange_code(code="x", code_verifier="v", redirect_uri="https://app/cb")
    ident = await p.identify(tokens.access_token)
    assert (ident.company_id, ident.user_id, ident.is_admin) == ("9", "5", True)
    targets = await p.list_targets("a", "9")
    assert {(t.target_type, t.target_id, t.parent_id) for t in targets} >= {
        ("office", "1", "9"),
        ("user", "5", "1"),
        ("user", "6", "1"),
    }
    assert await p.ensure_webhook("a", hook_url="h", secret="s") == "77"
    subs = await p.list_subscriptions("a", "call")
    assert subs[0].endpoint_id == "77" and subs[0].target_id == "5"
    await p.delete_subscription("a", "call", "3")
    from framework_sanic.core.ports.platform import PlatformRateLimited

    with pytest.raises(PlatformRateLimited) as exc:
        await p.list_subscriptions("a", "sms")
    assert exc.value.retry_after == 3.0
