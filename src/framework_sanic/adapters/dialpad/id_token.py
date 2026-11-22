"""IdTokenVerifier for the ``id_token`` carried by the Dialpad iframe URL.

VERIFY: token format and claims. Assumed here: an HS256 JWT signed with the OAuth app's client
secret, carrying the user id (``user_id`` or ``sub``) and company id (``company_id``/``cid``).
Swap this adapter once the real format is confirmed; the port does not change.
"""

from __future__ import annotations

from typing import Any

import jwt

from framework_sanic.core.domain.errors import Unauthorized
from framework_sanic.core.ports.security import IdTokenClaims


def _first(claims: dict[str, Any], *names: str) -> Any:
    for n in names:
        if claims.get(n) not in (None, ""):
            return claims[n]
    return None


class DialpadIdTokenVerifier:
    def __init__(self, secret: str, *, audience: str | None = None) -> None:
        self._secret = secret
        self._audience = audience

    async def verify(self, token: str) -> IdTokenClaims:
        try:
            claims: dict[str, Any] = jwt.decode(
                token,
                self._secret,
                algorithms=["HS256"],
                audience=self._audience,
                options={"verify_aud": self._audience is not None, "require": ["exp"]},
            )
        except jwt.PyJWTError as exc:
            raise Unauthorized("invalid id_token") from exc
        user_id = _first(claims, "user_id", "sub", "uid")
        company_id = _first(claims, "company_id", "cid", "company")
        if user_id is None or company_id is None:
            raise Unauthorized("id_token is missing user or company")
        is_admin = _first(claims, "is_admin", "admin")
        return IdTokenClaims(
            company_id=str(company_id),
            user_id=str(user_id),
            is_admin=bool(is_admin) if is_admin is not None else None,
            email=_first(claims, "email"),
            exp=float(claims["exp"]),
        )
