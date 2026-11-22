"""Security-related ports: KeyProvider, IdTokenVerifier, WebhookVerifier, TokenStore."""

from __future__ import annotations

from typing import Any, Literal, Protocol

from pydantic import BaseModel

from ..domain.entities import DialpadCredential, ExternalCredential
from .platform import OAuthTokens


class KeyProvider(Protocol):
    """Wraps/unwraps data-encryption keys (envelope encryption). GCP KMS in prod, local key in dev."""

    key_id: str

    async def wrap(self, dek: bytes) -> bytes: ...

    async def unwrap(self, wrapped: bytes) -> bytes: ...


class IdTokenClaims(BaseModel):
    company_id: str
    user_id: str
    is_admin: bool | None = None  # VERIFY: whether the id_token carries an admin claim
    email: str | None = None
    exp: float | None = None


class IdTokenVerifier(Protocol):
    """Verifies the per-user ``id_token`` carried by the Dialpad iframe URL (format VERIFY)."""

    async def verify(self, token: str) -> IdTokenClaims: ...


class InvalidSignature(Exception):
    pass


class WebhookVerifier(Protocol):
    def secret_for(self, company_id: str) -> str:
        """Per-company webhook secret. Must not require the database (accept path)."""
        ...

    def verify(self, company_id: str, body: bytes) -> dict[str, Any]:
        """Verify the signed body and return the decoded payload; raise InvalidSignature."""
        ...


class TokenStore(Protocol):
    """Encrypted-at-rest persistence of Dialpad and external credentials."""

    async def list_dialpad(self, company_id: str) -> list[DialpadCredential]: ...

    async def save_dialpad(
        self, company_id: str, credential_id: str, admin_user_id: str, tokens: OAuthTokens
    ) -> DialpadCredential: ...

    async def dialpad_tokens(self, cred: DialpadCredential) -> OAuthTokens: ...

    async def cas_update_dialpad(
        self,
        cred: DialpadCredential,
        *,
        tokens: OAuthTokens | None = None,
        status: Literal["ok", "bad"] | None = None,
    ) -> DialpadCredential | None:
        """Compare-and-set on ``version``. Returns the stored credential, or None on version mismatch."""
        ...

    async def get_external(
        self, company_id: str, scope: Literal["user", "company"], user_id: str = ""
    ) -> ExternalCredential | None: ...

    async def save_external(
        self,
        company_id: str,
        scope: Literal["user", "company"],
        user_id: str,
        connector: str,
        tokens: OAuthTokens,
        external_user_id: str | None = None,
    ) -> ExternalCredential: ...

    async def external_tokens(self, cred: ExternalCredential) -> OAuthTokens: ...

    async def cas_update_external(
        self,
        cred: ExternalCredential,
        *,
        tokens: OAuthTokens | None = None,
        status: Literal["ok", "bad"] | None = None,
    ) -> ExternalCredential | None: ...
