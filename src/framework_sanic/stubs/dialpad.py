"""In-memory fake Dialpad for tests and the local harness (PLATFORM_ADAPTER=fake).

OAuth "authorize" immediately redirects back to the callback with a code bound to the PKCE
challenge, so the full admin-connect flow can be exercised end to end without Dialpad.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import itertools
import secrets
from dataclasses import dataclass, field
from typing import Literal
from urllib.parse import urlencode

from framework_sanic.core.ports.platform import (
    OAuthTokens,
    PlatformAuthError,
    PlatformIdentity,
    RemoteSubscription,
    SubscriptionSpec,
    TargetInfo,
)


@dataclass
class FakeCompany:
    company_id: str
    name: str
    targets: list[TargetInfo] = field(default_factory=list)


def default_company(company_id: str = "c1", users: int = 3) -> FakeCompany:
    office = f"{company_id}-office-1"
    targets = [
        TargetInfo(target_type="company", target_id=company_id, name="Acme"),
        TargetInfo(
            target_type="office", target_id=office, name="HQ", parent_type="company", parent_id=company_id
        ),
        TargetInfo(
            target_type="department",
            target_id=f"{company_id}-dept-sales",
            name="Sales",
            parent_type="office",
            parent_id=office,
        ),
        TargetInfo(
            target_type="callcenter",
            target_id=f"{company_id}-cc-support",
            name="Support",
            parent_type="office",
            parent_id=office,
        ),
    ]
    for i in range(users):
        targets.append(
            TargetInfo(
                target_type="user",
                target_id=f"{company_id}-u{i}",
                name=f"User {i}",
                parent_type="office",
                parent_id=office,
                is_admin=i == 0,
            )
        )
    return FakeCompany(company_id=company_id, name="Acme", targets=targets)


class FakeDialpadPlatform:
    def __init__(self, *, latency: float = 0.0) -> None:
        self.companies: dict[str, FakeCompany] = {}
        self.identities: dict[str, PlatformIdentity] = {}  # access token -> identity
        self.refresh_tokens: dict[str, PlatformIdentity] = {}
        self.codes: dict[str, tuple[PlatformIdentity, str]] = {}  # code -> (identity, challenge)
        self.webhooks: dict[str, dict[str, tuple[str, str]]] = {}  # company -> id -> (url, secret)
        self.subs: dict[str, dict[str, RemoteSubscription]] = {}  # company -> id -> sub
        self.next_identity = PlatformIdentity(
            company_id="c1", company_name="Acme", user_id="c1-u0", is_admin=True
        )
        self.calls: list[str] = []
        self.refresh_count = 0
        self.token_ttl: float | None = 3600.0
        self._ids = itertools.count(1)
        self.latency = latency
        self.revoked: set[str] = set()

    def add_company(self, company: FakeCompany) -> None:
        self.companies[company.company_id] = company

    async def _tick(self, name: str) -> None:
        self.calls.append(name)
        if self.latency:
            await asyncio.sleep(self.latency)
        else:
            await asyncio.sleep(0)

    def _identity(self, token: str) -> PlatformIdentity:
        ident = self.identities.get(token)
        if ident is None or token in self.revoked:
            raise PlatformAuthError("invalid token")
        return ident

    def issue_tokens(self, identity: PlatformIdentity) -> OAuthTokens:
        access = "at-" + secrets.token_hex(8)
        refresh = "rt-" + secrets.token_hex(8)
        self.identities[access] = identity
        self.refresh_tokens[refresh] = identity
        return OAuthTokens(access_token=access, refresh_token=refresh, expires_in=self.token_ttl, scope=[])

    # --- OAuth ------------------------------------------------------------------------------
    def authorize_url(self, *, state: str, code_challenge: str, redirect_uri: str) -> str:
        code = "code-" + secrets.token_hex(8)
        self.codes[code] = (self.next_identity, code_challenge)
        return f"{redirect_uri}?{urlencode({'code': code, 'state': state})}"

    async def exchange_code(self, *, code: str, code_verifier: str, redirect_uri: str) -> OAuthTokens:
        await self._tick("exchange_code")
        item = self.codes.pop(code, None)
        if item is None:
            raise PlatformAuthError("bad code")
        identity, challenge = item
        expected = (
            base64.urlsafe_b64encode(hashlib.sha256(code_verifier.encode()).digest()).rstrip(b"=").decode()
        )
        if expected != challenge:
            raise PlatformAuthError("PKCE verification failed")
        if identity.company_id not in self.companies:
            self.add_company(default_company(identity.company_id))
        return self.issue_tokens(identity)

    async def refresh(self, refresh_token: str) -> OAuthTokens:
        await self._tick("refresh")
        identity = self.refresh_tokens.get(refresh_token)
        if identity is None or refresh_token in self.revoked:
            raise PlatformAuthError("bad refresh token")
        self.refresh_count += 1
        access = "at-" + secrets.token_hex(8)
        self.identities[access] = identity
        # Dialpad refresh tokens never expire; keep the same one.
        return OAuthTokens(access_token=access, refresh_token=refresh_token, expires_in=self.token_ttl)

    async def identify(self, access_token: str) -> PlatformIdentity:
        await self._tick("identify")
        return self._identity(access_token)

    async def probe(self, access_token: str) -> bool:
        await self._tick("probe")
        try:
            self._identity(access_token)
            return True
        except PlatformAuthError:
            return False

    # --- hierarchy and subscriptions --------------------------------------------------------
    async def list_targets(self, access_token: str, company_id: str) -> list[TargetInfo]:
        await self._tick("list_targets")
        self._identity(access_token)
        return list(self.companies[company_id].targets)

    async def ensure_webhook(self, access_token: str, *, hook_url: str, secret: str) -> str:
        await self._tick("ensure_webhook")
        cid = self._identity(access_token).company_id
        hooks = self.webhooks.setdefault(cid, {})
        for wid, (url, _) in hooks.items():
            if url == hook_url:
                hooks[wid] = (url, secret)
                return wid
        wid = f"wh-{next(self._ids)}"
        hooks[wid] = (hook_url, secret)
        return wid

    async def list_subscriptions(
        self, access_token: str, kind: Literal["call", "sms"]
    ) -> list[RemoteSubscription]:
        await self._tick(f"list_subscriptions:{kind}")
        cid = self._identity(access_token).company_id
        return [s.model_copy() for s in self.subs.get(cid, {}).values() if s.kind == kind]

    def _settings(self, spec: SubscriptionSpec) -> dict[str, object]:
        if spec.kind == "call":
            return {"call_states": list(spec.call_states), "group_calls_only": spec.group_calls_only}
        return {"direction": spec.direction, "include_internal": spec.include_internal, "status": spec.status}

    async def create_subscription(self, access_token: str, spec: SubscriptionSpec) -> RemoteSubscription:
        await self._tick(f"create_subscription:{spec.kind}")
        cid = self._identity(access_token).company_id
        sub = RemoteSubscription(
            id=f"sub-{next(self._ids)}",
            kind=spec.kind,
            target_type=spec.target_type,
            target_id=spec.target_id,
            endpoint_id=spec.endpoint_id,
            enabled=spec.enabled,
            settings=self._settings(spec),
        )
        self.subs.setdefault(cid, {})[sub.id] = sub
        return sub.model_copy()

    async def update_subscription(
        self, access_token: str, subscription_id: str, spec: SubscriptionSpec
    ) -> RemoteSubscription:
        await self._tick(f"update_subscription:{spec.kind}")
        cid = self._identity(access_token).company_id
        sub = self.subs[cid][subscription_id]
        sub.enabled = spec.enabled
        sub.settings = self._settings(spec)
        return sub.model_copy()

    async def delete_subscription(
        self, access_token: str, kind: Literal["call", "sms"], subscription_id: str
    ) -> None:
        await self._tick(f"delete_subscription:{kind}")
        cid = self._identity(access_token).company_id
        self.subs.get(cid, {}).pop(subscription_id, None)
