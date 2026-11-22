"""Short-lived UI session tokens and OAuth state/PKCE handling.

The UI exchanges the Dialpad ``id_token`` for a session token that it keeps in memory only.
Revoked session ids are kept in Redis until the token would have expired anyway.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import secrets
import uuid
from typing import Any

import jwt
from pydantic import BaseModel

from ..domain.errors import Unauthorized
from ..ports.cache import Cache, CacheUnavailable
from ..ports.runtime import Clock

log = logging.getLogger(__name__)


class Principal(BaseModel):
    company_id: str
    user_id: str
    is_admin: bool = False
    session_id: str | None = None
    email: str | None = None


class SessionService:
    def __init__(self, secret: str, ttl_seconds: int, cache: Cache, clock: Clock) -> None:
        self._secret = secret
        self._ttl = ttl_seconds
        self._cache = cache
        self._clock = clock

    def issue(self, principal: Principal) -> tuple[str, float]:
        now = self._clock.now()
        exp = now + self._ttl
        sid = uuid.uuid4().hex
        token = jwt.encode(
            {
                "sub": principal.user_id,
                "cid": principal.company_id,
                "adm": principal.is_admin,
                "eml": principal.email,
                "sid": sid,
                "iat": int(now),
                "exp": int(exp),
                "typ": "ui-session",
            },
            self._secret,
            algorithm="HS256",
        )
        return token, exp

    async def verify(self, token: str) -> Principal:
        try:
            claims = jwt.decode(
                token,
                self._secret,
                algorithms=["HS256"],
                options={"require": ["exp", "sid", "cid", "sub"], "verify_exp": False},
            )
        except jwt.PyJWTError as exc:
            raise Unauthorized("invalid session token") from exc
        if claims.get("typ") != "ui-session" or float(claims["exp"]) <= self._clock.now():
            raise Unauthorized("session expired")
        sid = str(claims["sid"])
        try:
            if await self._cache.get(f"revoked:{sid}") is not None:
                raise Unauthorized("session revoked")
        except CacheUnavailable:
            # Tokens are short-lived; fail soft on the revocation check rather than lock users out.
            log.warning("revocation list unavailable; accepting session %s", sid)
        return Principal(
            company_id=str(claims["cid"]),
            user_id=str(claims["sub"]),
            is_admin=bool(claims.get("adm")),
            session_id=sid,
            email=claims.get("eml"),
        )

    async def revoke(self, session_id: str) -> None:
        await self._cache.set(f"revoked:{session_id}", b"1", self._ttl + 60)


def pkce_pair() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


class OAuthStateStore:
    """``state`` + PKCE verifier kept in Redis with a short TTL; single use."""

    TTL_SECONDS = 600

    def __init__(self, cache: Cache) -> None:
        self._cache = cache

    async def create(self, data: dict[str, Any]) -> tuple[str, str]:
        state = secrets.token_urlsafe(24)
        verifier, challenge = pkce_pair()
        payload = json.dumps({**data, "verifier": verifier}).encode()
        await self._cache.set(f"oauth:{state}", payload, self.TTL_SECONDS)
        return state, challenge

    async def consume(self, state: str) -> dict[str, Any]:
        key = f"oauth:{state}"
        raw = await self._cache.get(key)
        if raw is None:
            raise Unauthorized("unknown or expired OAuth state")
        await self._cache.delete(key)
        data: dict[str, Any] = json.loads(raw)
        return data
