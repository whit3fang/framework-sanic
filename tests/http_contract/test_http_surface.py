"""Black-box tests over the HTTP surface (build gate 3).

Every test runs against each HTTP adapter. Today: ``sanic`` (real Sanic app over ASGI) and
``neutral`` (the framework-neutral dispatcher). A FastAPI adapter would be added to ADAPTERS.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import Any

import pytest

from framework_sanic.bootstrap import startup
from framework_sanic.http.adapters.sanic import create_app
from framework_sanic.testing import Harness, build_harness, call_event, sms_event

ADAPTERS = ["sanic", "neutral"]


@dataclass
class Resp:
    status: int
    headers: dict[str, str]
    text: str
    raw_headers: dict[str, str] = field(default_factory=dict)

    def json(self) -> Any:
        return json.loads(self.text)


class Client:
    def __init__(self, kind: str, harness: Harness) -> None:
        self.kind = kind
        self.h = harness
        if kind == "sanic":
            self.app = create_app(
                services=harness.services, name=f"t_{uuid.uuid4().hex[:10]}", embedded_workers=False
            )

    async def request(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str] | None = None,
        body: str | bytes | None = None,
        params: dict[str, str] | None = None,
    ) -> Resp:
        headers = headers or {}
        if self.kind == "neutral":
            r = await self.h.request(method, path, query=params, body=body, headers=headers)
            return Resp(r.status, {k.lower(): v for k, v in r.headers.items()}, r.body.decode())
        content = body.encode() if isinstance(body, str) else body
        _, resp = await self.app.asgi_client.request(
            method, path, headers=headers, content=content, params=params
        )
        return Resp(resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.text)


@pytest.fixture(params=ADAPTERS)
async def client(request: pytest.FixtureRequest) -> Client:
    h = build_harness()
    await startup(h.services, schedules=False)
    return Client(request.param, h)


async def session_token(c: Client, *, user: str = "c1-u0", admin: bool = True) -> str:
    r = await c.request(
        "GET", "/session", headers={"X-Dialpad-Id-Token": c.h.id_token(user_id=user, is_admin=admin)}
    )
    assert r.status == 200, r.text
    return str(r.json()["session_token"])


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def test_healthz_and_security_headers(client: Client) -> None:
    r = await client.request("GET", "/healthz")
    assert r.status == 200 and r.json() == {"status": "ok"}
    assert "frame-ancestors" in r.headers["content-security-policy"]
    assert "https://dialpad.com" in r.headers["content-security-policy"]
    assert r.headers["cache-control"] == "no-store"
    assert r.headers["x-request-id"]


async def test_readyz(client: Client) -> None:
    r = await client.request("GET", "/readyz")
    assert r.status == 200 and r.json()["ready"] is True
    client.h.queue.down = True
    assert (await client.request("GET", "/readyz")).status == 503


async def test_unknown_route_404(client: Client) -> None:
    assert (await client.request("GET", "/nope")).status == 404


async def test_webhook_signature(client: Client) -> None:
    path = "/webhooks/dialpad/c1/events"
    assert (await client.request("POST", path, body=json.dumps(call_event()))).status == 401  # unsigned
    wrong = client.h.sign("other-company", call_event())
    assert (await client.request("POST", path, body=wrong)).status == 401
    ok = await client.request("POST", path, body=client.h.sign("c1", call_event()))
    assert ok.status == 200 and ok.json()["accepted"] is True


async def test_webhook_never_4xx_for_signed_garbage(client: Client) -> None:
    r = await client.request(
        "POST", "/webhooks/dialpad/c1/events", body=client.h.sign("c1", {"what": "ever"})
    )
    assert r.status == 200
    r = await client.request(
        "POST", "/webhooks/dialpad/c1/events", body=client.h.sign("c1", {"call_id": 1, "state": "x"})
    )
    assert r.status == 200  # missing event_timestamp -> dead-lettered, still 2xx


async def test_session_requires_credentials(client: Client) -> None:
    assert (await client.request("GET", "/session")).status == 401
    assert (await client.request("GET", "/session", headers=bearer("garbage"))).status == 401
    r = await client.request("GET", "/session", headers={"X-Dialpad-Id-Token": "not-a-jwt"})
    assert r.status == 401


async def test_session_states(client: Client) -> None:
    r = await client.request(
        "GET", "/session", headers={"X-Dialpad-Id-Token": client.h.id_token(is_admin=True)}
    )
    body = r.json()
    assert body["state"] == "needs_admin_connect" and body["admin_notice"]
    r = await client.request(
        "GET", "/session", headers={"X-Dialpad-Id-Token": client.h.id_token(user_id="c1-u2", is_admin=False)}
    )
    assert r.json()["state"] == "awaiting_admin"
    await client.h.connect_company(external=False)
    token = await session_token(client)
    assert (await client.request("GET", "/session", headers=bearer(token))).json()[
        "state"
    ] == "needs_external_connect"
    await client.h.services.external_credentials.save(
        "c1",
        "company",
        "",
        __import__("framework_sanic.core.ports.platform", fromlist=["OAuthTokens"]).OAuthTokens(
            access_token="x"
        ),
    )
    assert (await client.request("GET", "/session", headers=bearer(token))).json()["state"] == "ready"


async def test_logout_revokes(client: Client) -> None:
    token = await session_token(client)
    assert (await client.request("POST", "/session/logout", headers=bearer(token))).status == 200
    assert (await client.request("GET", "/session", headers=bearer(token))).status == 401


async def test_cors_preflight(client: Client) -> None:
    r = await client.request(
        "OPTIONS",
        "/contact",
        headers={"Origin": "http://localhost:5173", "Access-Control-Request-Method": "GET"},
    )
    assert r.status == 204
    assert r.headers["access-control-allow-origin"] == "http://localhost:5173"
    r = await client.request("OPTIONS", "/contact", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in r.headers


async def test_contact_lookup_rematch_create(client: Client) -> None:
    await client.h.connect_company()
    ada = client.h.connector.seed_contact("Ada", "+14155550100")
    bob = client.h.connector.seed_contact("Bob", "+14155550111")
    token = await session_token(client)
    r = await client.request(
        "GET", "/contact", headers=bearer(token), params={"dialpad_ref": "phone:+14155550100"}
    )
    assert r.status == 200, r.text
    assert r.json()["entity"]["external_id"] == ada.external_id and r.json()["manifest"]["fields"]

    rematch = {
        "dialpad_ref": "phone:+14155550100",
        "external_type": "contact",
        "external_id": bob.external_id,
    }
    r = await client.request("POST", "/contact/rematch", headers=bearer(token), body=json.dumps(rematch))
    assert r.status == 422  # must confirm forward-only behaviour
    r = await client.request(
        "POST",
        "/contact/rematch",
        headers=bearer(token),
        body=json.dumps({**rematch, "confirm_forward_only": True}),
    )
    assert r.status == 200 and r.json()["applies_to"] == "future_activities_only"
    assert r.json()["external_id"] == bob.external_id
    r = await client.request(
        "GET", "/contact", headers=bearer(token), params={"dialpad_ref": "phone:+14155550100"}
    )
    assert r.json()["entity"]["external_id"] == bob.external_id

    r = await client.request("GET", "/contact/search", headers=bearer(token), params={"q": "Ada"})
    assert [e["external_id"] for e in r.json()["results"]] == [ada.external_id]

    r = await client.request(
        "POST",
        "/contact",
        headers=bearer(token),
        body=json.dumps(
            {"dialpad_ref": "phone:+15550009999", "properties": {"name": "New", "phone": "+15550009999"}}
        ),
    )
    assert r.status == 201 and r.json()["external_id"]


async def test_contact_validation(client: Client) -> None:
    await client.h.connect_company()
    token = await session_token(client)
    assert (await client.request("GET", "/contact", headers=bearer(token))).status == 422
    r = await client.request("POST", "/contact", headers=bearer(token), body="not json")
    assert r.status == 422


async def test_notes_idempotent(client: Client) -> None:
    await client.h.connect_company()
    token = await session_token(client)
    note = {"note_id": "note-abc-123", "call_id": "1001", "body": "follow up"}
    r1 = await client.request("POST", "/notes", headers=bearer(token), body=json.dumps(note))
    r2 = await client.request("POST", "/notes", headers=bearer(token), body=json.dumps(note))
    assert r1.status == 202 and r1.json()["created"] is True
    assert r2.status == 202 and r2.json()["created"] is False
    bad = await client.request(
        "POST", "/notes", headers=bearer(token), body=json.dumps({**note, "note_id": "x"})
    )
    assert bad.status == 422


async def test_admin_routes_require_admin(client: Client) -> None:
    await client.h.connect_company()
    user = await session_token(client, user="c1-u2", admin=False)
    assert (await client.request("GET", "/admin/targets", headers=bearer(user))).status == 403
    admin = await session_token(client)
    await client.h.services.reconciler.reconcile("c1")
    r = await client.request("GET", "/admin/targets", headers=bearer(admin))
    assert r.status == 200 and len(r.json()["targets"]) >= 5
    r = await client.request(
        "PUT",
        "/admin/targets/user/c1-u1/logging",
        headers=bearer(admin),
        body=json.dumps({"override": False}),
    )
    assert r.status == 200 and r.json()["effective_enabled"] is False
    r = await client.request(
        "PUT", "/admin/targets/user/nope/logging", headers=bearer(admin), body=json.dumps({"override": False})
    )
    assert r.status == 404


async def test_ui_rate_limit(client: Client) -> None:
    await client.h.connect_company()
    token = await session_token(client)
    statuses = []
    for _ in range(client.h.services.settings.UI_RATE_LIMIT_PER_MINUTE + 5):
        statuses.append((await client.request("GET", "/admin/targets", headers=bearer(token))).status)
    assert 429 in statuses


async def test_sms_text_never_in_responses_or_storage(client: Client) -> None:
    await client.h.connect_company()
    r = await client.request(
        "POST", "/webhooks/dialpad/c1/events", body=client.h.sign("c1", sms_event(text="TOP SECRET"))
    )
    assert r.status == 200 and "TOP SECRET" not in r.text
    published = json.dumps([m.model_dump(mode="json") for m in client.h.queue.published])
    assert "TOP SECRET" not in published


@pytest.mark.parametrize("source", ["setting", "packaged", "none"])
async def test_ui_is_served_from_the_package_or_setting(
    source: str, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The built UI ships in the package (framework_sanic/ui_dist); SERVE_UI_DIR overrides it."""
    import framework_sanic.http.adapters.sanic as sanic_adapter

    ui = tmp_path / "ui"
    (ui / "assets").mkdir(parents=True)
    (ui / "index.html").write_text("<!doctype html><title>shell</title>")
    (ui / "assets" / "app.js").write_text("console.log(1)")
    monkeypatch.setattr(
        sanic_adapter, "PACKAGED_UI_DIR", str(ui) if source == "packaged" else str(tmp_path / "no")
    )
    h = build_harness(SERVE_UI_DIR=str(ui) if source == "setting" else None)
    app = create_app(services=h.services, name=f"t_{uuid.uuid4().hex[:10]}", embedded_workers=False)
    _, index = await app.asgi_client.get("/app/")
    _, asset = await app.asgi_client.get("/app/assets/app.js")
    if source == "none":
        assert index.status == 404
        return
    assert index.status == 200 and "shell" in index.text
    assert "frame-ancestors" in index.headers["content-security-policy"]
    assert asset.status == 200 and "console.log" in asset.text
