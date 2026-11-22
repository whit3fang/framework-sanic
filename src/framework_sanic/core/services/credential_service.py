"""Credential resolution for Dialpad (company-level, primary + fallback) and the external service
(user-first, then company).

Refresh uses compare-and-set on the credential ``version``; a short Redis lease is only a fast
path to avoid a thundering herd of refreshes.
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable
from typing import Literal, TypeVar

from ..domain.entities import DialpadCredential, ExternalCredential
from ..domain.errors import NotConnected, RateLimited
from ..domain.records import Lane
from ..ports.cache import Cache, CacheUnavailable
from ..ports.connector import ConnectorAuth, ConnectorCredential
from ..ports.platform import (
    OAuthTokens,
    ParentPlatform,
    PlatformAuthError,
    PlatformIdentity,
    PlatformRateLimited,
)
from ..ports.runtime import Clock
from ..ports.security import TokenStore
from .rate_limit import RateLimiter

log = logging.getLogger(__name__)
T = TypeVar("T")

REFRESH_MARGIN_SECONDS = 120.0


async def _try_lease(cache: Cache, key: str, ttl: float) -> bool:
    try:
        return await cache.add(key, b"1", ttl)
    except CacheUnavailable:
        return True


class DialpadCredentialService:
    def __init__(self, store: TokenStore, platform: ParentPlatform, cache: Cache, clock: Clock) -> None:
        self.store = store
        self.platform = platform
        self.cache = cache
        self.clock = clock

    async def connect(self, identity: PlatformIdentity, tokens: OAuthTokens) -> DialpadCredential:
        """Store a new credential. Multiple admins can connect; first connected is primary."""
        return await self.store.save_dialpad(identity.company_id, identity.user_id, identity.user_id, tokens)

    async def ordered(self, company_id: str) -> list[DialpadCredential]:
        creds = await self.store.list_dialpad(company_id)
        return sorted(creds, key=lambda c: (c.status != "ok", c.priority, c.credential_id))

    async def has_usable(self, company_id: str) -> bool:
        return any(c.status == "ok" for c in await self.store.list_dialpad(company_id))

    async def access_token(self, company_id: str) -> tuple[DialpadCredential, str]:
        for cred in await self.ordered(company_id):
            if cred.status != "ok":
                continue
            try:
                return cred, await self._fresh_token(cred)
            except PlatformAuthError:
                log.warning(
                    "dialpad credential %s/%s failed refresh; falling back", company_id, cred.credential_id
                )
                await self.mark_bad(cred)
        raise NotConnected(f"no usable Dialpad credential for company {company_id}")

    async def _fresh_token(self, cred: DialpadCredential) -> str:
        tokens = await self.store.dialpad_tokens(cred)
        now = self.clock.now()
        if cred.token_expires_at is None or cred.token_expires_at - REFRESH_MARGIN_SECONDS > now:
            return tokens.access_token
        if not tokens.refresh_token:
            raise PlatformAuthError("access token expired and no refresh token")
        lease = f"lease:refresh:dialpad:{cred.company_id}:{cred.credential_id}"
        for _ in range(10):
            if await _try_lease(self.cache, lease, 15):
                break
            await asyncio.sleep(0.2)
            latest = await self._reload(cred)
            if latest and latest.version != cred.version:
                return (await self.store.dialpad_tokens(latest)).access_token
        new = await self.platform.refresh(tokens.refresh_token)
        if not new.refresh_token:
            new = new.model_copy(update={"refresh_token": tokens.refresh_token})
        stored = await self.store.cas_update_dialpad(cred, tokens=new)
        if stored is None:  # somebody else refreshed first; use theirs
            latest = await self._reload(cred)
            if latest is None:
                raise PlatformAuthError("credential vanished during refresh")
            return (await self.store.dialpad_tokens(latest)).access_token
        try:
            await self.cache.delete(lease)
        except CacheUnavailable:
            pass
        return new.access_token

    async def _reload(self, cred: DialpadCredential) -> DialpadCredential | None:
        for c in await self.store.list_dialpad(cred.company_id):
            if c.credential_id == cred.credential_id:
                return c
        return None

    async def mark_bad(self, cred: DialpadCredential) -> None:
        current: DialpadCredential | None = cred
        for _ in range(5):
            if current is None or current.status == "bad":
                return
            if await self.store.cas_update_dialpad(current, status="bad") is not None:
                return
            current = await self._reload(current)

    async def handle_auth_failure(self, cred: DialpadCredential) -> None:
        """Validity probe: if the credential is really invalid, mark it bad so calls fall through."""
        try:
            token = (await self.store.dialpad_tokens(cred)).access_token
            ok = await self.platform.probe(token)
        except Exception:  # noqa: BLE001 - probe failures are treated as invalid
            ok = False
        if not ok:
            await self.mark_bad(cred)


class PlatformGateway:
    """Every Dialpad API call goes through here: credential fallback + per-company API budget.

    Reconciler and UI calls share the 1,200 req/min budget; UI calls use the higher-priority lane.
    """

    def __init__(
        self,
        platform: ParentPlatform,
        credentials: DialpadCredentialService,
        limiter: RateLimiter,
        *,
        per_minute: float,
        max_wait_seconds: float = 120.0,
    ) -> None:
        self.platform = platform
        self.credentials = credentials
        self.limiter = limiter
        self.per_minute = per_minute
        self.max_wait = max_wait_seconds

    async def _budget(self, company_id: str, lane: Lane) -> None:
        waited = 0.0
        while True:
            wait = await self.limiter.acquire(f"dialpad:{company_id}", per_minute=self.per_minute, lane=lane)
            if wait <= 0:
                return
            if lane == Lane.UI or waited + wait > self.max_wait:
                raise RateLimited("Dialpad API budget exhausted", retry_after=wait)
            await asyncio.sleep(wait)
            waited += wait

    async def call(
        self, company_id: str, fn: Callable[[str], Awaitable[T]], *, lane: Lane = Lane.BACKFILL
    ) -> T:
        attempts = 0
        while True:
            attempts += 1
            await self._budget(company_id, lane)
            cred, token = await self.credentials.access_token(company_id)
            try:
                return await fn(token)
            except PlatformAuthError:
                await self.credentials.handle_auth_failure(cred)
                if attempts >= 3:
                    raise
            except PlatformRateLimited as exc:
                if attempts >= 4 or lane == Lane.UI:
                    raise RateLimited("Dialpad rate limited", retry_after=exc.retry_after) from exc
                await asyncio.sleep(exc.retry_after + random.uniform(0, exc.retry_after / 2 + 0.1))


class ExternalCredentialService:
    def __init__(
        self, store: TokenStore, auth: ConnectorAuth, connector_name: str, cache: Cache, clock: Clock
    ) -> None:
        self.store = store
        self.auth = auth
        self.connector_name = connector_name
        self.cache = cache
        self.clock = clock

    async def save(
        self,
        company_id: str,
        scope: Literal["user", "company"],
        user_id: str,
        tokens: OAuthTokens,
        external_user_id: str | None = None,
    ) -> ExternalCredential:
        return await self.store.save_external(
            company_id,
            scope,
            user_id if scope == "user" else "",
            self.connector_name,
            tokens,
            external_user_id,
        )

    async def status(self, company_id: str, user_id: str) -> dict[str, bool]:
        user = await self.store.get_external(company_id, "user", user_id)
        company = await self.store.get_external(company_id, "company", "")
        return {
            "user": bool(user and user.status == "ok"),
            "company": bool(company and company.status == "ok"),
        }

    async def resolve(self, company_id: str, user_id: str | None) -> ConnectorCredential:
        """User-first, then company. Raises NotConnected if neither is usable."""
        candidates: list[ExternalCredential | None] = []
        if user_id:
            candidates.append(await self.store.get_external(company_id, "user", user_id))
        candidates.append(await self.store.get_external(company_id, "company", ""))
        for cred in candidates:
            if cred is None or cred.status != "ok":
                continue
            try:
                tokens = await self._fresh(cred)
            except Exception:  # noqa: BLE001 - refresh failure: fall back to next credential
                log.warning("external credential refresh failed for %s/%s", company_id, cred.scope)
                await self.mark_bad(cred.company_id, cred.scope, cred.user_id)
                continue
            return ConnectorCredential(
                scope=cred.scope, user_id=cred.user_id, tokens=tokens, external_user_id=cred.external_user_id
            )
        raise NotConnected(f"no usable {self.connector_name} credential for company {company_id}")

    async def _fresh(self, cred: ExternalCredential) -> OAuthTokens:
        tokens = await self.store.external_tokens(cred)
        if cred.token_expires_at is None or cred.token_expires_at - REFRESH_MARGIN_SECONDS > self.clock.now():
            return tokens
        lease = f"lease:refresh:ext:{cred.company_id}:{cred.scope}:{cred.user_id}"
        if not await _try_lease(self.cache, lease, 15):
            await asyncio.sleep(0.5)
            latest = await self.store.get_external(cred.company_id, cred.scope, cred.user_id)
            if latest and latest.version != cred.version:
                return await self.store.external_tokens(latest)
        new = await self.auth.refresh(tokens)
        if not new.refresh_token:
            new = new.model_copy(update={"refresh_token": tokens.refresh_token})
        stored = await self.store.cas_update_external(cred, tokens=new)
        if stored is None:
            latest = await self.store.get_external(cred.company_id, cred.scope, cred.user_id)
            if latest is None:
                raise NotConnected("credential vanished during refresh")
            return await self.store.external_tokens(latest)
        return new

    async def mark_bad(self, company_id: str, scope: Literal["user", "company"], user_id: str) -> None:
        for _ in range(5):
            cred = await self.store.get_external(company_id, scope, user_id)
            if cred is None or cred.status == "bad":
                return
            if await self.store.cas_update_external(cred, status="bad") is not None:
                return
