"""RestConnector / OAuth2Auth (the connector toolkit) and the cassette transport."""

from __future__ import annotations

import datetime as dt
import json
from email.utils import format_datetime
from pathlib import Path

import httpx
import pytest

from framework_sanic.sdk import (
    ConnectorCredential,
    ConnectorError,
    OAuth2Auth,
    OAuth2Config,
    OAuthTokens,
    RestConnector,
    parse_retry_after,
)
from framework_sanic.testing.recording import CassetteMiss, CassetteTransport

CRED = ConnectorCredential(scope="company", tokens=OAuthTokens(access_token="tok"))


def client(responses: list[httpx.Response], seen: list[httpx.Request]) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return responses.pop(0)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def connector(responses: list[httpx.Response], seen: list[httpx.Request]) -> RestConnector:
    c = RestConnector(base_url="https://api.test/", http=client(responses, seen), retry_backoff=0)
    c.name = "acme"
    return c


async def test_request_sends_bearer_and_decodes_json() -> None:
    seen: list[httpx.Request] = []
    c = connector([httpx.Response(200, json={"ok": 1}), httpx.Response(204)], seen)
    assert await c.get(CRED, "/things", params={"q": "x"}) == {"ok": 1}
    assert await c.delete(CRED, "/things/1") is None
    assert (
        seen[0].headers["Authorization"] == "Bearer tok" and str(seen[0].url) == "https://api.test/things?q=x"
    )


async def test_idempotent_requests_retry_once_on_gateway_errors() -> None:
    seen: list[httpx.Request] = []
    c = connector([httpx.Response(503), httpx.Response(200, json={"ok": 1})], seen)
    assert await c.get(CRED, "/x") == {"ok": 1} and len(seen) == 2
    seen.clear()
    c = connector([httpx.Response(503), httpx.Response(200, json={})], seen)
    with pytest.raises(httpx.HTTPStatusError):  # POST is not retried: it might have been applied
        await c.post(CRED, "/x", json={})
    assert len(seen) == 1


async def test_transport_errors_retry_then_map_transient() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ConnectError("down", request=request)

    c = RestConnector(
        base_url="https://api.test",
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        retry_backoff=0,
    )
    with pytest.raises(httpx.ConnectError) as exc:
        await c.get(CRED, "/x")
    assert calls == 2 and c.map_error(exc.value).kind == "transient"


@pytest.mark.parametrize(
    ("status", "kind"),
    [
        (401, "auth"),
        (403, "permanent"),
        (404, "not_found"),
        (408, "transient"),
        (429, "rate_limited"),
        (422, "permanent"),
        (500, "transient"),
    ],
)
async def test_map_error(status: int, kind: str) -> None:
    c = connector([httpx.Response(status, headers={"Retry-After": "3"})] * 2, [])
    with pytest.raises(httpx.HTTPStatusError) as exc:
        await c.get(CRED, "/x")
    err = c.map_error(exc.value)
    assert err.kind == kind
    if kind == "rate_limited":
        assert err.retry_after == 3.0


def test_parse_retry_after() -> None:
    soon = format_datetime(dt.datetime.now(dt.UTC) + dt.timedelta(seconds=30), usegmt=True)
    assert 25 <= parse_retry_after(soon) <= 31
    assert parse_retry_after("5") == 5.0 and parse_retry_after(None) == 10.0
    assert parse_retry_after("soon", default=2.0) == 2.0


async def test_paginate_by_cursor() -> None:
    seen: list[httpx.Request] = []
    pages = [
        httpx.Response(200, json={"results": [1, 2], "paging": {"next": {"after": "c2"}}}),
        httpx.Response(200, json={"results": [3]}),
    ]
    c = connector(pages, seen)
    items = [
        i
        async for i in c.paginate(
            CRED,
            "/objects",
            items=lambda b: b["results"],
            next_page=lambda b: {"after": b["paging"]["next"]["after"]} if b.get("paging") else None,
            params={"limit": 2},
        )
    ]
    assert items == [1, 2, 3] and seen[1].url.params["after"] == "c2" and seen[1].url.params["limit"] == "2"


async def test_oauth2_auth() -> None:
    seen: list[httpx.Request] = []
    http = client(
        [
            httpx.Response(200, json={"access_token": "a", "refresh_token": "r", "expires_in": 60}),
            httpx.Response(400, json={"error": "invalid_grant"}),
        ],
        seen,
    )
    cfg = OAuth2Config(
        authorize_url="https://idp.test/authorize",
        token_url="https://idp.test/token",
        client_id="cid",
        client_secret="sec",
        scopes=["a", "b"],
        client_auth="basic",
    )

    async def identify(tokens: OAuthTokens) -> dict[str, object]:
        return {"external_user_id": 5, "tenant": None}

    auth = OAuth2Auth(cfg, http, identify=identify)
    url = auth.authorize_url(state="st", code_challenge="cc", redirect_uri="https://app/cb")
    assert "scope=a+b" in url and "code_challenge_method=S256" in url and "state=st" in url
    tokens = await auth.exchange_code(code="c", code_verifier="v", redirect_uri="https://app/cb")
    assert tokens.extra == {"external_user_id": "5"}
    assert seen[0].headers["Authorization"].startswith("Basic ") and b"client_secret" not in seen[0].content
    with pytest.raises(ConnectorError) as exc:  # invalid_grant -> reconnect needed
        await auth.refresh(tokens)
    assert exc.value.kind == "auth"


async def test_cassette_record_then_replay(tmp_path: Path) -> None:
    path = tmp_path / "c.json"
    real = httpx.MockTransport(lambda r: httpx.Response(200, json={"echo": json.loads(r.content)}))
    recorder = CassetteTransport(path, mode="record", real=real)
    async with httpx.AsyncClient(transport=recorder) as http:
        r = await http.post("https://api.test/x", json={"a": 1}, headers={"Authorization": "Bearer secret"})
    assert r.json() == {"echo": {"a": 1}}
    assert "secret" not in path.read_text()  # credentials are never written

    async with httpx.AsyncClient(transport=CassetteTransport(path)) as http:
        r = await http.post("https://api.test/x", json={"a": 1})
        assert r.json() == {"echo": {"a": 1}}
        with pytest.raises(CassetteMiss):
            await http.post("https://api.test/x", json={"a": 2})
    with pytest.raises(CassetteMiss):
        CassetteTransport(tmp_path / "missing.json")
