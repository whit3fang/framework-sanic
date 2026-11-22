"""Dialpad REST API implementation of ParentPlatform (httpx).

VERIFY: exact list endpoints and payload field names for departments/call centers and
subscriptions. They are centralised in ``ENDPOINTS`` and parsed defensively.
"""

from __future__ import annotations

from typing import Any, Literal
from urllib.parse import urlencode

import httpx

from framework_sanic.core.ports.platform import (
    OAuthTokens,
    PlatformAuthError,
    PlatformIdentity,
    PlatformRateLimited,
    RemoteSubscription,
    SubscriptionSpec,
    TargetInfo,
)

ENDPOINTS = {
    "authorize": "/oauth2/authorize",
    "token": "/oauth2/token",
    "me": "/api/v2/users/me",
    "company": "/api/v2/company",
    "offices": "/api/v2/offices",
    "users": "/api/v2/users",
    "departments": "/api/v2/departments",  # VERIFY
    "callcenters": "/api/v2/callcenters",  # VERIFY
    "webhooks": "/api/v2/webhooks",
    "subscriptions": "/api/v2/subscriptions/{kind}",
}


def _str(v: Any) -> str | None:
    return None if v is None else str(v)


class DialpadPlatform:
    def __init__(
        self,
        *,
        base_url: str,
        client_id: str,
        client_secret: str,
        scopes: list[str],
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._client_id = client_id
        self._client_secret = client_secret
        self._scopes = scopes
        self._http = client or httpx.AsyncClient(base_url=self._base, timeout=10.0)

    # --- helpers ----------------------------------------------------------------------------
    async def _request(self, method: str, path: str, token: str | None = None, **kw: Any) -> Any:
        headers = kw.pop("headers", {})
        if token:
            headers["Authorization"] = f"Bearer {token}"
        resp = await self._http.request(method, path, headers=headers, **kw)
        if resp.status_code == 401:
            raise PlatformAuthError(resp.text[:200])
        if resp.status_code == 429:
            raise PlatformRateLimited(float(resp.headers.get("Retry-After", "1") or 1))
        resp.raise_for_status()
        if not resp.content:
            return None
        return resp.json()

    async def _paged(
        self, path: str, token: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            q = dict(params or {})
            if cursor:
                q["cursor"] = cursor
            data = await self._request("GET", path, token, params=q)
            if isinstance(data, list):
                return items + data
            items.extend((data or {}).get("items") or [])
            cursor = (data or {}).get("cursor")
            if not cursor:
                return items

    # --- OAuth ------------------------------------------------------------------------------
    def authorize_url(self, *, state: str, code_challenge: str, redirect_uri: str) -> str:
        q = {
            "response_type": "code",
            "client_id": self._client_id,
            "redirect_uri": redirect_uri,
            "state": state,
            "scope": " ".join(self._scopes),
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
        return f"{self._base}{ENDPOINTS['authorize']}?{urlencode(q)}"

    def _tokens(self, data: dict[str, Any]) -> OAuthTokens:
        scope = data.get("scope") or []
        return OAuthTokens(
            access_token=data["access_token"],
            refresh_token=data.get("refresh_token"),
            expires_in=data.get("expires_in"),
            scope=scope.split() if isinstance(scope, str) else list(scope),
        )

    async def exchange_code(self, *, code: str, code_verifier: str, redirect_uri: str) -> OAuthTokens:
        data = await self._request(
            "POST",
            ENDPOINTS["token"],
            data={
                "grant_type": "authorization_code",
                "code": code,
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "redirect_uri": redirect_uri,
                "code_verifier": code_verifier,
            },
        )
        return self._tokens(data)

    async def refresh(self, refresh_token: str) -> OAuthTokens:
        data = await self._request(
            "POST",
            ENDPOINTS["token"],
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": self._client_id,
                "client_secret": self._client_secret,
            },
        )
        return self._tokens(data)

    async def identify(self, access_token: str) -> PlatformIdentity:
        me = await self._request("GET", ENDPOINTS["me"], access_token)
        company = await self._request("GET", ENDPOINTS["company"], access_token)
        emails = me.get("emails") or []
        return PlatformIdentity(
            company_id=str(me.get("company_id") or company.get("id")),
            company_name=company.get("name") or "",
            user_id=str(me["id"]),
            is_admin=bool(me.get("is_admin") or me.get("is_super_admin")),  # VERIFY
            email=emails[0] if emails else None,
            name=me.get("display_name") or "",
        )

    async def probe(self, access_token: str) -> bool:
        try:
            await self._request("GET", ENDPOINTS["me"], access_token)
            return True
        except PlatformAuthError:
            return False

    # --- hierarchy --------------------------------------------------------------------------
    async def list_targets(self, access_token: str, company_id: str) -> list[TargetInfo]:
        company = await self._request("GET", ENDPOINTS["company"], access_token)
        out = [TargetInfo(target_type="company", target_id=company_id, name=company.get("name") or "")]
        for o in await self._paged(ENDPOINTS["offices"], access_token):
            out.append(
                TargetInfo(
                    target_type="office",
                    target_id=str(o["id"]),
                    name=o.get("name") or "",
                    parent_type="company",
                    parent_id=company_id,
                )
            )
        for u in await self._paged(ENDPOINTS["users"], access_token):
            if u.get("state") in ("deleted", "suspended"):
                continue
            out.append(
                TargetInfo(
                    target_type="user",
                    target_id=str(u["id"]),
                    name=u.get("display_name") or "",
                    parent_type="office" if u.get("office_id") else "company",
                    parent_id=_str(u.get("office_id")) or company_id,
                    email=(u.get("emails") or [None])[0],
                    is_admin=bool(u.get("is_admin")),
                )
            )
        for kind, path in (("department", "departments"), ("callcenter", "callcenters")):
            try:
                rows = await self._paged(ENDPOINTS[path], access_token)
            except httpx.HTTPStatusError:
                rows = []  # VERIFY endpoint availability
            for d in rows:
                out.append(
                    TargetInfo(
                        target_type=kind,
                        target_id=str(d["id"]),
                        name=d.get("name") or "",
                        parent_type="office" if d.get("office_id") else "company",
                        parent_id=_str(d.get("office_id")) or company_id,
                    )
                )
        return out

    # --- webhooks and subscriptions --------------------------------------------------------
    async def ensure_webhook(self, access_token: str, *, hook_url: str, secret: str) -> str:
        for w in await self._paged(ENDPOINTS["webhooks"], access_token):
            if w.get("hook_url") == hook_url:
                return str(w["id"])
        created = await self._request(
            "POST", ENDPOINTS["webhooks"], access_token, json={"hook_url": hook_url, "secret": secret}
        )
        return str(created["id"])

    def _parse_sub(self, kind: Literal["call", "sms"], d: dict[str, Any]) -> RemoteSubscription:
        webhook = d.get("webhook") or {}
        settings = {
            k: d.get(k)
            for k in ("call_states", "group_calls_only", "direction", "include_internal", "status")
            if k in d
        }
        return RemoteSubscription(
            id=str(d["id"]),
            kind=kind,
            target_type=d.get("target_type"),
            target_id=_str(d.get("target_id")),
            endpoint_id=_str(webhook.get("id") or d.get("endpoint_id")),
            enabled=bool(d.get("enabled", True)),
            settings=settings,
        )

    def _sub_body(self, spec: SubscriptionSpec) -> dict[str, Any]:
        body: dict[str, Any] = {
            "enabled": spec.enabled,
            "endpoint_id": spec.endpoint_id,
            "target_type": spec.target_type,
            "target_id": spec.target_id,
        }
        if spec.kind == "call":
            body.update(call_states=spec.call_states, group_calls_only=spec.group_calls_only)
        else:
            body.update(direction=spec.direction, include_internal=spec.include_internal, status=spec.status)
        return body

    async def list_subscriptions(
        self, access_token: str, kind: Literal["call", "sms"]
    ) -> list[RemoteSubscription]:
        rows = await self._paged(ENDPOINTS["subscriptions"].format(kind=kind), access_token)
        return [self._parse_sub(kind, r) for r in rows]

    async def create_subscription(self, access_token: str, spec: SubscriptionSpec) -> RemoteSubscription:
        data = await self._request(
            "POST", ENDPOINTS["subscriptions"].format(kind=spec.kind), access_token, json=self._sub_body(spec)
        )
        return self._parse_sub(spec.kind, data)

    async def update_subscription(
        self, access_token: str, subscription_id: str, spec: SubscriptionSpec
    ) -> RemoteSubscription:
        path = ENDPOINTS["subscriptions"].format(kind=spec.kind) + f"/{subscription_id}"
        data = await self._request("PATCH", path, access_token, json=self._sub_body(spec))
        return self._parse_sub(spec.kind, data)

    async def delete_subscription(
        self, access_token: str, kind: Literal["call", "sms"], subscription_id: str
    ) -> None:
        await self._request(
            "DELETE", ENDPOINTS["subscriptions"].format(kind=kind) + f"/{subscription_id}", access_token
        )

    async def close(self) -> None:
        await self._http.aclose()
