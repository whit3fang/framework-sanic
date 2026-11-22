"""Dialpad webhook verification.

When a webhook endpoint has a secret, Dialpad sends the event as an HS256 JWT body signed with it.
Per-company secrets are derived (HMAC of the company id under a master key), so verification needs
neither the database nor Redis: the accept path keeps working while both are down.
"""

from __future__ import annotations

import hashlib
import hmac
from typing import Any

import jwt

from framework_sanic.core.ports.security import InvalidSignature


class DialpadWebhookVerifier:
    def __init__(self, master_key: str) -> None:
        self._master = master_key.encode()

    def secret_for(self, company_id: str) -> str:
        return hmac.new(self._master, f"dialpad-webhook:{company_id}".encode(), hashlib.sha256).hexdigest()

    def verify(self, company_id: str, body: bytes) -> dict[str, Any]:
        token = body.decode("utf-8", errors="replace").strip().strip('"')
        if not token or token.startswith("{"):
            raise InvalidSignature("unsigned webhook body")
        try:
            payload = jwt.decode(
                token, self.secret_for(company_id), algorithms=["HS256"], options={"verify_aud": False}
            )
        except jwt.PyJWTError as exc:
            raise InvalidSignature(str(exc)) from exc
        if not isinstance(payload, dict):
            raise InvalidSignature("payload is not an object")
        return payload

    def sign(self, company_id: str, payload: dict[str, Any]) -> str:
        """Used by the fake Dialpad server and tests."""
        return jwt.encode(payload, self.secret_for(company_id), algorithm="HS256")
